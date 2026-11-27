"""Command line interface for `its`: planning (new, import, validate, link), work (next, start, done, land, review)
and the backlog commands."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .backlog import BacklogError
from .edits import block, drop, promote, propose, set_item_status, unblock
from .hooks import hook_agents, install_hooks, stop_hook
from .ids import TASK_PATTERN
from .importer import ImportConflict, import_export
from .landing import land
from .linker import link_package
from .package import BACKLOG_STATUSES, PROFILES, Package, PackageError, new_package
from .review import review
from .scaffold import ScaffoldError, scaffold
from .validate import validate
from .work import (
    EXIT_NOT_LANDED,
    EXIT_NOTHING_READY,
    SCHEMA,
    WorkError,
    backlog_payload,
    claims_payload,
    done,
    fetch,
    next_payload,
    release,
    renumber,
    show_payload,
    shown,
    start,
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
    return cmd_validate(argparse.Namespace(dir=args.into))


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        pkg = Package.load(Path(args.dir))
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    report = validate(pkg)
    for e in report.errors:
        print(f"ERROR   {e}")
    for w in report.warnings:
        print(f"warning {w}")
    if report.valid and pkg.repo_layout:
        print("planning docs are valid")
    elif report.ready:
        print("package is valid and ready to scaffold")
    elif report.valid:
        print("package is valid, but not ready to scaffold yet")
    else:
        print(f"package has {len(report.errors)} error(s)")
    return 0 if report.valid else 1


def cmd_link(args: argparse.Namespace) -> int:
    try:
        pkg = Package.load(Path(args.dir))
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        changed = link_package(pkg, check=args.check)
    except UnicodeDecodeError as exc:
        print(f"error: a planning file is not valid UTF-8 text ({exc.reason})", file=sys.stderr)
        return 1
    changed = [shown(pkg, rel) for rel in changed]
    if args.check:
        if changed:
            print("cross-links are out of date in:")
            for rel in changed:
                print(f"  {rel}")
            print("run `its link` and commit the result")
            return 1
        print("cross-links are up to date")
        return 0
    print(f"updated {len(changed)} file(s)" if changed else "already up to date")
    for rel in changed:
        print(f"  {rel}")
    return 0


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
    if not items:
        what = " ready" if args.ready else (f" with status {args.status}" if args.status else "")
        print(f"no backlog items{what}")
        return 0
    id_width = max(8, *(len(i["id"]) + 1 for i in items))
    print(f"{'ID':<{id_width}} {'STATUS':<12} {'MILESTONE':<10} TITLE")
    for i in items:
        print(f"{i['id']:<{id_width}} {i['status']:<12} {i['milestone'] or '—':<10} {i['title']}")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    if args.fetch:
        fetch(pkg, args.base)
    payload = next_payload(pkg, base=args.base, parallel=args.parallel)
    if not args.all:
        payload.pop("ready")
    if args.parallel is not None:
        code = 0 if payload["parallel"] else EXIT_NOTHING_READY
    else:
        code = 0 if payload["item"] else EXIT_NOTHING_READY
    if args.json:
        _emit(payload)
        return code
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
        return _fail(args, WorkError("usage", "name the item to start, or pass --next", 2))
    pkg = _load(args)
    if pkg is None:
        return 1
    if args.fetch:
        fetch(pkg, args.base)
    try:
        payload = start(pkg, None if args.next else args.id, force=args.force, branch=args.branch,
                        worktree=args.worktree, agent=args.agent, base=args.base, allow_overlap=args.allow_overlap,
                        push=False if args.no_push else None)
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
        print("start an agent on it:")
        print(f"  {payload['commands']['claude']}")
        print(f"  {payload['commands']['codex']}")
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
        if c["orphaned"]:
            state = "orphaned: not in the backlog, release it"
        elif c["finished"]:
            state = "finished, release it"
        else:
            state = "remote" if c["remote"] else "active"
        extra = f"  worktree {c['worktree']}" if c["worktree"] else ""
        print(f"{c['id']:<10} {c['branch']}  [{state}]{extra}")
        if c.get("conflicts_with_base"):
            print(f"           would conflict with {payload['base']} in {', '.join(c['conflicts_with_base'])}")
        for other in c.get("conflicts_with", []):
            print(f"           would conflict with {other['id']} in {', '.join(other['files'])}")
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


def cmd_renumber(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = renumber(pkg, args.ids or None, dry_run=args.dry_run, force=args.force)
    except WorkError as err:
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    if not payload["renumbered"]:
        print("no placeholder items to renumber")
    for r in payload["renumbered"]:
        print(f"{r['from']} -> {r['to']}" + (" (dry run)" if args.dry_run else ""))
    for f in payload["files"]:
        print(f"  updated {f}")
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

    try:
        payload = done(pkg, args.id, verify=not args.no_verify, check_only=args.check,
                       allow_test_changes=args.allow_test_changes, allow_outside_touches=args.allow_outside_touches,
                       on_check=announce)
    except WorkError as err:
        if err.code in ("test_changes", "outside_touches") and not args.json:
            for finding in err.details["findings"]:
                print(f"  {finding['kind']}: {finding['message']}")
        if err.code == "check_failed" and not args.json:
            for check in err.details["checks"]:
                if not check["passed"]:
                    print(f"\n{check['name']} failed (exit {check['exit_code']}): {check['command']}")
                    if check["output"]:
                        print(check["output"])
        return _fail(args, err)
    if args.json:
        _emit(payload)
        return 0
    if payload["checks_skipped"]:
        print(f"checks skipped: {payload['checks_skipped']}")
    for finding in payload["findings"]:
        print(f"  allowed: {finding['kind']}: {finding['message']}")
    if payload.get("checked_only"):
        print(f"{payload['id']} passes its checks (status unchanged: {payload['status']})")
        return 0
    print(f"{payload['id']} is now done (was {payload['previous_status']})")
    if payload["manual_verify"]:
        print(f"  check by hand: {payload['manual_verify']}")
    if payload["now_ready"]:
        when = "ready once this branch is merged" if payload["merge_needed"] else "now ready"
        print(f"  {when}: {', '.join(payload['now_ready'])}")
    elif payload["merge_needed"]:
        print("  commit, then merge this branch; dependent items become ready once it is merged")
    return 0


def cmd_land(args: argparse.Namespace) -> int:
    pkg = _load(args)
    if pkg is None:
        return 1

    def announce(ident, check) -> None:
        if not args.json:
            print(f"{ident}: running {check.name}: {check.command}", flush=True)

    payload = land(pkg, args.ids or None, verify=not args.no_verify, fetch=args.fetch, push=args.push,
                   release=args.release, dry_run=args.dry_run, allow_test_changes=args.allow_test_changes,
                   allow_outside_touches=args.allow_outside_touches, on_check=announce)
    stuck = [s for s in payload["skipped"]
             if s["reason"] in ("conflict", "check_failed", "test_changes", "outside_touches")]
    if not payload["landed"] and not payload["skipped"]:
        code = EXIT_NOTHING_READY
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


def _edited(args: argparse.Namespace, payload: dict) -> int:
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
    return 1 if pkg is None else _edited(args, promote(pkg, args.id, force=args.force))


def cmd_block(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, block(pkg, args.id, args.reason))


def cmd_unblock(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, unblock(pkg, args.id))


def cmd_drop(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, drop(pkg, args.id, args.reason))


def cmd_set_status(args: argparse.Namespace) -> int:
    pkg = _load(args)
    return 1 if pkg is None else _edited(args, set_item_status(pkg, args.id, args.status, reason=args.reason))


def _confirm_delete(target: Path, assume_yes: bool) -> bool:
    count = sum(1 for p in target.rglob("*") if p.is_file())
    if assume_yes:
        return True
    if not sys.stdin.isatty():
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
    pkg = _load(args)
    if pkg is None:
        return 1
    try:
        payload = review(pkg, args.id, base=args.base)
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
        pkg = Package.find(Path(args.dir))
        decision = stop_hook(pkg, "" if sys.stdin.isatty() else sys.stdin.read())
    except Exception:  # noqa: BLE001 - a broken hook must not trap the agent
        return 0
    if decision:
        print(json.dumps(decision))
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    if args.no_git and args.github is not None:
        print("error: --github needs git; drop --no-git or --github", file=sys.stderr)
        return 2
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="its", description="idea-to-scaffold: planning package to agent-ready repo")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("link", help="cross-link IDs, refresh nav blocks and the planning index")
    s.add_argument("dir", nargs="?", default=".", help="package folder or scaffolded repo (default: .)")
    s.add_argument("--check", action="store_true", help="don't write; exit 1 if anything is out of date (for CI)")
    s.set_defaults(func=cmd_link)

    dir_help = "package folder or scaffolded repo (default: .)"
    json_help = f"print JSON (schema {SCHEMA}) instead of text"
    fetch_help = "fetch the integration branch and claim branches from the remote first"
    allow_tests_help = ("accept deleted tests, removed assertions, new skips and a changed Verify "
                        "(they refuse the item by default)")
    allow_touches_help = "accept changes to files outside the item's Touches, or to its Touches"
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
    s.add_argument("--no-push", action="store_true",
                   help="keep a claim local (by default it is pushed to the remote, if there is one)")
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("claims", help="list claim branches (its/<ID>-...), local and remote, and which would conflict")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_claims)

    s = sub.add_parser("land", help="merge finished claim branches one at a time, testing each merged result")
    s.add_argument("ids", nargs="*", metavar="ID", help="only these items (default: every claim that is done)")
    s.add_argument("--dir", default=".", help=dir_help)
    s.add_argument("--no-verify", action="store_true", help="merge without running the checks")
    s.add_argument("--fetch", action="store_true", help=fetch_help + "; fast-forward the integration branch first")
    s.add_argument("--push", action="store_true", help="push the integration branch after landing")
    s.add_argument("--release", action="store_true",
                   help="release each landed claim (with --push, its pushed branch too)")
    s.add_argument("--dry-run", action="store_true", help="show what would land, changing nothing")
    s.add_argument("--allow-test-changes", action="store_true", help=allow_tests_help)
    s.add_argument("--allow-outside-touches", action="store_true", help=allow_touches_help)
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
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_promote)

    s = sub.add_parser("block", help="mark an item blocked and say why (also how an agent stops honestly)")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", required=True, help="what it is waiting for")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_block)

    s = sub.add_parser("unblock", help="set a blocked item back to todo")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_unblock)

    s = sub.add_parser("drop", help="drop an item (a proposal's file is deleted)")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", help="why it was dropped")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_drop)

    s = sub.add_parser("set-status", help="set any item's status by hand")
    s.add_argument("id")
    s.add_argument("status", choices=BACKLOG_STATUSES)
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", help="recorded for blocked and dropped")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_set_status)

    s = sub.add_parser("release", help="drop a claim: remove its worktree and delete its local branch")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--remote", action="store_true", help="also delete the claim's pushed branches on every remote")
    s.add_argument("--force", action="store_true", help="also if the worktree has changes or a branch isn't merged")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("renumber", help="give placeholder items (T-new-...) permanent IDs, after merging")
    s.add_argument("ids", nargs="*", metavar="ID", help="only these placeholders (default: all that aren't dropped)")
    s.add_argument("--dir", default=".", help=dir_help)
    s.add_argument("--dry-run", action="store_true", help="show the new IDs without changing anything")
    s.add_argument("--force", action="store_true", help="renumber even when not on the integration branch")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_renumber)

    s = sub.add_parser("done", help="run an item's checks, then set it done (exit 5 if a check fails)")
    s.add_argument("id", nargs="?", help="the item (default: the only in-progress item)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--no-verify", action="store_true", help="don't run Verify, test and lint first")
    s.add_argument("--check", action="store_true", help="run every check but leave the status alone (exit 5 if any fails)")
    s.add_argument("--allow-test-changes", action="store_true", help=allow_tests_help)
    s.add_argument("--allow-outside-touches", action="store_true", help=allow_touches_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=cmd_done)

    s = sub.add_parser("review", help="print a review prompt with the item, its requirements and the diff")
    s.add_argument("id", nargs="?", help="the item (default: this claim branch's)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help="the ref to diff against (default: the integration branch)")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except WorkError as err:
        return _fail(args, err)
    except BacklogError as exc:
        return _fail(args, WorkError("bad_backlog", str(exc)))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # the JSON contract holds even for a bug: agents always get an error envelope
        if not getattr(args, "json", False):
            raise
        return _fail(args, WorkError("internal_error", f"{type(exc).__name__}: {exc}",
                                     details={"version": __version__}))


if __name__ == "__main__":
    raise SystemExit(main())
