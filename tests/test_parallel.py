"""Parallel work: claim branches and worktrees, readiness from the integration branch, Touches, renumbering."""
from __future__ import annotations

import json
import subprocess
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

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def run_json(capsys, *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def status_in(root: Path, ident: str) -> str:
    return next(i.status for i in load_backlog(Package.load(root)) if i.id == ident)


def set_field(root: Path, ident: str, name: str, value: str) -> None:
    """Add or replace a field on an item, then commit."""
    path = root / "docs/planning/07-backlog.md"
    text = path.read_text()
    head = text.index(f"### {ident}")
    end = text.find("\n### ", head + 1)
    end = len(text) if end == -1 else end
    section = text[head:end]
    lines = [ln for ln in section.split("\n") if not ln.startswith(f"- **{name}:**")]
    lines.insert(1, f"- **{name}:** {value}")
    path.write_text(text[:head] + "\n".join(lines) + text[end:])
    git(root, "commit", "-qam", f"set {name} on {ident}")


@pytest.fixture
def repo(plan: Path, tmp_path: Path) -> Path:
    """A scaffolded Streak repo on main, with its initial commit."""
    return scaffold(plan, tmp_path / "out", git=True).path


# --- claims ---------------------------------------------------------------------------------------------------

def test_worktree_claim_is_exclusive_and_hidden_from_next(repo: Path, capsys):
    code, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree", "--agent", "claude")
    assert code == 0 and out["claim"]["branch"] == "its/T-1.1-project-setup-verified"
    worktree = Path(out["claim"]["worktree"])
    assert worktree == repo.parent / "streak-T-1.1" and (worktree / "AGENTS.md").is_file()
    assert git(worktree, "branch", "--show-current") == "its/T-1.1-project-setup-verified"
    line = out["commands"]["claude"]
    assert line.startswith("cd ") and " && claude " in line and "Read AGENTS.md" in line
    assert str(worktree) in out["commands"]["codex"]
    assert status_in(repo, "T-1.1") == "todo"  # the branch is the claim; the backlog is untouched
    assert git(repo, "status", "--porcelain") == ""

    code, out = run_json(capsys, "start", "T-1.1", str(repo), "--branch")
    assert code == 1 and out["error"]["code"] == "claimed"
    code, out = run_json(capsys, "start", "T-1.1", str(repo))  # a plain start respects claims too
    assert code == 1 and out["error"]["code"] == "claimed"

    code, out = run_json(capsys, "next", str(repo))
    assert code == 3 and out["item"] is None
    assert [c["id"] for c in out["claimed"]] == ["T-1.1"] and out["claimed"][0]["agent"] == "claude"

    code, out = run_json(capsys, "claims", str(repo))
    assert [(c["id"], c["finished"], c["remote_only"]) for c in out["claims"]] == [("T-1.1", False, False)]


def test_done_means_merged(repo: Path, capsys):
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    worktree = Path(out["claim"]["worktree"])
    (worktree / "src/streak/setup_check.py").write_text("ok = True\n")
    code, out = run_json(capsys, "done", str(worktree), "--no-verify")  # the item comes from the branch name
    assert code == 0 and out["id"] == "T-1.1" and out["merge_needed"]
    git(worktree, "commit", "-qam", "feat: T-1.1")

    _, out = run_json(capsys, "next", str(worktree))  # read from main, where T-1.1 isn't done yet
    assert out["source"] == "main" and out["item"] is None
    assert {w["id"] for w in out["waiting"]} >= {"T-2.1", "T-2.2"}

    git(repo, "merge", "-q", "--no-edit", "its/T-1.1-project-setup-verified")
    _, out = run_json(capsys, "next", str(repo), "--all")
    assert out["source"] == "working tree" and [i["id"] for i in out["ready"]] == ["T-2.1", "T-2.2"]
    assert out["claimed"] == []  # finished claims don't hold their item
    _, out = run_json(capsys, "claims", str(repo))
    assert out["claims"][0]["finished"] is True


def test_branch_claim_checks_out_here_and_readiness_uses_main(repo: Path, capsys):
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--branch")
    assert code == 4 and out["error"]["blocked_by"] == ["T-1.1"]
    code, out = run_json(capsys, "start", "T-1.1", str(repo), "--branch")
    assert code == 0 and out["claim"]["worktree"] is None
    assert git(repo, "branch", "--show-current") == "its/T-1.1-project-setup-verified"
    # Marking T-1.1 done on this branch doesn't make T-2.1 startable: it isn't merged.
    (repo / "src/streak/setup_check.py").write_text("ok = True\n")
    assert main(["done", str(repo), "--no-verify"]) == 0
    git(repo, "commit", "-qam", "feat: T-1.1")
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--branch")
    assert code == 4 and "not done on main" in out["error"]["message"]


def test_remote_claim_branches_count(repo: Path, tmp_path: Path, capsys):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(other))
    git(other, "switch", "-q", "-c", "its/T-1.1-from-elsewhere")
    git(other, "push", "-q", "origin", "its/T-1.1-from-elsewhere")
    git(repo, "fetch", "-q", "origin")
    code, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    assert code == 1 and out["error"]["claim"]["branch"] == "origin/its/T-1.1-from-elsewhere"
    assert out["error"]["claim"]["remote_only"] is True


def test_claims_need_git(mini: Path, capsys):
    code, out = run_json(capsys, "start", "T-1.1", str(mini), "--branch")
    assert code == 1 and out["error"]["code"] == "no_base"
    assert run_json(capsys, "claims", str(mini))[1]["error"]["code"] == "not_git"


def test_release_removes_the_worktree_and_branch(repo: Path, capsys):
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    worktree = Path(out["claim"]["worktree"])
    (worktree / "notes.txt").write_text("wip")
    git(worktree, "add", "notes.txt")
    git(worktree, "commit", "-qm", "wip")
    code, out = run_json(capsys, "release", "T-1.1", str(repo))
    assert code == 1 and out["error"]["code"] == "git_error"  # unmerged work needs --force
    code, out = run_json(capsys, "release", "T-1.1", str(repo), "--force")
    assert code == 0 and out["deleted_branches"] == ["its/T-1.1-project-setup-verified"]
    assert not worktree.exists() and cl.list_claims(repo) == []
    assert run_json(capsys, "next", str(repo))[1]["item"]["id"] == "T-1.1"


def test_the_lock_is_exclusive_and_freed_when_its_holder_dies(repo: Path):
    script = (
        "import sys, time; from pathlib import Path; from idea_to_scaffold import claims as cl\n"
        "with cl.claims_lock(Path(sys.argv[1])):\n    print('held', flush=True); time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", script, str(repo)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(cl.GitError, match="still running"):
            with cl.claims_lock(repo, wait=0.2):
                pass
    finally:
        holder.kill()
        holder.wait()
    with cl.claims_lock(repo, wait=5):  # the OS released it when the holder died: no stale-lock rule needed
        with cl.claims_lock(repo, wait=0):  # and it is re-entrant within one thread
            pass


# --- Touches and parallel picks -------------------------------------------------------------------------------

@pytest.mark.parametrize("a, b, overlap", [
    ("src/store.py", "src/store.py", True),
    ("src/store/", "src/store/db.py", True),
    ("./src/store", "src/store/db.py", True),
    ("src/a.py", "src/b.py", False),
    ("src/a.py", "src/a.pyc", False),
    ("src/*.py", "src/b.py", True),
    ("src/*.py", "tests/test_b.py", False),
    ("src/a*", "src/b*", False),
    ("**/*.md", "docs/x.md", True),
])
def test_paths_overlap(a, b, overlap):
    assert cl.paths_overlap(a, b) is overlap and cl.paths_overlap(b, a) is overlap


def test_no_touches_means_it_could_touch_anything():
    assert cl.touches_overlap([], ["src/a.py"]) and cl.touches_overlap(["src/a.py"], [])
    assert not cl.touches_overlap(["src/a.py"], ["tests/"])


def test_parallel_keeps_overlapping_items_apart(repo: Path, capsys):
    set_field(repo, "T-1.1", "Status", "done")
    set_field(repo, "T-2.1", "Touches", "`src/streak/store.py`, tests/test_store.py")
    set_field(repo, "T-2.2", "Touches", "`src/streak/streak.py`")
    code, out = run_json(capsys, "next", str(repo), "--parallel")
    assert code == 0 and [i["id"] for i in out["parallel"]] == ["T-2.1", "T-2.2"]

    set_field(repo, "T-2.2", "Touches", "`src/streak/`")
    _, out = run_json(capsys, "next", str(repo), "--parallel")
    assert [i["id"] for i in out["parallel"]] == ["T-2.1"]
    assert out["held_back"] == [{"id": "T-2.2", "overlaps": ["T-2.1"], "touches_listed": True}]

    run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    _, out = run_json(capsys, "next", str(repo), "--parallel", "2")
    assert out["parallel"] == [] and out["held_back"][0]["overlaps"] == ["T-2.1"]
    assert main(["next", str(repo), "--parallel"]) == 3


# --- renumbering ----------------------------------------------------------------------------------------------

def test_renumber_gives_placeholders_permanent_ids(plan: Path, capsys):
    backlog = plan / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text() + (
        "\n### T-new-plant-aliases — Plant aliases\n- **Milestone:** M2\n- **Status:** proposed\n"
        "\n### T-new-export — Export\n- **Milestone:** —\n- **Status:** proposed\n- **Depends on:** T-new-plant-aliases\n"
        "\n### T-new-old — Old idea\n- **Status:** dropped\n"
    ))
    link_package(Package.load(plan))
    before = backlog.read_text()
    code, out = run_json(capsys, "renumber", "--dir", str(plan), "--dry-run")
    assert out["renumbered"] == [{"from": "T-new-plant-aliases", "to": "T-2.4"}, {"from": "T-new-export", "to": "T-9.2"}]
    assert backlog.read_text() == before

    code, out = run_json(capsys, "renumber", "--dir", str(plan))
    assert code == 0 and out["files"] == ["planning/07-backlog.md"]
    text = backlog.read_text()
    assert "### T-2.4 — Plant aliases" in text and "T-new-plant-aliases" not in text
    assert "**Depends on:** [T-2.4](#t-24--plant-aliases)" in text
    assert "### T-new-old — Old idea" in text  # dropped placeholders are left alone
    pkg = Package.load(plan)
    assert validate(pkg).errors == [] and link_package(pkg, check=True) == []

    assert run_json(capsys, "renumber", "T-2.4", "--dir", str(plan))[1]["error"]["code"] == "not_placeholder"
    assert run_json(capsys, "renumber", "T-new-nope", "--dir", str(plan))[1]["error"]["code"] == "not_found"


# --- only confirmed-apart items run in parallel ---------------------------------------------------------------

def test_start_refuses_an_item_that_may_overlap_running_work(repo: Path, capsys):
    set_field(repo, "T-1.1", "Status", "done")
    assert run_json(capsys, "start", "T-2.1", str(repo), "--worktree")[0] == 0

    code, out = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")  # neither lists Touches
    assert code == 6 and out["error"]["code"] == "overlap"
    assert out["error"]["overlaps"] == ["T-2.1"] and out["error"]["touches_listed"] is False
    code, out = run_json(capsys, "start", "T-2.2", str(repo))  # a plain start is held to the same rule
    assert code == 6 and status_in(repo, "T-2.2") == "todo"

    set_field(repo, "T-2.2", "Touches", "`src/streak/streak.py`")
    code, out = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")  # T-2.1 still lists none
    assert code == 6 and out["error"]["touches_listed"] is True

    set_field(repo, "T-2.1", "Touches", "`src/streak/store.py`")
    code, out = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    assert code == 0 and not out["forced"]


def test_in_progress_items_count_as_running(repo: Path, capsys):
    set_field(repo, "T-1.1", "Status", "done")
    set_field(repo, "T-2.1", "Status", "in-progress")
    _, out = run_json(capsys, "next", str(repo), "--parallel")
    assert out["parallel"] == [] and out["held_back"][0]["overlaps"] == ["T-2.1"]
    code, out = run_json(capsys, "start", "T-2.2", str(repo), "--allow-overlap")
    assert code == 0 and out["forced"] and status_in(repo, "T-2.2") == "in-progress"


# --- releasing pushed branches --------------------------------------------------------------------------------

def test_release_remote_deletes_pushed_branches(repo: Path, tmp_path: Path, capsys):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--worktree")
    worktree, branch = Path(out["claim"]["worktree"]), out["claim"]["branch"]
    (worktree / "src/streak/setup_check.py").write_text("ok = True\n")
    assert main(["done", str(worktree), "--no-verify"]) == 0
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "feat: T-1.1")
    git(worktree, "push", "-q", "origin", branch)

    code, out = run_json(capsys, "release", "T-1.1", str(repo), "--remote")
    assert code == 1 and "isn't merged" in out["error"]["message"]
    assert git(remote, "branch", "--list", branch)  # nothing was deleted

    git(repo, "merge", "-q", "--no-edit", branch)
    code, out = run_json(capsys, "release", "T-1.1", str(repo), "--remote")
    assert code == 0 and out["deleted_remote_branches"] == [f"origin/{branch}"] and out["remote_branches"] == []
    assert out["deleted_branches"] == [branch] and not worktree.exists()
    assert git(remote, "branch", "--list", branch) == ""


def test_release_without_remote_leaves_pushed_branches(repo: Path, tmp_path: Path, capsys):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    _, out = run_json(capsys, "start", "T-1.1", str(repo), "--branch")
    branch = out["claim"]["branch"]
    git(repo, "push", "-q", "origin", branch)
    git(repo, "switch", "-q", "main")
    code, out = run_json(capsys, "release", "T-1.1", str(repo))
    assert code == 0 and out["remote_branches"] == [f"origin/{branch}"]
    code, out = run_json(capsys, "release", "T-1.1", str(repo))  # only the pushed branch is left
    assert code == 1 and "add --remote" in out["error"]["message"]
    assert run_json(capsys, "release", "T-1.1", str(repo), "--remote")[0] == 0  # merged (no new commits): no --force
