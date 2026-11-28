"""Phase 12: working with vendor dispatchers. A branch Claude, Copilot or Codex made lands through the gate."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from idea_to_scaffold.cli import main

from .test_landing import git_identity, remote, repo  # noqa: F401 - fixtures
from .test_loop import held_out
from .test_parallel import git, run_json, set_field

STORE = ("from pathlib import Path\n\ndef test_store_adds():\n"
         "    assert 'def add' in Path('src/streak/store.py').read_text()\n")


def vendor_branch(remote_repo: Path, tmp_path: Path, branch: str, files: dict[str, str]) -> str:
    """What a vendor's cloud agent does: clone, commit on a branch of its own name, push. Returns the tip."""
    clone = tmp_path / ("vendor-" + branch.replace("/", "-"))
    git(tmp_path, "clone", "-q", str(remote_repo), str(clone))
    git(clone, "switch", "-q", "-c", branch)
    for rel, body in files.items():
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", f"agent work on {branch}")
    git(clone, "push", "-q", "origin", branch)
    return git(clone, "rev-parse", "HEAD")


def test_land_keeps_the_candidate_that_passes_the_held_out_tests(repo: Path, remote: Path, tmp_path: Path,  # noqa: F811
                                                                 capsys):
    held_out(repo, {"T-2.1/test_store_held_out.py": STORE})
    git(repo, "push", "-q", "origin", "main")
    vendor_branch(remote, tmp_path, "claude/store", {"src/streak/store.py": "def remove():\n    return 0\n"})
    vendor_branch(remote, tmp_path, "copilot/store", {"src/streak/store.py": "def add():\n    return 1\n"})
    code, out = run_json(capsys, "adopt", "claude/store", "T-2.1", str(repo))
    assert code == 0 and out["created"] and out["pushed"] and out["candidate"]["agent"] == "claude", out
    code, out = run_json(capsys, "adopt", "origin/copilot/store", "T-2.1", str(repo))
    assert code == 0 and not out["created"] and [c["name"] for c in out["candidates"]] == ["claude/store",
                                                                                         "copilot/store"]
    code, status = run_json(capsys, "status", str(repo))
    assert status["ready_to_land"] == ["T-2.1"], status
    code, landed = run_json(capsys, "land", "--dir", str(repo), "--push", "--release")
    assert code == 7, landed  # the first candidate failed its held-out tests
    assert [(x["branch"], x["landed"]) for x in landed["landed"]] == [("copilot/store", True)]
    first = next(x for x in landed["skipped"] if x["branch"] == "claude/store")
    assert first["reason"] == "check_failed" and first["held_out_failed"] == ["T-2.1"], first
    assert "Adopted: copilot/store" in git(remote, "log", "-1", "--format=%B", "main")
    backlog = git(remote, "show", "main:docs/planning/07-backlog.md")
    block = backlog[backlog.index("### T-2.1 "):]
    assert "**Status:** done" in block.split("### T-2.2")[0]
    assert landed["released"] == ["T-2.1"] and git(remote, "branch", "--list", "its/*") == ""
    assert git(remote, "branch", "--list", "copilot/*").strip() == "copilot/store"  # the vendor's branch stays


def reply(tmp_path: Path, name: str, verdict: str) -> str:
    """A reviewer's answer: approve with every requirement met, or changes requested with a blocker."""
    reqs = ("FR-001", "FR-002", "NFR-002", "FR-003", "FR-004", "FR-005", "NFR-001", "NFR-003")
    ok = verdict == "approve"
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({
        "verdict": verdict, "requirements": [{"id": r, "status": "met", "evidence": "read it"} for r in reqs],
        "findings": [] if ok else [{"severity": "blocker", "file": "src/streak/store.py", "line": 1,
                                    "message": "add returns the wrong value"}]}))
    return str(path)


def test_each_candidate_gets_its_own_review_and_land_respects_it(repo: Path, remote: Path, tmp_path: Path,  # noqa: F811
                                                                  capsys):
    held_out(repo, {"T-2.1/test_store_held_out.py": STORE})
    git(repo, "push", "-q", "origin", "main")
    vendor_branch(remote, tmp_path, "claude/store", {"src/streak/store.py": "def add():\n    return 1\n"})
    vendor_branch(remote, tmp_path, "codex/store", {"src/streak/store.py": "def add():\n    return 2\n"})
    for ref in ("claude/store", "codex/store"):
        assert run_json(capsys, "adopt", ref, "T-2.1", str(repo))[0] == 0
    code, first = run_json(capsys, "review", "T-2.1", str(repo))  # an adopted item: its first candidate
    assert code == 0 and first["reviewed_branch"] == "claude/store" and "return 1" in first["diff"], first
    assert "--branch claude/store" in first["commands"]["claude"]
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", reply(tmp_path, "claude", "changes_requested"))
    assert code == 5 and out["reviewed_branch"] == "claude/store" and out["current"], out
    code, second = run_json(capsys, "review", "T-2.1", str(repo), "--branch", "origin/codex/store")
    assert code == 0 and second["reviewed_branch"] == "codex/store" and "return 2" in second["diff"]
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--branch", "codex/store",
                         "--reply", reply(tmp_path, "codex", "approve"))
    assert code == 0 and out["passed"] and out["current"], out
    code, status = run_json(capsys, "status", str(repo))
    claim = next(c for c in status["claims"] if c["id"] == "T-2.1")
    assert claim["state"] == "ready_to_land" and claim["attention"] == "ready_to_land", claim
    assert [(c["name"], c["review"]["label"]) for c in claim["candidates"]] == [
        ("claude/store", "changes requested"), ("codex/store", "approved")]
    capsys.readouterr()
    assert main(["status", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "- claude/store (claude)  1 commit(s)" in text and "review: changes requested (blocker src/streak/store.py)" in text
    code, landed = run_json(capsys, "land", "--dir", str(repo), "--push", "--release")
    assert code == 7 and [x["branch"] for x in landed["landed"]] == ["codex/store"], landed
    assert [(x["branch"], x["reason"]) for x in landed["skipped"]] == [("claude/store", "changes_requested")]
    message = git(remote, "log", "-1", "--format=%B", "main")
    assert "Adopted: codex/store" in message and "Reviewed: approved" in message


def test_risky_items_need_a_review_and_finished_work_caps_new_starts(repo: Path, remote: Path, tmp_path: Path,  # noqa: F811
                                                                     capsys):
    manifest = repo / "docs/planning/package.yaml"
    manifest.write_text(manifest.read_text().replace("\ngate:\n", "\ngate:\n  max_unreviewed: 1\n", 1))
    git(repo, "commit", "-qam", "a WIP limit")
    set_field(repo, "T-2.1", "Risk", "high")
    git(repo, "push", "-q", "origin", "main")
    code, out = run_json(capsys, "next", str(repo))
    assert code == 0 and out["warnings"] == []
    vendor_branch(remote, tmp_path, "copilot/store", {"src/streak/store.py": "def add():\n    return 1\n"})
    assert run_json(capsys, "adopt", "copilot/store", "T-2.1", str(repo))[0] == 0
    code, out = run_json(capsys, "next", str(repo))
    assert [(w["kind"], w["items"]) for w in out["warnings"]] == [("wip_limit", ["T-2.1"])], out
    code, out = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    assert code == 0 and out["warnings"][0]["kind"] == "wip_limit", out  # a warning: nothing is refused
    code, status = run_json(capsys, "status", str(repo))
    claim = next(c for c in status["claims"] if c["id"] == "T-2.1")
    assert claim["state"] == "not_reviewed" and status["attention"]["needs_you"] == ["T-2.1"], status["attention"]
    assert status["attention"]["running"] == ["T-2.2"] and status["claims"][0]["id"] == "T-2.1"
    assert any(w["kind"] == "wip_limit" for w in status["warnings"])
    code, landed = run_json(capsys, "land", "T-2.1", "--dir", str(repo))
    assert code == 7, landed
    assert [x["reason"] for x in landed["skipped"]] == ["not_reviewed"], landed
    run_json(capsys, "review", "T-2.1", str(repo))
    assert run_json(capsys, "review", "T-2.1", str(repo), "--reply", reply(tmp_path, "copilot", "approve"))[0] == 0
    code, landed = run_json(capsys, "land", "T-2.1", "--dir", str(repo))
    assert code == 0 and [x["branch"] for x in landed["landed"]] == ["copilot/store"], landed


def test_a_claim_with_no_commit_for_days_is_stale(repo: Path, capsys, monkeypatch):  # noqa: F811
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    from idea_to_scaffold import status as st
    later = datetime.now(timezone.utc) + timedelta(days=4)
    monkeypatch.setattr(st, "datetime", type("Later", (datetime,), {"now": classmethod(lambda cls, tz=None: later)}))
    code, status = run_json(capsys, "status", str(repo))
    claim = status["claims"][0]
    assert claim["attention"] == "stale" and claim["idle_days"] == 4 and status["attention"]["stale"] == ["T-2.1"]
    capsys.readouterr()
    assert main(["status", str(repo)]) == 0
    assert "stale (no commit for a while" in capsys.readouterr().out
