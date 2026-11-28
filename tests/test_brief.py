"""Phase 12: `its brief` writes the task for an agent that never runs its, and `--retry` one more attempt at it."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from .test_landing import git_identity, remote, repo  # noqa: F401 - fixtures
from .test_loop import held_out
from .test_parallel import git, run_json
from .test_vendor import STORE, vendor_branch

GH = '''
import json, sys
from pathlib import Path
log = Path(__file__).with_name("gh.log")
body = sys.stdin.read() if "--body-file" in sys.argv else None
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "body": body}) + "\\n")
if sys.argv[1:3] == ["issue", "list"]:
    print("[]")
elif sys.argv[1:3] == ["issue", "create"]:
    print("https://github.com/acme/streak/issues/7")
elif sys.argv[1:3] == ["issue", "comment"]:
    print("https://github.com/acme/streak/issues/7#issuecomment-1")
'''


@pytest.fixture
def gh(tmp_path: Path, monkeypatch) -> Path:
    """A fake `gh` on PATH that logs each call (arguments and the body it was sent) to gh.log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh_fake.py"
    script.write_text(GH, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "gh.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        (bin_dir / "gh").write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return bin_dir / "gh.log"


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_brief_carries_the_item_the_requirements_and_the_rules(repo: Path, capsys):  # noqa: F811
    held_out(repo, {"T-2.1/test_store_held_out.py": STORE})
    code, out = run_json(capsys, "brief", "T-2.1", str(repo))
    text = out["brief"]
    assert code == 0 and text.startswith("# T-2.1: "), text
    assert "`src/streak/store.py`" in text and "### FR-001" in text  # the requirement's own text, not just its ID
    assert "[T-2.1]" in text and text.rstrip().splitlines()[-2].startswith("- If its is installed, end with `its done T-2.1 --check`")
    assert "Held-out acceptance tests that you can't see" in text and out["held_out"]
    assert [c["name"] for c in out["checks"]] == ["test", "lint"] and all(c["command"] in text for c in out["checks"])
    assert "tests/acceptance" not in text  # where the held-out tests live isn't said
    assert run_json(capsys, "brief", "T-1.1", str(repo))[1]["error"]["code"] == "bad_status"  # done already
    assert run_json(capsys, "brief", "T-2.1", str(repo), "--retry")[1]["error"]["code"] == "nothing_to_retry"


def test_retry_carries_only_what_failed_and_stops_after_max_attempts(repo: Path, remote: Path,  # noqa: F811
                                                                     tmp_path: Path, capsys):
    held_out(repo, {"T-2.1/test_store_held_out.py": STORE})
    git(repo, "push", "-q", "origin", "main")
    vendor_branch(remote, tmp_path, "claude/store", {"src/streak/store.py": "def remove():\n    return 0\n"})
    assert run_json(capsys, "adopt", "claude/store", "T-2.1", str(repo))[0] == 0
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 7  # fails its held-out test
    code, out = run_json(capsys, "brief", "T-2.1", str(repo), "--retry")
    again = out["retry"]
    assert code == 0 and again["attempt"] == 2 and again["max_attempts"] == 2 and again["branch"] == "claude/store"
    assert again["failing_checks"] == ["acceptance"] and again["held_out_failed"] == 1 and again["failing_tests"] == []
    assert out["brief"].startswith("# Retry T-2.1: ") and "(attempt 2 of 2)" in out["brief"]
    assert "1 held-out acceptance test(s) failed" in out["brief"] and "test_store_adds" not in out["brief"]
    assert "### FR-001" not in out["brief"]  # only what failed, not the whole brief again
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 7  # the same commit again: still one attempt
    assert run_json(capsys, "brief", "T-2.1", str(repo), "--retry")[1]["retry"]["attempt"] == 2

    vendor_branch(remote, tmp_path, "claude/store-2", {"src/streak/store.py": "def remove():\n    return 1\n"})
    assert run_json(capsys, "adopt", "claude/store-2", "T-2.1", str(repo))[0] == 0
    code, landed = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and landed["landed"] == []
    code, out = run_json(capsys, "brief", "T-2.1", str(repo), "--retry")
    assert code == 1 and out["error"]["code"] == "out_of_attempts", out
    assert out["error"]["attempts"] == 2 and out["error"]["max_attempts"] == 2


def test_done_says_which_attempt_failed(repo: Path, capsys):  # noqa: F811
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    (wt / "src/streak/streak.py").write_text("x = 1\n")  # outside T-2.1's Touches
    code, out = run_json(capsys, "done", "T-2.1", str(wt))
    assert code == 5 and out["error"]["code"] == "outside_touches"
    assert out["error"]["retry"]["attempt"] == 1 and out["error"]["retry"]["command"] == "its brief --retry T-2.1"
    code, out = run_json(capsys, "done", "T-2.1", str(wt))  # the same work again is the same attempt
    assert out["error"]["retry"]["attempt"] == 1
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "wip")
    code, out = run_json(capsys, "done", "T-2.1", str(wt))
    assert out["error"]["retry"]["attempt"] == 2 and out["error"]["retry"]["command"] is None
    assert run_json(capsys, "done", "T-2.1", str(wt), "--check")[1]["error"]["retry"]["attempt"] == 2


def test_github_creates_the_issue_then_updates_it_and_comments_a_retry(repo: Path, remote: Path,  # noqa: F811
                                                                       tmp_path: Path, gh: Path, capsys):
    held_out(repo, {"T-2.1/test_store_held_out.py": STORE})
    git(repo, "push", "-q", "origin", "main")
    code, out = run_json(capsys, "brief", "T-2.1", str(repo), "--github")
    assert code == 0 and out["issue"] == {"number": 7, "url": "https://github.com/acme/streak/issues/7",
                                          "action": "created"}, out
    log = calls(gh)
    assert log[0]["argv"][:2] == ["issue", "list"] and log[1]["argv"][:2] == ["issue", "create"]
    assert log[1]["argv"][log[1]["argv"].index("--title") + 1].startswith("[T-2.1] ")
    assert log[1]["body"] == out["brief"]
    assert run_json(capsys, "brief", "T-2.1", str(repo), "--github")[1]["issue"]["action"] == "updated"
    assert calls(gh)[-1]["argv"][:3] == ["issue", "edit", "7"]

    vendor_branch(remote, tmp_path, "copilot/store", {"src/streak/store.py": "def remove():\n    return 0\n"})
    assert run_json(capsys, "adopt", "pr:3", "T-2.1", str(repo))[1]["error"]["code"] == "no_branch"
    assert run_json(capsys, "adopt", "copilot/store", "T-2.1", str(repo))[0] == 0
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 7
    code, out = run_json(capsys, "brief", "T-2.1", str(repo), "--retry", "--github")
    assert code == 0 and out["issue"]["action"] == "commented" and calls(gh)[-1]["argv"][:3] == ["issue", "comment", "7"]
    assert calls(gh)[-1]["body"].startswith("# Retry T-2.1: ")


def test_github_needs_gh(repo: Path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("shutil.which", lambda name: None)
    code, out = run_json(capsys, "brief", "T-2.1", str(repo), "--github")
    assert code == 1 and out["error"]["code"] == "github_failed"
