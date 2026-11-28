"""Phase 12: working with vendor dispatchers. A branch Claude, Copilot or Codex made lands through the gate."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from idea_to_scaffold.cli import main

from .test_landing import git_identity, py, remote, repo  # noqa: F401 - fixtures
from .test_loop import held_out
from .test_parallel import git, run_json, set_field

STORE = ("from pathlib import Path\n\ndef test_store_adds():\n"
         "    assert 'def add' in Path('src/streak/store.py').read_text()\n")


def vendor_branch(remote_repo: Path, tmp_path: Path, branch: str, files: dict[str, str],
                  message: str | None = None) -> str:
    """What a vendor's cloud agent does: clone, commit on a branch of its own name, push. Returns the tip."""
    clone = tmp_path / ("vendor-" + branch.replace("/", "-"))
    git(tmp_path, "clone", "-q", str(remote_repo), str(clone))
    git(clone, "switch", "-q", "-c", branch)
    for rel, body in files.items():
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", message or f"agent work on {branch}")
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


ADD_IS_2 = py("import sys; sys.path.insert(0, 'src'); from streak.store import add; sys.exit(add() != 2)")


def reply(tmp_path: Path, name: str, verdict: str, repro: str | None = ADD_IS_2) -> str:
    """A reviewer's answer: approve with every requirement met, or changes requested with a blocker whose repro
    is `repro` (a command that fails when add() doesn't return 2)."""
    reqs = ("FR-001", "FR-002", "NFR-002", "FR-003", "FR-004", "FR-005", "NFR-001", "NFR-003")
    ok = verdict == "approve"
    path = tmp_path / f"{name}.json"
    blocker = {"severity": "blocker", "file": "src/streak/store.py", "line": 1, "message": "add returns the wrong value"}
    path.write_text(json.dumps({
        "verdict": verdict, "requirements": [{"id": r, "status": "met", "evidence": "read it"} for r in reqs],
        "findings": [] if ok else [{**blocker, **({"repro": {"command": repro}} if repro else {})}]}))
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
    assert out["outcome"] == "changes_requested" and [e["reproduced"] for e in out["evidence"]] == [True], out
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


def test_gate_passes_a_good_pull_request_and_refuses_a_bad_one(repo: Path, remote: Path, tmp_path: Path,  # noqa: F811
                                                               capsys, monkeypatch):
    held_out(repo, {"T-2.1/test_store_held_out.py": STORE})
    git(repo, "push", "-q", "origin", "main")
    vendor_branch(remote, tmp_path, "copilot/T-2.1-store", {"src/streak/store.py": "def add():\n    return 1\n"})
    vendor_branch(remote, tmp_path, "codex/store", {"src/streak/store.py": "def remove():\n    return 1\n"},
                  message="[T-2.1] a store that can't add")
    vendor_branch(remote, tmp_path, "claude/T-2.1-cheat", {"src/streak/store.py": "def remove():\n    return 1\n",
                                                           "tests/acceptance/T-2.1/test_store_held_out.py": "def test_x():\n    pass\n"})
    vendor_branch(remote, tmp_path, "copilot/readme", {"NOTES.txt": "hello\n"})
    git(repo, "fetch", "-q", "origin")
    before = git(repo, "status", "--porcelain")

    code, out = run_json(capsys, "gate", "origin/copilot/T-2.1-store", str(repo))
    assert code == 0 and out["passed"] and out["id"] == "T-2.1" and out["found_by"] == "branch_name", out
    assert any(c["name"] == "acceptance" and c["passed"] for c in out["checks"]) and out["held_out"]["included"] == ["T-2.1"]
    code, out = run_json(capsys, "gate", "origin/codex/store", str(repo))
    assert code == 5 and out["found_by"] == "commit_tags" and out["reason"] == "check_failed", out
    assert out["held_out_failed"] == ["T-2.1"]
    code, out = run_json(capsys, "gate", "origin/claude/T-2.1-cheat", str(repo))
    assert code == 5 and [f["kind"] for f in out["findings"]] == ["acceptance_changed"], out
    assert out["checks"] == []  # refused before anything ran
    code, out = run_json(capsys, "gate", "origin/copilot/readme", str(repo))
    assert code == 0 and out["id"] is None and out["reason"] == "no_item", out
    assert run_json(capsys, "gate", "origin/copilot/readme", str(repo), "--require-item")[0] == 5
    assert run_json(capsys, "gate", "origin/nope", str(repo))[1]["error"]["code"] == "no_branch"
    assert git(repo, "status", "--porcelain") == before  # the checkout is untouched

    # in a pull request's CI: a detached checkout of the pull request, the base only on the remote
    ci = tmp_path / "ci"
    git(tmp_path, "clone", "-q", str(remote), str(ci))
    git(ci, "checkout", "-q", "--detach", "origin/copilot/T-2.1-store")
    git(ci, "branch", "-q", "-D", "main")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_HEAD_REF", "copilot/T-2.1-store")
    capsys.readouterr()
    assert main(["gate", str(ci)]) == 0
    text = capsys.readouterr().out
    assert "gate T-2.1 (branch name) on copilot/T-2.1-store against origin/main: passed" in text, text


def test_a_blocking_claim_that_doesnt_reproduce_is_downgraded(repo: Path, remote: Path, tmp_path: Path,  # noqa: F811
                                                               capsys):
    git(repo, "push", "-q", "origin", "main")
    vendor_branch(remote, tmp_path, "codex/store", {"src/streak/store.py": "def add():\n    return 2\n"})
    assert run_json(capsys, "adopt", "codex/store", "T-2.1", str(repo))[0] == 0
    run_json(capsys, "review", "T-2.1", str(repo))
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", reply(tmp_path, "a", "changes_requested"))
    assert code == 5 and out["outcome"] == "changes_requested"  # a major still blocks at the default cut-off
    assert out["findings"][0]["severity"] == "major" and out["findings"][0]["downgraded_from"] == "blocker"
    assert out["evidence"][0]["why"] == "the command passed on the change"
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", reply(tmp_path, "b", "changes_requested"),
                         "--min-severity", "blocker")
    assert code == 5 and out["outcome"] == "inconclusive" and out["blocking"] == [], out

    unshown = tmp_path / "unshown.json"
    reqs = [{"id": r, "status": "met", "evidence": "read it"} for r in ("FR-002", "NFR-002")]
    unshown.write_text(json.dumps({"verdict": "changes_requested", "findings": [], "requirements": [
        {"id": "FR-001", "status": "not_met", "evidence": "I think add is wrong"}, *reqs]}))
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", str(unshown))
    assert code == 5 and out["outcome"] == "inconclusive" and out["requirements"][0]["status"] == "cant_tell", out
    assert out["evidence"][0]["why"] == "no repro given"
    shown = tmp_path / "shown.json"
    shown.write_text(json.dumps({"verdict": "changes_requested", "findings": [], "requirements": [
        {"id": "FR-001", "status": "not_met", "evidence": "no CLI", "repro": {
            "command": py("import sys; sys.path.insert(0, 'src'); from streak import cli"),
            "files": {"tests/test_cli_missing.py": "def test_cli():\n    import streak.cli\n"}}}, *reqs]}))
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", str(shown))
    assert code == 5 and out["outcome"] == "changes_requested" and out["requirements"][0]["status"] == "not_met"
    assert not (repo / "tests/test_cli_missing.py").exists()  # the repro's files went in a throwaway worktree

    run_json(capsys, "review", "T-2.1", str(repo))
    assert run_json(capsys, "review", "T-2.1", str(repo), "--reply", str(unshown))[1]["outcome"] == "inconclusive"
    code, landed = run_json(capsys, "land", "--dir", str(repo))  # an inconclusive review doesn't hold it back
    assert code == 0 and [x["branch"] for x in landed["landed"]] == ["codex/store"], landed


STREAK = ("from pathlib import Path\n\ndef test_streak_counts():\n"
          "    assert 'def streak' in Path('src/streak/streak.py').read_text()\n")


def test_branches_from_claude_copilot_and_codex_land_through_the_gate(repo: Path, remote: Path,  # noqa: F811
                                                                       tmp_path: Path, capsys):
    """The phase 12 gate: three vendors' agents, none of which runs its, push work; its gate and land hold every
    one to the same rules, and only what passes lands."""
    held_out(repo, {"T-2.1/test_store_held_out.py": STORE, "T-2.2/test_streak_held_out.py": STREAK})
    git(repo, "push", "-q", "origin", "main")
    for ident in ("T-2.1", "T-2.2"):
        assert run_json(capsys, "brief", ident, str(repo))[0] == 0  # what each vendor's agent is given
    vendor_branch(remote, tmp_path, "claude/T-2.1-store", {"src/streak/store.py": "def remove():\n    return 0\n"})
    tip = vendor_branch(remote, tmp_path, "copilot/fix-store", {"src/streak/store.py": "def add():\n    return 1\n"},
                        message="[T-2.1] add to the store")
    git(remote, "update-ref", "refs/pull/5/head", tip)  # a pull request from a fork: no branch here, only the PR
    git(remote, "update-ref", "-d", "refs/heads/copilot/fix-store")
    vendor_branch(remote, tmp_path, "codex/T-2.2-streak", {"src/streak/streak.py": "def streak():\n    return 3\n"})
    git(repo, "fetch", "-q", "origin")

    # in each pull request's CI
    assert run_json(capsys, "gate", "origin/claude/T-2.1-store", str(repo))[0] == 5
    assert run_json(capsys, "gate", "origin/codex/T-2.2-streak", str(repo))[0] == 0
    # the lead takes them in
    for ref, ident, *agent in (("claude/T-2.1-store", "T-2.1"), ("pr:5", "T-2.1", "--agent", "copilot"),
                               ("codex/T-2.2-streak", "T-2.2")):
        code, out = run_json(capsys, "adopt", ref, ident, str(repo), *agent)
        assert code == 0, out
    code, out = run_json(capsys, "gate", "refs/its/pull/5", str(repo), "--id", "T-2.1")
    assert code == 0 and out["passed"], out
    code, status = run_json(capsys, "status", str(repo))
    assert sorted(status["ready_to_land"]) == ["T-2.1", "T-2.2"], status
    agents = {c["name"]: c["agent"] for c in status["claims"][0]["candidates"]}
    assert agents == {"claude/T-2.1-store": "claude", "pr:5": "copilot"}  # a PR's agent is named at adopt

    code, landed = run_json(capsys, "land", "--dir", str(repo), "--push", "--release")
    assert code == 7, landed  # Claude's branch failed T-2.1's held-out test
    assert [(x["id"], x["branch"]) for x in landed["landed"]] == [("T-2.1", "pr:5"), ("T-2.2", "codex/T-2.2-streak")]
    assert [(x["branch"], x["reason"]) for x in landed["skipped"]] == [("claude/T-2.1-store", "check_failed")]
    log = git(remote, "log", "--format=%B", "-2", "main")
    assert "Adopted: pr:5" in log and "Adopted: codex/T-2.2-streak" in log
    backlog = git(remote, "show", "main:docs/planning/07-backlog.md")
    for ident in ("T-2.1", "T-2.2"):
        block = backlog[backlog.index(f"### {ident} "):].split("\n### ")[0]
        assert "**Status:** done" in block, block
    assert git(repo, "for-each-ref", "refs/its/") == ""  # the fetched pull request went with the claim
    _, stats = run_json(capsys, "stats", str(repo))
    assert stats["by_agent"]["claude"]["first_try"] == 0 and stats["by_agent"]["codex"]["first_try"] == 1
