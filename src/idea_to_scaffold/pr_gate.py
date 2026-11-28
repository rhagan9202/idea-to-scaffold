"""`its gate [REF]`: the done gate on any branch or pull request, for CI.

Agents that never run `its` (a vendor's cloud agent, a person, an IDE) push branches and open pull requests, and
neither the Stop hook nor `its done` runs there. `its land` gates those branches when they land; `its gate` gates
them earlier, in the pull request's CI, so the pull request shows red before anyone reviews it. Both run the whole
done gate with the rules from the integration branch, which the branch can't change: the scope and tamper checks,
the item's Verify, the test and lint commands, the item's held-out tests and red on parent. Land, and CI running
`its gate`, are the only gates that cover every surface: the hooks and `its done` are pre-checks an agent can skip.

The checks run in a throwaway worktree at REF, so the checkout `its gate` runs in is never changed. The item comes
from `--id`, else the branch: a claim branch (`its/T-2.1-...`), a branch adopted here (`its adopt`), a T-n.m in the
branch name (for HEAD in a pull request's CI, `GITHUB_HEAD_REF`), the item whose status the branch changes, or `[T-n.m]` in
its commit messages. A branch that names no item has nothing to gate and passes, unless `--require-item`. In a pull
request's CI the base is the branch it targets (`GITHUB_BASE_REF`), read from the remote when there is no local copy.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import claims as cl
from . import gate, gitx, heldout, ledger
from .backlog import BACKLOG, BacklogItem, load_backlog, parse_backlog
from .checks import RESULT_KINDS, Check, checks_for, result_findings, run_check
from .contract import SCHEMA, WorkError
from .package import Package
from .work import changed_tests, integration_branch, red_on_parent, remote_of

ID_IN_NAME = re.compile(r"(?<![\w.-])(T-\d+\.\d+)(?![\d.])")  # claude/T-2.1-store, T-2.1/store
TAG_RE = re.compile(r"\[(T-\d+\.\d+)\]")  # `[T-2.1] add the store` in a commit message
STARTED = ("in-progress", "blocked", "done")  # statuses an agent's branch sets on its item


def resolve_ref(pkg: Package, ref: str) -> str:
    """The commit `ref` names: a ref, a branch, or the remote's copy of a branch. Raises `no_branch`."""
    root = pkg.root
    remote = remote_of(pkg)
    for name in (ref, f"refs/remotes/{remote}/{ref}" if remote else None):
        if name:
            sha = gitx.out(root, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", check=False).strip()
            if sha:
                return sha
    raise WorkError("no_branch", f"there is no branch or commit {ref} here", details={"branch": ref})


def base_of(pkg: Package, base: str | None) -> str:
    """The ref the gate compares against and reads its rules from: `base`, else the branch a pull request targets
    (`GITHUB_BASE_REF`), else the integration branch; the remote's copy when there is no local one."""
    name = base or os.environ.get("GITHUB_BASE_REF") or integration_branch(pkg)
    resolved = cl.resolve_base(pkg.root, name, remote_of(pkg))
    if resolved is None and cl.ref_exists(pkg.root, name):
        resolved = name
    if resolved is None:
        raise WorkError("no_base", f"there is no {name} here to gate against; fetch it (CI: actions/checkout with "
                        "fetch-depth: 0), or name one with --base")
    return resolved


def branch_of(pkg: Package, ref: str) -> str | None:
    """The branch name REF stands for: the branch itself, or for HEAD the pull request's source branch in CI
    (`GITHUB_HEAD_REF`, since the checkout there is detached), else the branch checked out. A ref named outright is
    never renamed by the environment."""
    if ref == "HEAD":
        return os.environ.get("GITHUB_HEAD_REF") or cl.current_branch(pkg.root)
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def find_items(pkg: Package, base: str, head: str, branch: str | None) -> tuple[list[str], str | None]:
    """Which item a branch is for, and how that was found. Only items in the base's backlog count."""
    root = pkg.root
    text = cl.read_at(root, base, pkg.path_of(BACKLOG))
    planned = {i.id: i for i in parse_backlog(text)} if text else {}
    if branch:
        ident = cl.item_of_branch(branch) or cl.item_of_branch(branch.split("/", 1)[-1])
        if ident in planned:
            return [ident], "claim_branch"
        adopted = sorted({c.id for c in cl.list_claims(root) for x in c.candidates
                          if branch in (x.get("name"), x.get("ref"), f"{x.get('remote')}/{x.get('name')}")})
        if adopted:
            return adopted, "adopted"
        named = sorted(set(ID_IN_NAME.findall(branch)) & set(planned))
        if named:
            return named, "branch_name"
    fork = gitx.out(root, "merge-base", base, head, check=False).strip()
    after = cl.read_at(root, head, pkg.path_of(BACKLOG))
    if fork and after:
        before = {i.id: i.status for i in parse_backlog(cl.read_at(root, fork, pkg.path_of(BACKLOG)) or "")}
        moved = sorted(i.id for i in parse_backlog(after)
                       if i.id in planned and i.status in STARTED and i.status != before.get(i.id))
        if moved:
            return moved, "backlog"
    log = gitx.out(root, "log", "--format=%B", f"{base}..{head}", check=False)
    tagged = sorted(set(TAG_RE.findall(log)) & set(planned))
    if tagged:
        return tagged, "commit_tags"
    return [], None


def gate_ref(pkg: Package, ref: str = "HEAD", *, base: str | None = None, ident: str | None = None,
             require_item: bool = False, verify: bool = True,
             on_check: Callable[[str, Check], None] | None = None) -> dict:
    """Run the done gate and the item's checks on `ref` against `base`, with the base's rules, changing nothing."""
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "its gate needs a git repository")
    base_ref = base_of(pkg, base)
    head = resolve_ref(pkg, ref)
    branch = branch_of(pkg, ref)
    ids, found_by = ([ident], "--id") if ident else find_items(pkg, base_ref, head, branch)
    out = {"schema": SCHEMA, "ok": True, "ref": ref, "head": head, "base": base_ref, "branch": branch,
           "id": None, "found_by": found_by, "passed": True, "reason": None, "findings": [], "warnings": [],
           "checks": [], "held_out": None, "red_on_parent": None}
    if not ids:
        out.update(passed=not require_item, reason="no_item",
                   message=f"{branch or ref} names no backlog item, so there is nothing to gate; name it with --id, "
                           "a T-n.m in the branch name, or [T-n.m] in a commit message")
        return out
    if len(ids) > 1:
        raise WorkError("which_item", f"{branch or ref} could be for {', '.join(ids)}; name the one to gate with --id",
                        details={"candidates": ids, "found_by": found_by})
    ident = ids[0]
    out["id"] = ident
    try:
        inspection = gate.inspect(pkg, ident, base_ref, head=head)
    except gate.GateError as exc:
        raise WorkError("gate_error", f"{ident} can't be gated: {exc}", details={"id": ident}) from exc
    if inspection.planned is None:
        raise WorkError("not_found", f"{ident} isn't in {base_ref}'s backlog", details={"id": ident})
    out["policy_source"] = inspection.policy.source
    out = {**out, **_run(pkg, ident, inspection, head, verify=verify and pkg.repo_layout, on_check=on_check)}
    failed = not out["passed"] and out["reason"] in ledger.FAILURES
    why = out["findings"] or [{"message": out["message"]}]
    try:
        ledger.record(pkg, ident, "gate", "passed" if out["passed"] else out["reason"], branch=branch, head=head,
                      findings=sorted({f["kind"] for f in out["findings"]}) or None,
                      checks=[c["name"] for c in out["checks"] if not c["passed"]] or None,
                      attempt=len(ledger.failures(pkg, ident)) + 1 if failed else None,
                      **(ledger.failure_fields(out["checks"], why) if failed else {}))
    except (OSError, gitx.GitError):
        pass
    return out


def _run(pkg: Package, ident: str, inspection: gate.Inspection, head: str, *, verify: bool,
         on_check: Callable[[str, Check], None] | None) -> dict:
    """The findings, then the checks in a throwaway worktree at `head`: what land would run on the merge."""
    blocking = inspection.blocking()
    if blocking:
        tests = [f for f in blocking if f["kind"] in gate.TEST_KINDS]
        return _verdict(inspection, gate.refusal(blocking), (tests or blocking)[0]["message"], blocking)
    if not verify:
        return {**_verdict(inspection, None, f"{ident} passes the done gate's checks of the change (no commands ran)"),
                "checks_skipped": "--no-verify" if pkg.repo_layout else "this is a planning package"}
    root = pkg.root
    prefix = gitx.out(root, "rev-parse", "--show-prefix", check=False).strip()
    scratch = Path(tempfile.mkdtemp(prefix="its-gate-"))
    tree = scratch / "head"
    if gitx.run(cl.toplevel(root), "worktree", "add", "--detach", "--quiet", str(tree), head).returncode != 0:
        shutil.rmtree(scratch, ignore_errors=True)
        raise WorkError("git_error", f"couldn't create a worktree at {head} to run the checks in")
    try:
        here = tree / prefix if prefix else tree
        policy = inspection.policy
        item: BacklogItem = inspection.planned
        plan = checks_for(pkg, item, inspection.verify_command, policy.commands, acceptance=True)
        folder = prefix + policy.acceptance
        done = heldout.done_ids(load_backlog(Package.load(here))) | {ident}  # the gated item counts as done
        try:
            held = heldout.show_for(tree, folder, done)
        except gitx.GitError as exc:
            raise WorkError("git_error", str(exc)) from exc
        if held is not None and not held["runs"]:
            plan = [c for c in plan if c.name != "acceptance"]
        checks: list[dict] = []
        if policy.commands.get("setup") and plan:  # a fresh worktree has no installed dependencies yet
            setup = run_check(Check("setup", policy.commands["setup"]), here, policy.run)
            if not setup["passed"]:
                return {**_verdict(inspection, "check_failed", f"setup ({setup['command']}) failed"),
                        "checks": [setup], "held_out": held}
        for check in plan:
            if on_check:
                on_check(ident, check)
            checks.append(run_check(check, here, policy.run))
            if not checks[-1]["passed"]:
                failed = checks[-1]
                items = heldout.failing_items(failed, folder) if check.name == "acceptance" else []
                why = f"{check.name} ({failed['command']}) failed" + (
                    f": the held-out tests of {', '.join(items)}" if items else "")
                return {**_verdict(inspection, "check_failed", why), "checks": checks, "held_out": held,
                        "held_out_failed": items or None}
        for finding in result_findings(checks, changed_tests(pkg, inspection)):
            inspection.add(finding)
        refused = [f for f in inspection.blocking() if f["kind"] in RESULT_KINDS]
        if refused:
            return {**_verdict(inspection, "test_changes", refused[0]["message"], refused), "checks": checks,
                    "held_out": held}
        red = red_on_parent(pkg, ident, inspection, inspection.verify_command)
        refused = [f for f in inspection.blocking() if f["kind"] == "tests_pass_on_parent"]
        if refused:
            return {**_verdict(inspection, "test_changes", refused[0]["message"], refused), "checks": checks,
                    "held_out": held, "red_on_parent": red}
        return {**_verdict(inspection, None, f"{ident} passes the done gate"), "checks": checks, "held_out": held,
                "red_on_parent": red}
    finally:
        gitx.run(cl.toplevel(root), "worktree", "remove", "--force", str(tree))
        shutil.rmtree(scratch, ignore_errors=True)
        gitx.run(cl.toplevel(root), "worktree", "prune")


def _verdict(inspection: gate.Inspection, reason: str | None, message: str, findings: list[dict] | None = None) -> dict:
    return {"passed": reason is None, "reason": reason, "message": message, "findings": findings or [],
            "warnings": inspection.warnings}
