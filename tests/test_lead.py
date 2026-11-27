"""Phase 6: the lead's view from one checkout, and the command-line contract around it."""
from __future__ import annotations

import json
from pathlib import Path

from idea_to_scaffold.cli import main

from .test_gate import TEST, claim  # noqa: F401 - fixtures
from .test_landing import git_identity, py, remote, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json


def commit_all(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


def claim_state(capsys, repo: Path, ident: str) -> dict:  # noqa: F811
    _, out = run_json(capsys, "status", str(repo))
    return next(c for c in out["claims"] if c["id"] == ident)


def test_status_shows_each_claim_from_main(claim: Path, repo: Path, capsys):  # noqa: F811
    assert claim_state(capsys, repo, "T-2.1")["state"] == "working"
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    commit_all(claim, "wip: store")
    c = claim_state(capsys, repo, "T-2.1")
    assert c["state"] == "working" and c["last_commit"]["subject"] == "wip: store" and c["ahead"] == 1

    assert main(["block", "T-2.1", str(claim), "--reason", "the schema is undecided", "--commit"]) == 0
    c = claim_state(capsys, repo, "T-2.1")
    assert c["state"] == "blocked" and c["blocked_reason"] == "the schema is undecided"
    _, out = run_json(capsys, "status", str(repo))
    assert {"id": "T-2.1", "title": None, "reason": "the schema is undecided", "branch": c["branch"]} in out["blocked"]

    assert main(["unblock", "T-2.1", str(claim), "--commit"]) == 0
    assert main(["done", "T-2.1", str(claim)]) == 0
    commit_all(claim, "feat: store")
    _, out = run_json(capsys, "status", str(repo))
    assert out["ready_to_land"] == ["T-2.1"] and out["claims"][0]["state"] == "ready_to_land"

    capsys.readouterr()
    assert main(["status", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "T-2.1    ready to land" in text and "ready to land: T-2.1  (its land)" in text

    assert run_json(capsys, "land", "--dir", str(repo))[0] == 0
    _, out = run_json(capsys, "status", str(repo))
    assert out["claims"] == [] and out["to_release"] == ["T-2.1"] and out["counts"]["done"] >= 1


def test_review_from_main_reads_the_claim_branch(claim: Path, repo: Path, capsys):  # noqa: F811
    code, out = run_json(capsys, "review", "T-2.1", str(repo))
    assert code == 1 and out["error"]["code"] == "nothing_to_review"
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    commit_all(claim, "feat: store")
    code, out = run_json(capsys, "review", "T-2.1", str(repo))
    assert code == 0 and [f["path"] for f in out["changed_files"]] == ["src/streak/store.py"]
    assert "def add()" in out["diff"]


def test_verify_all_rechecks_every_done_item(claim: Path, repo: Path, capsys):  # noqa: F811
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    assert main(["done", "T-2.1", str(claim)]) == 0
    commit_all(claim, "feat: store")
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 0
    code, out = run_json(capsys, "verify", "--all", str(repo))
    assert code == 0 and "T-2.1" in [v["id"] for v in out["verified"]] and out["failed"] == []
    code, out = run_json(capsys, "verify", "--drifted", str(repo))
    assert code == 0 and out["verified"] == []  # nothing drifted
    assert main(["verify", str(repo), "--all", "--drifted", "--json"]) == 2


def test_dash_c_runs_a_command_from_another_folder(repo: Path, tmp_path: Path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    code, out = run_json(capsys, "-C", str(repo), "status")
    assert code == 0 and out["project"]
    code, out = run_json(capsys, "-C", str(repo), "next")
    assert code == 0 and out["item"]["id"]


def test_bad_usage_with_json_is_still_json(capsys):
    capsys.readouterr()
    assert main(["start", "--no-such-flag", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"]["code"] == "usage"


def test_backlog_text_lists_proposals(repo: Path, capsys):  # noqa: F811
    assert main(["propose", "Undo", "--dir", str(repo)]) == 0
    capsys.readouterr()
    assert main(["backlog", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "T-new-undo" in text and "proposal(s)" in text


def test_next_says_when_remote_claims_may_be_missing(repo: Path, remote: Path, capsys):  # noqa: F811
    _, out = run_json(capsys, "next", str(repo))
    assert out["stale_remote"] == {"remote": "origin", "fetched_at": None}
    _, out = run_json(capsys, "next", str(repo), "--fetch")
    assert out["stale_remote"] is None
    _, out = run_json(capsys, "next", str(repo))
    assert out["stale_remote"] is None  # fetched just now


def test_promote_commits_so_a_new_worktree_sees_it(repo: Path, capsys):  # noqa: F811
    assert main(["propose", "Undo", "--dir", str(repo), "--milestone", "M1", "--done-when", "undo works",
                 "--touches", "src/streak/undo.py"]) == 0
    code, out = run_json(capsys, "promote", "T-new-undo", str(repo))
    assert code == 0 and out["committed"]
    assert git(repo, "status", "--porcelain") == ""
    code, out = run_json(capsys, "start", out["id"], str(repo), "--worktree")
    assert code == 0, out
    _ = py  # keep the fixture helper import used
