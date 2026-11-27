"""Phase 1 hardening: races, the lock, strict Verify, the JSON contract, release and renumber safety, and edge cases."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from idea_to_scaffold import claims as cl
from idea_to_scaffold.backlog import find_cycles, parse_backlog
from idea_to_scaffold.cli import main
from idea_to_scaffold.linker import link_package
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import scaffold

from .test_parallel import IDENTITY, git, run_json, set_field, status_in

ITS = [sys.executable, "-m", "idea_to_scaffold.cli"]


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def repo(plan: Path, tmp_path: Path) -> Path:
    return scaffold(plan, tmp_path / "out", git=True).path


def its(*argv: str, cwd: Path | None = None) -> subprocess.Popen:
    return subprocess.Popen([*ITS, *argv, "--json"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def add_items(backlog: Path, count: int, touches: str | None = None) -> list[str]:
    ids = [f"T-5.{n}" for n in range(1, count + 1)]
    extra = f"- **Touches:** {touches}\n" if touches else ""
    backlog.write_text(backlog.read_text() + "".join(
        f"\n### {i} — Item {i}\n- **Milestone:** M1\n- **Status:** todo\n- **Satisfies:** FR-001\n"
        f"- **Depends on:** —\n{extra}" for i in ids))
    return ids


# --- races ----------------------------------------------------------------------------------------------------

def test_overlapping_worktree_starts_at_the_same_moment_never_both_win(repo: Path, tmp_path: Path):
    set_field(repo, "T-1.1", "Status", "done")
    set_field(repo, "T-2.1", "Touches", "`src/streak/shared.py`")
    set_field(repo, "T-2.2", "Touches", "`src/streak/shared.py`")
    for n in range(3):
        procs = [its("start", ident, str(repo), "--worktree", str(tmp_path / f"wt-{n}-{ident}"))
                 for ident in ("T-2.1", "T-2.2")]
        results = [(p.returncode, json.loads(out)) for p in procs for out in [p.communicate()[0]]]  # read as they
        # run: a child blocked on a full pipe never exits (Windows pipes hold only 4 KB)
        assert sorted(code for code, _ in results) == [0, 6], results
        winner = next(out["id"] for code, out in results if code == 0)
        assert main(["release", winner, str(repo), "--force"]) == 0


def test_concurrent_status_writes_lose_nothing(mini: Path):
    ids = add_items(mini / "planning/07-backlog.md", 12)
    procs = [its("start", ident, str(mini), "--allow-overlap") for ident in ids]
    codes = [p.communicate() and p.returncode for p in procs]
    assert codes == [0] * len(ids)
    assert all(status_in(mini, ident) == "in-progress" for ident in ids)


def test_the_lock_is_not_held_across_threads(repo: Path):
    import threading
    entered = threading.Event()
    with cl.claims_lock(repo):
        def other():
            try:
                with cl.claims_lock(repo, wait=0.1):
                    entered.set()
            except cl.GitError:
                pass
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert not entered.is_set()


# --- Verify ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, command", [
    ("`uv run pytest tests/test_store.py`", "uv run pytest tests/test_store.py"),
    ("`make check` # the fast suite", "make check"),
    ("manual: make sure `false` never runs", None),
    ("Manual — open the page", None),
    ("run `pytest` then look at the report", None),
    ("tests/test_store.py::test_add", None),
    ("`a` and `b`", None),
])
def test_only_a_lone_backticked_verify_is_a_command(value, command):
    item = parse_backlog(f"### T-1.1 — X\n- **Status:** todo\n- **Verify:** {value}\n")[0]
    assert item.verify_command == command


# --- the JSON contract ----------------------------------------------------------------------------------------

def test_duplicate_ids_give_a_json_error_not_a_traceback(mini: Path, capsys):
    backlog = mini / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text() + "\n### T-1.1 — Again\n- **Status:** todo\n")
    for argv in (("start", "T-1.1"), ("next",), ("done", "T-1.1")):
        code, out = run_json(capsys, argv[0], *argv[1:], str(mini))
        assert code == 1 and out["ok"] is False and out["error"]["code"] == "duplicate_id", argv


def test_done_refuses_an_item_with_no_status_before_running_checks(mini: Path, capsys):
    backlog = mini / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text().replace(
        "### T-1.1 — First\n- **Milestone:** M1\n- **Status:** todo\n", "### T-1.1 — First\n- **Milestone:** M1\n"))
    code, out = run_json(capsys, "done", "T-1.1", str(mini))
    assert code == 1 and out["error"]["code"] in ("bad_status", "no_status")


def test_unexpected_errors_still_return_json(mini: Path, capsys, monkeypatch):
    from idea_to_scaffold import cli
    monkeypatch.setattr(cli, "next_payload", lambda *a, **k: 1 / 0)
    code, out = run_json(capsys, "next", str(mini))
    assert code == 1 and out["error"]["code"] == "internal_error" and "ZeroDivisionError" in out["error"]["message"]


def test_check_output_that_is_not_utf8_is_reported(tmp_path: Path):
    from idea_to_scaffold.work import Check, run_check
    result = run_check(Check("verify", f"{sys.executable} -c \"import sys; sys.stdout.buffer.write(b'\\xff'); "
                                       "sys.exit(1)\""), tmp_path)
    assert result["exit_code"] == 1 and "�" in result["output"]


def test_a_long_dependency_chain_has_no_recursion_limit():
    n = 3000
    text = "".join(f"### T-1.{i} — X\n- **Status:** todo\n- **Depends on:** T-1.{i + 1}\n" for i in range(1, n))
    items = parse_backlog(text + f"### T-1.{n} — X\n- **Status:** todo\n- **Depends on:** T-1.1\n")
    cycles = find_cycles(items)
    assert len(cycles) == 1 and len(cycles[0]) == n + 1


# --- next and overlap -----------------------------------------------------------------------------------------

def test_next_only_offers_what_start_accepts(repo: Path, capsys):
    set_field(repo, "T-1.1", "Status", "done")
    set_field(repo, "T-2.2", "Touches", "`src/streak/streak.py`")
    assert run_json(capsys, "start", "T-2.2", str(repo), "--worktree")[0] == 0
    code, out = run_json(capsys, "next", str(repo))  # T-2.1 lists no Touches, so it can't start now
    assert code == 3 and out["item"] is None and out["reason"] == "overlap"

    set_field(repo, "T-2.1", "Touches", "`src/streak/store.py`")
    code, out = run_json(capsys, "next", str(repo))
    assert code == 0 and out["item"]["id"] == "T-2.1"
    assert run_json(capsys, "start", "T-2.1", str(repo), "--worktree")[0] == 0
    code, out = run_json(capsys, "next", str(repo))
    assert code == 3 and out["reason"] == "waiting"


def test_next_says_when_everything_is_done(mini: Path, capsys):
    backlog = mini / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text().replace("**Status:** todo", "**Status:** done"))
    code, out = run_json(capsys, "next", str(mini))
    assert code == 9 and out["reason"] == "complete"  # its own exit code, so a loop on `its next` ends


def test_overlap_errors_name_the_paths(repo: Path, capsys):
    set_field(repo, "T-1.1", "Status", "done")
    set_field(repo, "T-2.1", "Touches", "`src/streak/`")
    set_field(repo, "T-2.2", "Touches", "`src/streak/streak.py`")
    run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    code, out = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    assert code == 6 and "src/streak/" in out["error"]["message"]
    assert out["error"]["overlapping_touches"] == {"T-2.1": ["src/streak/"]}


def test_orphaned_and_placeholder_branches_do_not_block_parallel_work(repo: Path, capsys):
    git(repo, "branch", "its/T-7.7-old")
    git(repo, "branch", "its/T-new-export-csv")
    assert cl.item_of_branch("its/T-new-export-csv") is None
    _, out = run_json(capsys, "claims", str(repo))
    assert [(c["id"], c["orphaned"]) for c in out["claims"]] == [("T-7.7", True)]
    code, out = run_json(capsys, "next", str(repo), "--parallel")
    assert code == 0 and [i["id"] for i in out["parallel"]] == ["T-1.1"]
    assert run_json(capsys, "start", "T-1.1", str(repo), "--worktree")[0] == 0


def test_brace_globs_stay_whole():
    item = parse_backlog("### T-1.1 — X\n- **Touches:** `src/{a,b}.py`, docs/\n")[0]
    assert item.touches == ["src/{a,b}.py", "docs/"]
    assert cl.touches_overlap(item.touches, ["src/b.py"])


# --- start ------------------------------------------------------------------------------------------------------

def test_a_plain_start_off_main_reads_dependencies_from_main(repo: Path, capsys):
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "src/streak/setup_check.py").write_text("ok = True\n")
    assert main(["done", "T-1.1", str(repo), "--no-verify"]) == 0
    git(repo, "commit", "-qam", "T-1.1 done on a branch")
    code, out = run_json(capsys, "start", "T-2.1", str(repo))
    assert code == 4 and out["error"]["code"] == "blocked"


def test_starting_your_own_claim_again_is_a_no_op(repo: Path, capsys):
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    worktree = Path(out["claim"]["worktree"])
    code, out = run_json(capsys, "start", "T-1.1", str(worktree))
    assert code == 0 and out["claim_held_here"]


def test_claim_branches_do_not_track_main(repo: Path, capsys):
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=out["claim"]["worktree"],
                          capture_output=True, text=True)
    assert proc.returncode != 0  # no upstream: `git push` can never land in main by accident


# --- release ----------------------------------------------------------------------------------------------------

def test_a_refused_release_changes_nothing(repo: Path, capsys):
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    worktree = Path(out["claim"]["worktree"])
    (worktree / "notes.txt").write_text("wip")
    code, out = run_json(capsys, "release", "T-1.1", str(repo))
    assert code == 1 and "uncommitted" in out["error"]["message"]
    git(worktree, "add", "notes.txt")
    git(worktree, "commit", "-qm", "wip")
    code, out = run_json(capsys, "release", "T-1.1", str(repo))
    assert code == 1 and "isn't merged" in out["error"]["message"]
    assert worktree.is_dir() and [c.id for c in cl.list_claims(repo)] == ["T-1.1"]
    assert run_json(capsys, "claims", str(repo))[1]["claims"][0]["worktree"] == str(worktree)


def test_release_refuses_to_remove_the_folder_you_are_in(repo: Path, capsys, monkeypatch):
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    worktree = Path(out["claim"]["worktree"])
    monkeypatch.chdir(worktree)
    code, out = run_json(capsys, "release", "T-1.1", str(worktree), "--force")
    assert code == 1 and "inside" in out["error"]["message"] and worktree.is_dir()


def test_release_force_without_a_claim_is_a_no_op(repo: Path, capsys):
    code, out = run_json(capsys, "release", "T-1.1", str(repo), "--force")
    assert code == 0 and out["released"] is False
    assert run_json(capsys, "release", "T-1.1", str(repo))[0] == 1


def test_release_remote_fetches_first_and_never_deletes_unseen_work(repo: Path, tmp_path: Path, capsys):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--branch")
    branch = out["claim"]["branch"]
    git(repo, "push", "-q", "origin", branch)
    git(repo, "switch", "-q", "main")

    other = tmp_path / "other"  # someone else pushes work to the claim branch; our tracking ref doesn't know
    git(tmp_path, "clone", "-q", "-b", branch, str(remote), str(other))
    (other / "work.txt").write_text("theirs")
    git(other, "add", "work.txt")
    git(other, "commit", "-qm", "their work")
    git(other, "push", "-q", "origin", branch)

    code, out = run_json(capsys, "release", "T-1.1", str(repo), "--remote")
    assert code == 1 and "isn't merged" in out["error"]["message"]
    assert git(remote, "branch", "--list", branch)


# --- renumber ---------------------------------------------------------------------------------------------------

def test_renumber_refuses_duplicate_placeholders(plan: Path, capsys):
    backlog = plan / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text() + "\n### T-new-dup — A\n- **Status:** proposed\n"
                       "\n### T-new-dup — B\n- **Status:** proposed\n")
    code, out = run_json(capsys, "renumber", "--dir", str(plan))
    assert code == 1 and out["error"]["code"] == "duplicate_id" and out["error"]["ids"] == ["T-new-dup"]


def test_renumber_runs_on_the_integration_branch_only(repo: Path, capsys):
    backlog = repo / "docs/planning/07-backlog.md"
    backlog.write_text(backlog.read_text() + "\n### T-new-x — X\n- **Milestone:** M2\n- **Status:** proposed\n")
    git(repo, "switch", "-q", "-c", "feature")
    code, out = run_json(capsys, "renumber", "--dir", str(repo))
    assert code == 1 and out["error"]["code"] == "not_integration_branch"
    assert run_json(capsys, "renumber", "--dir", str(repo), "--dry-run")[0] == 0
    assert run_json(capsys, "renumber", "--dir", str(repo), "--force")[0] == 0


def test_renumber_skips_numbers_still_mentioned_elsewhere(plan: Path, capsys):
    plan_doc = plan / "planning/06-project-plan.md"
    plan_doc.write_text(plan_doc.read_text() + "\nT-2.4 was cut, see the notes.\n")
    backlog = plan / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text() + "\n### T-new-y — Y\n- **Milestone:** M2\n- **Status:** proposed\n")
    _, out = run_json(capsys, "renumber", "--dir", str(plan), "--dry-run")
    assert out["renumbered"] == [{"from": "T-new-y", "to": "T-2.5"}]


# --- smaller fixes ----------------------------------------------------------------------------------------------

def test_link_keeps_crlf_files_crlf(plan: Path):
    backlog = plan / "planning/07-backlog.md"
    raw = backlog.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    backlog.write_bytes(raw + b"\r\nSee FR-001.\r\n")
    link_package(Package.load(plan))
    data = backlog.read_bytes()
    assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n")
    assert link_package(Package.load(plan), check=True) == []


def test_commands_work_from_a_subfolder(repo: Path, capsys, monkeypatch):
    monkeypatch.chdir(repo / "docs" / "planning")
    code, out = run_json(capsys, "next")
    assert code == 0 and out["item"]["id"] == "T-1.1"


def test_done_finds_the_item_of_a_detached_worktree(repo: Path, capsys):
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    worktree = Path(out["claim"]["worktree"])
    git(worktree, "switch", "-q", "--detach")
    (worktree / "src/streak/setup_check.py").write_text("ok = True\n")
    code, out = run_json(capsys, "done", str(worktree), "--no-verify")
    assert code == 0 and out["id"] == "T-1.1"


def test_handoff_kickoff_has_no_shell_substitution(repo: Path):
    handoff = (repo / "HANDOFF.md").read_text()
    for line in handoff.splitlines():
        if line.startswith(("- **Codex CLI:**", "- **Claude Code:**")):
            command = line.split("`", 2)[1]
            assert "`" not in command and "$(" not in command, line


def test_launch_commands_include_argv():
    from idea_to_scaffold.work import launch_commands
    out = launch_commands("T-1.1", "/tmp/wt dir")
    assert out["argv"]["claude"][0] == "claude" and out["cwd"] == "/tmp/wt dir"
    if os.name == "nt":  # cmd.exe: /d changes drive too
        assert out["claude"].startswith('cd /d "/tmp/wt dir" && claude ')
    else:
        assert out["claude"].startswith("cd '/tmp/wt dir' && claude ")


def test_nul_is_not_a_person():
    """On Windows isatty is true of NUL, which is what an agent's tool gives a command as stdin."""
    from idea_to_scaffold.work import is_terminal
    with open(os.devnull, encoding="utf-8") as fh:
        assert not is_terminal(fh.fileno())
    assert not is_terminal(12345)  # not open at all
