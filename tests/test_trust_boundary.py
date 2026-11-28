# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""Phase 10: the trust boundary between an agent's worktree and the lead.

The phase's gate: an agent in a worktree can't reach the lead's token, move main or edit the gate's inputs. Each test
replays one way the round 4 review found to do one of those (the IDs are the review's) and asserts it is closed.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from idea_to_scaffold import checks
from idea_to_scaffold.checks import Check, Settings, check_env, run_check

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
