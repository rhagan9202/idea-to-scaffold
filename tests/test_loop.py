"""Phase 11: the workflow loops close. A headless run ends committed, pushed and in the ledger, whatever the agent
did; the launcher scripts always hand the result back."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import idea_to_scaffold
from idea_to_scaffold import ledger
from idea_to_scaffold.package import Package
from idea_to_scaffold.results import RESULT_SCHEMA

from .test_gate import TEST, claim  # noqa: F401 - fixtures
from .test_landing import git_identity, py, remote, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json


def envelope(result: dict | None, cost: float = 0.42, **extra) -> str:
    data = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost, "num_turns": 7,
            **extra}
    if result is not None:
        data["structured_output"] = result
    return json.dumps(data)


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "result.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def runs(root: Path, ident: str) -> list[dict]:
    return [e for e in ledger.read(Package.load(root)) if e["item"] == ident]


def status_at(root: Path, ref: str, ident: str) -> str:
    text = git(root, "show", f"{ref}:docs/planning/07-backlog.md")
    block = text[text.index(f"### {ident} "):]
    return block[block.index("**Status:**") + len("**Status:**"):].split("\n", 1)[0].strip()


# --- done --result commits and pushes -----------------------------------------------------------------------------

def test_a_done_result_is_committed_and_pushed(remote: Path, claim: Path, capsys):  # noqa: F811
    branch = git(claim, "branch", "--show-current")
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    path = write(claim.parent, envelope({"id": "T-2.1", "status": "done", "summary": "store",
                                         "commit_message": "feat(store): add the store"}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 0 and out["status"] == "done" and out["committed"] and out["pushed"], out
    assert git(claim, "status", "--porcelain") == ""
    message = git(claim, "log", "-1", "--format=%B")
    assert message.startswith("feat(store): add the store [T-2.1]") and "Refs: FR-" in message
    assert git(remote, "rev-parse", branch) == git(claim, "rev-parse", "HEAD")
    assert status_at(remote, branch, "T-2.1") == "done"
    assert "src/streak/store.py" in git(claim, "show", "--name-only", "--format=", "HEAD")  # new: the gate saw it


def test_a_blocked_result_is_committed_and_pushed(remote: Path, claim: Path, tmp_path: Path, capsys):  # noqa: F811
    branch = git(claim, "branch", "--show-current")
    (claim / "src/streak/store.py").write_text("half done\n")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "needs_human", "summary": "stuck",
                                     "reason": "the schema is undecided"}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 0 and out["status"] == "blocked" and out["committed"] and out["pushed"], out
    assert status_at(remote, branch, "T-2.1") == "blocked"
    assert "src/streak/store.py" in git(claim, "status", "--porcelain")  # the block, not the half-done work
    assert runs(claim, "T-2.1")[-1]["cost_usd"] == 0.42


def test_no_push_and_a_local_claim_stay_local(repo: Path, remote: Path, capsys, tmp_path: Path):  # noqa: F811
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree", "--no-push")
    wt = Path(out["claim"]["worktree"])
    (wt / "src/streak/store.py").write_text("def add(): ...\n")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}))
    code, out = run_json(capsys, "done", "T-2.1", str(wt), "--result", path)
    assert code == 0 and out["committed"] and not out["pushed"] and "never pushed" in out["push_skipped"]


def test_the_plain_flow_is_not_committed_for_the_person(repo: Path, tmp_path: Path, capsys):  # noqa: F811
    assert run_json(capsys, "start", "T-2.1", str(repo))[0] == 0
    git(repo, "commit", "-qam", "start T-2.1")
    (repo / "src/streak/store.py").write_text("def add(): ...\n")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}))
    code, out = run_json(capsys, "done", "T-2.1", str(repo), "--result", path)
    assert code == 0 and out["committed"] is None and "commit the work yourself" in out["commit_skipped"]


# --- D4: the agent already ran its done (and committed) -------------------------------------------------------------

def test_a_result_after_the_agent_marked_it_done_rechecks_and_records(remote: Path, claim: Path,  # noqa: F811
                                                                      tmp_path: Path, capsys):
    branch = git(claim, "branch", "--show-current")
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    assert run_json(capsys, "done", "T-2.1", str(claim))[0] == 0  # the agent followed the playbook
    git(claim, "add", "-A")
    git(claim, "commit", "-qm", "feat(store): add the store [T-2.1]")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}, cost=1.25))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 0 and out["already_done"] and out["status"] == "done" and out["pushed"], out
    assert git(remote, "rev-parse", branch) == git(claim, "rev-parse", "HEAD")
    assert any(e.get("cost_usd") == 1.25 and e["outcome"] == "passed" for e in runs(claim, "T-2.1"))


# --- B9: a result for another item ---------------------------------------------------------------------------------

def test_a_result_for_another_item_is_refused_without_an_id(claim: Path, tmp_path: Path, capsys):  # noqa: F811
    path = write(tmp_path, envelope({"id": "T-2.2", "status": "done", "summary": "x"}))
    code, out = run_json(capsys, "done", str(claim), "--result", path)
    assert code == 1 and out["error"]["code"] == "result_mismatch" and out["error"]["result_id"] == "T-2.2"
    assert runs(claim, "T-2.1")[-1]["outcome"] == "result_mismatch"  # the run's cost is still recorded
    assert not runs(claim, "T-2.2")


def test_a_result_file_written_by_powershell_is_read(claim: Path, tmp_path: Path, capsys):  # noqa: F811
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    path = tmp_path / "result.json"
    path.write_bytes(envelope({"id": "T-2.1", "status": "done", "summary": "store"}).encode("utf-16"))
    assert run_json(capsys, "done", "T-2.1", str(claim), "--result", str(path))[0] == 0


# --- the launcher scripts, run for real with a fake agent ----------------------------------------------------------

FAKE_AGENT = '''
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_AGENT_LOG"]).write_text(json.dumps(
    {"argv": sys.argv[1:], "cwd": os.getcwd(), "gh_token": os.environ.get("GH_TOKEN")}), encoding="utf-8")
if os.environ.get("FAKE_AGENT_MODE") == "fail":
    print(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                      "total_cost_usd": 5.01, "num_turns": 40}))
    sys.exit(1)
Path("src/streak/store.py").write_text("def add(): ...\\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 1.5,
                  "num_turns": 9, "structured_output": {"id": os.environ["FAKE_AGENT_ID"], "status": "done",
                                                        "summary": "added the store"}}))
'''


@pytest.fixture
def fake_agents(tmp_path: Path, monkeypatch) -> Path:
    """`claude` and `its` on PATH: a fake agent that logs what it was given, and this checkout's `its`. On Windows
    `claude` is a .cmd shim, as npm installs it, so the PowerShell launcher's quoting is tried for real."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent = tmp_path / "fake_agent.py"
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    src = str(Path(idea_to_scaffold.__file__).resolve().parents[1])
    its = "import sys; from idea_to_scaffold.cli import main; sys.exit(main())"
    if os.name == "nt":
        (bin_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{agent}" %*\r\n', encoding="utf-8")
        (bin_dir / "its.cmd").write_text(f'@set "PYTHONPATH={src}"\r\n@"{sys.executable}" -c "{its}" %*\r\n',
                                         encoding="utf-8")
    else:
        for name, line in (("claude", f"exec '{sys.executable}' '{agent}' \"$@\""),
                           ("its", f"PYTHONPATH='{src}' exec '{sys.executable}' -c '{its}' \"$@\"")):
            (bin_dir / name).write_text(f"#!/bin/sh\n{line}\n", encoding="utf-8")
            (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_AGENT_LOG", str(tmp_path / "agent.json"))
    monkeypatch.setenv("FAKE_AGENT_ID", "T-2.1")
    monkeypatch.setenv("GH_TOKEN", "a-secret-the-agent-must-not-see")
    return tmp_path / "agent.json"


def launch(headless: dict, agent: str = "claude") -> subprocess.CompletedProcess:
    script = headless["scripts"][agent]
    argv = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script["ps1"]] if os.name == "nt"
            else ["sh", script["sh"]])
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def test_the_launcher_runs_the_agent_then_hands_the_result_back(repo: Path, fake_agents: Path,  # noqa: F811
                                                               capsys):
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    proc = launch(out["commands"]["headless"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seen = json.loads(fake_agents.read_text(encoding="utf-8"))
    assert Path(seen["cwd"]).resolve() == wt.resolve() and seen["gh_token"] is None
    argv = seen["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == RESULT_SCHEMA  # its quotes survived the shell
    assert argv[argv.index("-p") + 1].startswith("Read AGENTS.md, then follow")
    assert status_at(wt, "HEAD", "T-2.1") == "done" and git(wt, "status", "--porcelain") == ""
    assert any(e.get("cost_usd") == 1.5 and e["outcome"] == "passed" for e in runs(repo, "T-2.1"))


def test_a_failed_run_still_reaches_the_ledger(repo: Path, fake_agents: Path, capsys, monkeypatch):  # noqa: F811
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    result = Path(out["commands"]["headless"]["result_file"])
    result.write_text(envelope({"id": "T-2.1", "status": "done", "summary": "a stale result"}), encoding="utf-8")
    monkeypatch.setenv("FAKE_AGENT_MODE", "fail")
    proc = launch(out["commands"]["headless"])
    assert proc.returncode == 1, proc.stdout + proc.stderr  # its done --result's code: no_result
    last = runs(repo, "T-2.1")[-1]
    assert last["outcome"] == "no_result" and last["cost_usd"] == 5.01 and last["agent_error"] == "error_max_budget_usd"
    wt = Path(out["claim"]["worktree"])
    assert status_at(wt, "HEAD", "T-2.1") == "todo"  # the stale result was removed before the run, not applied


# --- B1: a stale machine doesn't claim what another machine landed ---------------------------------------------------

def test_a_stale_machine_reads_the_remote_backlog_and_wont_start_landed_work(repo: Path, remote: Path,  # noqa: F811
                                                                           tmp_path: Path, capsys):
    stale = tmp_path / "machine2"
    git(tmp_path, "clone", "-q", str(remote), str(stale))
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    (wt / "src/streak/store.py").write_text("def add(): ...\n")
    assert run_json(capsys, "done", "T-2.1", str(wt), "--no-verify")[0] == 0
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "feat(store): add the store [T-2.1]")
    code, out = run_json(capsys, "land", "--dir", str(repo), "--push", "--release")
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1"], out

    code, out = run_json(capsys, "next", str(stale), "--fetch")
    assert code == 0 and out["item"]["id"] == "T-2.2" and out["source"] == "origin/main" and out["behind"] >= 1
    code, out = run_json(capsys, "start", "T-2.1", str(stale), "--worktree")
    assert code == 1 and out["error"]["code"] == "bad_status"  # done on origin/main, though todo here
    code, out = run_json(capsys, "start", "T-2.2", str(stale))
    assert code == 1 and out["error"]["code"] == "behind_remote" and out["error"]["behind"] >= 1
    code, out = run_json(capsys, "start", "--next", str(stale), "--worktree")
    assert code == 0 and out["id"] == "T-2.2", out
    git(stale, "pull", "-q", "--ff-only")
    _, out = run_json(capsys, "next", str(stale))
    assert out["source"] == "working tree" and not out["behind"]


def test_release_forgets_a_claim_branch_another_machine_deleted(repo: Path, remote: Path, tmp_path: Path,  # noqa: F811
                                                               capsys):
    other = tmp_path / "machine2"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    _, out = run_json(capsys, "start", "T-2.1", str(other), "--worktree")
    wt = Path(out["claim"]["worktree"])
    (wt / "src/streak/store.py").write_text("def add(): ...\n")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}))
    assert run_json(capsys, "done", "T-2.1", str(wt), "--result", path)[1]["pushed"]  # committed and pushed
    code, out = run_json(capsys, "land", "--dir", str(repo), "--fetch", "--push", "--release")
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1"], out
    git(other, "fetch", "-q", "origin")  # a plain fetch: the deleted claim branch's tracking ref stays
    assert any("its/T-2.1" in r for r in git(other, "branch", "-r").split())
    code, out = run_json(capsys, "release", "T-2.1", str(other))
    assert code == 0 and out["released"] and out["remote_branches"] == [], out
    assert not any("its/T-2.1" in r for r in git(other, "branch", "-r").split())
