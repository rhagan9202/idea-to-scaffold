"""The commands: each turns its parsed arguments into one service call (`service.py`) and hands the outcome to its
renderer (`render.py`). Nothing here decides what a command does or how it exits.

The one thing a command reads for itself is a folder given where an ID could go (`its done <dir>`): telling the two
apart is parsing.
"""
from __future__ import annotations

import json
import re
import sys
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path

from .. import service
from ..contract import WorkError
from ..terminal import is_terminal
from . import render

TASK_RE = re.compile(service.TASK_PATTERN)


def _run(args: Namespace, call: Callable[[], service.Outcome], text: Callable[[service.Outcome, Namespace], None],
         explain: Callable[[WorkError, Namespace], bool] | None = None) -> int:
    """Call the service, then print the outcome: the payload as JSON with --json, else as text. A failure goes to
    `explain` first (text only), which adds lines and says whether it reported the error itself."""
    as_json = getattr(args, "json", False)
    try:
        out = call()
    except WorkError as err:
        if explain is not None and not as_json and explain(err, args):
            return err.exit_code
        raise
    if as_json:
        render.emit(out.payload)
    else:
        text(out, args)
    return out.code


def _announce(args: Namespace) -> Callable:
    """Say which check is running, as it starts (text only)."""
    def announce(check) -> None:
        if not args.json:
            print(f"running {check.name}: {check.command}", flush=True)
    return announce


def _announce_item(args: Namespace) -> Callable:
    """The same, for commands that check several items."""
    def announce(ident, check) -> None:
        if not args.json:
            print(f"{ident}: running {check.name}: {check.command}", flush=True)
    return announce


def _folder_for_id(args: Namespace) -> None:
    """`its done <dir>`: a lone positional that isn't an item ID is the folder."""
    if args.id and args.dir == "." and not TASK_RE.fullmatch(args.id):
        args.dir, args.id = args.id, None


# --- planning -------------------------------------------------------------------------------------------------------

def cmd_new(args: Namespace) -> int:
    return _run(args, lambda: service.new(args.dir, args.profile), render.new)


def cmd_import(args: Namespace) -> int:
    return _run(args, lambda: service.import_export(args.export, args.into, seed=not args.no_defaults,
                                                    overwrite=args.overwrite), render.import_)


def cmd_validate(args: Namespace) -> int:
    return _run(args, lambda: service.validate(args.dir, trace=args.trace, sizing=args.sizing), render.validate)


def cmd_link(args: Namespace) -> int:
    return _run(args, lambda: service.link(args.dir, check=args.check), render.link, render.link_error)


def cmd_show(args: Namespace) -> int:
    return _run(args, lambda: service.show(args.dir, args.id), render.show)


def cmd_propose(args: Namespace) -> int:
    return _run(args, lambda: service.propose(args.dir, args.title, name=args.name, satisfies=args.satisfies,
                                              depends_on=args.depends_on, milestone=args.milestone, do=args.do,
                                              done_when=args.done_when, verify=args.verify, touches=args.touches),
                render.propose)


def cmd_promote(args: Namespace) -> int:
    return _run(args, lambda: service.promote(args.dir, args.id, force=args.force,
                                              allow_incomplete=args.allow_incomplete, commit=args.commit),
                render.edited)


def cmd_renumber(args: Namespace) -> int:
    return _run(args, lambda: service.renumber(args.dir, args.ids, dry_run=args.dry_run, force=args.force,
                                               commit=args.commit), render.renumber)


def _confirm_delete(assume_yes: bool) -> Callable[[Path, int], bool]:
    def confirm(target: Path, count: int) -> bool:
        if assume_yes:
            return True
        if not is_terminal(0):
            print(f"error: --force would delete {target} ({count} files). Re-run with --yes to confirm "
                  "non-interactively.", file=sys.stderr)
            return False
        answer = input(f"--force will delete {target} and everything in it ({count} files). Continue? [y/N] ")
        return answer.strip().lower() in ("y", "yes")
    return confirm


def cmd_scaffold(args: Namespace) -> int:
    return _run(args, lambda: service.scaffold(args.package, args.out, force=args.force, git=not args.no_git,
                                               github=args.github, allow_unready=args.allow_unready, hooks=args.hooks,
                                               confirm=_confirm_delete(args.yes)), render.scaffold)


# --- work -----------------------------------------------------------------------------------------------------------

def cmd_backlog(args: Namespace) -> int:
    return _run(args, lambda: service.backlog(args.dir, status=args.status, ready=args.ready, write=args.write),
                render.backlog)


def cmd_next(args: Namespace) -> int:
    return _run(args, lambda: service.next_item(args.dir, base=args.base, parallel=args.parallel,
                                                fetch_first=args.fetch, every=args.all), render.next_)


def cmd_start(args: Namespace) -> int:
    if args.next and args.id and args.dir == ".":  # `its start --next <dir>`
        args.dir, args.id = args.id, None
    return _run(args, lambda: service.start(args.dir, args.id, pick_next=args.next, fetch_first=args.fetch,
                                            base=args.base, force=args.force, branch=args.branch,
                                            worktree=args.worktree, agent=args.agent,
                                            allow_overlap=args.allow_overlap, push=False if args.no_push else None,
                                            setup=not args.no_setup), render.start, render.start_error)


def cmd_done(args: Namespace) -> int:
    _folder_for_id(args)
    return _run(args, lambda: service.done(args.dir, args.id, result=args.result, verify=not args.no_verify,
                                           check_only=args.check, push=False if args.no_push else None,
                                           on_check=_announce(args), allow_test_changes=args.allow_test_changes,
                                           allow_outside_touches=args.allow_outside_touches),
                render.done, render.done_error)


def cmd_block(args: Namespace) -> int:
    return _run(args, lambda: service.block(args.dir, args.id, args.reason, commit=args.commit), render.edited)


def cmd_unblock(args: Namespace) -> int:
    return _run(args, lambda: service.unblock(args.dir, args.id, commit=args.commit), render.edited)


def cmd_drop(args: Namespace) -> int:
    return _run(args, lambda: service.drop(args.dir, args.id, args.reason, commit=args.commit), render.edited)


def cmd_set_status(args: Namespace) -> int:
    return _run(args, lambda: service.set_status(args.dir, args.id, args.status, reason=args.reason,
                                                 commit=args.commit), render.edited)


# --- lead -----------------------------------------------------------------------------------------------------------

def cmd_status(args: Namespace) -> int:
    return _run(args, lambda: service.status(args.dir, base=args.base, fetch_first=args.fetch), render.status)


def cmd_claims(args: Namespace) -> int:
    return _run(args, lambda: service.claims(args.dir, base=args.base, fetch_first=args.fetch), render.claims)


def cmd_release(args: Namespace) -> int:
    return _run(args, lambda: service.release(args.dir, args.id, force=args.force, remote=args.remote),
                render.release)


def cmd_adopt(args: Namespace) -> int:
    return _run(args, lambda: service.adopt(args.dir, args.ref, args.id, push=False if args.no_push else None,
                                            fetch_first=args.fetch, agent=args.agent), render.adopt)


def cmd_brief(args: Namespace) -> int:
    return _run(args, lambda: service.brief(args.dir, args.id, retry=args.retry, github=args.github,
                                            base=args.base), render.brief)


def cmd_gate(args: Namespace) -> int:
    if args.ref and args.dir == "." and Path(args.ref).is_dir():  # `its gate DIR`
        args.dir, args.ref = args.ref, None
    return _run(args, lambda: service.gate(args.dir, args.ref, base=args.base, ident=args.id,
                                           require_item=args.require_item, verify=not args.no_verify,
                                           on_check=_announce_item(args)), render.gate)


def cmd_land(args: Namespace) -> int:
    return _run(args, lambda: service.land(args.dir, args.ids, verify=not args.no_verify, fetch=args.fetch,
                                           push=args.push, release=args.release, dry_run=args.dry_run,
                                           accept_base=args.accept_base, require_review=args.require_review,
                                           on_check=_announce_item(args), allow_test_changes=args.allow_test_changes,
                                           allow_outside_touches=args.allow_outside_touches),
                render.land, render.land_error)


def cmd_waive(args: Namespace) -> int:
    groups = [g for g, on in (("test-changes", args.test_changes), ("outside-touches", args.outside_touches),
                              ("dependencies", args.dependencies)) if on]
    return _run(args, lambda: service.waive(args.dir, args.id, groups, args.reason), render.waive)


def cmd_review(args: Namespace) -> int:
    _folder_for_id(args)
    if args.schema:
        print(json.dumps(service.review_schema().payload, indent=None if args.json else 2))
        return 0
    if args.reply:
        return _run(args, lambda: service.review_reply(args.dir, args.id, args.reply, read_stdin=sys.stdin.read,
                                                       min_severity=args.min_severity, base=args.base,
                                                       branch=args.branch, evidence=not args.no_evidence),
                    render.review_reply)
    return _run(args, lambda: service.review_prompt(args.dir, args.id, base=args.base, branch=args.branch),
                render.review_prompt)


# --- maintain -------------------------------------------------------------------------------------------------------

def cmd_drift(args: Namespace) -> int:
    return _run(args, lambda: service.drift(args.dir, check=args.check), render.drift)


def cmd_verify(args: Namespace) -> int:
    _folder_for_id(args)  # `its verify --all <dir>`
    on_item = None if args.json else (lambda ident: print(f"{ident}:"))
    return _run(args, lambda: service.verify(args.dir, args.id, drifted=args.drifted, every=args.all,
                                             commit=args.commit, on_check=_announce(args), on_item=on_item),
                render.verify, render.verify_error)


def cmd_stats(args: Namespace) -> int:
    return _run(args, lambda: service.stats(args.dir), render.stats)


def cmd_gate_audit(args: Namespace) -> int:
    _folder_for_id(args)  # `its gate-audit <dir>`
    return _run(args, lambda: service.gate_audit(args.dir, args.id, run=not args.no_run, allow_warn=args.allow_warn),
                render.gate_audit)


def cmd_hooks(args: Namespace) -> int:
    return _run(args, lambda: service.hooks(args.dir, args.agent), render.hooks)


def cmd_hook(args: Namespace) -> int:
    """`its hook stop`: what the files `its hooks` wrote run. It always exits 0, so the agent can stop."""
    out = service.stop_hook(lambda: "" if sys.stdin.isatty() else sys.stdin.read(), args.dir, headless=args.headless)
    render.hook(out, args)
    return out.code
