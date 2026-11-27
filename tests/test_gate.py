"""Phase 3: a done gate that the agent being checked can't quietly weaken."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from idea_to_scaffold.cli import main
from idea_to_scaffold.hooks import HOOK_COMMAND, stop_hook
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import scaffold

from .test_landing import git_identity, py, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json, set_field, status_in


def status_line(root: Path, ident: str, name: str) -> str:
    text = (root / "docs/planning/07-backlog.md").read_text()
    section = text[text.index(f"### {ident}"):]
    return next((ln for ln in section.split("\n") if f"**{name}:**" in ln), "")

TEST = "tests/test_store.py"


@pytest.fixture
def claim(repo: Path, capsys) -> Path:  # noqa: F811
    """T-2.1 claimed in a worktree, with a test file on main and Touches covering the store and its test."""
    (repo / TEST).write_text("def test_add():\n    assert 1 + 1 == 2\n    assert True\n")
    git(repo, "add", TEST)
    git(repo, "commit", "-qm", "store test")
    set_field(repo, "T-2.1", "Touches", f"`src/streak/store.py`, `{TEST}`")
    set_field(repo, "T-2.1", "Verify", f"`{py('print(3)')}`")
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    return Path(out["claim"]["worktree"])


def finish(worktree: Path, *flags: str, capsys) -> tuple[int, dict]:
    (worktree / "src/streak/store.py").write_text("def add(): ...\n")
    return run_json(capsys, "done", "T-2.1", str(worktree), *flags)


# --- the phase gate: a branch that edits its own Verify or deletes a test is refused ---------------------------

def test_a_branch_that_edits_its_own_verify_is_refused(claim: Path, capsys):
    set_field(claim, "T-2.1", "Verify", "`true`")
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and out["error"]["code"] == "test_changes"
    assert [f["kind"] for f in out["error"]["findings"]] == ["verify_changed"]
    assert status_in(claim, "T-2.1") == "todo"
    code, out = finish(claim, "--allow-test-changes=planned test rewrite", capsys=capsys)
    assert code == 0 and out["findings"][0]["kind"] == "verify_changed"
    assert [c["name"] for c in out["checks"]] == ["verify", "test", "lint"]  # main's Verify ran, not the branch's
    assert "planned test rewrite" in status_line(claim, "T-2.1", "Override")  # the waiver and its reason are kept


def test_a_branch_that_deletes_a_test_is_refused(claim: Path, capsys):
    (claim / TEST).unlink()
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and out["error"]["code"] == "test_changes"
    assert out["error"]["findings"][0] == {"kind": "test_deleted", "file": TEST,
                                          "message": f"test file {TEST} was deleted", "mode": "enforce"}
    git(claim, "add", "-A")
    git(claim, "commit", "-qm", "drop the test")  # committed or not, the gate sees it
    assert finish(claim, capsys=capsys)[1]["error"]["code"] == "test_changes"


def test_removed_assertions_and_new_skips_are_refused(claim: Path, capsys):
    (claim / TEST).write_text("import pytest\n\n@pytest.mark.skip\ndef test_add():\n    pass\n")
    code, out = finish(claim, capsys=capsys)
    assert code == 5
    assert {f["kind"] for f in out["error"]["findings"]} == {"assertions_removed", "skip_added", "test_disabled"}
    (claim / TEST).write_text("def test_add():\n    assert 1 + 1 == 2\n    assert True\n\n"
                              "def test_remove():\n    assert 2 - 1 == 1\n")
    code, out = finish(claim, capsys=capsys)
    assert code == 0  # adding tests is always fine, but this Verify passes whatever the code does:
    assert [(f["kind"], f["mode"]) for f in out["findings"]] == [("tests_pass_on_parent", "warn")]


# --- phase 7: tamper checks that comments, module marks, config and new files can't get past -----------------

CHEATS = {
    "comment out the assertions": ("def test_add():\n    pass  # assert later\n    # assert 1 + 1 == 2\n",
                                   {"assertions_removed"}),
    "pytestmark skips the module": ("import pytest\npytestmark = pytest.mark.skip(reason='x')\n\n"
                                    "def test_add():\n    assert 1 + 1 == 2\n    assert True\n", {"skip_added"}),
    "from unittest import skip": ("from unittest import skip\n\n@skip\ndef test_add():\n    assert 1 + 1 == 2\n"
                                  "    assert True\n", {"skip_added"}),
    "return before asserting": ("def test_add():\n    return\n    assert 1 + 1 == 2\n    assert True\n",
                                {"test_disabled"}),
    "assert True keeps the count": ("def test_add():\n    assert True\n    assert True\n",
                                    {"assertions_weakened"}),
}


@pytest.mark.parametrize("cheat", sorted(CHEATS))
def test_tampering_that_line_patterns_missed_is_found(claim: Path, capsys, cheat):
    content, expected = CHEATS[cheat]
    (claim / TEST).write_text(content)
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    _, out = run_json(capsys, "done", "T-2.1", str(claim), "--no-verify", "--check")
    kinds = {f["kind"] for f in out.get("findings", []) + out.get("error", {}).get("findings", [])}
    assert expected <= kinds, out


def test_a_weakened_test_the_branch_added_is_found(claim: Path, repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Touches", "`src/streak/store.py`, `tests/`")
    git(claim, "merge", "-q", "main")
    (claim / "tests/test_new.py").write_text("def test_new():\n    assert add() == 3 or True\n")
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--no-verify", "--check")
    assert code == 0  # a new check: it warns before it enforces
    assert [(f["kind"], f["mode"]) for f in out["findings"]] == [("assertions_weakened", "warn")]


def test_a_conftest_that_deselects_a_test_is_found_and_can_enforce(claim: Path, repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Touches", "`src/streak/store.py`, `tests/`")
    pkg_yaml = repo / "docs/planning/package.yaml"
    pkg_yaml.write_text(pkg_yaml.read_text() + "gate:\n  checks:\n    test_config_changed: enforce\n")
    git(repo, "commit", "-qam", "enforce test config checks")
    git(claim, "merge", "-q", "main")
    (claim / "tests/types").mkdir(parents=True)
    (claim / "tests/types/conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    items[:] = [i for i in items if 'url' not in i.name]\n")
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--no-verify", "--check")
    assert code == 5 and [f["kind"] for f in out["error"]["findings"]] == ["test_config_changed"]


def test_pytest_settings_in_pyproject_are_test_config(claim: Path, repo: Path, capsys):  # noqa: F811
    from idea_to_scaffold.tamper import config_findings
    before = "[project]\nname = 'x'\n\n[tool.pytest.ini_options]\naddopts = '-q'\n"
    assert config_findings("pyproject.toml", before, before.replace("name = 'x'", "name = 'y'")) == []
    after = before.replace("'-q'", "'-q --deselect tests/test_url.py'")
    assert [f["kind"] for f in config_findings("pyproject.toml", before, after)] == ["test_config_changed"]


def test_comments_that_mention_assert_are_not_assertions():
    from idea_to_scaffold.tamper import compare
    before = "def test_a():\n    # assert this later\n    assert f() == 1\n"
    assert compare("tests/test_a.py", before, before.replace("    # assert this later\n", "")) == []
    js = "test('a', () => {\n  // expect(x).toBe(1)\n  expect(f()).toBe(1)\n})\n"
    assert compare("tests/a.test.js", js, js.replace("  // expect(x).toBe(1)\n", "")) == []
    assert [f["kind"] for f in compare("tests/a.test.js", js, js.replace("expect(f()).toBe(1)", "// gone"))] \
        == ["assertions_removed"]


# --- red on parent, held-out acceptance tests -------------------------------------------------------------------

RUN_TESTS = py("import runpy, sys; sys.path.insert(0, 'src'); ns = runpy.run_path('tests/test_store.py'); "
               "[f() for n, f in ns.items() if n.startswith('test_')]")


@pytest.fixture
def real_verify(repo: Path, capsys) -> Path:  # noqa: F811
    """T-2.1 claimed, with a Verify that really runs its test file against the code."""
    (repo / TEST).write_text("def test_placeholder():\n    assert 1 + 1 == 2\n")
    git(repo, "add", TEST)
    git(repo, "commit", "-qm", "store test")
    set_field(repo, "T-2.1", "Touches", f"`src/streak/store.py`, `{TEST}`")
    set_field(repo, "T-2.1", "Verify", f"`{RUN_TESTS}`")
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    return Path(out["claim"]["worktree"])


def test_a_test_that_fails_before_the_change_is_red_on_parent(real_verify: Path, capsys):
    (real_verify / TEST).write_text("from streak.store import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (real_verify / "src/streak/store.py").write_text("def add(a, b):\n    return a + b\n")
    code, out = run_json(capsys, "done", "T-2.1", str(real_verify), "--check")
    assert code == 0 and out["findings"] == [] and out["red_on_parent"]["passed_on_parent"] is False


def test_a_test_that_passes_without_the_change_is_found(real_verify: Path, repo: Path, capsys):  # noqa: F811
    (real_verify / TEST).write_text("def test_add():\n    assert isinstance(3, int)\n")
    (real_verify / "src/streak/store.py").write_text("def add(a, b):\n    return a + b\n")
    code, out = run_json(capsys, "done", "T-2.1", str(real_verify), "--check")
    assert code == 0 and out["red_on_parent"]["passed_on_parent"] is True
    assert [(f["kind"], f["mode"]) for f in out["warnings"]] == [("tests_pass_on_parent", "warn")]
    pkg_yaml = repo / "docs/planning/package.yaml"
    pkg_yaml.write_text(pkg_yaml.read_text() + "gate:\n  checks:\n    tests_pass_on_parent: enforce\n")
    git(repo, "commit", "-qam", "enforce red on parent")
    code, out = run_json(capsys, "done", "T-2.1", str(real_verify))
    assert code == 5 and out["error"]["findings"][0]["kind"] == "tests_pass_on_parent"


def test_held_out_tests_are_hidden_from_the_worktree_and_run_by_land(repo: Path, capsys):  # noqa: F811
    (repo / "tests/acceptance").mkdir(parents=True)
    (repo / "tests/acceptance/test_store_works.py").write_text("print('held out')\n")
    pkg_yaml = repo / "docs/planning/package.yaml"
    text = pkg_yaml.read_text()
    marker = py("import pathlib, sys; sys.exit(0 if pathlib.Path('src/streak/store.py').read_text().strip() else 3)")
    pkg_yaml.write_text(text.replace("commands:\n", f"commands:\n  acceptance: {json.dumps(marker)}\n", 1))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "held-out acceptance tests")
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    worktree = Path(out["claim"]["worktree"])
    assert out["held_out"] == "tests/acceptance/" and not (worktree / "tests/acceptance").exists()
    assert (repo / "tests/acceptance/test_store_works.py").exists()  # still there for the lead

    (worktree / "tests/acceptance").mkdir()
    (worktree / "tests/acceptance/test_store_works.py").write_text("print('rewritten')\n")
    code, out = run_json(capsys, "done", "T-2.1", str(worktree), "--no-verify")
    assert code == 5 and out["error"]["findings"][0]["kind"] == "acceptance_changed"
    import shutil
    shutil.rmtree(worktree / "tests/acceptance")

    (worktree / "src/streak/store.py").write_text("def add(a, b):\n    return a + b\n")
    code, out = run_json(capsys, "done", "T-2.1", str(worktree))
    assert code == 0 and "acceptance" not in [c["name"] for c in out["checks"]], out  # done never runs them
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "store")
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 0, out
    entries = out["landed"]
    assert any(c["name"] == "acceptance" for e in entries for c in e.get("checks", [])), out


def test_verify_is_read_from_the_integration_branch(claim: Path, repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Verify", f"`{py('import sys; sys.exit(9)')}`")  # main tightened it after the fork
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and out["error"]["code"] == "check_failed"
    assert out["error"]["checks"][0]["exit_code"] == 9


def test_changes_outside_touches_are_refused_but_planning_files_are_not(claim: Path, capsys):
    assert main(["propose", "Follow-up", "--dir", str(claim), "--satisfies", "FR-001"]) == 0  # planning: exempt
    (claim / "src/streak/other.py").write_text("x = 1\n")
    code, out = finish(claim, capsys=capsys)
    assert code == 5 and out["error"]["code"] == "outside_touches"
    assert out["error"]["findings"][0]["files"] == ["src/streak/other.py"]
    code, out = finish(claim, "--allow-outside-touches=shared file", capsys=capsys)
    assert code == 0 and out["touches_checked"] and out["verify_source"] == "main"


def test_done_check_runs_the_gate_without_changing_the_status(claim: Path, capsys):
    code, out = finish(claim, "--check", capsys=capsys)
    assert code == 0 and out["checked_only"] and out["status"] == "todo"
    assert [c["name"] for c in out["checks"]] == ["verify", "test", "lint"]
    assert finish(claim, capsys=capsys)[0] == 0
    code, out = finish(claim, "--check", capsys=capsys)  # a done item can be re-checked
    assert code == 0 and out["status"] == "done"


def test_land_refuses_a_branch_that_edits_its_verify_after_done(claim: Path, repo: Path, capsys):  # noqa: F811
    assert finish(claim, capsys=capsys)[0] == 0
    git(claim, "add", "-A")
    git(claim, "commit", "-qm", "feat: store")
    set_field(claim, "T-2.1", "Verify", "`true`")  # edited by hand, after its done passed
    code, out = run_json(capsys, "land", "--dir", str(repo))
    assert code == 7 and out["landed"] == []
    assert out["skipped"][0]["reason"] == "test_changes"
    code, out = run_json(capsys, "land", "--dir", str(repo), "--allow-test-changes=planned test rewrite")
    assert code == 0 and [x["id"] for x in out["landed"]] == ["T-2.1"]


# --- review ---------------------------------------------------------------------------------------------------

def test_review_bundles_the_item_its_requirements_and_the_diff(claim: Path, capsys):
    (claim / "src/streak/store.py").write_text("def add(): return 'stored'\n")
    code, out = run_json(capsys, "review", "T-2.1", str(claim))
    assert code == 0
    assert [r["id"] for r in out["requirements"]] == ["FR-001", "FR-002", "NFR-002"]
    assert all(r["text"] for r in out["requirements"])
    assert "+def add(): return 'stored'" in out["diff"] and not out["diff_truncated"]
    assert '"verdict": "approve" | "changes_requested"' in out["prompt"] and "### FR-001" in out["prompt"]
    assert out["commands"]["claude"].startswith("its review T-2.1 | claude -p")
    assert out["reply_schema"]["required"] == ["verdict", "requirements", "findings"]
    capsys.readouterr()
    assert main(["review", str(claim)]) == 0  # no ID: this claim's item, printed as the prompt
    assert capsys.readouterr().out.startswith("You are reviewing a change for backlog item T-2.1")


# --- Stop hooks -----------------------------------------------------------------------------------------------

def test_hooks_are_added_to_both_agents_and_keep_existing_settings(repo: Path, capsys):  # noqa: F811
    settings = repo / ".claude/settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    code, out = run_json(capsys, "hooks", str(repo))
    assert code == 0 and sorted(out["written"]) == [".claude/settings.json", ".codex/hooks.json"]
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == HOOK_COMMAND
    assert json.loads((repo / ".codex/hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 900
    code, out = run_json(capsys, "hooks", str(repo))
    assert out["written"] == [] and len(json.loads(settings.read_text())["hooks"]["Stop"]) == 1


@pytest.mark.parametrize("content", [{"hooks": []}, {"hooks": {"Stop": {}}}, {"hooks": {"Stop": ["x"]}}])
def test_an_odd_settings_file_is_a_clear_error(repo: Path, capsys, content):  # noqa: F811
    settings = repo / ".claude/settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(content))
    code, out = run_json(capsys, "hooks", str(repo), "--agent", "claude")
    assert code == 1 and out["error"]["code"] == "bad_hooks_file"
    assert json.loads(settings.read_text()) == content  # left as it was


def test_the_stop_hook_finds_the_package_from_the_payload_cwd(claim: Path, repo: Path, capsys, monkeypatch):  # noqa: F811
    import io
    (claim / TEST).unlink()
    monkeypatch.chdir(repo)  # the hook runs from the main checkout, the session works in the worktree
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(claim), "stop_hook_active": False})))
    assert main(["hook", "stop"]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block" and "test_store.py was deleted" in decision["reason"]


def test_a_failing_test_is_reported_once():
    from idea_to_scaffold.work import failure_report
    trace = "E   assert 1 == 2\nFAILED tests/test_store.py::test_add"
    report = failure_report([
        {"name": "verify", "command": "pytest tests/test_store.py", "exit_code": 1, "passed": False, "output": trace},
        {"name": "lint", "command": "ruff check .", "exit_code": 0, "passed": True, "output": ""},
        {"name": "test", "command": "pytest", "exit_code": 1, "passed": False, "output": trace},
    ])
    assert report.count("assert 1 == 2") == 1 and "test failed (exit 1): pytest  (output above)" in report
    assert "lint" not in report


def test_the_stop_hook_blocks_until_the_gate_passes(claim: Path, repo: Path, capsys):  # noqa: F811
    pkg = Package.load(claim)
    (claim / TEST).unlink()
    decision = stop_hook(pkg, json.dumps({"stop_hook_active": False}))
    assert decision["decision"] == "block" and "test_store.py was deleted" in decision["reason"]
    assert "its block T-2.1" in decision["reason"]
    assert stop_hook(pkg, json.dumps({"stop_hook_active": True})) is None  # never twice in a row
    git(claim, "checkout", "--", TEST)
    decision = stop_hook(pkg, "{}")
    assert decision["decision"] == "block" and "no files outside the planning folder" in decision["reason"]
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    decision = stop_hook(pkg, "{}")
    assert decision["decision"] == "block" and "isn't marked done" in decision["reason"]
    assert main(["done", "T-2.1", str(claim)]) == 0
    assert stop_hook(pkg, "{}") is None
    assert stop_hook(Package.load(repo), "{}") is None  # on main nothing is in progress: let it stop


def test_the_stop_hook_lets_a_blocked_agent_stop(claim: Path, capsys):
    (claim / TEST).unlink()
    assert main(["block", "T-2.1", str(claim), "--reason", "the spec contradicts itself"]) == 0
    assert stop_hook(Package.load(claim), "{}") is None


# --- scaffolded CI and hooks ----------------------------------------------------------------------------------

def test_scaffold_adds_plan_checks_to_ci_and_hooks_on_request(plan: Path, tmp_path: Path):
    root = scaffold(plan, tmp_path / "out", git=True, hooks=True).path
    ci = (root / ".github/workflows/ci.yml").read_text()
    assert 'its validate .' in ci and 'its link --check .' in ci and "idea-to-scaffold" in ci
    assert (root / ".claude/settings.json").is_file() and (root / ".codex/hooks.json").is_file()
    assert git(root, "status", "--porcelain") == ""  # committed with the scaffold
    assert "fetch-depth: 0" in ci and "its drift --check ." in ci and "actions/setup-python" in ci
    assert "uv python install" not in ci  # a bare pip in the setup command would miss that Python


def test_scaffolded_ci_pins_its(plan: Path, tmp_path: Path, monkeypatch):
    from idea_to_scaffold import scaffold as sc
    monkeypatch.setattr(sc, "_its_commit", lambda: "0123456789abcdef")
    ci = (scaffold(plan, tmp_path / "a").path / ".github/workflows/ci.yml").read_text()
    assert f'"{sc.ITS_SOURCE}@0123456789abcdef"' in ci
    monkeypatch.setattr(sc, "_its_commit", lambda: None)
    result = scaffold(plan, tmp_path / "b")
    assert any("unpinned" in w for w in result.warnings)


# --- gate-audit ---------------------------------------------------------------------------------------------------

def test_gate_audit_catches_every_seeded_cheat(claim: Path, repo: Path, capsys):  # noqa: F811
    code, out = run_json(capsys, "gate-audit", "T-2.1", str(repo))
    assert out["missed"] == [] and code == 0, [v for v in out["variants"] if not v["caught"]]
    assert out["test_file"] == TEST and out["total"] == len(out["variants"]) - len(out["skipped"])
    names = {v["name"] for v in out["variants"] if v["caught"]}
    assert {"weak_new_test", "conftest_deselect"} <= names  # the two cheats that passed in round 3
    assert git(repo, "status", "--porcelain") == ""  # the user's checkout is untouched
    capsys.readouterr()
    assert main(["gate-audit", str(repo)]) == 0
    assert "caught" in capsys.readouterr().out


def test_gate_audit_reports_a_miss(claim: Path, repo: Path, capsys, monkeypatch):  # noqa: F811
    from idea_to_scaffold import audit
    blind = audit.Variant("blind_spot", "a change nothing looks for", ("no_such_kind",),
                          lambda t: audit._write(t, "src/streak/store.py", "x = 1\n"))
    monkeypatch.setattr(audit, "VARIANTS", [*audit.VARIANTS, blind])
    code, out = run_json(capsys, "gate-audit", "T-2.1", str(repo))
    assert code == 10 and out["missed"] == ["blind_spot"]



def test_review_leaves_out_the_authors_own_account(claim: Path, capsys):
    (claim / "src/streak/store.py").write_text("def add(): return 'stored'\n")
    (claim / "NOTES.md").write_text("I am confident this fully implements FR-001.\n")
    set_field(claim, "T-2.1", "Status", "done")
    _, out = run_json(capsys, "review", "T-2.1", str(claim))
    assert "confident" not in out["prompt"] and "Status" not in out["diff"]
    assert sorted(out["omitted"]) == ["NOTES.md", "docs/planning/07-backlog.md"]
    assert "NOTES.md (not shown: the author's own notes)" in out["prompt"]


REPLY = {"verdict": "approve", "requirements": [{"id": "FR-001", "status": "met", "evidence": "test_add"}],
         "findings": [{"severity": "minor", "file": "src/streak/store.py", "line": 1, "message": "name it better"}]}


def test_a_review_reply_is_checked_against_a_severity_cut_off(tmp_path: Path, capsys):
    reply = tmp_path / "review.json"
    reply.write_text("Here is my review:\n```json\n" + json.dumps(REPLY) + "\n```\n")
    code, out = run_json(capsys, "review", "--reply", str(reply))
    assert code == 0 and out["passed"] and out["blocking"] == []
    code, out = run_json(capsys, "review", "--reply", str(reply), "--min-severity", "minor")
    assert code == 5 and not out["passed"] and len(out["blocking"]) == 1
    envelope = {"type": "result", "result": json.dumps({**REPLY, "requirements": [
        {"id": "FR-001", "status": "cant_tell", "evidence": ""}]})}
    reply.write_text(json.dumps(envelope))  # claude -p --output-format json
    code, out = run_json(capsys, "review", "--reply", str(reply))
    assert code == 5 and out["unmet"] == ["FR-001"]
    reply.write_text('{"verdict": "lgtm"}')
    code, out = run_json(capsys, "review", "--reply", str(reply))
    assert code == 1 and out["error"]["code"] == "bad_review"
    capsys.readouterr()
    assert main(["review", "--schema"]) == 0 and json.loads(capsys.readouterr().out)["type"] == "object"
