"""Where the done gate compares a branch, and how it refuses: the integration branch's ref and the fork point,
the inspection, red on parent (do the new tests fail without the change?), and the refusal error."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import claims as cl
from . import gate, gitx, trust
from .checks import (
    RESULT_KINDS,
    Check,
    run_check,
)
from .contract import (
    WorkError,
)
from .package import Package
from .view import integration_branch, remote_of


def red_on_parent(pkg: Package, ident: str, inspection: gate.Inspection, verify_command: str | None) -> dict | None:
    """Run the branch's new and changed tests against the code they forked from. A test that passes there doesn't
    show that the change does anything, so that is a finding (`tests_pass_on_parent`).

    The parent is checked out in a throwaway worktree, the branch's versions of its changed test files are copied
    in, `commands.setup` runs, then the item's Verify command (or `commands.test` when Verify isn't a command). It
    passing is the finding; failing, or not running, is what a real change looks like. Returns what was run, or None
    when the check is off or there is nothing to run."""
    policy = inspection.policy
    if policy.mode("tests_pass_on_parent") == "off" or not inspection.merge_base or not cl.in_git(pkg.root):
        return None
    kind = (inspection.planned.get("Kind") or "").strip().lower() if inspection.planned else ""
    if kind.strip("`") == "test-only":
        return {"ran": False, "reason": "the item is Kind: test-only, so its tests are meant to pass on the parent",
                "files": []}
    globs = policy.test_paths or []
    tests = [c for c in inspection.changes if c.status != "D" and gate.is_test_path(pkg, c.path, globs)
             and not gate.covered(c.path, [policy.acceptance])]
    command = verify_command or policy.commands.get("test")
    if not tests or not command or command.strip() == "true":
        return {"ran": False, "reason": "no test changed" if not tests else "no Verify or test command",
                "files": [c.path for c in tests]}
    top = cl.toplevel(pkg.root)
    prefix = gate.run_git(pkg.root, "rev-parse", "--show-prefix", check=False).strip()
    scratch = Path(tempfile.mkdtemp(prefix="its-parent-"))
    tree = scratch / "parent"
    try:
        gate.run_git(top, "worktree", "add", "--detach", "--quiet", str(tree), inspection.merge_base)
        for change in tests:
            text = gate.after_content(pkg, inspection.head, change)
            if text is None:
                continue
            target = tree / change.git_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        here = tree / prefix if prefix else tree
        setup = policy.commands.get("setup")
        if setup and setup.strip() != "true":
            prepared = run_check(Check("setup", setup), here, policy.run)
            if not prepared["passed"]:
                return {"ran": False, "reason": "setup failed on the parent", "files": [c.path for c in tests],
                        "command": command}
        result = run_check(Check("verify", command), here, policy.run)
    except gate.GateError as exc:
        return {"ran": False, "reason": str(exc), "files": [c.path for c in tests]}
    finally:
        gitx.run(top, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(scratch, ignore_errors=True)
    files = [c.path for c in tests]
    if result["passed"]:
        inspection.add({
            "kind": "tests_pass_on_parent", "files": files, "command": command,
            "message": f"the branch's new or changed tests ({', '.join(files)}) also pass on the code before the "
                       f"change ({inspection.merge_base[:10]}), so they don't show {ident} works"})
    return {"ran": True, "passed_on_parent": result["passed"], "command": command, "files": files,
            "exit_code": result["exit_code"]}


def changed_tests(pkg: Package, inspection: gate.Inspection) -> set[str]:
    """The test files the branch adds or changes, package-relative, by the integration branch's test_paths."""
    globs = inspection.policy.test_paths or []
    return {c.path for c in inspection.changes if c.status != "D" and gate.is_test_path(pkg, c.path, globs)}


def gate_target(pkg: Package, ident: str) -> tuple[str | None, str | None]:
    """What the gate compares `ident`'s work with: the integration branch's ref, plus the fork point when the item
    is being worked on the integration branch itself (a plain `its start`).

    On a claim branch the integration branch is the one recorded when the claim was made, so editing
    `integration_branch` in the branch's own package.yaml can't point the gate at the branch itself."""
    if not cl.in_git(pkg.root):
        return None, None
    name = integration_branch(pkg)
    here = cl.current_branch(pkg.root)
    claim_id = cl.item_of_branch(here) or cl.claim_of_worktree(pkg.root)
    if claim_id:
        name = cl.claim_entry(pkg.root, claim_id).get("integration_branch") or name
    remote = remote_of(pkg) or "origin"
    ref = cl.resolve_base(pkg.root, name, remote)
    if ref is None:
        return None, None
    if claim_id and here and (name == here or ref in (here, f"{remote}/{here}")):
        raise WorkError("gate_error", f"{ident} can't be checked: the integration branch ({name}) is this claim's own "
                        "branch; set integration_branch in package.yaml to the branch work lands on",
                        details={"id": ident})
    fork = None
    if here is not None and here == name:
        fork = cl.fork_of(pkg.root, ident)
        if fork and not gitx.ok(pkg.root, "merge-base", "--is-ancestor", fork, "HEAD"):
            raise WorkError("gate_error", f"{ident} can't be checked: the commit it started from ({fork[:10]}) is no "
                            f"longer in {name}'s history; start it again", details={"id": ident})
    return ref, fork


def inspect_item(pkg: Package, ident: str) -> gate.Inspection:
    """Run the gate's comparison for an item, refusing (never passing) when git can't answer."""
    ref, fork = gate_target(pkg, ident)
    waiver_ref = trust.last_waiver(pkg.root, integration_branch(pkg), fork) if fork else None
    try:
        return gate.inspect(pkg, ident, ref, fork=fork, waiver_ref=waiver_ref, setup_files=cl.setup_of(pkg.root, ident))
    except gate.GateError as exc:
        raise WorkError("gate_error", f"{ident} can't be checked: {exc}", details={"id": ident}) from exc


def gate_error(ident: str, blocking: list[dict], inspection: gate.Inspection,
               checks: list[dict] | None = None) -> WorkError:
    """The refusal for findings that aren't allowed: test changes first, since they matter most. `checks` are the
    checks that ran, for a refusal that comes from what their test reports say."""
    code = gate.refusal(blocking)
    group = {"test_changes": gate.TEST_KINDS, "dependency_changes": gate.DEPENDENCY_KINDS}.get(code, ())
    worst = [f for f in blocking if f["kind"] in group] or blocking
    if worst[0]["kind"] in RESULT_KINDS:
        fix = ("Make the tests run to the end and report honestly (look for code that exits early, skips, or rewrites "
               "results)")
    elif worst[0]["kind"] == "no_changes":
        fix = "Make the change the item asks for, then run its done again"
    else:
        fix = "Undo that change"
    message = (f"{ident} is not done: {worst[0]['message']}"
               + (f" (and {len(blocking) - 1} more)" if len(blocking) > 1 else "")
               + f". {fix}; if you can't finish without it, run its block {ident} --reason '...' and "
                 "a person will decide")
    details = {"id": ident, **inspection.to_dict(), "findings": blocking,
               "warnings": [f for f in inspection.findings if f not in blocking]}
    if checks is not None:
        details["checks"] = checks
    return WorkError(code, message, details=details)
