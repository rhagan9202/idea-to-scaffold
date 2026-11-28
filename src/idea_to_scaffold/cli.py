"""Command line interface for `its`: planning (new, import, validate, link), work (next, start, done, land, review)
and the backlog commands."""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import sys
from pathlib import Path

from . import __version__, gate, trust
from . import claims as cl
from .audit import gate_audit
from .backlog import BacklogError
from .contract import (
    EXIT_AUDIT_MISSED,
    EXIT_CHECK_FAILED,
    EXIT_COMPLETE,
    EXIT_DRIFT,
    EXIT_ERROR,
    EXIT_NOT_LANDED,
    EXIT_NOTHING_READY,
    EXIT_USAGE,
    SCHEMA,
    WorkError,
)
from .dispatch import adopt, brief
from .drift import drift
from .edits import apply_result, block, drop, promote, propose, set_item_status, unblock, waive
from .hooks import hook_agents, hook_dir, install_hooks, parse_event, stop_hook
from .ids import TASK_PATTERN
from .importer import ImportConflict, import_export
from .landing import land
from .ledger import stats
from .linker import link_package
from .package import BACKLOG_STATUSES, PROFILES, Package, PackageError, new_package
from .pr_gate import gate_ref
from .review import REPLY_SCHEMA, judge, review
from .scaffold import ScaffoldError, scaffold
from .sizing import sizing
from .status import STATE_LABELS, render, status_payload
from .trace import trace
from .validate import validate
from .work import (
    _on_base_checkout,
    backlog_payload,
    claims_payload,
    done,
    failure_report,
    fetch,
    integration_branch,
    is_terminal,
    next_payload,
    release,
    remote_freshness,
    remote_of,
    renumber,
    show_payload,
    shown,
    stale_note,
    start,
    verify,
    verify_many,
    write_backlog_json,
)

TASK_RE = re.compile(TASK_PATTERN)


def cmd_new(args: argparse.Namespace) -> int:
    dest = Path(args.dir)
    if dest.exists() and any(dest.iterdir()):
        print(f"error: {dest} exists and is not empty", file=sys.stderr)
        return 1
    new_package(dest, args.profile)
    print(f"created blank {args.profile} planning package in {dest}")
    print("next: fill it in with the prompts in prompts/stages/, or run the plan-project skill")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        written, seeded = import_export(
            Path(args.export), Path(args.into), seed=not args.no_defaults, overwrite=args.overwrite
        )
    except (ValueError, OSError, ImportConflict) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"imported {len(written)} new or changed file(s) into {args.into}" + (":" if written else ""))
    for rel in written:
        print(f"  {rel}")
    if seeded:
        print(f"added {len(seeded)} default file(s): {', '.join(seeded)}")
    try:
        changed = link_package(Package.load(Path(args.into)))
        print(f"cross-linked {len(changed)} file(s) and wrote planning/README.md")
    except PackageError as exc:
        print(f"warning: could not cross-link: {exc}")
    return cmd_validate(argparse.Namespace(dir=args.into, trace=False, sizing=False, json=False))


def cmd_validate(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    report = validate(pkg)
    traced = trace(pkg) if args.trace else None
    sized = sizing(pkg) if getattr(args, "sizing", False) else None
    code = 0 if report.valid and not (traced and traced["uncovered"]) else 1
    if args.json:
        payload = {"schema": SCHEMA, "ok": True, "valid": report.valid, "ready": report.ready,
                   "errors": report.errors, "warnings": report.warnings}
        if traced is not None:
            payload["trace"] = traced
        if sized is not None:
            payload["sizing"] = sized
        _emit(payload)
        return code
    for e in report.errors:
        print(f"ERROR   {e}")
    for w in report.warnings:
        print(f"warning {w}")
    if traced is not None:
        _print_trace(traced)
    if sized is not None:
        for w in sized["warnings"]:
            print(f"sizing  {w['kind']}: {w['message']}")
        print(f"sizing: {len(sized['flagged'])} of {sized['items']} todo or proposed item(s) may be too big or vague"
              if sized["checked"] else f"sizing: skipped ({sized['reason']})")
    if report.valid and pkg.repo_layout:
        print("planning docs are valid")
    elif report.ready:
        print("package is valid and ready to scaffold")
    elif report.valid:
        print("package is valid, but not ready to scaffold yet")
    else:
        print(f"package has {len(report.errors)} error(s)")
    return code


def _print_trace(traced: dict) -> None:
    if not traced["checked"]:
        print(f"trace: skipped ({traced['reason']})")
        return
    for c in traced["uncovered"]:
        print(f"UNTESTED {c['id']} AC{c['ac']}: {c['text']}")
    for c in traced["by_requirement"]:
        print(f"coarse   {c['id']} AC{c['ac']}: only tests naming {c['id']} as a whole")
    covered = traced["criteria"] - len(traced["uncovered"])
    print(f"trace: {covered} of {traced['criteria']} Must acceptance criteria are named by a test")


def cmd_drift(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = drift(pkg)
    except WorkError as err:
        return _fail(args, err)
    code = EXIT_DRIFT if args.check and payload["drifted"] else 0
    if args.json:
        _emit(payload)
        return code
    for entry in payload["drifted"]:
        print(f"{entry['id']} {entry['title']}: changed since it was verified ({entry['verified'] or 'when done'})")
        for f in entry["files"]:
            print(f"  {f}")
        for c in entry["commits"]:
            print(f"    {c}")
    for req in payload["requirements"]:
        print(f"{req['id']} may no longer hold: re-verify {', '.join(req['items'])} (its verify <ID>)")
    for entry in payload["unknown"]:
        print(f"{entry['id']}: can't tell ({entry['message']})")
    if payload["no_touches"]:
        print(f"not tracked (no Touches): {', '.join(payload['no_touches'])}")
    if not payload["drifted"]:
        print(f"no drift: {len(payload['clean'])} done item(s) unchanged since they were verified")
    return code


def cmd_gate_audit(args: argparse.Namespace) -> int:
    if args.id and args.dir == "." and not TASK_RE.fullmatch(args.id):  # `its gate-audit <dir>`
        args.dir, args.id = args.id, None
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = gate_audit(pkg, args.id, run=not args.no_run)
    except WorkError as err:
        return _fail(args, err)
    ok = payload["ok"] or (args.allow_warn and not payload["missed"])
    code = 0 if ok else EXIT_AUDIT_MISSED
    payload["ok"] = ok
    if not ok:  # an ok: false object always has an error (section 16)
        missed = payload["missed"] or payload["warned"]
        payload["error"] = WorkError("audit_missed", f"the gate {'missed' if payload['missed'] else 'only warned about'} "
                                     f"{len(missed)} seeded cheat(s): {', '.join(missed)}", EXIT_AUDIT_MISSED,
                                     {"missed": payload["missed"], "warned": payload["warned"]}).to_dict()["error"]
    if args.json:
        _emit(payload)
        return code
    print(f"gate-audit for {payload['id']} against {payload['base']}"
          + (f", using {payload['test_file']}" if payload["test_file"] else ""))
    for v in payload["variants"]:
        if v["caught"] is None:
            mark, note = "skip   ", v["skipped"]
        elif v["caught"]:
            mark, note = ("refused" if v["mode"] == "enforce" else "warns  "), ", ".join(v["found"])
        else:
            mark, note = "MISSED ", "found " + (", ".join(v["found"]) or "nothing")
        print(f"  {mark}  {v['name']:<24} {v['description']}  ({note})")
    print(f"refused {payload['refused']} of {payload['total']}"
          + (f"; only warned: {', '.join(payload['warned'])}" if payload["warned"] else "")
          + (f"; missed: {', '.join(payload['missed'])}" if payload["missed"] else ""))
    if payload["warned_kinds"]:
        checks = "".join(f"\n    {kind}: enforce" for kind in payload["warned_kinds"])
        print(f"to refuse the warned ones, set this in package.yaml on the integration branch:\n  gate:\n  "
              f"  checks:{checks.replace(chr(10), chr(10) + '  ')}")
    return code


def cmd_verify(args: argparse.Namespace) -> int:
    if args.id and args.dir == "." and not TASK_RE.fullmatch(args.id):  # `its verify --all <dir>`
        args.dir, args.id = args.id, None
    pkg = _load(args)
    if pkg is None:
        return 1

    def announce(check) -> None:
        if not args.json:
            print(f"running {check.name}: {check.command}", flush=True)

    if args.drifted or args.all:
        return _verify_many(args, pkg, announce)
    if args.id is None:
        return _fail(args, WorkError("usage", "name the item to verify, or pass --drifted or --all", EXIT_USAGE))
    try:
        payload = verify(pkg, args.id, on_check=announce)
    except WorkError as err:
        if err.code == "check_failed" and not args.json:
            print("\n" + failure_report(err.details["checks"]))
        return _fail(args, err)
    _commit(args, pkg, payload, f"Re-verify {payload['id']}")
    if args.json:
        _emit(payload)
        return 0
    print(f"{payload['id']} verified again: {payload['verified']}"
          + (f" (committed {payload['committed']})" if payload.get("committed") else " (commit the backlog to record it)"))
    return 0


def _verify_many(args: argparse.Namespace, pkg: Package, announce) -> int:
    """`its verify --drifted` / `--all`: re-verify each item, carrying on past failures."""
    if args.drifted:
        ids = [d["id"] for d in drift(pkg)["drifted"]]
    else:
        from .backlog import load_backlog
        ids = [i.id for i in load_backlog(pkg) if i.status == "done" and i.touches]
    verified, failed = verify_many(pkg, ids, on_check=announce,
                                   on_item=None if args.json else (lambda ident: print(f"{ident}:")))
    result = {"schema": SCHEMA, "ok": not failed, "verified": verified, "failed": failed}
    if failed:  # an ok: false object always has an error (section 16)
        result["error"] = WorkError("not_verified", f"{len(failed)} item(s) failed their checks again: "
                                    + ", ".join(f["id"] for f in failed), EXIT_CHECK_FAILED,
                                    {"failed": [f["id"] for f in failed]}).to_dict()["error"]
    if verified:
        _commit(args, pkg, result, "Re-verify " + ", ".join(v["id"] for v in verified))
    if args.json:
        _emit(result)
    elif not ids:
        print("nothing to verify" + (": no item has drifted" if args.drifted else ""))
    else:
        for f in failed:
            print(f"  not verified: {f['id']}: {f['message']}")
        print(f"verified again: {', '.join(v['id'] for v in verified) or 'none'}"
              + (f"; failed: {', '.join(f['id'] for f in failed)}" if failed else "")
              + ((f" (committed {result['committed']})" if result.get("committed") else
                  " (commit the backlog to record it)") if verified else ""))
    return EXIT_CHECK_FAILED if failed else 0


def cmd_link(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        # in a claim's checkout the commands are the lead's: rewriting AGENTS.md there would be an agent config change
        changed = link_package(pkg, check=args.check, adopt=True, commands=not _claim_checkout(pkg))
    except UnicodeDecodeError as exc:
        return _fail(args, WorkError("not_utf8", f"a planning file is not valid UTF-8 text ({exc.reason})"))
    changed = [shown(pkg, rel) for rel in changed]
    if args.json:
        if args.check and changed:
            return _fail(args, WorkError("link_outdated", f"{len(changed)} file(s) are out of date; run `its link` "
                                         "and commit the result", details={"changed": changed}))
        _emit({"schema": SCHEMA, "ok": True, "check": args.check, "changed": changed})
        return 0
    if args.check:
        if changed:
            print("generated links or commands are out of date in:")
            for rel in changed:
                print(f"  {rel}")
            print("run `its link` and commit the result")
            return 1
        print("cross-links and commands are up to date")
        return 0
    print(f"updated {len(changed)} file(s)" if changed else "already up to date")
    for rel in changed:
        print(f"  {rel}")
    return 0


def _claim_checkout(pkg: Package) -> bool:
    if not cl.in_git(pkg.root):
        return False
    return bool(cl.claim_of_worktree(pkg.root) or cl.item_of_branch(cl.current_branch(pkg.root) or ""))


def _load(args: argparse.Namespace) -> Package | None:
    try:
        return Package.find(Path(args.dir))
    except PackageError as exc:
        _fail(args, WorkError("no_package", str(exc)))
        return None


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _fail(args: argparse.Namespace, err: WorkError) -> int:
    if getattr(args, "json", False):
        _emit(err.to_dict())
    else:
        print(f"error: {err.message}", file=sys.stderr)
    return err.exit_code


def _item_line(item: dict) -> str:
    milestone = f" ({item['milestone']})" if item.get("milestone") and item["milestone"] not in ("—", "-") else ""
    return f"{item['id']} — {item['title']}{milestone}"


def cmd_backlog(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    if args.write:
        path = write_backlog_json(pkg)
        if not args.json:
            print(f"wrote {path.relative_to(pkg.root).as_posix()} (generated and gitignored; don't edit it)")
            return 0
    payload = backlog_payload(pkg)
    if args.status:
        payload["items"] = [i for i in payload["items"] if i["status"] == args.status]
    if args.ready:
        payload["items"] = [i for i in payload["items"] if i["ready"]]
    if args.json:
        _emit(payload)
        return 0
    items = payload["items"]
    proposals = [] if args.ready or args.status not in (None, "proposed") else payload["proposals"]
    if not items and not proposals:
        what = " ready" if args.ready else (f" with status {args.status}" if args.status else "")
        print(f"no backlog items{what}")
        return 0
    rows = items + proposals
    id_width = max(8, *(len(i["id"]) + 1 for i in rows))
    print(f"{'ID':<{id_width}} {'STATUS':<12} {'MILESTONE':<10} TITLE")
    for i in rows:
        print(f"{i['id']:<{id_width}} {i['status']:<12} {i['milestone'] or '—':<10} {i['title']}")
    if proposals:
        print(f"{len(proposals)} proposal(s) in {payload['proposals'][0]['file'].rsplit('/', 1)[0]}/ wait for "
              "its promote or its renumber")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    if args.fetch:
        fetch(pkg, args.base)
    payload = next_payload(pkg, base=args.base, parallel=args.parallel)
    payload["stale_remote"] = None if args.fetch else remote_freshness(pkg)
    if not args.all:
        payload.pop("ready")
    note = stale_note(payload["stale_remote"])
    if note and not args.json:
        print(note, file=sys.stderr)
    if payload["behind"] and not args.json:
        print(f"note: {payload['source']} is ahead of this checkout by {payload['behind']} commit(s); the backlog is "
              f"read from there. Pull before starting work here: git pull --ff-only", file=sys.stderr)
    nothing = EXIT_COMPLETE if payload["reason"] == "complete" else EXIT_NOTHING_READY
    if args.parallel is not None:
        code = 0 if payload["parallel"] else nothing
    else:
        code = 0 if payload["item"] else nothing
    if args.json:
        _emit(payload)
        return code
    for w in payload.get("warnings", []):
        print(f"warning: {w['message']}", file=sys.stderr)
    if args.parallel is not None:
        for item in payload["parallel"]:
            print(_item_line(item))
            print(f"  touches: {', '.join(item['touches']) or '(not listed)'}")
            print(f"  start:   its start {item['id']} --worktree")
        for held in payload["held_back"]:
            why = "" if held["touches_listed"] else " (it lists no Touches)"
            print(f"held back: {held['id']}{why} may touch the same files as {', '.join(held['overlaps'])}")
        if not payload["parallel"]:
            print("nothing can start in parallel right now")
        return code
    for claim in payload["claimed"]:
        print(f"claimed: {claim['id']} on {claim['branch']}" + (f" ({claim['worktree']})" if claim["worktree"] else ""))
    if payload["item"] is None:
        print({
            "overlap": "nothing can start now: every ready item may touch the same files as running work "
                       "(its next --parallel shows which)",
            "waiting": "nothing is ready",
            "in_progress": "nothing is ready: everything left is in progress, claimed or blocked",
            "complete": "nothing is left: every item is done",
        }.get(payload["reason"], "nothing is ready"))
        if payload["in_progress"]:
            print(f"  in progress: {', '.join(payload['in_progress'])}")
        for w in payload["waiting"]:
            print(f"  {w['id']} waits on {', '.join(w['blocked_by'])}")
        for cycle in payload["cycles"]:
            print(f"  dependency cycle: {' -> '.join(cycle)}")
        return code
    shown_items = payload["ready"] if args.all else [payload["item"]]
    for item in shown_items:
        print(_item_line(item))
        if item["satisfies"]:
            print(f"  satisfies: {', '.join(item['satisfies'])}")
        if item["verify"]:
            print(f"  verify:    {item['verify']}")
    if not args.all:
        print(f"next: its start {payload['item']['id']}")
    return code


def cmd_show(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = show_payload(pkg, args.id)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    where = f"{payload['file']}#{payload['anchor']}" if payload["anchor"] else payload["file"]
    print(f"{payload['id']} ({payload['kind']}), defined in {where}, line {payload['line']}")
    print()
    print(payload["text"])
    print()
    refs = payload["referenced_by"]
    print("mentioned in: " + (", ".join(refs) if refs else "no other doc"))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    if args.next and args.id and args.dir == ".":  # `its start --next <dir>`
        args.dir, args.id = args.id, None
    if not args.next and not args.id:
        return _fail(args, WorkError("usage", "name the item to start, or pass --next", EXIT_USAGE))
    pkg = _load(args)
    if pkg is None:
        return 1
    if args.fetch:
        fetch(pkg, args.base)
    try:
        payload = start(pkg, None if args.next else args.id, force=args.force, branch=args.branch,
                        worktree=args.worktree, agent=args.agent, base=args.base, allow_overlap=args.allow_overlap,
                        push=False if args.no_push else None, setup=not args.no_setup)
    except WorkError as err:
        if err.code == "blocked" and not args.json:
            print(f"error: {err.message} (use --force to start it anyway)", file=sys.stderr)
            return err.exit_code
        if err.code == "overlap" and not args.json:
            print(f"error: {err.message}", file=sys.stderr)
            print("see what can run now: its next --parallel", file=sys.stderr)
            return err.exit_code
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    item = payload["item"]
    claim = payload["claim"]
    if claim:
        where = f" in the new worktree {claim['worktree']}" if claim["worktree"] else ", checked out here"
        print(f"{_item_line(item)} is claimed by branch {claim['branch']}{where}")
        if payload.get("pushed"):
            print(f"  pushed to {payload['remote']}, so other machines see the claim")
        elif payload.get("push_error"):
            print(f"  warning: the claim is only local; pushing it failed: {payload['push_error']}")
        setup = payload.get("setup")
        if setup and setup["passed"]:
            wrote = f"; it wrote {', '.join(setup['lockfiles'])}, which done lets through" if setup["lockfiles"] else ""
            print(f"  ran setup in the worktree: {setup['command']}{wrote}")
        elif setup:
            print(f"  warning: setup failed in the worktree (exit {setup['exit_code']}): {setup['command']}")
            for line in (setup.get("output") or "").splitlines()[-5:]:
                print(f"    {line}")
        for w in payload.get("warnings", []):
            print(f"  warning: {w['message']}")
        print("start an agent on it:")
        print(f"  {payload['commands']['claude']}")
        print(f"  {payload['commands']['codex']}")
        headless = payload["commands"].get("headless")
        if headless:
            print(f"or unattended (sandboxed, budget ${headless['budget_usd']:g}, push tokens unset; the script ends "
                  "with `its done --result`, which commits the work and pushes the branch):")
            print(f"  {headless['claude']}")
            print(f"  {headless['codex']}")
            if os.name == "nt":
                print(f"  (from cmd you can also run {headless['scripts']['claude']['cmd']})")
    else:
        print(f"{_item_line(item)} is now in-progress (was {payload['previous_status']})")
    if item["verify"]:
        print(f"  verify: {item['verify']}")
    print(f"when it's finished: its done {item['id']}")
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    if args.fetch:
        fetch(pkg, args.base)
    try:
        payload = claims_payload(pkg, base=args.base)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    if not payload["claims"]:
        print("no claims")
    for c in payload["claims"]:
        state = STATE_LABELS.get(c.get("state", ""), "active")
        if c.get("state") == "blocked" and c.get("blocked_reason"):
            state += f": {c['blocked_reason']}"
        extra = f"  worktree {c['worktree']}" if c["worktree"] else ""
        print(f"{c['id']:<10} {c['branch']}  [{state}]{extra}")
        if c.get("conflicts_with_base"):
            print(f"           would conflict with {payload['base']} in {', '.join(c['conflicts_with_base'])}")
        for other in c.get("conflicts_with", []):
            print(f"           would conflict with {other['id']} in {', '.join(other['files'])}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        if args.fetch:
            fetch(pkg, args.base)
        payload = status_payload(pkg, base=args.base)
    except WorkError as err:
        return _fail(args, err)
    payload["stale_remote"] = None if args.fetch else remote_freshness(pkg)
    if args.json:
        _emit(payload)
        return 0
    print("\n".join(render(payload)))
    note = stale_note(payload["stale_remote"])
    if note:
        print(note)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = release(pkg, args.id, force=args.force, remote=args.remote)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    if not payload["released"]:
        print(f"{payload['id']} has no claim to release")
    if payload["removed_worktree"]:
        print(f"removed worktree {payload['removed_worktree']}")
    for b in payload["deleted_branches"]:
        print(f"deleted branch {b}")
    for b in payload["deleted_remote_branches"]:
        print(f"deleted pushed branch {b}")
    for b in payload["remote_branches"]:
        print(f"still on the remote: {b} (add --remote to delete it)")
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = adopt(pkg, args.ref, args.id, push=False if args.no_push else None, fetch=args.fetch,
                        agent=args.agent)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    cand = payload["candidate"]
    if payload["created"]:
        where = f", pushed to {payload['remote']}" if payload["pushed"] else ""
        print(f"claimed {payload['id']} with {payload['claim']['branch']}{where}")
    if payload["push_error"]:
        print(f"warning: the claim wasn't pushed: {payload['push_error']}", file=sys.stderr)
    verb = "adopted" if payload["added"] else "already adopted"
    print(f"{verb} {cand['name']} ({cand['tip'][:10]}) for {payload['id']}: {payload['title']}")
    if len(payload["candidates"]) > 1:
        print(f"candidates, tried in this order: {', '.join(c['name'] for c in payload['candidates'])}")
    for other in payload["overlaps"]:
        print(f"note: {payload['id']} may change the same files as {other}, which is running")
    print(f"next: {payload['commands']['land']}  (runs the done gate and the held-out tests on each candidate)")
    return 0


def cmd_renumber(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = renumber(pkg, args.ids or None, dry_run=args.dry_run, force=args.force)
    except WorkError as err:
        return _fail(args, err)
    if payload["renumbered"]:
        _commit(args, pkg, payload, "Renumber " + ", ".join(f"{r['from']} as {r['to']}" for r in payload["renumbered"]))
    if args.json:
        _emit(payload)
        return 0
    if not payload["renumbered"]:
        print("no placeholder items to renumber")
    for r in payload["renumbered"]:
        print(f"{r['from']} -> {r['to']}" + (" (dry run)" if args.dry_run else ""))
    for f in payload["files"]:
        print(f"  updated {f}")
    if payload.get("committed"):
        print(f"  committed {payload['committed']}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    if args.id and args.dir == "." and not TASK_RE.fullmatch(args.id):  # `its done <dir>`: no ID, just a folder
        args.dir, args.id = args.id, None
    pkg = _load(args)
    if pkg is None:
        return 1

    def announce(check) -> None:
        if not args.json:
            print(f"running {check.name}: {check.command}", flush=True)

    gone = _waiver_flags(args)
    if gone:
        return _fail(args, gone)
    options = {"verify": not args.no_verify, "check_only": args.check, "on_check": announce}
    try:
        if args.result:
            payload = apply_result(pkg, args.id, args.result, push=False if args.no_push else None, **options)
            if payload["status"] == "blocked":
                if not args.json:
                    print(f"{payload['id']} is now blocked (was {payload['previous_status']})")
                    print(f"  reason: {payload['reason']}")
                    _print_run_commit(payload)
                    return 0
                _emit(payload)
                return 0
        else:
            payload = done(pkg, args.id, **options)
    except WorkError as err:
        if err.code in gate.REFUSALS and not args.json \
                and len(err.details["findings"]) > 1:
            for finding in err.details["findings"]:
                print(f"  {finding['kind']}: {finding['message']}")
        if err.code == "check_failed" and not args.json:
            print("\n" + failure_report(err.details["checks"]))
        again = err.details.get("retry")
        if again and getattr(args, "result", None) and not args.json:  # a headless run ended: say what's next
            print(f"attempt {again['attempt']} of {again['max_attempts']}: "
                  + (f"retry with {again['command']}" if again["command"] else "no retries left; it needs a person"),
                  file=sys.stderr)
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    if payload["checks_skipped"]:
        print(f"checks skipped: {payload['checks_skipped']}")
    for finding in payload["findings"]:
        if finding.get("mode") == "warn":
            print(f"  warning: {finding['kind']}: {finding['message']} (warn mode: not refused yet)")
        else:
            print(f"  allowed: {finding['kind']}: {finding['message']}")
    if payload.get("checked_only"):
        print(f"{payload['id']} passes its checks (status unchanged: {payload['status']})")
        return 0
    if payload.get("already_done"):
        print(f"{payload['id']} was already done; its checks pass again and its Verified mark is {payload['verified']}")
        _print_run_commit(payload)
        return 0
    print(f"{payload['id']} is now done (was {payload['previous_status']})")
    if payload["manual_verify"]:
        print(f"  check by hand: {payload['manual_verify']}")
    if payload["now_ready"]:
        when = "ready once this branch is merged" if payload["merge_needed"] else "now ready"
        print(f"  {when}: {', '.join(payload['now_ready'])}")
    elif payload["merge_needed"] and "committed" not in payload:
        print("  commit, then merge this branch; dependent items become ready once it is merged")
    _print_run_commit(payload)
    return 0


def _print_run_commit(payload: dict) -> None:
    """What `done --result` did with the run's work: the commit and the push."""
    if "committed" not in payload:
        return
    if payload.get("commit_error"):
        print(f"  warning: the work isn't committed: {payload['commit_error']}")
    elif payload.get("commit_skipped"):
        print(f"  {payload['commit_skipped']}")
    elif payload["committed"]:
        print(f"  committed {payload['committed']}")
    if payload.get("pushed"):
        print(f"  pushed {payload['branch']} to {payload['remote']}; land it from the integration branch: its land --fetch")
    elif payload.get("push_error"):
        print(f"  warning: the branch isn't pushed: {payload['push_error']}")
    elif payload.get("push_skipped"):
        print(f"  not pushed: {payload['push_skipped']}")


def cmd_stats(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    out = stats(pkg)
    if args.json:
        _emit({"schema": SCHEMA, "ok": True, **out})
        return 0
    if not out["records"]:
        print("the run ledger is empty: start, done, block and land record to it" if out["ledger"] else
              "the run ledger needs a git repository")
        return 0
    def rate(value: float | None, interval: list[float] | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.0%}" + (f", 95% interval {interval[0]:.0%}-{interval[1]:.0%}" if interval else "")

    print(f"{out['attempted']} item(s) attempted, {out['first_try']} passed first time "
          f"({rate(out['first_try_rate'], out['first_try_interval'])})")
    few = False
    for name, label in (("by_size", "size"), ("by_agent", "agent")):
        for key, g in out[name].items():
            few = few or not g["comparable"]
            print(f"  {label} {key}: {g['first_try']} of {g['items']} first time "
                  f"({rate(g['first_try_rate'], g['interval'])})")
    if few:
        print(f"  (groups under {out['comparable_from']} items are too few to compare: their intervals overlap)")
    if out["refusals"]:
        print("refused for: " + ", ".join(f"{k} {v}" for k, v in out["refusals"].items()))
    if out["land"]:
        print("land: " + ", ".join(f"{k} {v}" for k, v in out["land"].items()))
    if out["reviews"]:
        print("reviews: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in out["reviews"].items()))
    if out["cost_usd"]:
        p90 = f", p90 per run ${out['cost_p90_usd']:.2f}" if out["cost_p90_usd"] is not None else ""
        print(f"agent cost ${out['cost_usd']:.2f}{p90}")
    print(f"ledger: {out['ledger']}")
    return 0


def cmd_land(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1

    def announce(ident, check) -> None:
        if not args.json:
            print(f"{ident}: running {check.name}: {check.command}", flush=True)

    gone = _waiver_flags(args)
    if gone:
        return _fail(args, gone)
    try:
        payload = land(pkg, args.ids or None, verify=not args.no_verify, fetch=args.fetch, push=args.push,
                       release=args.release, dry_run=args.dry_run, accept_base=args.accept_base,
                       require_review=args.require_review, on_check=announce)
    except WorkError as err:
        if err.code == "base_untrusted" and not args.json:
            for line in err.details["commits"]:
                print(f"  {line}", file=sys.stderr)
        return _fail(args, err)
    stuck = [s for s in payload["skipped"] if s["reason"] not in ("waiting", "dry_run")]
    if not payload["landed"] and not stuck and not any(s["reason"] == "dry_run" for s in payload["skipped"]):
        code = EXIT_NOTHING_READY  # nothing was ready, or only items still waiting on dependencies
    elif stuck:
        code = EXIT_NOT_LANDED
    else:
        code = 0
    if args.json:
        _emit(payload)
        return code
    for done_ in payload["landed"]:
        print(f"landed {done_['id']} ({done_['branch']})")
    for skip in payload["skipped"]:
        if skip["reason"] == "dry_run":
            print(f"would land {skip['id']} ({skip['branch']})")
            continue
        print(f"not landed: {skip['id']}: {skip['message']}")
        for check in skip.get("checks", []):
            if not check["passed"]:
                print(f"  {check['name']} failed (exit {check['exit_code']}): {check['command']}")
                if check["output"]:
                    print(check["output"])
    if not payload["landed"] and not payload["skipped"]:
        print("nothing to land: no claim branch has a done item that isn't merged yet")
    if payload["pushed"]:
        print(f"pushed {payload['base']}")
    for ident in payload["released"]:
        print(f"released the claim on {ident}")
    if payload["proposals"]:
        print(f"{len(payload['proposals'])} proposal(s) are waiting: its promote <ID>, or its renumber to number them")
    return code


def cmd_brief(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = brief(pkg, args.id, retry=args.retry, github=args.github, base=args.base)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    issue = payload["issue"]
    if issue is None:
        sys.stdout.write(payload["brief"])
        return 0
    where = f": {issue['url']}" if issue.get("url") else ""
    print(f"{issue['action']} issue #{issue['number']} for {payload['id']}{where}")
    if issue["action"] == "created":
        print("assign it to an agent on GitHub (Copilot, Claude or Codex), then: "
              f"its adopt <its branch or pr:N> {payload['id']}")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    if args.ref and args.dir == "." and Path(args.ref).is_dir():  # `its gate DIR`
        args.dir, args.ref = args.ref, None
    pkg = _load(args)
    if pkg is None:
        return 1

    def announce(ident, check) -> None:
        if not args.json:
            print(f"{ident}: running {check.name}: {check.command}", flush=True)

    try:
        payload = gate_ref(pkg, args.ref or "HEAD", base=args.base, ident=args.id, require_item=args.require_item,
                           verify=not args.no_verify, on_check=announce)
    except WorkError as err:
        return _fail(args, err)
    code = 0 if payload["passed"] else EXIT_CHECK_FAILED
    if args.json:
        _emit(payload)
        return code
    what = f"{payload['id']} ({payload['found_by'].replace('_', ' ')})" if payload["id"] else "nothing"
    print(f"gate {what} on {payload['branch'] or payload['ref']} against {payload['base']}: "
          + ("passed" if payload["passed"] else "refused"))
    print(f"  {payload['message']}")
    for finding in payload["findings"]:
        print(f"  {finding['kind']}: {finding['message']}")
    for check in payload["checks"]:
        print(f"  {'ok' if check['passed'] else 'failed'}: {check['name']}: {check['command']}")
        if not check["passed"] and check.get("output"):
            print(check["output"])
    for w in payload["warnings"]:
        print(f"  warning: {w['kind']}: {w['message']}")
    return code


def _waiver_flags(args: argparse.Namespace) -> WorkError | None:
    """The error for the --allow-* flags, which phase 10 replaced with `its waive`."""
    for flag in ("allow_test_changes", "allow_outside_touches"):
        if getattr(args, flag, None) is not None:
            option = "--" + flag.replace("_", "-")
            group = "--test-changes" if flag == "allow_test_changes" else "--outside-touches"
            return WorkError("use_waive", f"{option} is gone: the lead waives on the integration branch with its waive "
                             f"<ID> {group} --reason '...', and done and land honour that waiver",
                             details={"flag": option})
    return None


def _commit(args: argparse.Namespace, pkg: Package, payload: dict, message: str) -> dict:
    """Commit the planning folder when asked (`--commit`, or by default for promote and renumber). A commit made in
    the lead's checkout of the integration branch is recorded as trusted (`trust.py`), so land doesn't take it for
    an agent's."""
    if not getattr(args, "commit", False) or payload.get("dry_run") or not cl.in_git(pkg.root):
        return payload
    folder = str(pkg.planning_dir)
    cl.git(pkg.root, "add", "--all", "--", folder)
    staged = cl.git(pkg.root, "diff", "--cached", "--name-only", "--", folder, check=False)
    if staged:
        before = cl.git(pkg.root, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        cl.git(pkg.root, "commit", "--quiet", "-m", message, "--", folder)
        payload["committed"] = cl.git(pkg.root, "rev-parse", "--short", "HEAD")
        if _on_base_checkout(pkg):
            trust.record(pkg.root, integration_branch(pkg), "HEAD", after=before or None, remote=remote_of(pkg))
    return payload


def cmd_waive(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    groups = [g for g, on in (("test-changes", args.test_changes), ("outside-touches", args.outside_touches),
                              ("dependencies", args.dependencies)) if on]
    try:
        payload = waive(pkg, args.id, groups, args.reason)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    w = payload["waiver"]
    print(f"{payload['id']}: waived {', '.join(w['groups'])} ({w['reason']})")
    print(f"  committed {payload['committed']}; its done and its land honour it from now on")
    return 0


def _edited(args: argparse.Namespace, payload: dict, pkg: Package | None = None) -> int:
    if pkg is not None:
        what = {"todo": "Schedule", "blocked": "Block", "dropped": "Drop"}.get(payload.get("status"), "Set status of")
        _commit(args, pkg, payload, f"{what} {payload['id']}")
    if args.json:
        _emit(payload)
        return 0
    what = payload.get("renumbered_from")
    if what:
        print(f"{what} is now {payload['id']}")
    if payload.get("deleted_file"):
        print(f"{payload['id']} dropped: deleted {payload['deleted_file']}")
    else:
        print(f"{payload['id']} is now {payload['status']} (was {payload['previous_status']})")
    if payload.get("reason"):
        print(f"  reason: {payload['reason']}")
    for w in payload.get("sizing") or []:
        print(f"  sizing: {w['message']}")
    if payload.get("committed"):
        print(f"  committed {payload['committed']}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    payload = propose(pkg, args.title, name=args.name, satisfies=args.satisfies, depends_on=args.depends_on,
                      milestone=args.milestone, do=args.do, done_when=args.done_when, verify=args.verify,
                      touches=args.touches)
    if args.json:
        _emit(payload)
        return 0
    print(f"proposed {payload['id']} in {payload['file']}")
    print("  it gets a permanent ID when someone runs its promote or its renumber on the integration branch")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, promote(pkg, args.id, force=args.force,
                                                       allow_incomplete=args.allow_incomplete), pkg)


def cmd_block(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, block(pkg, args.id, args.reason), pkg)


def cmd_unblock(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, unblock(pkg, args.id), pkg)


def cmd_drop(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, drop(pkg, args.id, args.reason), pkg)


def cmd_set_status(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, set_item_status(pkg, args.id, args.status, reason=args.reason), pkg)


def _confirm_delete(target: Path, assume_yes: bool) -> bool:
    count = sum(1 for p in target.rglob("*") if p.is_file())
    if assume_yes:
        return True
    if not is_terminal(0):
        print(
            f"error: --force would delete {target} ({count} files). Re-run with --yes to confirm non-interactively.",
            file=sys.stderr,
        )
        return False
    answer = input(f"--force will delete {target} and everything in it ({count} files). Continue? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def cmd_review(args: argparse.Namespace) -> int:
    if args.id and args.dir == "." and not TASK_RE.fullmatch(args.id):
        args.dir, args.id = args.id, None
    if args.schema:
        print(json.dumps(REPLY_SCHEMA, indent=None if args.json else 2))
        return 0
    if args.reply:
        pkg = _load(args)
        if pkg is None:
            return 1
        try:
            text = sys.stdin.read() if args.reply == "-" else Path(args.reply).read_text(encoding="utf-8")
            payload = judge(pkg, args.id, text, min_severity=args.min_severity, base=args.base, branch=args.branch,
                            run_evidence=not args.no_evidence)
        except OSError as exc:
            return _fail(args, WorkError("not_found", f"can't read {args.reply}: {exc}"))
        except WorkError as err:
            return _fail(args, err)
        code = 0 if payload["passed"] else EXIT_CHECK_FAILED
        if args.json:
            _emit(payload)
            return code
        print(f"review of {payload['id']}: {payload['outcome'].replace('_', ' ')}"
              + (f" (the reviewer said {payload['verdict'].replace('_', ' ')})" if payload["verdict"] != "approve"
                 and payload["outcome"] != "changes_requested" else ""))
        for e in payload["evidence"]:
            print(f"  {'reproduced' if e['reproduced'] else 'not reproduced, downgraded'}: {e['kind']} {e['of']}: "
                  f"{e['why']}" + (f" ({e['command']})" if e.get("command") else ""))
        for ident in payload["unmet"]:
            print(f"  {ident}: " + ("not in the reply" if ident in payload["missing"] else "not met"))
        for f in payload["findings"]:
            mark = "!" if f in payload["blocking"] else " "
            print(f" {mark}{f['severity']:<8} {f['file']}{':' + str(f['line']) if f.get('line') else ''}  "
                  f"{f['message']}")
        if not payload["current"]:
            print(f"{payload['id']} changed since the review prompt was made: this verdict counts only for what the "
                  "reviewer saw")
        elif payload["recorded"]:
            print("recorded: its land " + ("will take it" if payload["passed"] else
                                           "refuses this version until it changes and is reviewed again"
                                           if payload["outcome"] == "changes_requested" else
                                           "takes it, unless it needs an approving review"))
        return code
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = review(pkg, args.id, base=args.base, branch=args.branch)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
    else:
        sys.stdout.write(payload["prompt"])
    return 0


def cmd_hooks(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = install_hooks(pkg.root, [args.agent] if args.agent else hook_agents(pkg))
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    for rel in payload["written"]:
        print(f"added a Stop hook ({payload['command']}) to {rel}")
    for rel in payload["unchanged"]:
        print(f"{rel} already has the Stop hook")
    print("commit them to share; Codex runs project hooks only once you trust them (/hooks)")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """`its hook stop`: the Stop hook itself. It never fails the agent's tooling: any problem lets the agent stop."""
    try:
        stdin = "" if sys.stdin.isatty() else sys.stdin.read()
        pkg = Package.find(hook_dir(parse_event(stdin), args.dir))
        decision = stop_hook(pkg, stdin, headless=args.headless)
    except Exception:  # noqa: BLE001 - a broken hook must not trap the agent
        return 0
    if decision:
        print(json.dumps(decision))
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    if args.no_git and args.github is not None:
        print("error: --github needs git; drop --no-git or --github", file=sys.stderr)
        return EXIT_USAGE
    github = None
    if args.github is not None:
        github = args.github  # "" means use the slug
    if args.force:
        try:
            slug = Package.load(Path(args.package)).slug
        except PackageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        target = Path(args.out) / slug
        if target.exists() and any(target.iterdir()) and not _confirm_delete(target, args.yes):
            print("aborted: nothing was changed", file=sys.stderr)
            return 1
    try:
        result = scaffold(
            Path(args.package),
            Path(args.out),
            force=args.force,
            git=not args.no_git,
            github=github,
            allow_unready=args.allow_unready,
            hooks=args.hooks,
        )
    except (ScaffoldError, PackageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"scaffolded {result.path} ({len(result.files)} files)")
    for w in result.warnings:
        print(f"warning {w}")
    if result.report and result.report.warnings:
        for w in result.report.warnings:
            print(f"warning {w}")
    if result.committed:
        print("git: initialised on main with the initial commit")
    if result.remote:
        print(f"github: pushed to {result.remote}")
    print(f"next: open {result.path / 'HANDOFF.md'} and give its kick-off prompt to your agent")
    return 0


class _Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors keep the JSON contract when `--json` is on the command line."""

    json_errors = False

    def error(self, message: str):  # type: ignore[override]
        if _Parser.json_errors:
            _emit(WorkError("usage", f"{self.prog}: {message}", EXIT_USAGE).to_dict())
            raise SystemExit(EXIT_USAGE)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="its", description="idea-to-scaffold: planning package to agent-ready repo")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-C", dest="chdir", metavar="DIR",
                   help="run as if started in DIR (like git -C); every command's folder is taken relative to it")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("new", help="create a blank planning package")
    s.add_argument("dir")
    s.add_argument(
        "--profile", choices=PROFILES, default="full",
        help="lite: idea, vision, requirements and backlog only; architecture, design and plan are optional",
    )
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("import", help="import a chat export (Markdown with file markers) into a package")
    s.add_argument("export", help="the exported chat .md file")
    s.add_argument("--into", required=True, help="package folder to write into")
    s.add_argument("--no-defaults", action="store_true", help="don't add default playbooks and ADR-0001")
    s.add_argument(
        "--overwrite", action="store_true",
        help="replace existing files that differ from the export (keeps each original as <name>.orig)",
    )
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("validate", help="check a package (or scaffolded repo) against the spec")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--trace", action="store_true",
                   help="also list Must acceptance criteria that no test names (exit 1 if any)")
    s.add_argument("--sizing", action="store_true",
                   help="also warn about todo and proposed items too big or vague for one agent run (never fails)")
    s.add_argument("--json", action="store_true", help=f"print JSON (schema {SCHEMA}) instead of text")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("link", help="cross-link IDs, refresh nav blocks, the planning index and the commands blocks")
    s.add_argument("dir", nargs="?", default=".", help="package folder or scaffolded repo (default: .)")
    s.add_argument("--check", action="store_true", help="don't write; exit 1 if anything is out of date (for CI)")
    s.add_argument("--json", action="store_true", help=f"print JSON (schema {SCHEMA}) instead of text")
    s.set_defaults(func=cmd_link)

    dir_help = "package folder or scaffolded repo (default: .)"
    json_help = f"print JSON (schema {SCHEMA}) instead of text"
    fetch_help = "fetch the integration branch and claim branches from the remote first"
    allow = {"nargs": "?", "const": "", "default": None, "metavar": "REASON", "help": argparse.SUPPRESS}  # gone
    base_help = "integration branch to read readiness from (default: integration_branch in package.yaml, or main)"

    s = sub.add_parser("backlog", help="list backlog items")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--status", choices=BACKLOG_STATUSES, help="only items with this status")
    s.add_argument("--ready", action="store_true", help="only items that can start now")
    s.add_argument("--json", action="store_true", help=json_help)
    s.add_argument("--write", action="store_true", help="write the backlog.json mirror next to the backlog")
    s.set_defaults(func=cmd_backlog)

    s = sub.add_parser("next", help="the next ready backlog item (exit 3 if nothing is ready)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--all", action="store_true", help="every ready item, in backlog order")
    s.add_argument(
        "--parallel", nargs="?", type=int, const=3, default=None, metavar="N",
        help="up to N ready items (default 3) whose Touches don't overlap each other or active claims",
    )
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_next)

    s = sub.add_parser("show", help="where an ID is defined, its text, and the docs that mention it")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("start", help="set a ready item in-progress (exit 4 if a dependency isn't done)")
    s.add_argument("id", nargs="?", help="the item (or --next)")
    s.add_argument("--next", action="store_true",
                   help="start the item its next would offer, picked and claimed in one step (exit 3 if none)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--force", action="store_true", help="start it even if dependencies aren't done, or it is blocked")
    s.add_argument("--branch", action="store_true", help="claim it with a branch its/<ID>-<slug>, checked out here")
    s.add_argument(
        "--worktree", nargs="?", const="", default=None, metavar="PATH",
        help="claim it with a branch in a new worktree (default: ../<repo>-<ID>)",
    )
    s.add_argument("--agent", help="who is working on it (recorded with the claim), e.g. claude or codex")
    s.add_argument(
        "--allow-overlap", action="store_true",
        help="start it even though its Touches don't confirm it is apart from the items already running",
    )
    s.add_argument("--no-setup", action="store_true",
                   help="with --worktree: don't run the setup command (commands.setup) in the new worktree")
    s.add_argument("--no-push", action="store_true",
                   help="keep a claim local (by default it is pushed to the remote, if there is one)")
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("status", help="the lead's view: every claim's state (working, blocked, ready to land), "
                       "what can start next, what to release, proposals and drift")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("claims", help="list claim branches (its/<ID>-...), local and remote, and which would conflict")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_claims)

    s = sub.add_parser("brief", help="print a self-contained task for an agent that may never run its "
                                     "(or put it on a GitHub issue)")
    s.add_argument("id", help="the item")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--retry", action="store_true", help="a brief for one more attempt: only what the last failed "
                                                        "attempt got wrong (up to gate.max_attempts)")
    s.add_argument("--github", action="store_true", help="create or update the item's GitHub issue with gh "
                                                         "(a retry is posted as a comment)")
    s.add_argument("--base", help="the integration branch to read the item and the rules from")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("gate", help="run the done gate on a branch or pull request with the integration branch's "
                                    "rules, for CI; changes nothing")
    s.add_argument("ref", nargs="?", help="the branch or commit to gate (default: HEAD, the pull request in CI)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help="the branch to compare with and take the rules from (default: GITHUB_BASE_REF in a "
                                  "pull request, else the integration branch)")
    s.add_argument("--id", help="the item the branch is for (default: from the branch name, its backlog change, or "
                                "[T-n.m] in its commits)")
    s.add_argument("--require-item", action="store_true", help="fail when the branch names no backlog item")
    s.add_argument("--no-verify", action="store_true", help="only the checks of the change; run no commands")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_gate)

    s = sub.add_parser("land", help="merge finished claim branches one at a time, testing each merged result")
    s.add_argument("ids", nargs="*", metavar="ID", help="only these items (default: every claim that is done)")
    s.add_argument("--dir", default=".", help=dir_help)
    s.add_argument("--no-verify", action="store_true", help="merge without running the checks")
    s.add_argument("--fetch", action="store_true", help=fetch_help + "; fast-forward the integration branch first")
    s.add_argument("--push", action="store_true", help="push the integration branch after landing")
    s.add_argument("--release", action="store_true",
                   help="release each landed claim (with --push, its pushed branch too)")
    s.add_argument("--dry-run", action="store_true", help="show what would land, changing nothing")
    s.add_argument("--accept-base", action="store_true",
                   help="trust the integration branch's commits that its didn't make, after you have looked at them")
    s.add_argument("--require-review", action="store_true",
                   help="land only items with an approving its review of exactly what would merge "
                        "(gate.require_review in package.yaml does this always)")
    s.add_argument("--allow-test-changes", **allow)
    s.add_argument("--allow-outside-touches", **allow)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_land)

    s = sub.add_parser("propose", help="propose a new backlog item, in its own file under planning/proposals/")
    s.add_argument("title")
    s.add_argument("--dir", default=".", help=dir_help)
    s.add_argument("--name", help="the placeholder name, T-new-<name> (default: from the title)")
    s.add_argument("--satisfies", help="requirements it delivers, e.g. FR-003")
    s.add_argument("--depends-on", help="items it needs first, e.g. T-2.1")
    s.add_argument("--milestone", help="e.g. M2 (default: unscheduled)")
    s.add_argument("--do", help="what to do")
    s.add_argument("--done-when", help="how to tell it is finished")
    s.add_argument("--verify", help="a command in backticks, a test ID, or manual: ...")
    s.add_argument("--touches", help="paths or globs it will change, comma-separated")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_propose)

    s = sub.add_parser("promote", help="schedule a proposed item: give a placeholder its ID and set it todo")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--force", action="store_true", help="renumber even when not on the integration branch")
    s.add_argument("--allow-incomplete", action="store_true",
                   help="schedule it even if Done when or Touches is empty")
    s.add_argument("--no-commit", dest="commit", action="store_false",
                   help="leave the change uncommitted (by default it is committed, so a worktree started next sees it)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_promote)

    s = sub.add_parser("block", help="mark an item blocked and say why (also how an agent stops honestly)")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", required=True, help="what it is waiting for")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_block)

    s = sub.add_parser("unblock", help="set a blocked item back to todo")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_unblock)

    s = sub.add_parser("drop", help="drop an item (a proposal's file is deleted)")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", help="why it was dropped")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_drop)

    s = sub.add_parser("set-status", help="set any item's status by hand")
    s.add_argument("id")
    s.add_argument("status", choices=BACKLOG_STATUSES)
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", help="recorded for blocked and dropped")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_set_status)

    s = sub.add_parser("waive", help="let one item through part of the gate: the lead, on the integration branch "
                       "(the waiver is committed there, and done and land honour only that)")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--test-changes", action="store_true",
                   help="deleted or weakened tests, new skips, test config, agent config and changes to the plan")
    s.add_argument("--outside-touches", action="store_true",
                   help="changes to files outside the item's Touches, or to its Touches")
    s.add_argument("--dependencies", action="store_true", help="added or changed dependencies and lockfiles")
    s.add_argument("--reason", required=True, help="why; recorded on the item and in the commit")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_waive)

    s = sub.add_parser("release", help="drop a claim: remove its worktree and delete its local branch")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--remote", action="store_true", help="also delete the claim's pushed branches on every remote")
    s.add_argument("--force", action="store_true", help="also if the worktree has changes or a branch isn't merged")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("adopt", help="claim an item for a branch a vendor's agent made (claude/..., copilot/..., a "
                       "pull request), so its land gates and merges it")
    s.add_argument("ref", help="the branch (claude/x or origin/claude/x) or pull request (pr:12, or its URL)")
    s.add_argument("id", help="the item the branch implements")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--agent", help="who made the branch, for the run ledger (default: from its name, e.g. claude)")
    s.add_argument("--no-push", action="store_true",
                   help="keep the claim local (by default it is pushed to the remote, if there is one)")
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser("renumber", help="give placeholder items (T-new-...) permanent IDs, after merging")
    s.add_argument("ids", nargs="*", metavar="ID", help="only these placeholders (default: all that aren't dropped)")
    s.add_argument("--dir", default=".", help=dir_help)
    s.add_argument("--dry-run", action="store_true", help="show the new IDs without changing anything")
    s.add_argument("--force", action="store_true", help="renumber even when not on the integration branch")
    s.add_argument("--no-commit", dest="commit", action="store_false",
                   help="leave the change uncommitted (by default it is committed, so a worktree started next sees it)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_renumber)

    s = sub.add_parser("done", help="run an item's checks, then set it done (exit 5 if a check fails)")
    s.add_argument("id", nargs="?", help="the item (default: the only in-progress item)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--no-verify", action="store_true", help="don't run Verify, test and lint first")
    s.add_argument("--check", action="store_true", help="run every check but leave the status alone (exit 5 if any fails)")
    s.add_argument("--result", metavar="FILE",
                   help="act on a headless agent's result JSON: done runs the checks, blocked or needs_human blocks "
                        "it; in a claim checkout the work is then committed and the branch pushed")
    s.add_argument("--no-push", action="store_true", help="with --result: commit, but don't push the claim branch")
    s.add_argument("--allow-test-changes", **allow)
    s.add_argument("--allow-outside-touches", **allow)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_done)

    s = sub.add_parser("drift", help="done items whose files changed after they were verified, and their requirements")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--check", action="store_true", help="exit 8 if anything drifted (for CI)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_drift)

    s = sub.add_parser("stats", help="what the run ledger says: first-time pass rate, refusals, land results, cost")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("gate-audit", help="feed the done gate known cheats and report which it catches")
    s.add_argument("id", nargs="?", help="the item to audit as (default: the first item with Touches)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--no-run", action="store_true",
                   help="leave out the cheats that need the tests to run (os._exit in the item's source)")
    s.add_argument("--allow-warn", action="store_true",
                   help="exit 0 when every cheat is at least warned about, not only when every one is refused")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_gate_audit)

    s = sub.add_parser("verify", help="re-run a done item's checks and refresh its Verified mark")
    s.add_argument("id", nargs="?", help="the item (or pass --drifted or --all)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    which = s.add_mutually_exclusive_group()
    which.add_argument("--drifted", action="store_true", help="every item its drift reports")
    which.add_argument("--all", action="store_true", help="every done item that lists Touches")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("review", help="print a review prompt with the item, its requirements and the diff")
    s.add_argument("id", nargs="?", help="the item (default: this claim branch's)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help="the ref to diff against (default: the integration branch)")
    s.add_argument("--branch", metavar="REF", help="review this branch: one of an adopted item's candidates, or any "
                                                   "branch here (default: the claim's, or its first candidate)")
    s.add_argument("--schema", action="store_true", help="print the JSON schema the reviewer's reply must follow")
    s.add_argument("--reply", metavar="FILE", help="read a reviewer's JSON reply to the item's review (- for stdin), "
                                                   "record the verdict, and say if it passes (exit 5 if not)")
    s.add_argument("--min-severity", default="major", choices=["blocker", "major", "minor"],
                   help="with --reply: the lowest severity that fails the review (default: major)")
    s.add_argument("--no-evidence", action="store_true",
                   help="with --reply: don't run the reviewer's repro commands (their claims are then downgraded)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("hooks", help="add an opt-in Stop hook for Claude Code and Codex that runs the done gate")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--agent", choices=["claude", "codex"], help="only this agent (default: the package's agents)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_hooks)

    s = sub.add_parser("hook", help="run a hook (what the files written by its hooks call)")
    s.add_argument("event", choices=["stop"])
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--headless", action="store_true",
                   help="an unattended run: ask only for passing checks; its done --result marks and commits")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("scaffold", help="create the project repo from a package")
    s.add_argument("package", help="planning package folder")
    s.add_argument("--out", default=".", help="parent folder for the new repo (default: current)")
    s.add_argument("--force", action="store_true", help="delete and replace an existing non-empty target folder")
    s.add_argument("--yes", "-y", action="store_true", help="don't ask before --force deletes the target folder")
    s.add_argument("--no-git", action="store_true", help="skip git init and the initial commit")
    s.add_argument(
        "--github", nargs="?", const="", default=None, metavar="OWNER/NAME",
        help="also create a private GitHub repo with gh and push (default name: the slug)",
    )
    s.add_argument("--allow-unready", action="store_true", help="scaffold even if key docs aren't approved")
    s.add_argument("--hooks", action="store_true", help="also add the Stop hooks (see its hooks)")
    s.set_defaults(func=cmd_scaffold)
    return p


def _utf8_streams() -> None:
    """Read and write UTF-8 on the process's own pipes and files, whatever the locale says (cp1252 on most Windows
    machines), so `--json` output and the hook's stdin always mean the same thing. A console is left alone: Python
    already writes Unicode to it."""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not getattr(sys, f"__{name}__") or not hasattr(stream, "reconfigure"):
            continue  # replaced by a caller (a test, an embedding program): theirs to set
        try:
            if is_terminal(stream.fileno()) or (stream.encoding or "").lower().replace("-", "") == "utf8":
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def _stdout_closed() -> bool:
    """Whether stdout's reader has gone: the write that failed left its bytes in the buffer, so flushing again fails
    the same way. An EINVAL from anything else leaves stdout able to flush."""
    try:
        sys.stdout.flush()
    except OSError:
        return True
    return False


def _reader_gone() -> int:
    with contextlib.suppress(OSError, ValueError, AttributeError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    argv = sys.argv[1:] if argv is None else list(argv)
    _Parser.json_errors = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # usage errors, --help and --version: return the code rather than exiting
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    finally:
        _Parser.json_errors = False
    if args.chdir:
        for name in ("dir", "into", "package"):
            value = getattr(args, name, None)
            if isinstance(value, str) and not Path(value).is_absolute():
                setattr(args, name, str(Path(args.chdir) / value))
    try:
        return args.func(args)
    except BrokenPipeError:  # `its status | head`: the reader stopped reading, so stop writing, quietly
        return _reader_gone()
    except WorkError as err:
        return _fail(args, err)
    except BacklogError as exc:
        return _fail(args, WorkError("bad_backlog", str(exc)))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # the JSON contract holds even for a bug: agents always get an error envelope
        if isinstance(exc, OSError) and os.name == "nt" and exc.errno == errno.EINVAL and _stdout_closed():
            return _reader_gone()  # on Windows a write to a closed pipe fails with EINVAL, not EPIPE
        if not getattr(args, "json", False):
            raise
        return _fail(args, WorkError("internal_error", f"{type(exc).__name__}: {exc}",
                                     details={"version": __version__}))


if __name__ == "__main__":
    raise SystemExit(main())
