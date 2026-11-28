"""The `its` command line: its commands, grouped by who runs them, and their options.

`its --help` lists the commands in four groups: plan (write the planning package and make the repo), work (pick up
an item and finish it), lead (run the work from the integration branch) and maintain (keep finished work honest).
`its hook`, which only the files `its hooks` writes call, isn't listed.
"""
from __future__ import annotations

import argparse
import textwrap

from .. import __version__
from ..contract import EXIT_USAGE, SCHEMA, WorkError
from ..service import BACKLOG_STATUSES, PROFILES
from . import commands, render

GROUPS = {  # group: (what it is for, its commands in the order `its --help` lists them)
    "plan": ("write the planning package and make the repo",
             "new import validate link show propose promote renumber scaffold"),
    "work": ("pick up an item and finish it", "next start done block backlog"),
    "lead": ("run the work from the integration branch",
             "status claims brief adopt review gate land release waive unblock drop set-status"),
    "maintain": ("keep finished work honest", "drift verify stats gate-audit hooks"),
}
HIDDEN = ("hook",)  # run by the files `its hooks` writes, not by people


def _grouped(summaries: dict[str, str]) -> str:
    """The commands, by group, for the end of `its --help`."""
    width = max(map(len, summaries)) + 2
    blocks = []
    for group, (purpose, names) in GROUPS.items():
        lines = [f"{group}: {purpose}"]
        for name in names.split():
            lines.append(textwrap.fill(summaries[name], width=100, initial_indent=f"  {name:<{width}}",
                                       subsequent_indent=" " * (width + 2)))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\nrun `its <command> --help` for a command's options"


class Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors keep the JSON contract when `--json` is on the command line."""

    json_errors = False

    def error(self, message: str):  # type: ignore[override]
        if Parser.json_errors:
            render.emit(WorkError("usage", f"{self.prog}: {message}", EXIT_USAGE).to_dict())
            raise SystemExit(EXIT_USAGE)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    p = Parser(prog="its", usage="%(prog)s [-h] [--version] [-C DIR] <command> ...",
               description="idea-to-scaffold: planning package to agent-ready repo",
               formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-C", dest="chdir", metavar="DIR",
                   help="run as if started in DIR (like git -C); every command's folder is taken relative to it")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>", help=argparse.SUPPRESS, prog="its")
    summaries: dict[str, str] = {}

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        summaries[name] = summary
        return sub.add_parser(name, help=summary, description=summary)

    s = add("new", "create a blank planning package")
    s.add_argument("dir")
    s.add_argument(
        "--profile", choices=PROFILES, default="lite",
        help="lite (the default): idea, vision, requirements and backlog, with architecture, design and plan "
             "optional; full: every doc",
    )
    s.set_defaults(func=commands.cmd_new)

    s = add("import", "import a chat export (Markdown with file markers) into a package")
    s.add_argument("export", help="the exported chat .md file")
    s.add_argument("--into", required=True, help="package folder to write into")
    s.add_argument("--no-defaults", action="store_true", help="don't add default playbooks and ADR-0001")
    s.add_argument(
        "--overwrite", action="store_true",
        help="replace existing files that differ from the export (keeps each original as <name>.orig)",
    )
    s.set_defaults(func=commands.cmd_import)

    s = add("validate", "check a package (or scaffolded repo) against the spec")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--trace", action="store_true",
                   help="also list Must acceptance criteria that no test names (exit 1 if any)")
    s.add_argument("--sizing", action="store_true",
                   help="also warn about todo and proposed items too big or vague for one agent run (never fails)")
    s.add_argument("--json", action="store_true", help=f"print JSON (schema {SCHEMA}) instead of text")
    s.set_defaults(func=commands.cmd_validate)

    s = add("link", "cross-link IDs, refresh nav blocks, the planning index and the commands blocks")
    s.add_argument("dir", nargs="?", default=".", help="package folder or scaffolded repo (default: .)")
    s.add_argument("--check", action="store_true", help="don't write; exit 8 if anything is out of date (for CI)")
    s.add_argument("--json", action="store_true", help=f"print JSON (schema {SCHEMA}) instead of text")
    s.set_defaults(func=commands.cmd_link)

    dir_help = "package folder or scaffolded repo (default: .)"
    json_help = f"print JSON (schema {SCHEMA}) instead of text"
    fetch_help = "fetch the integration branch and claim branches from the remote first"
    allow = {"nargs": "?", "const": "", "default": None, "metavar": "REASON", "help": argparse.SUPPRESS}  # gone
    base_help = "integration branch to read readiness from (default: integration_branch in package.yaml, or main)"

    s = add("backlog", "list backlog items")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--status", choices=BACKLOG_STATUSES, help="only items with this status")
    s.add_argument("--ready", action="store_true", help="only items that can start now")
    s.add_argument("--json", action="store_true", help=json_help)
    s.add_argument("--write", action="store_true", help="write the backlog.json mirror next to the backlog")
    s.set_defaults(func=commands.cmd_backlog)

    s = add("next", "the next ready backlog item (exit 3 if nothing is ready)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--all", action="store_true", help="every ready item, in backlog order")
    s.add_argument(
        "--parallel", nargs="?", type=int, const=3, default=None, metavar="N",
        help="up to N ready items (default 3) whose Touches don't overlap each other or active claims",
    )
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_next)

    s = add("show", "where an ID is defined, its text, and the docs that mention it")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_show)

    s = add("start", "set a ready item in-progress (exit 4 if a dependency isn't done)")
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
    s.set_defaults(func=commands.cmd_start)

    s = add("status", "the lead's view: every claim's state (working, blocked, ready to land), "
                       "what can start next, what to release, proposals and drift")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_status)

    s = add("claims", "list claim branches (its/<ID>-...), local and remote, and which would conflict")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--base", help=base_help)
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_claims)

    s = add("brief", "print a self-contained task for an agent that may never run its "
                                     "(or put it on a GitHub issue)")
    s.add_argument("id", help="the item")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--retry", action="store_true", help="a brief for one more attempt: only what the last failed "
                                                        "attempt got wrong (up to gate.max_attempts)")
    s.add_argument("--github", action="store_true", help="create or update the item's GitHub issue with gh "
                                                         "(a retry is posted as a comment)")
    s.add_argument("--base", help="the integration branch to read the item and the rules from")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_brief)

    s = add("gate", "run the done gate on a branch or pull request with the integration branch's "
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
    s.set_defaults(func=commands.cmd_gate)

    s = add("land", "merge finished claim branches one at a time, testing each merged result")
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
    s.set_defaults(func=commands.cmd_land)

    s = add("propose", "propose a new backlog item, in its own file under planning/proposals/")
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
    s.set_defaults(func=commands.cmd_propose)

    s = add("promote", "schedule a proposed item: give a placeholder its ID and set it todo")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--force", action="store_true", help="renumber even when not on the integration branch")
    s.add_argument("--allow-incomplete", action="store_true",
                   help="schedule it even if Done when or Touches is empty")
    s.add_argument("--no-commit", dest="commit", action="store_false",
                   help="leave the change uncommitted (by default it is committed, so a worktree started next sees it)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_promote)

    s = add("block", "mark an item blocked and say why (also how an agent stops honestly)")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", required=True, help="what it is waiting for")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_block)

    s = add("unblock", "set a blocked item back to todo")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_unblock)

    s = add("drop", "drop an item (a proposal's file is deleted)")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", help="why it was dropped")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_drop)

    s = add("set-status", "set any item's status by hand")
    s.add_argument("id")
    s.add_argument("status", choices=BACKLOG_STATUSES)
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--reason", help="recorded for blocked and dropped")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_set_status)

    s = add("waive", "let one item through part of the gate: the lead, on the integration branch "
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
    s.set_defaults(func=commands.cmd_waive)

    s = add("release", "drop a claim: remove its worktree and delete its local branch")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--remote", action="store_true", help="also delete the claim's pushed branches on every remote")
    s.add_argument("--force", action="store_true", help="also if the worktree has changes or a branch isn't merged")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_release)

    s = add("adopt", "claim an item for a branch a vendor's agent made (claude/..., copilot/..., a "
                       "pull request), so its land gates and merges it")
    s.add_argument("ref", help="the branch (claude/x or origin/claude/x) or pull request (pr:12, or its URL)")
    s.add_argument("id", help="the item the branch implements")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--agent", help="who made the branch, for the run ledger (default: from its name, e.g. claude)")
    s.add_argument("--no-push", action="store_true",
                   help="keep the claim local (by default it is pushed to the remote, if there is one)")
    s.add_argument("--fetch", action="store_true", help=fetch_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_adopt)

    s = add("renumber", "give placeholder items (T-new-...) permanent IDs, after merging")
    s.add_argument("ids", nargs="*", metavar="ID", help="only these placeholders (default: all that aren't dropped)")
    s.add_argument("--dir", default=".", help=dir_help)
    s.add_argument("--dry-run", action="store_true", help="show the new IDs without changing anything")
    s.add_argument("--force", action="store_true", help="renumber even when not on the integration branch")
    s.add_argument("--no-commit", dest="commit", action="store_false",
                   help="leave the change uncommitted (by default it is committed, so a worktree started next sees it)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_renumber)

    s = add("done", "run an item's checks, then set it done (exit 5 if a check fails, 12 if refused)")
    s.add_argument("id", nargs="?", help="the item (default: the only in-progress item)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--no-verify", action="store_true", help="don't run Verify, test and lint first")
    s.add_argument("--check", action="store_true", help="run every check but leave the status alone (exit 5 if any fails, 12 if refused)")
    s.add_argument("--result", metavar="FILE",
                   help="act on a headless agent's result JSON: done runs the checks, blocked or needs_human blocks "
                        "it; in a claim checkout the work is then committed and the branch pushed")
    s.add_argument("--no-push", action="store_true", help="with --result: commit, but don't push the claim branch")
    s.add_argument("--allow-test-changes", **allow)
    s.add_argument("--allow-outside-touches", **allow)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_done)

    s = add("drift", "done items whose files changed after they were verified, and their requirements")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--check", action="store_true", help="exit 8 if anything drifted (for CI)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_drift)

    s = add("stats", "what the run ledger says: first-time pass rate, refusals, land results, cost")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_stats)

    s = add("gate-audit", "feed the done gate known cheats and report which it catches")
    s.add_argument("id", nargs="?", help="the item to audit as (default: the first item with Touches)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--no-run", action="store_true",
                   help="leave out the cheats that need the tests to run (os._exit in the item's source)")
    s.add_argument("--allow-warn", action="store_true",
                   help="exit 0 when every cheat is at least warned about, not only when every one is refused")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_gate_audit)

    s = add("verify", "re-run a done item's checks and refresh its Verified mark")
    s.add_argument("id", nargs="?", help="the item (or pass --drifted or --all)")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    which = s.add_mutually_exclusive_group()
    which.add_argument("--drifted", action="store_true", help="every item its drift reports")
    which.add_argument("--all", action="store_true", help="every done item that lists Touches")
    s.add_argument("--commit", action="store_true", help="commit the planning folder afterwards")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_verify)

    s = add("review", "print a review prompt with the item, its requirements and the diff")
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
    s.set_defaults(func=commands.cmd_review)

    s = add("hooks", "add an opt-in Stop hook for Claude Code and Codex that runs the done gate")
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--agent", choices=["claude", "codex"], help="only this agent (default: the package's agents)")
    s.add_argument("--json", action="store_true", help=json_help)
    s.set_defaults(func=commands.cmd_hooks)

    s = add("hook", "run a hook (what the files written by its hooks call)")
    s.add_argument("event", choices=["stop"])
    s.add_argument("dir", nargs="?", default=".", help=dir_help)
    s.add_argument("--headless", action="store_true",
                   help="an unattended run: ask only for passing checks; its done --result marks and commits")
    s.set_defaults(func=commands.cmd_hook)

    s = add("scaffold", "create the project repo from a package")
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
    s.set_defaults(func=commands.cmd_scaffold)
    p.epilog = _grouped(summaries)
    return p
