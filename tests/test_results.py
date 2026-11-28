"""Phase 9: done reads what the test runner reports, assertions are counted per test, and gate-audit runs the tests."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from idea_to_scaffold import gate
from idea_to_scaffold.checks import Check, failure_report, read_report, result_findings, run_check, runs_pytest
from idea_to_scaffold.package import Package
from idea_to_scaffold.tamper import compare, config_findings

from .test_gate import older_project
from .test_landing import git_identity, py, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json, set_field, status_in

STORE = "src/streak/store.py"
TEST = "tests/test_store.py"
MAIN_TEST = ("from streak.store import add\n\n\n"
             "def test_add():\n    assert add(1, 1) == 2\n\n\n"
             "def test_importable():\n    assert callable(add)\n")
BROKEN = "def add(a, b):\n    raise NotImplementedError\n"
HONEST = "def add(a, b):\n    return a + b\n"


def quote(arg: str) -> str:
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


PYTEST = f"{quote(sys.executable)} -m pytest -q -p no:cacheprovider {TEST}"


def commit(root: Path, msg: str = "wip") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)


@pytest.fixture
def real(repo: Path, capsys) -> Path:  # noqa: F811
    """T-2.1 claimed in a worktree, with a real pytest Verify on main that fails until `add` is implemented."""
    (repo / STORE).write_text(BROKEN)
    (repo / TEST).write_text(MAIN_TEST)
    commit(repo, "store stub and its test")
    set_field(repo, "T-2.1", "Touches", f"`{STORE}`, `tests/`")
    set_field(repo, "T-2.1", "Verify", f"`{PYTEST}`")
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    assert code == 0, out
    return Path(out["claim"]["worktree"])


def done(wt: Path, capsys, *flags: str) -> tuple[int, dict]:
    return run_json(capsys, "done", "T-2.1", str(wt), *flags)


def kinds(out: dict) -> set[str]:
    return {f["kind"] for f in out.get("error", {}).get("findings", out.get("findings", []))}


# --- the runner's report --------------------------------------------------------------------------------------

def test_an_honest_fix_passes_and_its_report_is_read(real: Path, capsys):
    (real / STORE).write_text(HONEST)
    code, out = done(real, capsys)
    assert code == 0 and out["status"] == "done" and out["findings"] == [], out
    results = out["checks"][0]["results"]
    assert results["report"] and results["tests"] == 2 and results["passed"] == 2 and results["failed"] == 0
    assert "test" not in {c["name"] for c in out["checks"] if "results" in c}  # print(1) doesn't run pytest


def test_os_exit_in_the_items_source_is_refused(real: Path, repo: Path, capsys):  # noqa: F811
    (real / STORE).write_text("import os\nos._exit(0)\n")
    code, out = done(real, capsys)
    assert code == 5 and out["error"]["code"] == "test_changes" and kinds(out) == {"no_test_report"}, out
    assert "stopped before pytest finished" in out["error"]["message"]
    assert "Make the tests run to the end" in out["error"]["message"]
    assert status_in(real, "T-2.1") == "todo"

    older_project(repo)  # a project scaffolded before phase 9 warns first, so a person decides when to enforce
    code, out = done(real, capsys, "--check")
    assert code == 0 and [(f["kind"], f["mode"]) for f in out["warnings"]] == [("no_test_report", "warn")]


def test_land_refuses_os_exit_on_the_merged_result(real: Path, repo: Path, capsys):  # noqa: F811
    (real / STORE).write_text("import os\nos._exit(0)\n")
    assert done(real, capsys, "--no-verify")[0] == 0
    commit(real, "implement add")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and out["landed"] == [], out
    skipped = out["skipped"][0]
    assert skipped["reason"] == "test_changes" and [f["kind"] for f in skipped["findings"]] == ["no_test_report"]
    assert status_in(repo, "T-2.1") == "todo"


def test_a_conftest_that_resets_the_exit_status_is_refused(real: Path, repo: Path, capsys):  # noqa: F811
    (real / STORE).write_text(BROKEN + "# touched\n")
    (real / "tests/conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n")
    code, out = done(real, capsys)
    assert code == 5 and kinds(out) == {"test_config_changed"}  # a new scaffold refuses the conftest.py itself
    older_project(repo)
    code, out = done(real, capsys)
    assert code == 5 and kinds(out) == {"failures_hidden"}, out  # and the report shows the failure it hid
    assert out["error"]["findings"][0]["tests"] == ["tests/test_store.py::test_add"]
    assert [f["kind"] for f in out["error"]["warnings"]] == ["test_config_changed"]


def test_pytest_addopts_from_the_agents_shell_is_dropped(real: Path, capsys, monkeypatch):
    (real / STORE).write_text(BROKEN + "# touched\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_plugin")
    code, out = done(real, capsys)
    assert code == 5 and out["error"]["code"] == "check_failed"  # the tests really ran, and failed


def test_a_skip_from_the_code_under_test_is_no_tests_ran(real: Path, repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Verify", f"`{PYTEST} -k test_add`")
    (real / STORE).write_text("def add(a, b):\n    import pytest\n    pytest.skip('not today')\n")
    code, out = done(real, capsys)
    assert code == 5 and "no_tests_ran" in kinds(out), out  # every test skipped, so pytest exits 0
    assert "skipped every test it collected (1)" in out["error"]["message"]


def test_a_test_skipped_at_run_time_is_found(real: Path, repo: Path, capsys):  # noqa: F811
    (real / STORE).write_text("def add(a, b):\n    import pytest\n    pytest.skip('later')\n")
    code, out = done(real, capsys)
    assert code == 5 and kinds(out) == {"tests_skipped"}, out
    assert out["error"]["findings"][0]["tests"] == ["tests/test_store.py::test_add"]
    older_project(repo)
    code, out = done(real, capsys, "--check")
    assert code == 0 and [(f["kind"], f["mode"]) for f in out["warnings"]] == [("tests_skipped", "warn")]


def test_a_renamed_test_is_removed_but_one_moved_to_another_file_is_not(real: Path, capsys):
    (real / STORE).write_text(BROKEN + "# touched\n")
    (real / TEST).write_text(MAIN_TEST.replace("def test_add", "def check_add"))
    code, out = done(real, capsys)
    assert code == 5 and kinds(out) == {"test_removed"}, out
    assert out["error"]["findings"][0]["tests"] == ["test_add"]
    (real / TEST).write_text("from streak.store import add\n\n\ndef test_importable():\n    assert callable(add)\n")
    (real / "tests/test_store_add.py").write_text(
        "from streak.store import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n")
    code, out = done(real, capsys, "--no-verify", "--check")
    assert code == 0 and "test_removed" not in kinds(out), out


def test_an_empty_parametrize_and_a_list_assert_are_refused(real: Path, capsys):
    (real / STORE).write_text(BROKEN + "# touched\n")
    (real / TEST).write_text(MAIN_TEST.replace(
        "def test_add():", "import pytest\n\n@pytest.mark.parametrize('x', [])\ndef test_add(x):"))
    code, out = done(real, capsys)
    assert code == 5 and "skip_added" in kinds(out)
    (real / TEST).write_text(MAIN_TEST.replace("assert add(1, 1) == 2", "assert [add(1, 1) == 2]"))
    code, out = done(real, capsys)
    assert code == 5 and {"assertions_removed", "assertions_weakened"} <= kinds(out)


def test_kind_test_only_skips_red_on_parent(real: Path, repo: Path, capsys):  # noqa: F811
    (real / "tests/test_more.py").write_text(
        "from streak.store import add\n\n\ndef test_add_is_callable():\n    assert callable(add)\n")
    set_field(repo, "T-2.1", "Verify", f"`{PYTEST.replace(TEST, 'tests/test_more.py')}`")
    code, out = done(real, capsys, "--check")
    assert code == 5 and kinds(out) == {"tests_pass_on_parent"}  # the new test passes before any change
    set_field(repo, "T-2.1", "Kind", "test-only")
    code, out = done(real, capsys)
    assert code == 0 and out["red_on_parent"]["ran"] is False and "test-only" in out["red_on_parent"]["reason"]


# --- the report, unit by unit ---------------------------------------------------------------------------------

def test_which_commands_run_pytest():
    for command in ("pytest", "uv run pytest -q", "python -m pytest tests", ".venv/bin/pytest", "py.test -x",
                    "cd app && pytest", r"C:\venv\Scripts\pytest.exe -q"):
        assert runs_pytest(command), command
    for command in ("ruff check .", "npm test", "make test", "python -c 'print(1)'", "pytest-watch"):
        assert not runs_pytest(command), command


def test_the_report_is_read_from_the_commands_own_junitxml(tmp_path: Path):
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert len('a') == 1\n")
    command = f"{quote(sys.executable)} -m pytest -q -p no:cacheprovider --junitxml=out.xml test_x.py"
    result = run_check(Check("test", command), tmp_path)
    assert result["passed"] and result["results"]["report"] and result["results"]["passed"] == 1


def test_a_forged_or_broken_report_counts_as_none(tmp_path: Path):
    forged = tmp_path / "forged.xml"
    forged.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><testsuites/>')
    assert read_report(forged)["report"] is False
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><testsuite>")
    assert read_report(broken)["report"] is False and "valid XML" in read_report(broken)["reason"]
    assert read_report(tmp_path / "missing.xml")["report"] is False


def test_result_findings_compare_the_report_with_the_exit_code():
    passed = {"name": "verify", "command": "pytest", "exit_code": 0, "passed": True, "output": ""}
    report = {"report": True, "tests": 2, "passed": 1, "failed": 1, "errors": 0, "skipped": 0,
              "failed_tests": ["tests/test_a.py::test_b"], "skipped_tests": [], "files": ["tests/test_a.py"]}
    assert [f["kind"] for f in result_findings([{**passed, "results": report}])] == ["failures_hidden"]
    failed = {**passed, "exit_code": 1, "passed": False, "results": report}
    assert result_findings([failed]) == []  # a failing check is reported as a failure, not as a finding
    empty = {**report, "tests": 0, "passed": 0, "failed": 0, "failed_tests": []}
    assert [f["kind"] for f in result_findings([{**passed, "results": empty}])] == ["no_tests_ran"]
    skipped = {**report, "failed": 0, "failed_tests": [], "skipped": 1,
               "skipped_tests": [{"id": "tests/test_c.py::test_d", "file": "tests/test_c.py", "reason": "flaky"}]}
    full_suite = {**passed, "name": "test", "results": skipped}
    assert result_findings([full_suite]) == []  # a skip in a file the branch didn't touch is the suite's business
    assert [f["kind"] for f in result_findings([full_suite], {"tests/test_c.py"})] == ["tests_skipped"]


def test_each_different_failure_output_is_shown():
    report = failure_report([
        {"name": "verify", "command": "pytest tests/test_a.py", "exit_code": 1, "passed": False, "output": "E  a"},
        {"name": "test", "command": "pytest", "exit_code": 1, "passed": False, "output": "E  a"},
        {"name": "lint", "command": "ruff check .", "exit_code": 1, "passed": False, "output": "F401 unused"},
    ])
    assert report.count("E  a") == 1 and "(same output as above)" in report and "F401 unused" in report


# --- tamper: per-test assertions, JS skips, outcome hooks -------------------------------------------------------

def test_assertions_are_counted_per_test():
    before = "def test_a():\n    assert f() == 1\n\n\ndef test_b():\n    assert g()\n"
    swapped = "def test_a():\n    # assert f() == 1\n    assert True\n    assert 1 == 1\n\n\ndef test_b():\n    assert g()\n"
    found = compare("tests/test_x.py", before, swapped)
    assert found[0]["kind"] == "assertions_removed" and found[0]["tests"] == ["test_a"]
    moved = "def test_a():\n    pass\n\n\ndef test_b():\n    assert g()\n    assert f() == 1\n"
    assert compare("tests/test_x.py", before, moved)[0]["tests"] == ["test_a"]  # an extra one elsewhere doesn't help
    for trivial in ("assert 1 + 1 == 2", "assert (f(), 'f works')", "assert f'got {f()}'", "assert 2 > 1"):
        after = before.replace("assert f() == 1", trivial)
        assert {f["kind"] for f in compare("tests/test_x.py", before, after)} >= {"assertions_removed",
                                                                                 "assertions_weakened"}, trivial
    placeholder = "def test_placeholder():\n    assert 1 + 1 == 2\n"
    assert compare("tests/test_x.py", placeholder, before) == []  # a test that asserted nothing can go


def test_js_skips_only_and_per_test_counts_are_found():
    before = "test('adds', () => {\n  expect(add(1, 1)).toBe(2)\n})\n\ntest('subs', () => {\n  expect(sub()).toBe(0)\n})\n"
    path = "tests/math.test.js"
    for skip in ("test('adds', { skip: 'flaky' }, () => {", "test.only('adds', () => {",
                 "it('adds', { todo: true }, () => {", "test('adds', (t) => {\n  t.skip()"):
        after = before.replace("test('adds', () => {", skip, 1)
        assert "skip_added" in {f["kind"] for f in compare(path, before, after)}, skip
    assert compare(path, before, before.replace("test('adds', () => {", "test('adds', { skip: false }, () => {")) == []
    swapped = before.replace("  expect(add(1, 1)).toBe(2)\n", "").replace("  expect(sub()).toBe(0)\n",
                                                                         "  expect(sub()).toBe(0)\n  expect(add(1, 1)).toBe(2)\n")
    assert compare(path, before, swapped)[0]["tests"] == ["adds"]
    renamed = before.replace("'subs'", "'subtracts'")
    assert [f["kind"] for f in compare(path, before, renamed)] == ["test_removed"]


def test_hooks_that_rewrite_results_are_test_config():
    for line in ("def pytest_runtest_makereport(item, call):", "@pytest.hookimpl(hookwrapper=True)",
                 "def pytest_sessionfinish(session, exitstatus):", "    session.exitstatus = 0",
                 "import os; os._exit(0)", "pytest_plugins = ['cheat']"):
        assert config_findings("tests/conftest.py", "", line + "\n"), line
    for name in ("sitecustomize.py", "src/usercustomize.py", "src/its.pth"):
        assert config_findings(name, None, "import cheat\n")[0]["kind"] == "test_config_changed", name
    pyproject = "[project]\nname = 'x'\n"
    plugin = pyproject + "\n[project.entry-points.pytest11]\ncheat = 'x.cheat'\n"
    assert config_findings("pyproject.toml", pyproject, plugin)[0]["kind"] == "test_config_changed"
    assert config_findings("pyproject.toml", pyproject, pyproject.replace("'x'", "'y'")) == []
    test = "def test_a():\n    assert f()\n"
    for addition in ("pytest_plugins = ['cheat']\n", "import os\nos._exit(0)\n", "import sys\nsys.exit(0)\n"):
        found = compare("tests/test_a.py", test, addition + test)
        assert [f["kind"] for f in found] == ["test_config_changed"], addition


# --- policy and new scaffolds ------------------------------------------------------------------------------------

def test_a_new_scaffold_enforces_every_check(repo: Path):  # noqa: F811
    from idea_to_scaffold.scaffold import SCAFFOLD_WARN
    policy = gate.Policy.of(Package.load(repo).manifest, "here")
    expected = {kind: "warn" if kind in SCAFFOLD_WARN else "enforce" for kind in gate.DEFAULT_MODES}
    assert {kind: policy.mode(kind) for kind in gate.DEFAULT_MODES} == expected
    assert set(SCAFFOLD_WARN) == {"dependency_added"}  # until scaffold commits the lockfile setup writes
    assert gate.Policy.of({}, "here").mode("no_test_report") == "warn"  # an older project: warn first


def test_only_the_relaxable_checks_can_be_relaxed():
    policy = gate.Policy.of({"gate": {"checks": {"failures_hidden": "warn", "test_removed": "off",
                                                 "test_deleted": "off", "verify_changed": "warn"}}}, "here")
    assert policy.mode("failures_hidden") == "warn" and policy.mode("test_removed") == "off"
    assert policy.mode("test_deleted") == "enforce" and policy.mode("verify_changed") == "enforce"


# --- gate-audit ---------------------------------------------------------------------------------------------------

def test_gate_audit_runs_the_tests_and_refuses_os_exit(real: Path, repo: Path, capsys):  # noqa: F811
    code, out = run_json(capsys, "gate-audit", "T-2.1", str(repo))
    assert code == 0 and out["ok"] and out["missed"] == [] and out["warned"] == [], out["variants"]
    by_name = {v["name"]: v for v in out["variants"]}
    assert by_name["os_exit"]["caught"] and by_name["os_exit"]["mode"] == "enforce"
    assert "no_test_report" in by_name["os_exit"]["found"]
    for name in ("rename_test", "empty_parametrize", "assert_list", "comment_and_add", "sessionfinish_exitstatus",
                 "makereport_rewrite", "sitecustomize"):
        assert by_name[name]["caught"] and by_name[name]["mode"] == "enforce", by_name[name]
    assert git(repo, "status", "--porcelain") == ""

    _, out = run_json(capsys, "gate-audit", "T-2.1", str(repo), "--no-run")
    assert "os_exit" in out["skipped"]


def test_gate_audit_fails_when_cheats_are_only_warned(real: Path, repo: Path, capsys):  # noqa: F811
    older_project(repo)
    code, out = run_json(capsys, "gate-audit", "T-2.1", str(repo), "--no-run")
    assert code == 10 and not out["ok"] and out["missed"] == []
    assert {"assert_true", "sessionfinish_exitstatus"} <= set(out["warned"])
    assert "test_config_changed" in out["warned_kinds"]
    assert run_json(capsys, "gate-audit", "T-2.1", str(repo), "--no-run", "--allow-warn")[0] == 0
    capsys.readouterr()
    from idea_to_scaffold.cli import main
    assert main(["gate-audit", "T-2.1", str(repo), "--no-run"]) == 10
    text = capsys.readouterr().out
    assert "only warned: " in text and "test_config_changed: enforce" in text


# --- every platform ------------------------------------------------------------------------------------------------

def test_json_output_is_utf8_whatever_the_locale(mini: Path):
    backlog = mini / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text(encoding="utf-8").replace("— First", "— First → 日本"), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}  # what a Windows pipe gets by default
    proc = subprocess.run([sys.executable, "-m", "idea_to_scaffold.cli", "show", "T-1.1", str(mini), "--json"],
                          capture_output=True, env=env, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert json.loads(proc.stdout.decode("utf-8"))["item"]["title"] == "First → 日本"
