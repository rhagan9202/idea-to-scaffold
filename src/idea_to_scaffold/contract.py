"""What `its --json` promises the agents and scripts that read it: the schema number, the exit codes, and every error
code with the exit code it comes with.

Section 16 of spec/PACKAGE_SPEC.md shows the error codes and the exit codes as two tables generated from this
module: `python -m idea_to_scaffold.contract spec/PACKAGE_SPEC.md` rewrites them between their markers, and
`--check` fails when they are out of date. tests/test_contract.py checks that the spec matches, and that every code
the source raises is registered here with the exit codes it is raised with. So a new error code starts here.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOTHING_READY = 3
EXIT_BLOCKED = 4
EXIT_CHECK_FAILED = 5
EXIT_OVERLAP = 6
EXIT_NOT_LANDED = 7
EXIT_DRIFT = 8
EXIT_COMPLETE = 9        # `next`: nothing left, every item is done
EXIT_AUDIT_MISSED = 10   # gate-audit: the gate missed a seeded cheat

EXIT_CODES: dict[int, str] = {
    EXIT_OK: "ok",
    EXIT_ERROR: "error",
    EXIT_USAGE: "bad command-line usage",
    EXIT_NOTHING_READY: "`next`, `start --next`, `land`: nothing is ready (with `--parallel`: nothing can start "
                        "alongside the claims)",
    EXIT_BLOCKED: "`start`: a dependency isn't done",
    EXIT_CHECK_FAILED: "`done`: a check or the done gate failed, or a headless run's launch settings were changed; "
                       "the status is unchanged",
    EXIT_OVERLAP: "`start`: the item may change the same files as one already running",
    EXIT_NOT_LANDED: "`land`: a branch could not land (it conflicted, failed the done gate or its checks, isn't done, "
                     "has no commits, or the base moved), or the integration branch has commits nothing vouches for "
                     "(`base_untrusted`); branches only waiting on dependencies don't count",
    EXIT_DRIFT: "`drift --check`: a done item's files changed after it was verified",
    EXIT_COMPLETE: "`next`, `start --next`: every item is done",
    EXIT_AUDIT_MISSED: "`gate-audit`: a seeded cheat was missed, or only warned about (unless `--allow-warn`)",
}


@dataclass(frozen=True)
class Code:
    """One error code: what it means, the exit codes it comes with (the usual one first), and the fields its error
    object has besides `code`, `message` and the item's `id`."""
    meaning: str
    exits: tuple[int, ...] = (EXIT_ERROR,)
    fields: tuple[str, ...] = ()


_REFUSAL = "`done`: the done gate refused the branch's "

ERRORS: dict[str, Code] = {
    "already_done": Code("`done`: the item is already done"),
    "already_started": Code("`start`: the item is already in progress"),
    "audit_missed": Code("`gate-audit`: the gate missed a seeded cheat, or only warned about one (unless "
                         "`--allow-warn`)", (EXIT_AUDIT_MISSED,), ("missed", "warned")),
    "bad_agent": Code("`hooks`: no Stop hook is known for that agent"),
    "bad_backlog": Code("the backlog can't be read or changed as asked"),
    "bad_hooks_file": Code("`hooks`: the agent's settings file isn't a JSON object"),
    "bad_name": Code("`propose`: a placeholder name is lowercase letters, digits and hyphens", fields=("name",)),
    "bad_proposal": Code("`promote`, `renumber`: a proposal file isn't well formed", fields=("problems",)),
    "bad_result": Code("`done --result`: the result file can't be read, isn't JSON or doesn't match the schema",
                       fields=("result_file",)),
    "bad_review": Code("`review --reply`: the reply doesn't match the reply schema", fields=("problems",)),
    "bad_status": Code("the status isn't a backlog status, or the item's status doesn't allow the command",
                       fields=("status",)),
    "bad_title": Code("`propose`: a proposal needs a title"),
    "base_untrusted": Code("`land`: the integration branch has commits that `its` didn't record and that aren't on "
                           "the remote; a person looks, then `land --accept-base` trusts them",
                           (EXIT_NOT_LANDED,), ("integration_branch", "commits")),
    "behind_remote": Code("`start` on the integration branch: it is behind its remote copy, so its backlog is "
                          "stale; pull first", fields=("branch", "base", "behind")),
    "blocked": Code("`start`: a dependency isn't done", (EXIT_BLOCKED,), ("blocked_by",)),
    "check_failed": Code("`done`: a check failed", (EXIT_CHECK_FAILED,), ("checks",)),
    "claimed": Code("`start`: another branch holds the item's claim", fields=("claim",)),
    "dependency_changes": Code(_REFUSAL + "dependency changes (`dependency_added` enforced); the lead waives them "
                               "with `its waive <ID> --dependencies`", (EXIT_CHECK_FAILED,), ("findings", "warnings")),
    "dirty": Code("`land`, `waive`: uncommitted or staged changes are in the way"),
    "diverged": Code("`land --fetch`: the integration branch and its remote copy have diverged"),
    "duplicate_id": Code("an ID is defined more than once in the backlog", fields=("ids",)),
    "exists": Code("`propose`: that proposal exists already"),
    "fetch_failed": Code("`--fetch`: fetching the remote failed", fields=("remote",)),
    "gate_error": Code("`done`, `review`: the gate couldn't compare the branch, so it refused (exit 1 from `review`)",
                       (EXIT_CHECK_FAILED, EXIT_ERROR)),
    "git_error": Code("a git command failed"),
    "incomplete": Code("`promote`: the item has no Done when or Touches", fields=("missing",)),
    "internal_error": Code("a bug in `its`; the message says what to report", fields=("version",)),
    "launch_settings_changed": Code("`done --result`: the headless launch files were changed during the run, so it "
                                    "may have had no Stop hook or sandbox; nothing was marked", (EXIT_CHECK_FAILED,),
                                    ("files", "result_file")),
    "link_outdated": Code("`link --check`: generated links, the index or the commands blocks are out of date",
                          fields=("changed",)),
    "locked": Code("another `its` command holds the package lock"),
    "no_base": Code("`start --claim`, `gate-audit`: there is no integration branch"),
    "no_changes": Code("`done`: the branch changes nothing outside the planning folder, so nothing shows the item "
                       "was implemented", (EXIT_CHECK_FAILED,), ("findings", "warnings")),
    "no_fork_point": Code("`done` on the integration branch: no start was recorded for the item, so the gate can't "
                          "tell which commits are its work; `its start <ID> --force` records one", (EXIT_CHECK_FAILED,)),
    "no_package": Code("no planning package was found"),
    "no_reason": Code("`block`, `waive`: `--reason` is missing"),
    "no_result": Code("`done --result`: the agent's run ended without a result; nothing was marked",
                      fields=("result_file",)),
    "no_status": Code("`done`: the item has no Status field"),
    "no_waiver_group": Code("`waive`: name what is waived (`--test-changes`, `--outside-touches`, `--dependencies`)"),
    "not_found": Code("no such ID, or a file `its` was given can't be read", fields=("mentioned_in",)),
    "not_git": Code("the command needs a git repository"),
    "not_integration_branch": Code("`land`, `renumber`: run it on the integration branch",
                                   fields=("branch", "integration_branch")),
    "not_on_base": Code("`waive`: run it in the main checkout, on the integration branch",
                        fields=("branch", "integration_branch")),
    "not_verified": Code("`verify --drifted`, `verify --all`: some items failed their checks again; the rest were "
                         "re-verified", (EXIT_CHECK_FAILED,), ("failed",)),
    "not_placeholder": Code("`renumber`: the ID is permanent already"),
    "not_utf8": Code("`link`: a planning file isn't valid UTF-8 text"),
    "nothing_ready": Code("`start --next`: nothing can start, and `reason` says why; exit 9 when every item is done",
                          (EXIT_NOTHING_READY, EXIT_COMPLETE), ("reason",)),
    "nothing_to_review": Code("`review`: the branch only changes the backlog"),
    "outside_touches": Code(_REFUSAL + "scope changes (files outside Touches, or Touches changed); the lead waives "
                            "them with `its waive <ID> --outside-touches`", (EXIT_CHECK_FAILED,),
                            ("findings", "warnings")),
    "overlap": Code("`start`: the item may change the same files as one already running", (EXIT_OVERLAP,),
                    ("overlaps", "touches_listed", "overlapping_touches")),
    "placeholder": Code("the item has a placeholder ID; `its promote` gives it a permanent one"),
    "push_failed": Code("`land --push`: the branches landed locally, but the push failed", fields=("landed", "skipped")),
    "result_mismatch": Code("`done --result`: the result is for another item", fields=("result_id", "result_file")),
    "status_changed": Code("`done`: the item's status changed while its checks ran (a `block`, say), and that "
                           "change is kept", fields=("status", "was")),
    "test_changes": Code(_REFUSAL + "test changes (tests, test config, or how the item is verified); the lead waives "
                         "them with `its waive <ID> --test-changes`", (EXIT_CHECK_FAILED,), ("findings", "warnings")),
    "usage": Code("the command line is wrong", (EXIT_USAGE,)),
    "use_waive": Code("an `--allow-*` flag, which is gone: the lead waives with `its waive`", fields=("flag",)),
    "which_item": Code("`done`, `review`: name the item; none could be inferred", fields=("in_progress",)),
    "worktree_exists": Code("`start --claim`: the worktree's folder exists already", fields=("worktree",)),
}


@dataclass
class WorkError(Exception):
    """An error `its` reports as `{"schema", "ok": false, "error": {"code", "message", ...}}`, exiting `exit_code`.
    `code` is a key of ERRORS."""
    code: str
    message: str
    exit_code: int = EXIT_ERROR
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


# --- the spec's tables -----------------------------------------------------------------------------------------

def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def error_table() -> str:
    rows = ["| Code | Exit | Meaning | Also has |", "| ---- | ---- | ------- | -------- |"]
    for code in sorted(ERRORS):
        entry = ERRORS[code]
        fields = ", ".join(f"`{f}`" for f in entry.fields)
        rows.append(f"| `{code}` | {', '.join(map(str, entry.exits))} | {_cell(entry.meaning)} | {fields} |")
    return "\n".join(rows)


def exit_table() -> str:
    rows = ["| Code | Meaning |", "| ---- | ------- |"]
    rows += [f"| {code} | {_cell(meaning)} |" for code, meaning in sorted(EXIT_CODES.items())]
    return "\n".join(rows)


TABLES = {"errors": error_table, "exit-codes": exit_table}


def _region(name: str) -> re.Pattern:
    return re.compile(rf"(<!-- contract:{name} -->\n)(.*?)(\n<!-- /contract:{name} -->)", re.S)


def spec_regions(text: str) -> dict[str, str | None]:
    """The text between each table's markers in a spec, None where the markers are missing."""
    out = {}
    for name in TABLES:
        m = _region(name).search(text)
        out[name] = m.group(2) if m else None
    return out


def render_spec(text: str) -> str:
    """The spec with each table's region replaced by the table as this module has it."""
    for name, table in TABLES.items():
        m = _region(name).search(text)
        if not m:
            raise ValueError(f"the spec has no <!-- contract:{name} --> ... <!-- /contract:{name} --> markers")
        text = text[:m.start(2)] + table() + text[m.end(2):]
    return text


def main(argv: list[str] | None = None) -> int:
    """`python -m idea_to_scaffold.contract SPEC [--check]`: rewrite the spec's tables, or with --check, exit 1 if
    they are out of date."""
    args = sys.argv[1:] if argv is None else argv
    check = "--check" in args
    paths = [a for a in args if a != "--check"]
    if len(paths) != 1:
        print("usage: python -m idea_to_scaffold.contract SPEC [--check]", file=sys.stderr)
        return EXIT_USAGE
    path = Path(paths[0])
    text = path.read_text(encoding="utf-8")
    try:
        new = render_spec(text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if new == text:
        return EXIT_OK
    if check:
        print(f"{path}: the error and exit-code tables are out of date; run python -m idea_to_scaffold.contract "
              f"{path}", file=sys.stderr)
        return EXIT_ERROR
    path.write_text(new, encoding="utf-8", newline="\n")
    print(f"updated {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
