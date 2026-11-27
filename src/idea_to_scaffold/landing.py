"""`its land`: the landing lane. Finished claim branches are merged into the integration branch one at a time, and
the checks run on each merged result before the next one lands.

Each branch passing its own checks doesn't mean the merge of several does: two branches can each be green and
still break each other. So `land` merges one branch, runs the item's Verify plus the test and lint commands on
the merged tree, and keeps the merge only if they pass; otherwise it undoes the merge and moves on. Main only
ever advances to a state that was tested as a whole.

A branch is ready to land when its item is `done` on the branch (its agent ran `its done`). Branches land in
backlog order, and an item lands only once its dependencies are done on the integration branch.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from . import claims as cl
from . import gate
from .backlog import BacklogItem, blocked_by, load_backlog, load_proposals, parse_backlog
from .ids import META_FILES
from .linker import link_package
from .package import Package
from .work import (
    BACKLOG,
    BACKLOG_JSON,
    SCHEMA,
    Check,
    WorkError,
    _locked,
    _refresh_mirror,
    checks_for,
    integration_branch,
    remote_of,
    run_check,
    shown,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


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
    allow_test_changes: bool = False,
    allow_outside_touches: bool = False,
    on_check: Callable[[str, Check], None] | None = None,
) -> dict:
    """Land finished claim branches on the integration branch, one at a time, testing each merged result."""
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
            outcome = _land_one(pkg, claim, ref, verify=verify, dry_run=dry_run, on_check=on_check,
                                allow=(allow_test_changes, allow_outside_touches))
            (landed if outcome["landed"] else skipped).append(outcome)
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
        "released": released, "pushed": pushed, "proposals": [shown(pkg, pkg.rel(p)) for p, _ in load_proposals(pkg)],
    }


def _land_one(pkg: Package, claim: cl.Claim, ref: str, *, verify: bool, dry_run: bool,
              on_check: Callable[[str, Check], None] | None, allow: tuple[bool, bool] = (False, False)) -> dict:
    root = pkg.root
    result = {"id": claim.id, "branch": claim.branch, "landed": False}
    inspection = gate.inspect(pkg, claim.id, "HEAD", head=ref)  # the done gate again, on what would merge
    blocking = inspection.blocking(allow_test_changes=allow[0], allow_outside_touches=allow[1])
    if blocking:
        tests = [f for f in blocking if f["kind"] in gate.TEST_KINDS]
        return {**result, "reason": "test_changes" if tests else "outside_touches", "findings": blocking,
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
    item_title = (_item_at(pkg, ref, claim.id) or BacklogItem(claim.id, "", 3, 0, 0)).title
    with _locked(pkg):
        merge = _git(root, "merge", "--no-ff", "--no-edit", "-m", f"Land {claim.id}: {item_title}".rstrip(": "), ref)
        if merge.returncode != 0:
            unresolved = _git(root, "diff", "--name-only", "--diff-filter=U").stdout.split()
            if not unresolved or any(f not in generated for f in unresolved):
                _git(root, "merge", "--abort")
                return {**result, "reason": "conflict", "files": unresolved or [],
                        "message": merge.stderr.strip() or merge.stdout.strip()}
            for f in unresolved:  # generated files: take ours, regenerate below
                _git(root, "checkout", "--ours", "--", f)
                _git(root, "add", "--", f)
            if _git(root, "commit", "--no-edit", "--quiet").returncode != 0:
                _git(root, "merge", "--abort")
                return {**result, "reason": "conflict", "files": unresolved}
        if link_package(pkg):  # the merged docs may need fresh links and index
            _git(root, "add", "--all", "--", str(pkg.planning_dir))
            _git(root, "commit", "--quiet", "--amend", "--no-edit")
    _refresh_mirror(pkg)

    checks: list[dict] = []
    if verify and pkg.repo_layout:
        item = next((i for i in load_backlog(pkg) if i.id == claim.id), None)
        for check in checks_for(pkg, item) if item else []:
            if on_check:
                on_check(claim.id, check)
            checks.append(run_check(check, root))
            if not checks[-1]["passed"]:
                break
    failed = [c["name"] for c in checks if not c["passed"]]
    if failed:
        with _locked(pkg):
            _git(root, "reset", "--hard", "--quiet", before)
        _refresh_mirror(pkg)
        return {**result, "reason": "check_failed", "checks": checks,
                "message": f"{', '.join(failed)} failed on the merged result; the merge was undone"}
    return {**result, "landed": True, "commit": _git(root, "rev-parse", "HEAD").stdout.strip(), "checks": checks,
            "checks_skipped": None if (verify and pkg.repo_layout) else
            ("--no-verify" if not verify else "this is a planning package, not a scaffolded repo")}
