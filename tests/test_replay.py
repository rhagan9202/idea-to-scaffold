"""Phase 11's gate: the round 4 run (Flagpole), replayed end to end with no manual git steps.

Two machines (the lead's checkout and a second clone) share a bare remote. The lead writes held-out tests for two
items and pushes them, which is authoring, like any edit on main. From then on nothing runs git by hand:

1. each machine claims an item with `its start --next --worktree`; the second, which hasn't fetched, loses the race
   for the first item and takes the next one;
2. a headless agent runs in each worktree through the launcher script `its start` wrote, and the script's last step,
   `its done --result`, checks the work, commits it and pushes the claim branch;
3. the lead sees both from main with `its status --fetch`, reviews both, and lands them with
   `its land --fetch --push --release --require-review`: each merged result runs its own held-out tests only;
4. `its drift --check` is clean, and the second machine, now behind, won't claim the landed work and releases its
   finished claim.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

import idea_to_scaffold

from .test_landing import git_identity, remote, repo  # noqa: F401 - fixtures
from .test_loop import NEEDS, held_out, launch
from .test_parallel import git, run_json

AGENT = '''
import json, re, sys
from pathlib import Path
prompt = sys.argv[sys.argv.index("-p") + 1]
ident = re.search(r"implement-task.md for (T-[0-9.]+)", prompt).group(1).rstrip(".")
path = {"T-2.1": "src/streak/store.py", "T-2.2": "src/streak/streak.py"}[ident]
Path(path).write_text("def run():\\n    return 1\\n", encoding="utf-8")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.75, "num_turns": 6,
                  "structured_output": {"id": ident, "status": "done", "summary": f"add {path}",
                                        "commit_message": f"feat: add {path}"}}))
'''
REQUIREMENTS = ("FR-001", "FR-002", "NFR-002", "FR-003", "FR-004", "FR-005", "NFR-001", "NFR-003")


@pytest.fixture
def agents(tmp_path: Path, monkeypatch) -> None:
    """`claude` and `its` on PATH: a fake agent that implements whichever item its prompt names."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT, encoding="utf-8")
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


def approve(tmp_path: Path, name: str) -> str:
    reply = tmp_path / f"{name}.json"
    reply.write_text(json.dumps({"verdict": "approve", "findings": [],
                                 "requirements": [{"id": r, "status": "met", "evidence": "test"} for r in REQUIREMENTS]}))
    return str(reply)


def tip_status(remote_repo: Path, branch: str, ident: str) -> str | None:
    text = git(remote_repo, "show", f"refs/heads/{branch}:docs/planning/07-backlog.md")
    block = text[text.index(f"### {ident} "):]
    match = re.search(r"\*\*Status:\*\* ([\w-]+)", block)
    return match.group(1) if match else None


def test_the_round_4_run_replays_with_no_manual_git_steps(repo: Path, remote: Path, tmp_path: Path,  # noqa: F811
                                                          agents, capsys):
    held_out(repo, {"T-2.1/test_store_held_out.py": NEEDS.format(name="store"),
                    "T-2.2/test_streak_held_out.py": NEEDS.format(name="streak")})
    git(repo, "push", "-q", "origin", "main")  # the lead's authoring, like any edit on main
    machine2 = tmp_path / "machine2"
    git(tmp_path, "clone", "-q", str(remote), str(machine2))
    # -- no git by hand from here on --------------------------------------------------------------------------------

    # 1. both machines claim; machine2 hasn't fetched, loses T-2.1 to the lead, and takes T-2.2
    code, first = run_json(capsys, "start", "--next", str(repo), "--worktree", "--agent", "claude")
    assert code == 0 and first["id"] == "T-2.1" and first["pushed"], first
    code, second = run_json(capsys, "start", "--next", str(machine2), "--worktree", "--agent", "claude")
    assert code == 0 and second["id"] == "T-2.2" and second["lost_to_others"] == ["T-2.1"], second
    for out in (first, second):
        assert not (Path(out["claim"]["worktree"]) / "tests/acceptance").exists()  # held out from the agents

    # 2. the headless runs: each ends committed and pushed by its done --result
    for out in (first, second):
        proc = launch(out["commands"]["headless"])
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "committed" in proc.stdout and "pushed" in proc.stdout, proc.stdout
        assert tip_status(remote, out["claim"]["branch"], out["id"]) == "done"

    # 3. the lead sees both from main, reviews them, and lands them
    code, status = run_json(capsys, "status", str(repo), "--fetch")
    assert code == 0 and sorted(status["ready_to_land"]) == ["T-2.1", "T-2.2"], status
    for out in (first, second):
        run_json(capsys, "review", out["id"], str(repo))
        code, verdict = run_json(capsys, "review", out["id"], str(repo), "--reply", approve(tmp_path, out["id"]))
        assert code == 0 and verdict["passed"] and verdict["current"], verdict
    code, landed = run_json(capsys, "land", "--dir", str(repo), "--fetch", "--push", "--release", "--require-review")
    assert code == 0 and [x["id"] for x in landed["landed"]] == ["T-2.1", "T-2.2"], landed
    assert landed["pushed"] and sorted(landed["released"]) == ["T-2.1", "T-2.2"]
    one, two = landed["landed"]
    assert one["held_out"]["included"] == ["T-2.1"] and one["held_out"]["left_out"] == ["T-2.2"]
    assert two["held_out"]["included"] == ["T-2.1", "T-2.2"]
    assert all(any(c["name"] == "acceptance" and c["passed"] for c in x["checks"]) for x in (one, two))
    assert "Reviewed: approved" in git(remote, "log", "-1", "--format=%B", "main")
    assert git(remote, "branch", "--list", "its/*") == ""  # the claim branches are gone from the remote

    # 4. nothing drifted; the stale machine won't claim landed work, and releases its finished claim
    code, drift = run_json(capsys, "drift", str(repo), "--check")
    assert code == 0 and drift["drifted"] == [], drift
    code, out = run_json(capsys, "start", "T-2.1", str(machine2), "--worktree", "--fetch")
    assert code == 1 and out["error"]["code"] == "bad_status", out
    code, out = run_json(capsys, "release", "T-2.2", str(machine2))
    assert code == 0 and out["removed_worktree"], out
    code, status = run_json(capsys, "status", str(repo))
    assert status["claims"] == [] and status["to_release"] == [] and status["counts"]["done"] >= 3
    _, stats = run_json(capsys, "stats", str(repo))
    assert stats["reviews"] == {"approved": 2} and stats["land"] == {"landed": 2}
