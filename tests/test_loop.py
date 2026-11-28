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
from idea_to_scaffold import claims as cl
from idea_to_scaffold import ledger
from idea_to_scaffold.cli import main
from idea_to_scaffold.package import Package
from idea_to_scaffold.results import RESULT_SCHEMA

from .test_gate import TEST, claim  # noqa: F401 - fixtures
from .test_landing import git_identity, py, remote, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json, set_field


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


# --- setup in new worktrees, and the lockfiles it writes (B3) --------------------------------------------------------

def set_setup(repo: Path, code: str) -> None:  # noqa: F811
    manifest = repo / "docs/planning/package.yaml"
    text = manifest.read_text()
    start = text.index("setup: ")
    manifest.write_text(text[:start] + f"setup: {json.dumps(py(code))}" + text[text.index("\n", start):])
    git(repo, "commit", "-qam", "setup writes a lockfile")


def test_start_runs_setup_and_done_lets_its_lockfile_through(repo: Path, capsys):  # noqa: F811
    set_setup(repo, "open('uv.lock', 'w').write('version = 1\\n')")
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    assert code == 0 and out["setup"]["passed"] and out["setup"]["lockfiles"] == ["uv.lock"], out
    assert (wt / "uv.lock").is_file()
    (wt / "src/streak/store.py").write_text("def add(): ...\n")
    code, out = run_json(capsys, "done", "T-2.1", str(wt))
    assert code == 0 and out["setup_files"] == ["uv.lock"], out  # neither outside Touches nor a dependency change
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "feat(store): add the store [T-2.1]")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1"], out
    assert (repo / "uv.lock").is_file()  # the lockfile is on main now, so later items don't write one


def test_a_lockfile_the_agent_changed_is_still_a_dependency_change(repo: Path, capsys):  # noqa: F811
    set_setup(repo, "open('uv.lock', 'w').write('version = 1\\n')")
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    (wt / "src/streak/store.py").write_text("def add(): ...\n")
    (wt / "uv.lock").write_text("version = 1\n[[package]]\nname = 'evil'\n")
    code, out = run_json(capsys, "done", "T-2.1", str(wt))
    assert code == 5 and out["error"]["code"] == "outside_touches" and out["error"]["setup_files"] == [], out
    assert "uv.lock" in out["error"]["findings"][0]["files"]
    assert any(f["kind"] == "dependency_added" for f in out["error"]["warnings"])  # warn mode until it is committed


def test_release_doesnt_count_setup_output_as_work(repo: Path, capsys):  # noqa: F811
    set_setup(repo, "open('uv.lock', 'w').write('version = 1\\n')")
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    assert cl.uncommitted(wt, "T-2.1") == [] and cl.uncommitted(wt) == ["uv.lock"]
    code, out = run_json(capsys, "release", "T-2.1", str(repo))  # no commits, only setup's lockfile: nothing lost
    assert code == 0 and out["removed_worktree"] and not wt.exists(), out
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    (wt / "uv.lock").write_text("version = 2\n")  # changed since setup wrote it: that is work
    code, out = run_json(capsys, "release", "T-2.1", str(repo))
    assert code == 1 and "uncommitted" in out["error"]["message"] and wt.exists()


def test_no_setup_skips_it(repo: Path, capsys):  # noqa: F811
    set_setup(repo, "open('uv.lock', 'w').write('version = 1\\n')")
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree", "--no-setup")
    assert code == 0 and out["setup"] is None and not (Path(out["claim"]["worktree"]) / "uv.lock").exists()


@pytest.mark.parametrize("profile", ["package", "lite"])
def test_the_templates_first_item_touches_nothing_the_gate_always_refuses(profile: str):  # H1
    from idea_to_scaffold import gate
    from idea_to_scaffold.backlog import parse_backlog
    text = (Path(idea_to_scaffold.__file__).parent / "templates" / profile / "planning/07-backlog.md").read_text(
        encoding="utf-8")
    first = next(i for i in parse_backlog(text) if i.id == "T-1.1")
    assert first.touches and not any(gate.AGENT_CONFIG.search(t) for t in first.touches)
    assert "lockfile" in first.get("Do")


# --- per-item held-out tests (B2), and failures that are the base's (B14) -------------------------------------------

def held_out(repo: Path, files: dict[str, str]) -> None:  # noqa: F811
    """Held-out tests on main, and an acceptance command that runs them."""
    for rel, body in files.items():
        path = repo / "tests/acceptance" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    manifest = repo / "docs/planning/package.yaml"
    text = manifest.read_text()
    command = subprocess.list2cmdline([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                       "tests/acceptance"]) if os.name == "nt" else \
        f"'{sys.executable}' -m pytest -q -p no:cacheprovider tests/acceptance"
    if "  acceptance: " not in text:
        manifest.write_text(text.replace("commands:\n", f"commands:\n  acceptance: {json.dumps(command)}\n", 1))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "held-out tests")


def finish(repo: Path, ident: str, path: str, capsys) -> Path:  # noqa: F811
    _, out = run_json(capsys, "start", ident, str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    assert not (wt / "tests/acceptance").exists()  # hidden from the agent
    (wt / path).write_text("def f(): ...\n")
    assert run_json(capsys, "done", ident, str(wt), "--no-verify")[0] == 0
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", f"feat: {ident} [{ident}]")
    return wt


NEEDS = "from pathlib import Path\n\ndef test_{name}():\n    assert Path('src/streak/{name}.py').is_file()\n"


def test_parallel_items_with_held_out_tests_both_land(repo: Path, capsys):  # noqa: F811
    held_out(repo, {"T-2.1/test_store_held_out.py": NEEDS.format(name="store"),
                    "T-2.2/test_streak_held_out.py": NEEDS.format(name="streak"),
                    "test_shared_held_out.py": "def test_shared():\n    assert True\n"})
    finish(repo, "T-2.1", "src/streak/store.py", capsys)
    finish(repo, "T-2.2", "src/streak/streak.py", capsys)
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1", "T-2.2"], out
    first, second = out["landed"]
    assert first["held_out"]["included"] == ["T-2.1"] and first["held_out"]["left_out"] == ["T-2.2"]
    assert second["held_out"]["included"] == ["T-2.1", "T-2.2"] and second["held_out"]["left_out"] == []
    assert any(c["name"] == "acceptance" and c["passed"] for c in first["checks"])


def test_a_held_out_failure_names_the_item_and_a_failing_base_isnt_the_items(repo: Path, capsys):  # noqa: F811
    held_out(repo, {"T-2.1/test_store_held_out.py": NEEDS.format(name="nowhere")})
    finish(repo, "T-2.1", "src/streak/store.py", capsys)
    code, out = run_json(capsys, "land", "--dir", str(repo))
    skip = out["skipped"][0]
    assert code == 7 and skip["reason"] == "check_failed" and skip["held_out_failed"] == ["T-2.1"], out
    assert "T-2.1's held-out tests (tests/acceptance/T-2.1/)" in skip["message"]

    held_out(repo, {"test_shared_held_out.py": "def test_shared():\n    assert False\n"})  # the lead's own file
    code, out = run_json(capsys, "land", "--dir", str(repo), "--accept-base")  # a person made that commit
    skip = out["skipped"][0]
    assert code == 7 and skip["reason"] == "base_failing" and not skip["base_check"]["passed"], out
    assert "fails on main itself, before T-2.1 is merged" in skip["message"]


def test_plain_done_ignores_the_held_out_tests_of_items_not_done(repo: Path, capsys):  # noqa: F811
    held_out(repo, {"T-2.2/test_streak_held_out.py": NEEDS.format(name="streak")})
    manifest = repo / "docs/planning/package.yaml"
    text = manifest.read_text()
    start = text.index("  test: ")
    test = subprocess.list2cmdline([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]) \
        if os.name == "nt" else f"'{sys.executable}' -m pytest -q -p no:cacheprovider tests"
    manifest.write_text(text[:start] + f"  test: {json.dumps(test)}" + text[text.index("\n", start):])
    git(repo, "commit", "-qam", "pytest runs every test")
    assert run_json(capsys, "start", "T-2.1", str(repo))[0] == 0
    git(repo, "commit", "-qam", "start T-2.1")
    (repo / "src/streak/store.py").write_text("def add(): ...\n")
    code, out = run_json(capsys, "done", "T-2.1", str(repo))
    assert code == 0, out  # T-2.2's held-out test, which needs streak.py, isn't collected


# --- drift stays quiet after land (E1) -------------------------------------------------------------------------------

def test_a_landed_item_is_marked_from_what_landed_and_doesnt_drift(repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Touches", "`src/streak/`")
    set_field(repo, "T-1.1", "Touches", "`src/streak/`")
    set_field(repo, "T-1.1", "Verify", f"`{py('print(4)')}`")
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    (wt / "src/streak/store.py").write_text("def add(): ...\n")
    (wt / "src/streak/scratch_notes.txt").write_text("an untracked note the agent never commits\n")
    assert run_json(capsys, "done", "T-2.1", str(wt))[0] == 0
    git(wt, "add", "src/streak/store.py", "docs/planning")
    git(wt, "commit", "-qm", "feat(store): add the store [T-2.1]")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 0 and out["landed"][0]["reverified"] == ["T-1.1"], out  # its files changed: Verify ran again
    assert any(c["name"] == "verify T-1.1" for c in out["landed"][0]["checks"])
    code, out = run_json(capsys, "drift", str(repo), "--check")
    assert code == 0 and out["drifted"] == [] and {"T-1.1", "T-2.1"} <= set(out["clean"]), out


def test_a_merge_that_breaks_a_done_items_verify_doesnt_land(repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Touches", "`src/streak/`")
    set_field(repo, "T-1.1", "Touches", "`src/streak/`")
    broken = py("import pathlib, sys; sys.exit(pathlib.Path('src/streak/store.py').exists())")
    set_field(repo, "T-1.1", "Verify", f"`{broken}`")
    finish(repo, "T-2.1", "src/streak/store.py", capsys)
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and "T-1.1's Verify fails on the merged result" in out["skipped"][0]["message"], out


def test_the_committed_hash_matches_the_disk_hash_on_a_clean_checkout(repo: Path):  # noqa: F811
    from idea_to_scaffold.work import touches_hash, touches_hash_at
    pkg = Package.load(repo)
    for touches in (["src/"], ["tests/"], ["src/streak/"]):
        assert touches_hash(pkg, touches) == touches_hash_at(pkg, touches, "HEAD")


# --- G1: done keeps a status written while its checks ran ------------------------------------------------------------

def test_a_block_written_while_the_checks_run_is_kept(claim: Path):  # noqa: F811
    from idea_to_scaffold.contract import WorkError
    from idea_to_scaffold.edits import block
    from idea_to_scaffold.work import done
    pkg = Package.load(claim)
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    blocked = []

    def lead_blocks(check) -> None:  # the lead runs its block while done's checks run
        if not blocked:
            blocked.append(block(pkg, "T-2.1", "the schema changed"))

    with pytest.raises(WorkError) as exc:
        done(pkg, "T-2.1", on_check=lead_blocks)
    assert exc.value.code == "status_changed" and exc.value.details["status"] == "blocked"
    assert status_at(claim, "HEAD", "T-2.1") == "todo"
    text = (claim / "docs/planning/07-backlog.md").read_text()
    assert "the schema changed" in text and "**Status:** blocked" in text


# --- B7: verify --drifted re-verifies every drifted item in one run --------------------------------------------------

def test_verify_drifted_fixes_every_drifted_item_in_one_run(repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-1.1", "Touches", "`src/streak/__init__.py`")
    set_field(repo, "T-2.1", "Status", "done")
    for ident in ("T-1.1", "T-2.1"):
        set_field(repo, ident, "Verified", "2026-01-01, tree 000000000000")
    (repo / "src/streak/store.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "changes after both were verified")
    code, out = run_json(capsys, "drift", str(repo))
    assert {d["id"] for d in out["drifted"]} == {"T-1.1", "T-2.1"}
    code, out = run_json(capsys, "verify", "--drifted", str(repo), "--commit")
    assert code == 0 and {v["id"] for v in out["verified"]} == {"T-1.1", "T-2.1"} and not out["failed"], out
    assert run_json(capsys, "drift", str(repo), "--check")[0] == 0


# --- review verdicts are recorded and count at land, for the content they judged ------------------------------------

def _reply(path: Path, verdict: str = "approve", status: str = "met", findings: tuple = ()) -> Path:
    reqs = [{"id": r, "status": status, "evidence": "test_add"} for r in ("FR-001", "FR-002", "NFR-002")]
    path.write_text(json.dumps({"verdict": verdict, "requirements": reqs, "findings": list(findings)}))
    return path


def _done_and_committed(claim: Path, capsys, body: str = "def add(): return 1\n") -> None:  # noqa: F811
    (claim / "src/streak/store.py").write_text(body)
    code, out = run_json(capsys, "done", "T-2.1", str(claim))
    assert code == 0, out
    git(claim, "add", "-A")
    git(claim, "commit", "-qm", "feat(store): add the store [T-2.1]")


def test_changes_requested_keeps_that_version_from_landing(claim: Path, repo: Path, tmp_path: Path,  # noqa: F811
                                                           capsys):
    _done_and_committed(claim, capsys)
    _, prompt = run_json(capsys, "review", "T-2.1", str(repo))  # from main: it reviews the claim branch
    blocker = {"severity": "blocker", "file": "src/streak/store.py", "line": 1, "message": "add() stores nothing"}
    reply = _reply(tmp_path / "review.json", "changes_requested", findings=(blocker,))
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", str(reply))
    assert code == 5 and out["recorded"] and out["current"] and out["digest"] == prompt["digest"], out
    _, out = run_json(capsys, "status", str(repo))
    entry = next(c for c in out["claims"] if c["id"] == "T-2.1")
    assert entry["state"] == "changes_requested" and out["ready_to_land"] == []
    assert entry["review"]["blocking"] == ["blocker src/streak/store.py"]
    capsys.readouterr()
    assert main(["status", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "done, but its review requested changes" in text and "review: blocker src/streak/store.py" in text
    code, out = run_json(capsys, "land", "--dir", str(repo), "--dry-run")
    assert code == 7 and out["skipped"][0]["reason"] == "changes_requested", out
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and out["landed"] == [] and out["skipped"][0]["reason"] == "changes_requested"
    # once the reviewed content changes, that verdict no longer counts either way
    (claim / "src/streak/store.py").write_text("def add(): return 'stored'\n")
    git(claim, "commit", "-qam", "fix(store): store it [T-2.1]")
    _, out = run_json(capsys, "status", str(repo))
    entry = next(c for c in out["claims"] if c["id"] == "T-2.1")
    assert entry["state"] == "ready_to_land" and entry["review"]["label"] == "reviewed an earlier version"
    code, out = run_json(capsys, "land", "--dir", str(repo), "--dry-run", "--require-review")
    assert code == 7 and out["skipped"][0]["reason"] == "not_reviewed", out
    assert "earlier version" in out["skipped"][0]["message"]
    run_json(capsys, "review", "T-2.1", str(repo))
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", str(_reply(reply)))
    assert code == 0 and out["passed"] and out["current"]
    code, out = run_json(capsys, "land", "--dir", str(repo), "--require-review")
    assert code == 0 and out["landed"][0]["review"]["passed"], out
    assert "Reviewed: approved" in git(repo, "log", "-1", "--format=%B")
    events = [e["outcome"] for e in ledger.read(Package.find(repo)) if e["event"] == "review"]
    assert events == ["changes_requested", "approved"]
    _, out = run_json(capsys, "stats", str(repo))
    assert out["reviews"] == {"changes_requested": 1, "approved": 1}


def test_a_verdict_is_for_what_the_reviewer_saw(claim: Path, repo: Path, tmp_path: Path, capsys):  # noqa: F811
    _done_and_committed(claim, capsys)
    run_json(capsys, "review", "T-2.1", str(repo))  # the prompt the reviewer gets
    (claim / "src/streak/store.py").write_text("def add(): return 2\n")  # the agent goes on after the prompt
    git(claim, "commit", "-qam", "more [T-2.1]")
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", str(_reply(tmp_path / "r.json")))
    assert code == 0 and out["passed"] and not out["current"]  # an approval of the earlier version
    code, out = run_json(capsys, "land", "--dir", str(repo), "--dry-run", "--require-review")
    assert code == 7 and out["skipped"][0]["reason"] == "not_reviewed"


def test_the_integration_branch_can_require_a_review(claim: Path, repo: Path, tmp_path: Path, capsys):  # noqa: F811
    manifest = repo / "docs/planning/package.yaml"
    manifest.write_text(manifest.read_text().replace("\ngate:\n", "\ngate:\n  require_review: true\n", 1))
    git(repo, "commit", "-qam", "land needs a review")
    _done_and_committed(claim, capsys)
    _, out = run_json(capsys, "status", str(repo))
    assert next(c for c in out["claims"] if c["id"] == "T-2.1")["state"] == "not_reviewed"
    code, out = run_json(capsys, "land", "--dir", str(repo), "--dry-run")
    assert code == 7 and out["skipped"][0]["reason"] == "not_reviewed"
    # the claim's own package.yaml can't turn it off: the rule comes from the integration branch
    wt_manifest = claim / "docs/planning/package.yaml"
    assert "require_review" not in wt_manifest.read_text()
    run_json(capsys, "review", "T-2.1", str(claim))  # a review made in the worktree counts once committed
    code, out = run_json(capsys, "review", "T-2.1", str(repo), "--reply", str(_reply(tmp_path / "r.json")))
    assert code == 0 and out["current"]
    code, out = run_json(capsys, "land", "--dir", str(repo), "--dry-run")
    assert code == 0 and out["skipped"][0]["reason"] == "dry_run", out
