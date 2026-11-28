"""`its done` and `its verify`: run the gate and an item's checks, then mark it done, or verified again.

A failure raises `WorkError` with the refusal's or the failed check's code, and carries a retry block (`retry`)
saying which attempt failed and what to try again."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date

from . import claims as cl
from . import gate, gitx, heldout, ledger
from .backlog import (
    BacklogError,
    find_item,
    load_backlog,
    ready_items,
    set_field,
    set_status,
)
from .checks import (
    RESULT_KINDS,
    Check,
    checks_for,
    result_findings,
    run_check,
)
from .contract import (
    EXIT_CHECK_FAILED,
    SCHEMA,
    WorkError,
)
from .gating import changed_tests, gate_error, inspect_item, red_on_parent
from .locking import holding_lock
from .mirror import refresh_mirror
from .package import Package
from .verified import mark_verified
from .view import base_ref, integration_branch, item_here, off_integration_branch, on_base_checkout


def done(
    pkg: Package,
    ident: str | None = None,
    *,
    verify: bool = True,
    check_only: bool = False,
    on_check: Callable[[Check], None] | None = None,
    run: dict | None = None,
) -> dict:
    """`_done`, with the attempt recorded in the run ledger. `run` is what the agent reported about its run (agent,
    cost, turns, ...: see `results.py`), recorded alongside."""
    started = time.monotonic()
    extra = dict(run or {})
    try:
        payload = _done(pkg, ident, verify=verify, check_only=check_only, on_check=on_check)
    except WorkError as err:
        target = err.details.get("id") or ident
        if target and err.code in ledger.FAILURES:
            findings = err.details.get("findings", [])
            kinds = sorted({f["kind"] for f in findings})
            head = _head_of(pkg)
            block = retry_block(pkg, target, err.details.get("checks"), findings, head=head, run=extra)
            ledger.record(pkg, target, "done", err.code, findings=kinds, check_only=check_only or None, head=head,
                          checks=[c["name"] for c in err.details.get("checks", []) if not c["passed"]] or None,
                          touches=_touch_count(pkg, target), gate_ms=int((time.monotonic() - started) * 1000),
                          attempt=None if check_only else block["attempt"],
                          **ledger.failure_fields(err.details.get("checks"), findings), **extra)
            err.details["retry"] = block
        raise
    attempt = None if check_only else len(ledger.failures(pkg, payload["id"])) + 1
    ledger.record(pkg, payload["id"], "done", "passed", check_only=check_only or None,
                  warnings=sorted({f["kind"] for f in payload.get("warnings", [])}) or None,
                  touches=_touch_count(pkg, payload["id"]), gate_ms=int((time.monotonic() - started) * 1000),
                  attempt=attempt, **extra)
    if attempt is not None:
        payload["attempt"] = attempt
    return payload


def _head_of(pkg: Package) -> str | None:
    if not cl.in_git(pkg.root):
        return None
    return gitx.out(pkg.root, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip() or None


def retry_block(pkg: Package, ident: str, checks: list[dict] | None, findings: list[dict] | None, *,
                head: str | None = None, run: dict | None = None) -> dict:
    """What a failed attempt tells whoever runs the next one: its number, how many the integration branch allows
    (`gate.max_attempts`), and the failing checks and tests. A failure of the same run or commit as an earlier one
    is the same attempt (ledger.failures)."""
    base = base_ref(pkg) if cl.in_git(pkg.root) else None
    allowed = gate.Policy.of((gate.manifest_at(pkg, base) if base else pkg.manifest) or {}, base or "").max_attempts
    key = str((run or {}).get("session_id") or head or "")
    earlier = ledger.failures(pkg, ident)
    attempt = len(earlier) + (0 if key and any(str(e.get("session_id") or e.get("head") or "") == key
                                               for e in earlier) else 1)
    fields = ledger.failure_fields(checks, findings)
    return {"attempt": attempt, "max_attempts": allowed, "retries_left": max(allowed - attempt, 0),
            "failing_checks": fields["failing_checks"] or [], "failing_tests": fields["failing_tests"] or [],
            "command": f"its brief --retry {ident}" if attempt < allowed else None}


def _touch_count(pkg: Package, ident: str) -> int | None:
    try:
        item = next((i for i in load_backlog(pkg) if i.id == ident), None)
    except BacklogError:
        return None
    return len(item.touches) if item and item.touches else None


def _done(
    pkg: Package,
    ident: str | None = None,
    *,
    verify: bool = True,
    check_only: bool = False,
    on_check: Callable[[Check], None] | None = None,
) -> dict:
    """Check the item and, if it passes, set it `done`. With no ID, uses the item of the claim branch that is
    checked out, else the only `in-progress` item.

    First the branch is compared with the integration branch (`gate.inspect`): the Verify command comes from there,
    and test tampering or changes outside Touches refuse the item unless the integration branch's copy of the item
    has a Waiver that covers them (`its waive`). Then Verify, test and lint run, only in a scaffolded repo (a
    planning package has no code to test). `check_only` does all of that but leaves the status alone, so it can run
    again on a done item (the Stop hook does). Without `verify` the item is set done unchecked, and its Verified
    field says so: `its drift` can't vouch for it, and `its land` still runs the checks.
    """
    items = load_backlog(pkg)
    if ident is None:
        ident = item_here(pkg, items)
    if ident is None:
        active = [i.id for i in items if i.status == "in-progress"]
        why = "no item is in-progress" if not active else f"more than one item is in-progress ({', '.join(active)})"
        raise WorkError("which_item", f"{why}; name the item, e.g. `its done T-1.1`",
                        details={"in_progress": active})
    item = find_item(items, ident)
    if item.status == "done" and not check_only:
        raise WorkError("already_done", f"{ident} is already done; to check a change to it again and refresh its "
                        f"Verified mark, run its verify {ident}", details={"id": ident})
    if item.status in ("proposed", "dropped") or item.placeholder:
        raise WorkError("bad_status", f"{ident} is {item.status}; it can't be done before it is scheduled",
                        details={"id": ident, "status": item.status})
    if "status" not in item.fields:
        raise WorkError("no_status", f"{ident} has no **Status:** field; add one, then run its done again",
                        details={"id": ident})
    inspection = inspect_item(pkg, ident)
    if item.status != "done" and on_base_checkout(pkg) and cl.fork_of(pkg.root, ident) is None:
        raise WorkError(
            "no_fork_point", f"{ident} is {item.status} on {integration_branch(pkg)}, but no start was recorded for "
            f"it, so the gate can't tell which commits are its work; run its start {ident} --force to record where "
            "it starts from, or work on it in a claim (its start --worktree)", details={"id": ident})
    if item.status == "done":  # re-checking finished work (its verify, the Stop hook): it needn't change anything
        inspection.findings = [f for f in inspection.findings if f["kind"] != "no_changes"]
    blocking = inspection.blocking()
    if blocking:
        raise gate_error(ident, blocking, inspection)
    verify_command = inspection.verify_command  # always the integration branch's, even when changes are waived
    results: list[dict] = []
    skipped = None
    if not verify:
        skipped = "--no-verify"
    elif not pkg.repo_layout:
        skipped = "this is a planning package, not a scaffolded repo"
    else:
        ignore = heldout.ignored_folders(pkg.root, inspection.policy.acceptance, items)
        for check in checks_for(pkg, item, verify_command, inspection.policy.commands):
            if on_check:
                on_check(check)
            results.append(run_check(check, pkg.root, inspection.policy.run, ignore=ignore))
        failed = [r["name"] for r in results if not r["passed"]]
        if failed:
            raise WorkError(
                "check_failed", f"{ident} is not done: {', '.join(failed)} failed", EXIT_CHECK_FAILED,
                {"id": ident, "checks": results},
            )
        for finding in result_findings(results, changed_tests(pkg, inspection)):
            inspection.add(finding)
        blocking = [f for f in inspection.blocking() if f["kind"] in RESULT_KINDS]
        if blocking:
            raise gate_error(ident, blocking, inspection, checks=results)
    red = None
    if not skipped and item.status != "done":
        red = red_on_parent(pkg, ident, inspection, verify_command)
        blocking = [f for f in inspection.blocking() if f["kind"] == "tests_pass_on_parent"]
        if blocking:
            raise gate_error(ident, blocking, inspection)
    manual = item.verify if item.verify and not item.verify_command else None
    common = {
        "schema": SCHEMA,
        "ok": True,
        "id": ident,
        "checks": results,
        "checks_skipped": skipped,
        "manual_verify": manual,
        "verify_source": inspection.verify_source,
        "touches_checked": inspection.touches_checked,
        "findings": inspection.findings,
        "setup_files": inspection.setup_files,
        "red_on_parent": red,
    }
    common["warnings"] = inspection.warnings  # checks in warn mode: reported, never refusing
    waived = inspection.waived  # every other enforced finding refused the item above
    if waived:
        common["override"] = {"waived": sorted({f["kind"] for f in waived}), "waiver": inspection.waiver.to_dict()}
    if check_only:
        return {**common, "checked_only": True, "status": item.status, "previous_status": item.status}
    try:
        with holding_lock(pkg):
            now = find_item(load_backlog(pkg), ident)
            if now.status != item.status:  # a person (or its block) changed it while the checks ran: theirs wins
                raise WorkError("status_changed", f"{ident} became {now.status or 'statusless'} while its checks ran "
                                f"(it was {item.status}), so it wasn't set done; run its done again if it should be",
                                details={"id": ident, "status": now.status, "was": item.status})
            previous = set_status(pkg, ident, "done")
            verified = mark_verified(pkg, ident, inspection, skipped=skipped == "--no-verify")
            if waived:
                waiver = inspection.waiver
                set_field(pkg, ident, "Override", f"{date.today().isoformat()}, waived "
                          f"{', '.join(common['override']['waived'])} (waiver of {waiver.date}: {waiver.reason})",
                          add=True)
    except BacklogError as exc:
        raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
    if cl.in_git(pkg.root):
        cl.clear_fork(pkg.root, ident)
    common["verified"] = verified
    refresh_mirror(pkg)
    items = load_backlog(pkg)
    return {
        **common,
        "previous_status": previous,
        "status": "done",
        "now_ready": [i.id for i in ready_items(items) if ident in i.depends_on],
        "merge_needed": off_integration_branch(pkg),
    }


def verify(pkg: Package, ident: str, *, on_check: Callable[[Check], None] | None = None,
           run: dict | None = None, mark: bool = True) -> dict:
    """`its verify`: re-run a done item's checks on the current code and, if they pass, refresh its Verified mark, so
    `its drift` stops reporting changes that have now been checked. `run` is a headless run's report, for the ledger."""
    items = load_backlog(pkg)
    item = find_item(items, ident)
    if item.status != "done":
        raise WorkError("bad_status", f"{ident} is {item.status}; only a done item can be re-verified (use its done)",
                        details={"id": ident, "status": item.status})
    result = done(pkg, ident, check_only=True, on_check=on_check, run=run)
    result.pop("checked_only", None)
    result["previous_verified"] = item.get("Verified") or None
    if not mark:
        return result
    try:
        with holding_lock(pkg):
            result["verified"] = mark_verified(pkg, ident, inspect_item(pkg, ident))
    except BacklogError as exc:
        raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
    refresh_mirror(pkg)
    return result


def verify_many(pkg: Package, ids: list[str], *, on_check: Callable[[Check], None] | None = None,
                on_item: Callable[[str], None] | None = None) -> tuple[list[dict], list[dict]]:
    """`its verify --drifted` or `--all`: check every item first, then refresh the marks of those that passed. Marking
    one as it passed would change the backlog, and the next item's gate would take that edit for its own change to
    another item (B7). Returns (verified, failed: the error objects)."""
    passed: list[dict] = []
    failed: list[dict] = []
    for ident in ids:
        if on_item:
            on_item(ident)
        try:
            passed.append(verify(pkg, ident, on_check=on_check, mark=False))
        except WorkError as err:
            failed.append({"id": ident, **err.to_dict()["error"]})
    try:
        with holding_lock(pkg):
            for result in passed:
                result["verified"] = mark_verified(pkg, result["id"], inspect_item(pkg, result["id"]))
    except BacklogError as exc:
        raise WorkError("bad_backlog", str(exc)) from exc
    refresh_mirror(pkg)
    return passed, failed
