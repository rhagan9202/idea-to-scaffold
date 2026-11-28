"""`its land`: the landing lane. Finished claim branches are merged into the integration branch one at a time, and
the checks run on each merged result before the next one lands.

Each branch passing its own checks doesn't mean the merge of several does: two branches can each be green and
still break each other. So `land` merges one branch, runs the item's Verify plus the test and lint commands on
the merged tree, and keeps the merge only if they pass; otherwise it undoes the merge and moves on. Main only
ever advances to a state that was tested as a whole.

A branch is ready to land when its item is `done` on the branch (its agent ran `its done`). Branches land in
backlog order, and an item lands only once its dependencies are done on the integration branch.

Land is where the gate is enforced, so it first checks that the integration branch itself holds only commits `its`
trusts (`trust.py`): an agent can move main from its worktree, and main holds the gate's rules. While it holds a
commit that is neither pushed nor made by `its`, land refuses (`base_untrusted`) until a person accepts it
(`--accept-base`). Findings are waived only by a Waiver on the integration branch's copy of the item (`its waive`).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import claims as cl
from . import gate, gitx, ledger, trust
from .backlog import BacklogItem, blocked_by, load_backlog, load_proposals, parse_backlog
from .checks import RESULT_KINDS, Check, checks_for, result_findings, run_check
from .ids import META_FILES
from .linker import link_package
from .package import Package
from .work import (
    BACKLOG,
    BACKLOG_JSON,
    EXIT_NOT_LANDED,
    SCHEMA,
    WorkError,
    _locked,
    _refresh_mirror,
    changed_tests,
    integration_branch,
    remote_of,
    shown,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return gitx.run(cwd, *args)


def _generated(pkg: Package) -> set[str]:
    """Files `its` regenerates, whose conflicts land resolves by regenerating them."""
    return {shown(pkg, rel) for rel in META_FILES} | {shown(pkg, BACKLOG_JSON)}


def _item_at(pkg: Package, ref: str, ident: str) -> BacklogItem | None:
    text = cl.read_at(pkg.root, ref, pkg.path_of(BACKLOG))
    if text is None:
        return None
    return next((i for i in parse_backlog(text) if i.id == ident), None)


def _candidates(pkg: Package, base: str, ids: list[str] | None) -> tuple[list[tuple[cl.Claim, str]], list[dict]]:
    """Claim branches that are ready to land, in backlog order, and why the named ones that aren't are skipped."""
    items = load_backlog(pkg)
    order = {item.id: n for n, item in enumerate(items)}
    closed = {i.id for i in items if i.status in ("done", "dropped")}
    claims = [c for c in cl.list_claims(pkg.root, closed, set(order)) if not c.finished and not c.orphaned]
    skipped: list[dict] = []
    if ids:
        known = {c.id for c in claims}
        for ident in ids:
            if ident not in known:
                skipped.append({"id": ident, "reason": "no_claim",
                                "message": f"{ident} has no unmerged claim branch to land"})
        claims = [c for c in claims if c.id in ids]
    ready: list[tuple[cl.Claim, str]] = []
    for claim in sorted(claims, key=lambda c: order.get(c.id, len(order))):
        ref = f"refs/remotes/{claim.branch}" if claim.remote else f"refs/heads/{claim.branch}"
        if not cl.ahead(pkg.root, ref, base):
            if ids:
                skipped.append({"id": claim.id, "reason": "no_commits", "message": f"{claim.branch} has no new commits"})
            continue
        item = _item_at(pkg, ref, claim.id)
        if item is None or item.status != "done":
            if ids:
                skipped.append({"id": claim.id, "reason": "not_done",
                                "message": f"{claim.id} isn't done on {claim.branch}: run its done there first"})
            continue
        ready.append((claim, ref))
    return ready, skipped


def land(
    pkg: Package,
    ids: list[str] | None = None,
    *,
    verify: bool = True,
    fetch: bool = False,
    push: bool = False,
    release: bool = False,
    dry_run: bool = False,
    accept_base: bool = False,
    on_check: Callable[[str, Check], None] | None = None,
) -> dict:
    """Land finished claim branches on the integration branch, one at a time, testing each merged result.
    `accept_base` trusts the commits on the integration branch that `its` didn't make (a person has looked)."""
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "landing needs a git repository")
    root = pkg.root
    base = integration_branch(pkg)
    if cl.current_branch(root) != base:
        raise WorkError("not_integration_branch", f"run its land on {base}, in the main checkout",
                        details={"branch": cl.current_branch(root), "integration_branch": base})
    if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise WorkError("dirty", f"{base} has uncommitted changes; commit or stash them first")
    remote = remote_of(pkg)
    if fetch and remote:
        try:
            cl.fetch(root, remote, base)
        except cl.GitError as exc:
            raise WorkError("fetch_failed", str(exc)) from exc
        tracking = f"{remote}/{base}"
        if cl.ref_exists(root, tracking) and _git(root, "merge", "--ff-only", "--quiet", tracking).returncode != 0:
            raise WorkError("diverged", f"{base} and {tracking} have diverged; reconcile them first")
    trusted = check_base(pkg, base, remote, accept=accept_base, write=not dry_run)

    landed: list[dict] = []
    skipped: list[dict] = []
    ready, early = _candidates(pkg, "HEAD", ids)
    skipped += early
    pending = list(ready)
    progress = True
    while pending and progress:  # an item whose dependency lands later in this run gets another turn
        progress = False
        for claim, ref in list(pending):
            items = load_backlog(pkg)
            item = next((i for i in items if i.id == claim.id), None)
            waiting = blocked_by(item, items) if item else []
            if waiting:
                continue
            pending.remove((claim, ref))
            progress = True
            outcome = _land_one(pkg, claim, ref, verify=verify, dry_run=dry_run, on_check=on_check, remote=remote)
            (landed if outcome["landed"] else skipped).append(outcome)
            if not dry_run:
                ledger.record(pkg, claim.id, "land", "landed" if outcome["landed"] else outcome.get("reason", "skipped"),
                              findings=sorted({f["kind"] for f in outcome.get("findings", [])}) or None,
                              checks=[c["name"] for c in outcome.get("checks", []) if not c["passed"]] or None)
    for claim, _ in pending:
        items = load_backlog(pkg)
        item = next((i for i in items if i.id == claim.id), None)
        skipped.append({"id": claim.id, "branch": claim.branch, "reason": "waiting", "landed": False,
                        "blocked_by": blocked_by(item, items) if item else [],
                        "message": f"{claim.id} depends on items that aren't done on {base}"})

    pushed = False
    if push and remote and landed and not dry_run:
        proc = _git(root, "push", "--quiet", remote, f"refs/heads/{base}:refs/heads/{base}")
        if proc.returncode != 0:
            raise WorkError("push_failed", f"landed {len(landed)} item(s) locally, but pushing {base} failed: "
                            f"{proc.stderr.strip()}", details={"landed": landed, "skipped": skipped})
        pushed = True
    released: list[str] = []
    if release and not dry_run:
        for outcome in landed:
            try:  # pushed claim branches go too, but only once the integration branch holds their work remotely
                cl.release_claim(root, outcome["id"], remote=pushed, base_ref=base, closed={outcome["id"]})
                released.append(outcome["id"])
            except cl.GitError as exc:
                outcome["release_error"] = str(exc)
    return {
        "schema": SCHEMA, "ok": True, "base": base, "dry_run": dry_run, "landed": landed, "skipped": skipped,
        "released": released, "pushed": pushed, "base_trust": trusted,
        "proposals": [shown(pkg, pkg.rel(p)) for p, _ in load_proposals(pkg)],
    }


def check_base(pkg: Package, base: str, remote: str | None, *, accept: bool = False, write: bool = True) -> str:
    """Refuse (`base_untrusted`) while the integration branch holds commits `its` doesn't trust, unless `accept`.
    Returns how it is trusted: "trusted", "first_use" (nothing was recorded yet) or "accepted". Without `write`
    (a dry run) nothing is recorded."""
    root = pkg.root
    try:
        left = trust.untrusted(root, base, "HEAD", remote)
    except gitx.GitError as exc:
        raise WorkError("git_error", f"couldn't read {base}'s history: {exc}") from exc
    if left is None or (left and accept):
        if write:
            trust.record(root, base, "HEAD")
        return "first_use" if left is None else "accepted"
    if not left:
        return "trusted"
    where = f"on {remote}/{base} " if remote else ""
    raise WorkError(
        "base_untrusted",
        f"{base} has {len(left)} commit(s) that its didn't make and that aren't {where or 'pushed '}"
        f"({left[0]}{', ...' if len(left) > 1 else ''}). An agent's worktree shares {base}'s ref and can move it, "
        f"and {base} holds the gate's rules; look at them (git log {base}), then run its land --accept-base if they "
        "are yours", EXIT_NOT_LANDED, {"integration_branch": base, "commits": left})


def _land_one(pkg: Package, claim: cl.Claim, ref: str, *, verify: bool, dry_run: bool,
              on_check: Callable[[str, Check], None] | None, remote: str | None = None) -> dict:
    """Merge one claim in a throwaway worktree, check the merged result there, and only then fast-forward the
    integration branch to it. The user's checkout is never merged into, reset or cleaned, so nothing in it (untracked
    notes included) can be lost, and a status written there while the checks run survives."""
    root = pkg.root
    result = {"id": claim.id, "branch": claim.branch, "landed": False}
    try:  # the done gate again, on what would merge
        inspection = gate.inspect(pkg, claim.id, "HEAD", head=ref)
    except gate.GateError as exc:
        return {**result, "reason": "gate_error", "message": f"{claim.id} can't be checked: {exc}"}
    blocking = inspection.blocking()
    if blocking:
        tests = [f for f in blocking if f["kind"] in gate.TEST_KINDS]
        return {**result, "reason": gate.refusal(blocking), "findings": blocking,
                "message": (tests or blocking)[0]["message"]}
    conflicts = cl.conflicting_files(root, "HEAD", ref)
    generated = _generated(pkg)
    real = [f for f in conflicts or [] if f not in generated]
    if real:
        return {**result, "reason": "conflict", "files": real,
                "message": f"{claim.branch} conflicts with the integration branch in {', '.join(real)}; "
                           "merge the integration branch into it and resolve them there"}
    if dry_run:
        return {**result, "reason": "dry_run", "message": "would land"}
    before = _git(root, "rev-parse", "HEAD").stdout.strip()
    if trust.untrusted(root, integration_branch(pkg), before, remote):  # moved since land checked it
        return {**result, "reason": "base_untrusted",
                "message": f"{integration_branch(pkg)} moved to a commit its didn't make while landing; run its land "
                           "again to see it"}
    prefix = _git(root, "rev-parse", "--show-prefix").stdout.strip()
    item_title = (_item_at(pkg, ref, claim.id) or BacklogItem(claim.id, "", 3, 0, 0)).title
    scratch = Path(tempfile.mkdtemp(prefix="its-land-"))
    tree = scratch / "merge"
    if _git(root, "worktree", "add", "--detach", "--quiet", str(tree), before).returncode != 0:
        shutil.rmtree(scratch, ignore_errors=True)
        return {**result, "reason": "git_error", "message": "couldn't create a worktree to test the merge in"}
    try:
        merged = _merge_in(tree, prefix, ref, f"Land {claim.id}: {item_title}".rstrip(": "), generated)
        if "reason" in merged:
            return {**result, **merged}
        checks: list[dict] = []
        if verify and pkg.repo_layout:
            here = tree / prefix if prefix else tree
            commands = inspection.policy.commands
            item = inspection.planned or _item_at(pkg, ref, claim.id)
            plan = checks_for(pkg, item, inspection.verify_command, commands, acceptance=True) if item else []
            if commands.get("setup") and plan:  # a fresh worktree has no installed dependencies yet
                setup = run_check(Check("setup", commands["setup"]), here, inspection.policy.run)
                if not setup["passed"]:
                    return {**result, "reason": "check_failed", "checks": [setup],
                            "message": f"setup ({commands['setup']}) failed in the merge worktree; "
                                       f"{claim.id} was not landed"}
            for check in plan:
                if on_check:
                    on_check(claim.id, check)
                checks.append(run_check(check, here, inspection.policy.run))
                if not checks[-1]["passed"]:
                    break
        failed = [c["name"] for c in checks if not c["passed"]]
        if failed:
            return {**result, "reason": "check_failed", "checks": checks,
                    "message": f"{', '.join(failed)} failed on the merged result; {claim.id} was not landed"}
        for finding in result_findings(checks, changed_tests(pkg, inspection)):
            inspection.add(finding)
        refused = [f for f in inspection.blocking() if f["kind"] in RESULT_KINDS]
        if refused:
            return {**result, "reason": "test_changes", "findings": refused, "checks": checks,
                    "message": f"{refused[0]['message']}; {claim.id} was not landed"}
        commit = _git(tree, "rev-parse", "HEAD").stdout.strip()
        with _locked(pkg):
            if _git(root, "rev-parse", "HEAD").stdout.strip() != before:
                return {**result, "reason": "base_moved", "checks": checks,
                        "message": "the integration branch moved while the checks ran; run its land again"}
            ff = _git(root, "merge", "--ff-only", "--quiet", commit)
            if ff.returncode != 0:
                return {**result, "reason": "checkout_blocked", "checks": checks,
                        "message": "the merged result passed, but your checkout couldn't be moved to it: "
                                   f"{ff.stderr.strip() or ff.stdout.strip()}"}
            trust.record(root, integration_branch(pkg), commit)
        _refresh_mirror(pkg)
        return {**result, "landed": True, "commit": commit, "checks": checks, "warnings": inspection.warnings,
                "checks_skipped": None if (verify and pkg.repo_layout) else
                ("--no-verify" if not verify else "this is a planning package, not a scaffolded repo")}
    finally:
        _git(root, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(scratch, ignore_errors=True)
        _git(root, "worktree", "prune")


def _merge_in(tree: Path, prefix: str, ref: str, message: str, generated: set[str]) -> dict:
    """Merge `ref` into the throwaway worktree, regenerating its generated files. {} on success, else why not."""
    merge = _git(tree, "merge", "--no-ff", "--no-edit", "-m", message, ref)
    if merge.returncode != 0:
        unresolved = _git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split()
        mine = [f[len(prefix):] if prefix and f.startswith(prefix) else f for f in unresolved]
        if not unresolved or any(f not in generated for f in mine):
            return {"reason": "conflict", "files": mine, "message": merge.stderr.strip() or merge.stdout.strip()}
        for f in unresolved:  # generated files: take ours, regenerate below
            _git(tree, "checkout", "--ours", "--", f)
            _git(tree, "add", "--", f)
        if _git(tree, "commit", "--no-edit", "--quiet").returncode != 0:
            return {"reason": "conflict", "files": mine}
    merged = Package.load(tree / prefix if prefix else tree)
    if link_package(merged):  # the merged docs may need fresh links and index
        _git(tree, "add", "--all", "--", str(merged.planning_dir))
        _git(tree, "commit", "--quiet", "--amend", "--no-edit")
    return {}
