"""Phase 13's gate: a Node project's item is gated on what its test runner reports, end to end, as a Python
project's is on pytest's report. The project is scaffolded from the Node stack and tests with `node --test` through
`npm test`; `its done` reads node's JUnit report, and refuses a pass that the report doesn't back."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from idea_to_scaffold.checks import Check, read_report, run_check
from idea_to_scaffold.scaffold import scaffold
from idea_to_scaffold.stacks import NODE_TEST, PYTEST, runner_for

from .test_landing import git_identity, py  # noqa: F401 - git_identity is autouse: commits need a name
from .test_parallel import git, run_json, set_field

needs_node = pytest.mark.skipif(shutil.which("node") is None or shutil.which("npm") is None,
                                reason="needs node and npm on PATH")

STORE = "src/store.js"
TEST = "tests/store.test.js"
STORE_TEST = ('import { test } from "node:test";\nimport assert from "node:assert/strict";\n\n'
              'import { add } from "../src/store.js";\n\n'
              'test("add keeps a habit", () => {\n  assert.deepEqual(add([], "read"), ["read"]);\n});\n')
BROKEN = 'export function add(habits, name) {\n  throw new Error("not yet");\n}\n'
HONEST = "export function add(habits, name) {\n  return [...habits, name];\n}\n"


@pytest.fixture
def node_repo(plan: Path, tmp_path: Path) -> Path:
    """The Streak plan as a Node project: scaffolded from the node stack, with T-1.1 done and T-2.1's Verify
    `npm test` on main, which fails until `add` is written."""
    manifest = plan / "package.yaml"
    text = manifest.read_text()
    text = text.replace('language: "python"', 'language: "javascript"').replace('runtime: "3.12"', 'runtime: "22"')
    text = text.replace('setup: "uv sync"', f"setup: {json.dumps(py('print(0)'))}")
    text = text.replace('test: "uv run pytest"', 'test: "npm test"')
    text = text.replace('lint: "uv run ruff check ."', f"lint: {json.dumps(py('print(2)'))}")
    manifest.write_text(text)
    root = scaffold(plan, tmp_path / "out", git=True).path
    (root / STORE).write_text(BROKEN)
    (root / TEST).write_text(STORE_TEST)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "store stub and its test")
    set_field(root, "T-1.1", "Status", "done")
    set_field(root, "T-2.1", "Touches", f"`{STORE}`")
    set_field(root, "T-2.1", "Verify", "`npm test`")
    return root


@pytest.fixture
def node_claim(node_repo: Path, capsys) -> Path:
    code, out = run_json(capsys, "start", "T-2.1", str(node_repo), "--worktree")
    assert code == 0, out
    return Path(out["claim"]["worktree"])


def test_the_node_stack_scaffolds_a_project_that_node_tests(node_repo: Path):
    assert json.loads((node_repo / "package.json").read_text())["scripts"]["test"] == "node --test"
    assert "actions/setup-node" in (node_repo / ".github/workflows/ci.yml").read_text()
    assert (node_repo / "tests/smoke.test.js").is_file() and not (node_repo / "pyproject.toml").exists()


@needs_node
def test_an_honest_change_is_done_on_nodes_report(node_claim: Path, capsys):
    (node_claim / STORE).write_text(HONEST)
    code, out = run_json(capsys, "done", "T-2.1", str(node_claim))
    assert code == 0 and out["status"] == "done" and out["findings"] == [], out
    results = out["checks"][0]["results"]
    assert results["runner"] == "node-test" and results["report"] and results["failed"] == 0
    assert results["tests"] == results["passed"] == 2  # the smoke test and the store's


@needs_node
def test_process_exit_in_the_code_is_refused_though_npm_test_exits_0(node_claim: Path, capsys):
    (node_claim / STORE).write_text("process.exit(0);\n" + HONEST)
    code, out = run_json(capsys, "done", "T-2.1", str(node_claim))
    assert code == 12 and out["error"]["code"] == "test_changes", out
    assert {f["kind"] for f in out["error"]["findings"]} == {"failures_hidden"}
    assert "tests/store.test.js (ended before its tests ran)" in out["error"]["findings"][0]["tests"]


@needs_node
def test_a_failing_node_test_fails_the_check(node_claim: Path, capsys):
    (node_claim / STORE).write_text("export function add(habits, name) {\n  return habits;\n}\n")
    code, out = run_json(capsys, "done", "T-2.1", str(node_claim))
    assert code == 5 and out["error"]["code"] == "check_failed", out
    assert "add keeps a habit" in out["error"]["checks"][0]["output"]  # the spec reporter's output is kept


def test_which_commands_run_node_test(tmp_path: Path):
    for command in ("node --test", "node --import tsx --test tests/", "cd app && node --test"):
        assert runner_for(command, tmp_path) is NODE_TEST, command
    assert runner_for("npm test", tmp_path) is None  # no package.json: npm's script is unknown
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "node --test", "test:unit": "jest"}}))
    assert runner_for("npm test", tmp_path) is NODE_TEST and runner_for("npm t", tmp_path) is NODE_TEST
    assert runner_for("pnpm test", tmp_path) is NODE_TEST and runner_for("yarn test", tmp_path) is NODE_TEST
    assert runner_for("npm run test:unit", tmp_path) is None  # jest: gated on its exit code
    for command in ("node script.js --testing", "npx jest", "npm install", "nodemon --test"):
        assert runner_for(command, tmp_path) is None, command
    assert runner_for("uv run pytest", tmp_path) is PYTEST


def test_only_a_test_named_for_a_file_that_exists_ended_early(tmp_path: Path):
    """Node names the stand-in for a file that ended early by its path, with `\\` on Windows; a test whose name
    only ends in `.js` is a test."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/store.test.js").write_text("")
    report = tmp_path / "report.xml"
    report.write_text('<testsuites><testcase name="tests\\store.test.js"/><testcase name="loads config.js"/>'
                      '<testcase name="tests/gone.test.js"/></testsuites>')
    summary = read_report(report, NODE_TEST, tmp_path)
    assert summary["failed_tests"] == ["tests/store.test.js (ended before its tests ran)"], summary
    assert summary["errors"] == 1 and summary["passed"] == 2


@needs_node
def test_node_options_from_the_agents_shell_is_dropped(tmp_path: Path, monkeypatch):
    (tmp_path / "a.test.js").write_text('import { test } from "node:test";\ntest("a", () => {});\n')
    (tmp_path / "package.json").write_text(json.dumps({"type": "module"}))
    monkeypatch.setenv("NODE_OPTIONS", "--test-only")  # would run no test without `{ only: true }`
    result = run_check(Check("test", "node --test"), tmp_path)
    assert result["passed"] and result["results"]["passed"] == 1, result


@needs_node
def test_land_runs_nodes_tests_on_the_merged_result(node_claim: Path, node_repo: Path, capsys):
    (node_claim / STORE).write_text(HONEST)
    assert run_json(capsys, "done", "T-2.1", str(node_claim))[0] == 0
    git(node_claim, "add", "-A")
    git(node_claim, "commit", "-qm", "feat: T-2.1 store")
    code, out = run_json(capsys, "land", "--dir", str(node_repo))
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1"], out
    assert (node_repo / STORE).read_text() == HONEST
