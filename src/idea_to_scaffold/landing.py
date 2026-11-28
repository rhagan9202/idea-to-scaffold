"""`its land`: the landing lane. Finished claim branches are merged into the integration branch one at a time, and
the checks run on each merged result before the next one lands.

Each branch passing its own checks doesn't mean the merge of several does: two branches can each be green and
still break each other. So `land` merges one branch, runs the item's Verify plus the test and lint commands on
the merged tree, and keeps the merge only if they pass; otherwise it undoes the merge and moves on. Main only
ever advances to a state that was tested as a whole.

A branch is ready to land when its item is `done` on the branch (its agent ran `its done`). Branches land in
backlog order, and an item lands only once its dependencies are done on the integration branch.

An adopted claim (`its adopt`) has candidates instead: branches a vendor's agent made, where the item isn't done
because nobody ran `its done` there. Land runs the whole done gate on each candidate in turn (the scope and tamper
checks, Verify, test and lint, the held-out tests of the item, and red on parent), lands the first that passes with
the item set done in the merge commit, and leaves the others (`not_chosen`). So when several agents tried the
item, the held-out tests pick the one that lands.

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
from datetime import date
from pathlib import Path

from . import claims as cl
from . import gate, gitx, heldout, ledger, trust
from . import review as reviews
from .backlog import BACKLOG, BacklogItem, blocked_by, load_backlog, load_proposals, parse_backlog, set_field
from .checks import RESULT_KINDS, Check, checks_for, result_findings, run_check
from .contract import EXIT_NOT_LANDED, SCHEMA, WorkError
from .gating import changed_tests, red_on_parent
from .ids import META_FILES
from .linker import link_package
from .locking import holding_lock
from .mirror import BACKLOG_JSON, refresh_mirror
from .package import Package
from .touches import covered
from .verified import SKIPPED_MARK, touches_hash_at
from .view import integration_branch, remote_of, shown


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


Option = tuple[str, "dict | None"]  # the ref to merge, and the adopted candidate it is (None for the claim branch)


def _candidates(pkg: Package, base: str, ids: list[str] | None, *,
                fetch: bool = False) -> tuple[list[tuple[cl.Claim, list[Option]]], list[dict]]:
    """Claims that are ready to land, in backlog order, each with what to try merging (its branch, or an adopted
    claim's candidates in the order they were adopted), and why the named ones that aren't are skipped."""
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
    ready: list[tuple[cl.Claim, list[Option]]] = []
    for claim in sorted(claims, key=lambda c: order.get(c.id, len(order))):
        if claim.candidates:
            options, why = _adopted(pkg, claim, base, fetch=fetch)
            if options:
                ready.append((claim, options))
            elif ids:
                skipped.append({"id": claim.id, "reason": "no_commits", "landed": False, "message": why})
            continue
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
        ready.append((claim, [(ref, None)]))
    return ready, skipped


def _adopted(pkg: Package, claim: cl.Claim, base: str, *, fetch: bool) -> tuple[list[Option], str]:
    """An adopted claim's candidates that have commits to land (fetched first with `fetch`), and else why not."""
    options: list[Option] = []
    gone: list[str] = []
    for cand in claim.candidates:
        if fetch:
            try:
                if cl.fetch_candidate(pkg.root, cand) is None:
                    gone.append(cand.get("name") or cand["ref"])
                    continue
            except cl.GitError:
                pass  # the remote can't be reached: try what was fetched last
        if cl.ref_exists(pkg.root, cand["ref"]) and cl.ahead(pkg.root, cand["ref"], base):
            options.append((cand["ref"], cand))
    names = ", ".join(c.get("name") or c["ref"] for c in claim.candidates)
    why = f"{claim.id}'s adopted branches ({names}) have no new commits" + (f"; gone: {', '.join(gone)}" if gone else "")
    return options, why


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
    require_review: bool = False,
    on_check: Callable[[str, Check], None] | None = None,
) -> dict:
    """Land finished claim branches on the integration branch, one at a time, testing each merged result.
    `accept_base` trusts the commits on the integration branch that `its` didn't make (a person has looked).
    `require_review` lands only what has an approving review of exactly what would merge (as does the integration
    branch's `gate.require_review`); a changes_requested verdict on it always refuses (review.py)."""
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
    ready, early = _candidates(pkg, "HEAD", ids, fetch=fetch)
    skipped += early
    pending = list(ready)
    progress = True
    broken: dict | None = None  # the integration branch itself fails a check: no other item can land on it
    while pending and progress and broken is None:  # an item whose dependency lands later gets another turn
        progress = False
        for claim, options in list(pending):
            if broken is not None:
                break
            items = load_backlog(pkg)
            item = next((i for i in items if i.id == claim.id), None)
            waiting = blocked_by(item, items) if item else []
            if waiting:
                continue
            pending.remove((claim, options))
            progress = True
            chosen: dict | None = None
            for ref, cand in options:  # an adopted claim's candidates, in turn: the first that passes lands
                if chosen is not None:
                    skipped.append({"id": claim.id, "branch": cand["name"] if cand else claim.branch,
                                    "reason": "not_chosen", "landed": False,
                                    "message": f"{claim.id} landed from {chosen['branch']}, which was tried first"})
                    continue
                outcome = _land_one(pkg, claim, ref, verify=verify, dry_run=dry_run, on_check=on_check,
                                    remote=remote, require_review=require_review, candidate=cand)
                (landed if outcome["landed"] else skipped).append(outcome)
                if not dry_run:
                    reason = "landed" if outcome["landed"] else outcome.get("reason", "skipped")
                    failed = reason in ledger.FAILURES
                    why = outcome.get("findings") or ([{"message": outcome["message"]}] if outcome.get("message") else [])
                    ledger.record(pkg, claim.id, "land", reason,
                                  findings=sorted({f["kind"] for f in outcome.get("findings", [])}) or None,
                                  checks=[c["name"] for c in outcome.get("checks", []) if not c["passed"]] or None,
                                  branch=cand["name"] if cand else None, agent=cand.get("agent") if cand else None,
                                  head=gitx.out(root, "rev-parse", "--verify", "--quiet", ref, check=False).strip()
                                  or None, attempt=len(ledger.failures(pkg, claim.id)) + 1 if failed else None,
                                  collateral=True if reason == "base_failing" else None,
                                  **(ledger.failure_fields(outcome.get("checks"), why) if failed else {}))
                if outcome["landed"]:
                    chosen = outcome
                if outcome.get("reason") == "base_failing":
                    broken = outcome
                    break
    for claim, _ in pending if broken is not None else []:
        skipped.append({"id": claim.id, "branch": claim.branch, "reason": "base_failing", "landed": False,
                        "message": f"not tried: {broken['message']}"})
    for claim, _ in pending if broken is None else []:
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
              on_check: Callable[[str, Check], None] | None, remote: str | None = None,
              require_review: bool = False, candidate: dict | None = None) -> dict:
    """Merge one claim in a throwaway worktree, check the merged result there, and only then fast-forward the
    integration branch to it. The user's checkout is never merged into, reset or cleaned, so nothing in it (untracked
    notes included) can be lost, and a status written there while the checks run survives.

    `candidate` is an adopted claim's vendor branch (`ref`): nobody ran `its done` on it, so land also runs red on
    parent, counts the item's own held-out tests in, and sets the item done in the merge commit."""
    root = pkg.root
    result = {"id": claim.id, "branch": candidate["name"] if candidate else claim.branch, "landed": False}
    if candidate:
        result.update(adopted=True, claim_branch=claim.branch)
    setup = {} if candidate else cl.setup_of(root, claim.id)
    try:  # the done gate again, on what would merge
        inspection = gate.inspect(pkg, claim.id, "HEAD", head=ref, setup_files=setup)
    except gate.GateError as exc:
        return {**result, "reason": "gate_error", "message": f"{claim.id} can't be checked: {exc}"}
    blocking = inspection.blocking()
    if blocking:
        tests = [f for f in blocking if f["kind"] in gate.TEST_KINDS]
        return {**result, "reason": gate.refusal(blocking), "findings": blocking,
                "message": (tests or blocking)[0]["message"]}
    refused = reviews.land_refusal(pkg, claim.id, inspection, require=require_review or reviews.needs_review(inspection))
    if refused:
        return {**result, **refused}
    review = reviews.standing(pkg, claim.id, inspection)
    approved = review if review and review["current"] and review["passed"] else None
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
    item_title = (inspection.planned or _item_at(pkg, ref, claim.id) or BacklogItem(claim.id, "", 3, 0, 0)).title
    scratch = Path(tempfile.mkdtemp(prefix="its-land-"))
    tree = scratch / "merge"
    if _git(root, "worktree", "add", "--detach", "--quiet", str(tree), before).returncode != 0:
        shutil.rmtree(scratch, ignore_errors=True)
        return {**result, "reason": "git_error", "message": "couldn't create a worktree to test the merge in"}
    try:
        message = f"Land {claim.id}: {item_title}".rstrip(": ")
        trailers = [f"Adopted: {candidate['name']}"] if candidate else []
        if approved:  # a trailer, so the history says what review the landed content had
            trailers.append(f"Reviewed: approved {approved['at'][:10]} ({approved['digest']})")
        if trailers:
            message += "\n\n" + "\n".join(trailers)
        merged = _merge_in(tree, prefix, ref, message, generated)
        if "reason" in merged:
            return {**result, **merged}
        checks: list[dict] = []
        held: dict | None = None
        reverified: list[str] = []
        if verify and pkg.repo_layout:
            here = tree / prefix if prefix else tree
            commands = inspection.policy.commands
            settings = inspection.policy.run
            item = inspection.planned or _item_at(pkg, ref, claim.id)
            plan = checks_for(pkg, item, inspection.verify_command, commands, acceptance=True) if item else []
            folder = prefix + inspection.policy.acceptance
            done_there = heldout.done_ids(load_backlog(Package.load(here)))
            if candidate:  # landing sets the item done, so its own held-out tests count
                done_there = set(done_there) | {claim.id}
            try:  # held-out tests of items not done on the merged result stay out (heldout.py)
                held = heldout.show_for(tree, folder, done_there)
            except gitx.GitError as exc:
                return {**result, "reason": "git_error", "message": str(exc)}
            if held is not None and not held["runs"]:  # every held-out test belongs to an item that isn't done yet
                plan = [c for c in plan if c.name != "acceptance"]
            if commands.get("setup") and plan:  # a fresh worktree has no installed dependencies yet
                setup = run_check(Check("setup", commands["setup"]), here, settings)
                if not setup["passed"]:
                    return {**result, "reason": "check_failed", "checks": [setup],
                            "message": f"setup ({commands['setup']}) failed in the merge worktree; "
                                       f"{claim.id} was not landed"}
            for check in plan:
                if on_check:
                    on_check(claim.id, check)
                checks.append(run_check(check, here, settings))
                if not checks[-1]["passed"]:
                    break
            if checks and not checks[-1]["passed"]:
                return _failed(pkg, claim, result, checks, plan[len(checks) - 1], tree, here, before, folder,
                               commands, settings, held)
            for finding in result_findings(checks, changed_tests(pkg, inspection)):
                inspection.add(finding)
            refused = [f for f in inspection.blocking() if f["kind"] in RESULT_KINDS]
            if refused:
                return {**result, "reason": "test_changes", "findings": refused, "checks": checks,
                        "message": f"{refused[0]['message']}; {claim.id} was not landed"}
            if candidate:  # what its done would have run on the branch: do its new tests fail without the change?
                red = red_on_parent(pkg, claim.id, inspection, inspection.verify_command)
                result["red_on_parent"] = red
                refused = [f for f in inspection.blocking() if f["kind"] == "tests_pass_on_parent"]
                if refused:
                    return {**result, "reason": "test_changes", "findings": refused, "checks": checks,
                            "message": f"{refused[0]['message']}; {claim.id} was not landed"}
            others = _reverify(pkg, claim.id, tree, here, before, settings, on_check)
            if "reason" in others:
                return {**result, **others, "checks": checks + others.pop("checks", [])}
            checks += others["checks"]
            reverified = others["reverified"]
            _mark_landed(here, tree, claim.id, before, reverified, set_done=bool(candidate))
        elif candidate:  # no checks ran (--no-verify, or a planning package), but the item still becomes done
            _mark_landed(tree / prefix if prefix else tree, tree, claim.id, before, [], set_done=True,
                         skipped=not verify)
        commit = _git(tree, "rev-parse", "HEAD").stdout.strip()
        with holding_lock(pkg):
            if _git(root, "rev-parse", "HEAD").stdout.strip() != before:
                return {**result, "reason": "base_moved", "checks": checks,
                        "message": "the integration branch moved while the checks ran; run its land again"}
            ff = _git(root, "merge", "--ff-only", "--quiet", commit)
            if ff.returncode != 0:
                return {**result, "reason": "checkout_blocked", "checks": checks,
                        "message": "the merged result passed, but your checkout couldn't be moved to it: "
                                   f"{ff.stderr.strip() or ff.stdout.strip()}"}
            trust.record(root, integration_branch(pkg), commit)
        refresh_mirror(pkg)
        return {**result, "landed": True, "commit": commit, "checks": checks, "warnings": inspection.warnings,
                "held_out": held, "reverified": reverified, "review": approved,
                "checks_skipped": None if (verify and pkg.repo_layout) else
                ("--no-verify" if not verify else "this is a planning package, not a scaffolded repo")}
    finally:
        _git(root, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(scratch, ignore_errors=True)
        _git(root, "worktree", "prune")


BASE_CHECKED = ("test", "lint", "acceptance")  # Verify is the item's own: it is meant to fail without the item


def _failed(pkg: Package, claim: cl.Claim, result: dict, checks: list[dict], check: Check, tree: Path, here: Path,
            before: str, folder: str, commands: dict, settings, held: dict | None) -> dict:
    """Why a check failed on the merged result, and whose fault it is. A test, lint or held-out check that fails on
    the integration branch too, before the item is merged, is the integration branch's (`base_failing`), not the
    item's (B14). A held-out failure is named by the item whose held-out tests failed."""
    failed = checks[-1]
    name = check.name
    if name in BASE_CHECKED:
        on_base = _on_base(tree, here, before, check, folder, commands, settings)
        if on_base is not None and not on_base["passed"]:
            base = integration_branch(pkg)
            return {**result, "reason": "base_failing", "checks": checks, "base_check": on_base,
                    "message": f"{name} fails on {base} itself, before {claim.id} is merged ({check.command}); fix "
                               f"{base}, then run its land again"}
    owners = heldout.failing_items(failed, folder)
    if owners:
        whose = ", ".join(f"{o}'s held-out tests ({folder.rstrip('/')}/{o}/)" for o in owners)
        message = f"{name} failed on the merged result: {whose}; {claim.id} was not landed"
    else:
        message = f"{name} failed on the merged result; {claim.id} was not landed"
    return {**result, "reason": "check_failed", "checks": checks, "held_out": held, "held_out_failed": owners,
            "message": message}


def _on_base(tree: Path, here: Path, before: str, check: Check, folder: str, commands: dict, settings) -> dict | None:
    """Run `check` on the integration branch as it was before the merge, in the same worktree: held-out tests of
    the items done there, after setup. None if the worktree can't be moved there."""
    if _git(tree, "checkout", "--detach", "--quiet", "--force", before).returncode != 0:
        return None
    try:
        view = heldout.show_for(tree, folder, heldout.done_ids(load_backlog(Package.load(here))))
    except (gitx.GitError, OSError):
        return None
    if check.name == "acceptance" and (view is None or not view["runs"]):
        return {"name": check.name, "command": check.command, "passed": True, "exit_code": 0, "output": "",
                "skipped": "no held-out test of an item done there"}
    if commands.get("setup"):
        setup = run_check(Check("setup", commands["setup"]), here, settings)
        if not setup["passed"]:
            return setup
    return run_check(check, here, settings)


def _reverify(pkg: Package, ident: str, tree: Path, here: Path, before: str, settings,
              on_check: Callable[[str, Check], None] | None) -> dict:
    """Run again the Verify command of every other done item whose Touches the merge changed, so its Verified mark
    can be refreshed in the merge commit and `its drift` stays quiet for what land has checked. One that fails
    refuses the landing: the merge broke an item that was done."""
    merged = Package.load(here)
    changed = [f for f in _git(here, "diff", "-z", "--name-only", "--relative", before, "HEAD").stdout.split("\0") if f]
    checks: list[dict] = []
    again: list[str] = []
    for other in load_backlog(merged):
        if other.id == ident or other.status != "done" or not other.touches or not other.verify_command:
            continue
        if (other.get("Verified") or "").startswith("skipped") or not any(covered(f, other.touches) for f in changed):
            continue
        check = Check(f"verify {other.id}", other.verify_command)
        if on_check:
            on_check(ident, check)
        checks.append(run_check(check, here, settings))
        if not checks[-1]["passed"]:
            return {"reason": "check_failed", "checks": checks,
                    "message": f"{other.id}'s Verify fails on the merged result ({other.verify_command}), so merging "
                               f"{ident} broke work that was done; {ident} was not landed"}
        again.append(other.id)
    return {"checks": checks, "reverified": again}


def _mark_landed(here: Path, tree: Path, ident: str, before: str, others: list[str], *, set_done: bool = False,
                 skipped: bool = False) -> None:
    """Write the Verified mark of the landed item, and of the other items checked again, from the merged tree, and
    amend the merge commit with it: the mark then describes what landed, not the agent's working tree (E1).
    `set_done` also sets the landed item done (an adopted branch, where nobody ran `its done`); `skipped` marks it
    unchecked, as `--no-verify` does."""
    merged = Package.load(here)
    stamp = f"{date.today().isoformat()}, base {before[:10]}"
    wrote = False
    for item in load_backlog(merged):
        if item.id != ident and item.id not in others:
            continue
        if set_done and item.id == ident and item.status != "done":
            set_field(merged, item.id, "Status", "done")
        if skipped and item.id == ident:
            set_field(merged, item.id, "Verified", SKIPPED_MARK, add=True)
            wrote = True
            continue
        tree_hash = touches_hash_at(merged, item.touches, "HEAD")
        set_field(merged, item.id, "Verified", stamp + (f", tree {tree_hash}" if tree_hash else ""), add=True)
        wrote = True
    if wrote:
        _git(tree, "add", "--", str(merged.path_of(BACKLOG)))
        _git(tree, "commit", "--quiet", "--amend", "--no-edit")


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
