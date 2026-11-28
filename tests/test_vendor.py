"""Phase 12: working with vendor dispatchers. A branch Claude, Copilot or Codex made lands through the gate."""
from __future__ import annotations

from pathlib import Path

from .test_landing import git_identity, remote, repo  # noqa: F401 - fixtures
from .test_loop import held_out
from .test_parallel import git, run_json

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
