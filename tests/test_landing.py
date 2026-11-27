"""Phase 2: proposals, backlog commands, shared claims, conflict checks, start --next and the landing lane."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from idea_to_scaffold import claims as cl
from idea_to_scaffold.backlog import load_backlog
from idea_to_scaffold.cli import main
from idea_to_scaffold.linker import link_package
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import scaffold
from idea_to_scaffold.validate import validate

from .test_parallel import IDENTITY, git, run_json, set_field, status_in


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)


def py(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.fixture
def repo(plan: Path, tmp_path: Path) -> Path:
    """A scaffolded Streak repo on main whose test and lint commands are cheap, with T-1.1 done."""
    root = scaffold(plan, tmp_path / "out", git=True).path
    manifest = root / "docs/planning/package.yaml"
    text = manifest.read_text()
    text = text.replace('test: "uv run pytest"', f"test: {json.dumps(py('print(1)'))}")
    text = text.replace('lint: "uv run ruff check ."', f"lint: {json.dumps(py('print(2)'))}")
    manifest.write_text(text)
    git(root, "commit", "-qam", "cheap checks")
    set_field(root, "T-1.1", "Status", "done")
    set_field(root, "T-2.1", "Touches", "`src/streak/store.py`")
    set_field(root, "T-2.2", "Touches", "`src/streak/streak.py`")
    return root


@pytest.fixture
def remote(repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    return bare


def work_on(worktree: Path, ident: str, path: str, content: str, propose: str | None = None,
            *flags: str) -> None:
    """What an agent does in its worktree: change a file, maybe propose follow-up work, finish with its done."""
    target = worktree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if propose:
        assert main(["propose", propose, "--dir", str(worktree), "--satisfies", "FR-001"]) == 0
    assert main(["done", ident, str(worktree), "--no-verify", *flags]) == 0
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", f"feat: {ident}")


# --- proposals and backlog commands ---------------------------------------------------------------------------

def test_propose_writes_one_file_per_item_and_renumber_moves_it_into_the_backlog(plan: Path, capsys):
    code, out = run_json(capsys, "propose", "Export to JSON", "--dir", str(plan), "--satisfies", "FR-001",
                         "--milestone", "M2", "--touches", "src/streak/export.py")
    assert code == 0 and out["id"] == "T-new-export-to-json"
    assert out["file"] == "planning/proposals/T-new-export-to-json.md"
    assert run_json(capsys, "propose", "Export to JSON", "--dir", str(plan))[1]["error"]["code"] == "exists"
    assert validate(Package.load(plan)).errors == []  # proposals aren't planning docs
    assert link_package(Package.load(plan), check=True) == []
    _, out = run_json(capsys, "backlog", str(plan))
    assert [p["id"] for p in out["proposals"]] == ["T-new-export-to-json"]
    _, out = run_json(capsys, "show", "T-new-export-to-json", str(plan))
    assert out["proposal"] and out["item"]["touches"] == ["src/streak/export.py"]

    code, out = run_json(capsys, "renumber", "--dir", str(plan))
    assert code == 0 and out["renumbered"] == [{"from": "T-new-export-to-json", "to": "T-2.4"}]
    assert out["proposals_moved"] == ["planning/proposals/T-new-export-to-json.md"]
    pkg = Package.load(plan)
    assert pkg.proposal_files() == []
    item = next(i for i in load_backlog(pkg) if i.id == "T-2.4")
    assert item.title == "Export to JSON" and item.status == "proposed" and item.touches == ["src/streak/export.py"]
    assert validate(pkg).errors == [] and link_package(pkg, check=True) == []


def test_promote_numbers_and_schedules_a_proposal(plan: Path, capsys):
    run_json(capsys, "propose", "Undo", "--dir", str(plan), "--milestone", "M1")
    code, out = run_json(capsys, "promote", "T-new-undo", str(plan))
    assert code == 0 and out["id"] == "T-1.2" and out["renumbered_from"] == "T-new-undo" and out["status"] == "todo"
    code, out = run_json(capsys, "promote", "T-9.1", str(plan))  # already in the backlog as proposed
    assert code == 0 and out["status"] == "todo" and out["renumbered_from"] is None
    assert run_json(capsys, "promote", "T-9.1", str(plan))[1]["error"]["code"] == "bad_status"


def test_block_unblock_drop_and_set_status(plan: Path, capsys):
    code, out = run_json(capsys, "block", "T-2.1", str(plan), "--reason", "waiting on the schema decision")
    assert code == 0 and out["status"] == "blocked" and status_in(plan, "T-2.1") == "blocked"
    backlog = (plan / "planning/07-backlog.md").read_text()
    assert "- **Blocked:** waiting on the schema decision" in backlog
    _, out = run_json(capsys, "next", str(plan))
    assert out["blocked"] == [{"id": "T-2.1", "title": "Store", "reason": "waiting on the schema decision"}]

    code, out = run_json(capsys, "unblock", "T-2.1", str(plan))
    assert code == 0 and out["status"] == "todo" and "**Blocked:**" not in (plan / "planning/07-backlog.md").read_text()

    code, out = run_json(capsys, "drop", "T-9.1", str(plan), "--reason", "out of scope")
    assert code == 0 and status_in(plan, "T-9.1") == "dropped"
    assert "- **Dropped:** out of scope" in (plan / "planning/07-backlog.md").read_text()

    run_json(capsys, "propose", "Idea", "--dir", str(plan))
    code, out = run_json(capsys, "drop", "T-new-idea", str(plan))
    assert code == 0 and out["deleted_file"] and not (plan / "planning/proposals/T-new-idea.md").exists()

    assert run_json(capsys, "set-status", "T-2.2", "in-progress", str(plan))[1]["status"] == "in-progress"
    run_json(capsys, "propose", "Other", "--dir", str(plan))
    code, out = run_json(capsys, "set-status", "T-new-other", "todo", str(plan))
    assert code == 1 and out["error"]["code"] == "placeholder"
    assert validate(Package.load(plan)).errors == []


# --- shared claims --------------------------------------------------------------------------------------------

def test_start_pushes_the_claim_so_other_machines_see_it(repo: Path, remote: Path, tmp_path: Path, capsys):
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    assert code == 0 and out["pushed"] and out["remote"] == "origin"
    branch = out["claim"]["branch"]
    assert git(remote, "branch", "--list", branch)

    other = tmp_path / "laptop"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    code, out = run_json(capsys, "next", str(other), "--fetch")
    assert out["item"]["id"] == "T-2.2"  # T-2.1 is claimed elsewhere
    code, out = run_json(capsys, "start", "T-2.1", str(other), "--branch")
    assert code == 1 and out["error"]["code"] == "claimed"


def test_a_race_between_machines_is_settled_by_the_remote(repo: Path, remote: Path, tmp_path: Path, capsys):
    other = tmp_path / "laptop"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    assert run_json(capsys, "start", "T-2.1", str(repo), "--branch")[0] == 0  # pushed first
    code, out = run_json(capsys, "start", "T-2.1", str(other), "--branch")  # didn't fetch: only the push can tell
    assert code == 1 and out["error"]["code"] == "claimed" and "undone" in out["error"]["message"]
    assert git(other, "branch", "--show-current") == "main"
    assert all(c.remote for c in cl.list_claims(other))  # the local claim is gone


def test_no_push_keeps_a_claim_local(repo: Path, remote: Path, capsys):
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--branch", "--no-push")
    assert code == 0 and out["pushed"] is False
    assert git(remote, "branch", "--list", out["claim"]["branch"]) == ""


def test_start_next_picks_and_claims_in_one_step(repo: Path, capsys):
    code, out = run_json(capsys, "start", "--next", str(repo), "--worktree")
    assert code == 0 and out["id"] == "T-2.1"
    code, out = run_json(capsys, "start", "--next", str(repo), "--worktree")
    assert code == 0 and out["id"] == "T-2.2"
    code, out = run_json(capsys, "start", "--next", str(repo), "--worktree")
    assert code == 3 and out["error"]["code"] == "nothing_ready" and out["error"]["reason"] == "waiting"


def test_claims_report_conflicts_before_anyone_merges(repo: Path, capsys):
    _, a = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    _, b = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    work_on(Path(a["claim"]["worktree"]), "T-2.1", "src/streak/shared.py", "a = 1\n", None, "--allow-outside-touches")
    work_on(Path(b["claim"]["worktree"]), "T-2.2", "src/streak/shared.py", "b = 2\n", None,
            "--allow-outside-touches")  # Touches lied
    _, out = run_json(capsys, "claims", str(repo))
    by_id = {c["id"]: c for c in out["claims"]}
    assert by_id["T-2.1"]["conflicts_with"] == [{"id": "T-2.2", "files": ["src/streak/shared.py"]}]
    assert by_id["T-2.1"]["ahead"] == 1 and by_id["T-2.1"]["conflicts_with_base"] == []


# --- the landing lane -----------------------------------------------------------------------------------------

def test_two_branches_that_both_propose_items_land_without_conflicts(repo: Path, remote: Path, capsys):
    """The phase 2 gate."""
    _, a = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    _, b = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    work_on(Path(a["claim"]["worktree"]), "T-2.1", "src/streak/store.py", "STORE = {}\n", propose="Store backups")
    work_on(Path(b["claim"]["worktree"]), "T-2.2", "src/streak/streak.py", "def streak(): ...\n",
            propose="Weekly streaks")

    code, out = run_json(capsys, "land", "--dir", str(repo), "--push", "--release")
    assert code == 0, out
    assert [x["id"] for x in out["landed"]] == ["T-2.1", "T-2.2"] and out["skipped"] == []
    assert all(c["passed"] for x in out["landed"] for c in x["checks"])
    assert sorted(out["proposals"]) == ["docs/planning/proposals/T-new-store-backups.md",
                                        "docs/planning/proposals/T-new-weekly-streaks.md"]
    assert out["pushed"] and out["released"] == ["T-2.1", "T-2.2"] and cl.list_claims(repo) == []
    assert status_in(repo, "T-2.1") == "done" and status_in(repo, "T-2.2") == "done"
    assert git(repo, "status", "--porcelain") == ""
    assert git(remote, "rev-parse", "main") == git(repo, "rev-parse", "main")

    code, out = run_json(capsys, "renumber", "--dir", str(repo))
    assert code == 0 and [r["to"] for r in out["renumbered"]] == ["T-9.2", "T-9.3"]
    pkg = Package.load(repo)
    assert validate(pkg).errors == [] and link_package(pkg, check=True) == []


def test_land_undoes_a_merge_whose_checks_fail(repo: Path, capsys):
    _, a = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    worktree = Path(a["claim"]["worktree"])
    set_field(repo, "T-2.1", "Verify", f"`{py('import sys; sys.exit(4)')}`")  # main's Verify fails once merged
    work_on(worktree, "T-2.1", "src/streak/store.py", "x\n", None, "--allow-test-changes")
    before = git(repo, "rev-parse", "HEAD")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and out["landed"] == []
    assert out["skipped"][0]["reason"] == "check_failed" and out["skipped"][0]["checks"][0]["exit_code"] == 4
    assert git(repo, "rev-parse", "HEAD") == before and git(repo, "status", "--porcelain") == ""


def test_land_refuses_real_conflicts_and_lands_the_rest(repo: Path, capsys):
    _, a = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    _, b = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    work_on(Path(a["claim"]["worktree"]), "T-2.1", "src/streak/shared.py", "a = 1\n", None, "--allow-outside-touches")
    work_on(Path(b["claim"]["worktree"]), "T-2.2", "src/streak/shared.py", "b = 2\n", None, "--allow-outside-touches")
    code, out = run_json(capsys, "land", "--dir", str(repo), "--allow-outside-touches")
    assert code == 7 and [x["id"] for x in out["landed"]] == ["T-2.1"]
    skipped = out["skipped"][0]
    assert skipped["id"] == "T-2.2" and skipped["reason"] == "conflict" and skipped["files"] == ["src/streak/shared.py"]
    assert git(repo, "status", "--porcelain") == ""


def test_land_waits_for_dependencies_and_needs_done(repo: Path, capsys):
    _, a = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    worktree = Path(a["claim"]["worktree"])
    (worktree / "notes.txt").write_text("wip")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "wip")
    code, out = run_json(capsys, "land", "T-2.1", "--dir", str(repo))
    assert code == 0 and out["skipped"][0]["reason"] == "not_done"
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 3  # nothing is ready to land
    code, out = run_json(capsys, "land", "T-2.1", "--dir", str(repo), "--dry-run")
    assert out["skipped"][0]["reason"] == "not_done"


def test_land_runs_on_the_integration_branch_only(repo: Path, capsys):
    git(repo, "switch", "-q", "-c", "feature")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 1 and out["error"]["code"] == "not_integration_branch"


def test_land_regenerates_the_planning_index_when_only_it_conflicts(repo: Path, capsys):
    _, a = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    _, b = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    for worktree, ident, fr, title, where in ((Path(a["claim"]["worktree"]), "T-2.1", "FR-101", "Backups", "end"),
                                              (Path(b["claim"]["worktree"]), "T-2.2", "FR-102", "Weekly", "### FR-002")):
        req = worktree / "docs/planning/03-requirements.md"
        text = req.read_text()
        block = f"### {fr} — {title}\n**Feature:** F-01 · **Priority:** Could\n\n"
        req.write_text(text + "\n" + block if where == "end" else text.replace(where, block + where, 1))
        assert main(["link", str(worktree)]) == 0
        work_on(worktree, ident, {"T-2.1": "src/streak/store.py", "T-2.2": "src/streak/streak.py"}[ident], "x = 1\n")
    _, claims = run_json(capsys, "claims", str(repo))
    files = {f for c in claims["claims"] for other in c["conflicts_with"] for f in other["files"]}
    assert files == {"docs/planning/README.md"}  # only the generated index collides
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1", "T-2.2"], out
    assert git(repo, "status", "--porcelain") == ""
    pkg = Package.load(repo)
    assert link_package(pkg, check=True) == [] and validate(pkg).errors == []
    index = (repo / "docs/planning/README.md").read_text()
    assert "FR-101" in index and "FR-102" in index


def test_readiness_uses_a_fetched_main_that_is_ahead_of_local_main(repo: Path, remote: Path, tmp_path: Path, capsys):
    other = tmp_path / "laptop"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    set_field(other, "T-2.1", "Status", "done")
    git(other, "push", "-q", "origin", "main")
    git(repo, "switch", "-q", "-c", "feature")
    _, out = run_json(capsys, "next", str(repo), "--all")
    assert "T-2.3" not in [i["id"] for i in out["ready"]]
    _, out = run_json(capsys, "next", str(repo), "--all", "--fetch")
    assert out["source"] == "origin/main"
