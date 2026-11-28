"""The service layer: one function for each `its` command, between the command line and the modules that do the work.

A service function takes plain values (a folder, an ID, options), never parsed arguments, and returns an `Outcome`:
the JSON payload (spec section 16) and the exit code. A failure raises `WorkError`, with its stable code and exit.
The rules that decide what a command does and how it exits live here, so the CLI (`cli/`) only parses arguments and
renders payloads, and anything else that drives `its` gets the same behaviour from the same call.

Progress callbacks (`on_check`, `on_item`) are the caller's: the CLI passes ones that print, and passes none for
`--json`.
"""
from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import claims as cl
from . import edits, trust
from .audit import gate_audit as audit_gate
from .backlog import BacklogError, load_backlog
from .checks import failure_report  # noqa: F401 - text the CLI shares with the Stop hook
from .contract import (
    EXIT_AUDIT_MISSED,
    EXIT_CHECK_FAILED,
    EXIT_COMPLETE,
    EXIT_DRIFT,
    EXIT_ERROR,
    EXIT_NOT_LANDED,
    EXIT_NOTHING_READY,
    EXIT_OK,
    EXIT_USAGE,
    SCHEMA,
    WorkError,
)
from .dispatch import adopt as adopt_branch
from .dispatch import brief as write_brief
from .done import done as mark_done
from .done import verify as verify_item
from .done import verify_many
from .drift import drift as drift_payload
from .gate import REFUSALS  # noqa: F401 - the refusals' error codes, for the CLI's text
from .hooks import hook_agents, hook_dir, install_hooks, parse_event
from .hooks import stop_hook as decide_stop
from .ids import TASK_PATTERN  # noqa: F401 - the CLI tells an item from a folder by it
from .importer import ImportConflict
from .importer import import_export as import_files
from .landing import land as land_claims
from .ledger import stats as ledger_stats
from .linker import link_package
from .mirror import write_backlog_json
from .package import BACKLOG_STATUSES, PROFILES, Package, PackageError, new_package  # noqa: F401 - the CLI's choices
from .pr_gate import exit_code as gate_exit
from .pr_gate import gate_ref
from .renumber import renumber as renumber_items
from .review import REPLY_SCHEMA, judge
from .review import review as review_of
from .scaffold import ScaffoldError
from .scaffold import scaffold as scaffold_repo
from .sizing import sizing as sizing_of
from .start import start as start_item
from .status import status_payload
from .tips import claims_payload
from .tips import release as release_claim
from .trace import trace as trace_of
from .validate import validate as validate_package
from .view import (
    backlog_payload,
    fetch,
    integration_branch,
    next_payload,
    on_base_checkout,
    remote_freshness,
    remote_of,
    show_payload,
    shown,
)

OnCheck = Callable[[Any], None]


@dataclass
class Outcome:
    """What a command did: the JSON payload, its exit code, and facts only the text output needs (`extra`), which
    stay out of the JSON contract."""

    payload: dict
    code: int = EXIT_OK
    extra: dict = field(default_factory=dict)


def _service(fn):
    """A backlog that can't be read is the `bad_backlog` error, whichever command found it."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BacklogError as exc:
            raise WorkError("bad_backlog", str(exc)) from exc
    return wrapper


def package(where: str | Path) -> Package:
    """The package `where` is in, or the `no_package` error."""
    try:
        return Package.find(Path(where))
    except PackageError as exc:
        raise WorkError("no_package", str(exc)) from exc


def commit_planning(pkg: Package, payload: dict, message: str, commit: bool) -> dict:
    """Commit the planning folder when asked (`--commit`, or by default for promote and renumber). A commit made in
    the lead's checkout of the integration branch is recorded as trusted (`trust.py`), so land doesn't take it for
    an agent's."""
    if not commit or payload.get("dry_run") or not cl.in_git(pkg.root):
        return payload
    folder = str(pkg.planning_dir)
    cl.git(pkg.root, "add", "--all", "--", folder)
    staged = cl.git(pkg.root, "diff", "--cached", "--name-only", "--", folder, check=False)
    if staged:
        before = cl.git(pkg.root, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        cl.git(pkg.root, "commit", "--quiet", "-m", message, "--", folder)
        payload["committed"] = cl.git(pkg.root, "rev-parse", "--short", "HEAD")
        if on_base_checkout(pkg):
            trust.record(pkg.root, integration_branch(pkg), "HEAD", after=before or None, remote=remote_of(pkg))
    return payload


def _no_allow_flags(test_changes: str | None, outside_touches: str | None) -> None:
    """The error for the --allow-* flags, which phase 10 replaced with `its waive`."""
    for option, value, group in (("--allow-test-changes", test_changes, "--test-changes"),
                                 ("--allow-outside-touches", outside_touches, "--outside-touches")):
        if value is not None:
            raise WorkError("use_waive", f"{option} is gone: the lead waives on the integration branch with its waive "
                            f"<ID> {group} --reason '...', and done and land honour that waiver",
                            details={"flag": option})


def _claim_checkout(pkg: Package) -> bool:
    if not cl.in_git(pkg.root):
        return False
    return bool(cl.claim_of_worktree(pkg.root) or cl.item_of_branch(cl.current_branch(pkg.root) or ""))


# --- planning -------------------------------------------------------------------------------------------------------

@_service
def new(dest: str | Path, profile: str) -> Outcome:
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise WorkError("not_empty", f"{dest} exists and is not empty")
    new_package(dest, profile)
    return Outcome({"schema": SCHEMA, "ok": True, "path": str(dest), "profile": profile})


@_service
def import_export(export: str | Path, into: str | Path, *, seed: bool = True, overwrite: bool = False) -> Outcome:
    """Import a chat export, cross-link the result and validate it: the exit is validate's."""
    try:
        written, seeded = import_files(Path(export), Path(into), seed=seed, overwrite=overwrite)
    except (ValueError, OSError, ImportConflict) as exc:
        raise WorkError("import_failed", str(exc)) from exc
    linked, link_warning = None, None
    try:
        linked = len(link_package(Package.load(Path(into))))
    except PackageError as exc:
        link_warning = str(exc)
    payload = {"schema": SCHEMA, "ok": True, "written": written, "seeded": seeded, "linked": linked,
               "link_warning": link_warning, "validation": None}
    try:
        checked = validate(into)
    except WorkError as err:  # what was imported stays reported
        return Outcome({**payload, "ok": False, **err.to_dict()}, err.exit_code)
    return Outcome({**payload, "validation": checked.payload}, checked.code, checked.extra)


@_service
def validate(where: str | Path, *, trace: bool = False, sizing: bool = False) -> Outcome:
    pkg = package(where)
    report = validate_package(pkg)
    traced = trace_of(pkg) if trace else None
    sized = sizing_of(pkg) if sizing else None
    payload = {"schema": SCHEMA, "ok": True, "valid": report.valid, "ready": report.ready,
               "errors": report.errors, "warnings": report.warnings}
    if traced is not None:
        payload["trace"] = traced
    if sized is not None:
        payload["sizing"] = sized
    code = EXIT_OK if report.valid and not (traced and traced["uncovered"]) else EXIT_ERROR
    return Outcome(payload, code, {"repo_layout": pkg.repo_layout})


@_service
def link(where: str | Path, *, check: bool = False) -> Outcome:
    pkg = package(where)
    try:
        # in a claim's checkout the commands are the lead's: rewriting AGENTS.md there would be an agent config change
        changed = link_package(pkg, check=check, adopt=True, commands=not _claim_checkout(pkg))
    except UnicodeDecodeError as exc:
        raise WorkError("not_utf8", f"a planning file is not valid UTF-8 text ({exc.reason})") from exc
    changed = [shown(pkg, rel) for rel in changed]
    if check and changed:
        raise WorkError("link_outdated", f"{len(changed)} file(s) are out of date; run `its link` and commit the result",
                        details={"changed": changed})
    return Outcome({"schema": SCHEMA, "ok": True, "check": check, "changed": changed})


@_service
def show(where: str | Path, ident: str) -> Outcome:
    return Outcome(show_payload(package(where), ident))


@_service
def propose(where: str | Path, title: str, **fields: str | None) -> Outcome:
    return Outcome(edits.propose(package(where), title, **fields))


def _edited(pkg: Package, payload: dict, commit: bool) -> Outcome:
    what = {"todo": "Schedule", "blocked": "Block", "dropped": "Drop"}.get(payload.get("status"), "Set status of")
    return Outcome(commit_planning(pkg, payload, f"{what} {payload['id']}", commit))


@_service
def promote(where: str | Path, ident: str, *, force: bool = False, allow_incomplete: bool = False,
            commit: bool = True) -> Outcome:
    pkg = package(where)
    return _edited(pkg, edits.promote(pkg, ident, force=force, allow_incomplete=allow_incomplete), commit)


@_service
def renumber(where: str | Path, ids: list[str] | None = None, *, dry_run: bool = False, force: bool = False,
             commit: bool = True) -> Outcome:
    pkg = package(where)
    payload = renumber_items(pkg, ids or None, dry_run=dry_run, force=force)
    if payload["renumbered"]:
        commit_planning(pkg, payload, "Renumber " + ", ".join(f"{r['from']} as {r['to']}" for r in payload["renumbered"]),
                        commit)
    return Outcome(payload)


@_service
def scaffold(package_dir: str | Path, out: str | Path, *, force: bool = False, git: bool = True,
             github: str | None = None, allow_unready: bool = False, hooks: bool = False,
             confirm: Callable[[Path, int], bool] | None = None) -> Outcome:
    """Create the project repo. With `force`, a target folder that has files is replaced only if `confirm(target,
    file count)` says yes; if it says no, nothing changes and the exit is 1."""
    if not git and github is not None:
        raise WorkError("usage", "--github needs git; drop --no-git or --github", EXIT_USAGE)
    if force:
        try:
            slug = Package.load(Path(package_dir)).slug
        except PackageError as exc:
            raise WorkError("scaffold_failed", str(exc)) from exc
        target = Path(out) / slug
        if target.exists() and any(target.iterdir()):
            count = sum(1 for p in target.rglob("*") if p.is_file())
            if confirm is not None and not confirm(target, count):
                return Outcome({"schema": SCHEMA, "ok": False, "aborted": True, "path": str(target)}, EXIT_ERROR)
    try:
        result = scaffold_repo(Path(package_dir), Path(out), force=force, git=git, github=github,
                               allow_unready=allow_unready, hooks=hooks)
    except (ScaffoldError, PackageError) as exc:
        raise WorkError("scaffold_failed", str(exc)) from exc
    warnings = list(result.warnings) + (list(result.report.warnings) if result.report else [])
    return Outcome({"schema": SCHEMA, "ok": True, "aborted": False, "path": str(result.path), "files": len(result.files),
                    "warnings": warnings, "committed": bool(result.committed), "remote": result.remote})


# --- work -----------------------------------------------------------------------------------------------------------

@_service
def backlog(where: str | Path, *, status: str | None = None, ready: bool = False, write: bool = False) -> Outcome:
    pkg = package(where)
    written = write_backlog_json(pkg).relative_to(pkg.root).as_posix() if write else None
    payload = backlog_payload(pkg)
    if status:
        payload["items"] = [i for i in payload["items"] if i["status"] == status]
    if ready:
        payload["items"] = [i for i in payload["items"] if i["ready"]]
    return Outcome(payload, extra={"written": written})


@_service
def next_item(where: str | Path, *, base: str | None = None, parallel: int | None = None, fetch_first: bool = False,
              every: bool = False) -> Outcome:
    """The next ready item (or, with `parallel`, up to that many that can run side by side). Exit 3 if nothing is
    ready, 9 if every item is done."""
    pkg = package(where)
    if fetch_first:
        fetch(pkg, base)
    payload = next_payload(pkg, base=base, parallel=parallel)
    payload["stale_remote"] = None if fetch_first else remote_freshness(pkg)
    if not every:
        payload.pop("ready")
    found = payload["parallel"] if parallel is not None else payload["item"]
    nothing = EXIT_COMPLETE if payload["reason"] == "complete" else EXIT_NOTHING_READY
    return Outcome(payload, EXIT_OK if found else nothing)


@_service
def start(where: str | Path, ident: str | None, *, pick_next: bool = False, fetch_first: bool = False,
          base: str | None = None, **options: Any) -> Outcome:
    if not pick_next and not ident:
        raise WorkError("usage", "name the item to start, or pass --next", EXIT_USAGE)
    pkg = package(where)
    if fetch_first:
        fetch(pkg, base)
    return Outcome(start_item(pkg, None if pick_next else ident, base=base, **options))


@_service
def done(where: str | Path, ident: str | None, *, result: str | None = None, verify: bool = True,
         check_only: bool = False, push: bool | None = None, on_check: OnCheck | None = None,
         allow_test_changes: str | None = None, allow_outside_touches: str | None = None) -> Outcome:
    """Run the item's checks and mark it done; with `result`, act on a headless run's result file instead, which
    may block the item rather than finish it."""
    pkg = package(where)
    _no_allow_flags(allow_test_changes, allow_outside_touches)
    options = {"verify": verify, "check_only": check_only, "on_check": on_check}
    if result:
        return Outcome(edits.apply_result(pkg, ident, result, push=push, **options))
    return Outcome(mark_done(pkg, ident, **options))


@_service
def block(where: str | Path, ident: str, reason: str, *, commit: bool = False) -> Outcome:
    pkg = package(where)
    return _edited(pkg, edits.block(pkg, ident, reason), commit)


@_service
def unblock(where: str | Path, ident: str, *, commit: bool = False) -> Outcome:
    pkg = package(where)
    return _edited(pkg, edits.unblock(pkg, ident), commit)


@_service
def drop(where: str | Path, ident: str, reason: str | None = None, *, commit: bool = False) -> Outcome:
    pkg = package(where)
    return _edited(pkg, edits.drop(pkg, ident, reason), commit)


@_service
def set_status(where: str | Path, ident: str, status: str, *, reason: str | None = None,
               commit: bool = False) -> Outcome:
    pkg = package(where)
    return _edited(pkg, edits.set_item_status(pkg, ident, status, reason=reason), commit)


# --- lead -----------------------------------------------------------------------------------------------------------

@_service
def status(where: str | Path, *, base: str | None = None, fetch_first: bool = False) -> Outcome:
    pkg = package(where)
    if fetch_first:
        fetch(pkg, base)
    payload = status_payload(pkg, base=base)
    payload["stale_remote"] = None if fetch_first else remote_freshness(pkg)
    return Outcome(payload)


@_service
def claims(where: str | Path, *, base: str | None = None, fetch_first: bool = False) -> Outcome:
    pkg = package(where)
    if fetch_first:
        fetch(pkg, base)
    return Outcome(claims_payload(pkg, base=base))


@_service
def release(where: str | Path, ident: str, *, force: bool = False, remote: bool = False) -> Outcome:
    return Outcome(release_claim(package(where), ident, force=force, remote=remote))


@_service
def adopt(where: str | Path, ref: str, ident: str, *, push: bool | None = None, fetch_first: bool = False,
          agent: str | None = None) -> Outcome:
    return Outcome(adopt_branch(package(where), ref, ident, push=push, fetch=fetch_first, agent=agent))


@_service
def brief(where: str | Path, ident: str, *, retry: bool = False, github: bool = False,
          base: str | None = None) -> Outcome:
    return Outcome(write_brief(package(where), ident, retry=retry, github=github, base=base))


@_service
def gate(where: str | Path, ref: str | None = None, *, base: str | None = None, ident: str | None = None,
         require_item: bool = False, verify: bool = True, on_check: Callable[[str, Any], None] | None = None) -> Outcome:
    """The done gate on a branch, for CI. Exit 0 if it passes, 5 if a check fails, 12 if the gate refuses it."""
    payload = gate_ref(package(where), ref or "HEAD", base=base, ident=ident, require_item=require_item, verify=verify,
                       on_check=on_check)
    return Outcome(payload, gate_exit(payload))


@_service
def land(where: str | Path, ids: list[str] | None = None, *, allow_test_changes: str | None = None,
         allow_outside_touches: str | None = None, **options: Any) -> Outcome:
    """Merge finished claims one at a time. Exit 3 if nothing was ready to land, 7 if something ready didn't land."""
    pkg = package(where)
    _no_allow_flags(allow_test_changes, allow_outside_touches)
    payload = land_claims(pkg, ids or None, **options)
    stuck = [s for s in payload["skipped"] if s["reason"] not in ("waiting", "dry_run")]
    if not payload["landed"] and not stuck and not any(s["reason"] == "dry_run" for s in payload["skipped"]):
        code = EXIT_NOTHING_READY  # nothing was ready, or only items still waiting on dependencies
    elif stuck:
        code = EXIT_NOT_LANDED
    else:
        code = EXIT_OK
    return Outcome(payload, code)


@_service
def waive(where: str | Path, ident: str, groups: list[str], reason: str) -> Outcome:
    return Outcome(edits.waive(package(where), ident, groups, reason))


def review_schema() -> Outcome:
    """The JSON schema a reviewer's reply follows."""
    return Outcome(REPLY_SCHEMA)


@_service
def review_prompt(where: str | Path, ident: str | None, *, base: str | None = None,
                  branch: str | None = None) -> Outcome:
    return Outcome(review_of(package(where), ident, base=base, branch=branch))


@_service
def review_reply(where: str | Path, ident: str | None, reply: str, *, read_stdin: Callable[[], str] | None = None,
                 min_severity: str = "major", base: str | None = None, branch: str | None = None,
                 evidence: bool = True) -> Outcome:
    """Judge a reviewer's reply (the file `reply`, or stdin when it is `-`) and record the verdict. Exit 5 if the
    review doesn't pass."""
    pkg = package(where)
    try:
        text = read_stdin() if reply == "-" and read_stdin else Path(reply).read_text(encoding="utf-8")
        payload = judge(pkg, ident, text, min_severity=min_severity, base=base, branch=branch, run_evidence=evidence)
    except OSError as exc:
        raise WorkError("not_found", f"can't read {reply}: {exc}") from exc
    return Outcome(payload, EXIT_OK if payload["passed"] else EXIT_CHECK_FAILED)


# --- maintain -------------------------------------------------------------------------------------------------------

@_service
def drift(where: str | Path, *, check: bool = False) -> Outcome:
    """Done items whose files changed after they were verified. With `check`, exit 8 if any did."""
    payload = drift_payload(package(where))
    return Outcome(payload, EXIT_DRIFT if check and payload["drifted"] else EXIT_OK)


@_service
def verify(where: str | Path, ident: str | None = None, *, drifted: bool = False, every: bool = False,
           commit: bool = False, on_check: OnCheck | None = None,
           on_item: Callable[[str], None] | None = None) -> Outcome:
    """Re-run a done item's checks and refresh its Verified mark; with `drifted` or `every`, each such item in turn,
    carrying on past failures (exit 5 if any failed)."""
    pkg = package(where)
    if drifted or every:
        if drifted:
            ids = [d["id"] for d in drift_payload(pkg)["drifted"]]
        else:
            ids = [i.id for i in load_backlog(pkg) if i.status == "done" and i.touches]
        verified, failed = verify_many(pkg, ids, on_check=on_check, on_item=on_item)
        payload = {"schema": SCHEMA, "ok": not failed, "verified": verified, "failed": failed}
        if failed:  # an ok: false object always has an error (section 16)
            payload["error"] = WorkError("not_verified", f"{len(failed)} item(s) failed their checks again: "
                                         + ", ".join(f["id"] for f in failed), EXIT_CHECK_FAILED,
                                         {"failed": [f["id"] for f in failed]}).to_dict()["error"]
        if verified:
            commit_planning(pkg, payload, "Re-verify " + ", ".join(v["id"] for v in verified), commit)
        return Outcome(payload, EXIT_CHECK_FAILED if failed else EXIT_OK, {"ids": ids})
    if ident is None:
        raise WorkError("usage", "name the item to verify, or pass --drifted or --all", EXIT_USAGE)
    payload = verify_item(pkg, ident, on_check=on_check)
    return Outcome(commit_planning(pkg, payload, f"Re-verify {payload['id']}", commit))


@_service
def stats(where: str | Path) -> Outcome:
    return Outcome({"schema": SCHEMA, "ok": True, **ledger_stats(package(where))})


@_service
def gate_audit(where: str | Path, ident: str | None = None, *, run: bool = True, allow_warn: bool = False) -> Outcome:
    """Feed the gate known cheats. Exit 10 unless it refuses every one (with `allow_warn`: at least warns)."""
    payload = audit_gate(package(where), ident, run=run)
    ok = payload["ok"] or (allow_warn and not payload["missed"])
    payload["ok"] = ok
    if not ok:  # an ok: false object always has an error (section 16)
        missed = payload["missed"] or payload["warned"]
        payload["error"] = WorkError("audit_missed", f"the gate {'missed' if payload['missed'] else 'only warned about'} "
                                     f"{len(missed)} seeded cheat(s): {', '.join(missed)}", EXIT_AUDIT_MISSED,
                                     {"missed": payload["missed"], "warned": payload["warned"]}).to_dict()["error"]
    return Outcome(payload, EXIT_OK if ok else EXIT_AUDIT_MISSED)


@_service
def hooks(where: str | Path, agent: str | None = None) -> Outcome:
    pkg = package(where)
    return Outcome(install_hooks(pkg.root, [agent] if agent else hook_agents(pkg)))


def stop_hook(read_event: Callable[[], str], where: str | Path, *, headless: bool = False) -> Outcome:
    """`its hook stop`: the Stop hook itself. The payload is the hook's decision ({} lets the agent stop). It never
    fails the agent's tooling: any problem, even reading the event, lets the agent stop."""
    try:
        event = read_event()
        decision = decide_stop(Package.find(hook_dir(parse_event(event), where)), event, headless=headless)
    except Exception:  # noqa: BLE001 - a broken hook must not trap the agent
        return Outcome({})
    return Outcome(decision or {})
