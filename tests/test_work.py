"""Working the backlog from the CLI: next, show, start, done, the JSON contract, backlog.json and the lite profile."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from idea_to_scaffold.backlog import load_backlog
from idea_to_scaffold.cli import main
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import scaffold
from idea_to_scaffold.validate import validate

from .conftest import write


def run_json(capsys, *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def status_of(root: Path, ident: str) -> str:
    return next(i.status for i in load_backlog(Package.load(root)) if i.id == ident)


def py(code: str) -> str:
    """A shell command that runs a line of Python with this interpreter."""
    if os.name == "nt":  # cmd.exe: double quotes, and no single-quote quoting
        return subprocess.list2cmdline([sys.executable, "-c", code])
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def add_verify(backlog: Path, value: str) -> None:
    """Give T-1.1 a Verify field."""
    text = backlog.read_text()
    head = text.index("### T-1.1")
    status = text.index("- **Status:**", head)
    eol = text.index("\n", status) + 1
    backlog.write_text(text[:eol] + f"- **Verify:** {value}\n" + text[eol:])


@pytest.fixture
def repo(plan: Path, tmp_path: Path, golden) -> Path:
    """A scaffolded Streak repo whose test and lint commands are cheap Python one-liners."""
    def build(folder: Path) -> Path:
        root = scaffold(plan, folder / "out", git=False).path
        manifest = root / "docs/planning/package.yaml"
        text = manifest.read_text()
        text = text.replace('test: "uv run pytest"', f"test: {json.dumps(py('print(1)'))}")
        text = text.replace('lint: "uv run ruff check ."', f"lint: {json.dumps(py('print(2)'))}")
        manifest.write_text(text)
        return root
    return golden("work-repo", build, tmp_path)


# --- next -----------------------------------------------------------------------------------------------------

def test_next_follows_dependencies_and_exits_3_when_nothing_is_ready(mini: Path, capsys):
    code, out = run_json(capsys, "next", str(mini))
    assert code == 0 and out["schema"] == 1 and out["ok"]
    assert out["item"]["id"] == "T-1.1" and out["item"]["ready"]
    assert out["waiting"] == [{"id": "T-1.2", "title": "Second", "blocked_by": ["T-1.1"]}]
    assert "ready" not in out
    code, out = run_json(capsys, "next", str(mini), "--all")
    assert [i["id"] for i in out["ready"]] == ["T-1.1"]
    assert main(["start", "T-1.1", str(mini)]) == 0
    code, out = run_json(capsys, "next", str(mini))
    assert code == 3 and out["item"] is None and out["in_progress"] == ["T-1.1"]
    capsys.readouterr()
    assert main(["next", str(mini)]) == 3
    text = capsys.readouterr().out
    assert "nothing is ready" in text and "T-1.2 waits on T-1.1" in text


# --- start ----------------------------------------------------------------------------------------------------

def test_start_refuses_unmet_dependencies_unless_forced(mini: Path, capsys):
    code, out = run_json(capsys, "start", "T-1.2", str(mini))
    assert code == 4 and out["ok"] is False
    assert out["error"]["code"] == "blocked" and out["error"]["blocked_by"] == ["T-1.1"]
    assert status_of(mini, "T-1.2") == "todo"
    code, out = run_json(capsys, "start", "T-1.2", str(mini), "--force")
    assert code == 0 and out["forced"] and out["previous_status"] == "todo"
    assert status_of(mini, "T-1.2") == "in-progress"
    code, out = run_json(capsys, "start", "T-1.2", str(mini))
    assert code == 1 and out["error"]["code"] == "already_started"


def test_start_only_takes_scheduled_items(mini: Path, capsys):
    backlog = mini / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text() + (
        "\n### T-new-idea — Idea\n- **Status:** proposed\n"
        "\n### T-1.3 — Gone\n- **Milestone:** M1\n- **Status:** dropped\n- **Satisfies:** FR-001\n"
    ))
    assert run_json(capsys, "start", "T-new-idea", str(mini))[1]["error"]["code"] == "placeholder"
    assert run_json(capsys, "start", "T-1.3", str(mini))[1]["error"]["code"] == "bad_status"
    assert run_json(capsys, "start", "T-9.9", str(mini))[1]["error"]["code"] == "not_found"
    capsys.readouterr()
    assert main(["start", "T-9.9", str(mini)]) == 1
    assert "T-9.9 is not in the backlog" in capsys.readouterr().err


# --- done -----------------------------------------------------------------------------------------------------

def test_done_in_a_package_skips_checks_and_reports_what_is_now_ready(mini: Path, capsys):
    main(["start", "T-1.1", str(mini)])
    code, out = run_json(capsys, "done", str(mini))  # the only in-progress item
    assert code == 0 and out["id"] == "T-1.1" and out["previous_status"] == "in-progress"
    assert out["checks"] == [] and "planning package" in out["checks_skipped"]
    assert out["now_ready"] == ["T-1.2"]
    assert status_of(mini, "T-1.1") == "done"
    code, out = run_json(capsys, "done", str(mini))
    assert code == 1 and out["error"]["code"] == "which_item"
    assert run_json(capsys, "done", "T-1.1", str(mini))[1]["error"]["code"] == "already_done"


def test_done_runs_verify_test_and_lint_and_refuses_on_failure(repo: Path, capsys):
    backlog = repo / "docs/planning/07-backlog.md"
    add_verify(backlog, f"`{py('import sys; print(42); sys.exit(3)')}`")
    assert main(["start", "T-1.1", str(repo)]) == 0
    code, out = run_json(capsys, "done", "T-1.1", str(repo))
    assert code == 5 and out["error"]["code"] == "check_failed"
    verify = out["error"]["checks"][0]
    assert (verify["name"], verify["exit_code"], verify["passed"]) == ("verify", 3, False)
    assert "42" in verify["output"]
    assert status_of(repo, "T-1.1") == "in-progress"

    backlog.write_text(backlog.read_text().replace("sys.exit(3)", "sys.exit(0)"))
    code, out = run_json(capsys, "done", "T-1.1", str(repo))
    assert code == 0 and [c["name"] for c in out["checks"]] == ["verify", "test", "lint"]
    assert all(c["passed"] for c in out["checks"]) and out["checks_skipped"] is None
    assert status_of(repo, "T-1.1") == "done"


def test_done_with_a_manual_verify_reminds_the_human(repo: Path, capsys):
    backlog = repo / "docs/planning/07-backlog.md"
    add_verify(backlog, "manual: open the CI page")
    code, out = run_json(capsys, "done", "T-1.1", str(repo), "--no-verify")
    assert code == 0 and out["checks"] == [] and out["checks_skipped"] == "--no-verify"
    assert out["manual_verify"] == "manual: open the CI page"


# --- show -----------------------------------------------------------------------------------------------------

def test_show_prints_the_definition_and_the_docs_that_mention_it(plan: Path, capsys):
    code, out = run_json(capsys, "show", "FR-001", str(plan))
    assert code == 0 and out["kind"] == "Functional requirement"
    assert out["file"] == "planning/03-requirements.md" and out["anchor"] == "fr-001--add-a-habit"
    assert out["text"].startswith("### FR-001 — Add a habit") and "](" not in out["text"]
    assert "### FR-002" not in out["text"]
    assert "planning/07-backlog.md" in out["referenced_by"] and "planning/03-requirements.md" not in out["referenced_by"]
    lines = (plan / out["file"]).read_text().split("\n")
    assert lines[out["line"] - 1].startswith("### FR-001")

    code, out = run_json(capsys, "show", "T-1.1", str(plan))
    assert out["item"]["status"] == "todo" and out["item"]["ready"]

    code, out = run_json(capsys, "show", "ADR-0002", str(plan))
    assert out["anchor"] is None and "its:nav" not in out["text"]

    code, out = run_json(capsys, "show", "FR-999", str(plan))
    assert code == 1 and out["error"]["code"] == "not_found"


def test_show_in_a_repo_uses_repo_paths(repo: Path, capsys):
    code, out = run_json(capsys, "show", "NFR-001", str(repo))
    assert out["file"] == "docs/planning/03-requirements.md"
    assert out["text"].startswith("| NFR-001")
    capsys.readouterr()
    assert main(["show", "NFR-001", str(repo)]) == 0
    assert "mentioned in: docs/planning/" in capsys.readouterr().out


# --- backlog.json ---------------------------------------------------------------------------------------------

def test_backlog_json_mirror_is_on_demand_and_stays_current(mini: Path, capsys, tmp_path: Path):
    code, out = run_json(capsys, "backlog", str(mini))
    assert [i["id"] for i in out["items"]] == ["T-1.2", "T-1.1"] and out["cycles"] == []
    assert out["items"][0]["blocked_by"] == ["T-1.1"]
    assert run_json(capsys, "backlog", str(mini), "--ready")[1]["items"][0]["id"] == "T-1.1"
    mirror = mini / "planning/backlog.json"
    assert not mirror.exists()
    assert main(["backlog", str(mini), "--write"]) == 0
    assert json.loads(mirror.read_text())["items"][1]["status"] == "todo"
    main(["start", "T-1.1", str(mini)])
    assert json.loads(mirror.read_text())["items"][1]["status"] == "in-progress"


def test_scaffold_never_copies_the_mirror(plan: Path, tmp_path: Path):
    main(["backlog", str(plan), "--write"])
    assert (plan / "planning/backlog.json").is_file()
    repo = scaffold(plan, tmp_path / "out", git=False).path
    assert not (repo / "docs/planning/backlog.json").exists()


# --- lite profile ---------------------------------------------------------------------------------------------

def test_new_is_lite_unless_asked_for_full(tmp_path: Path, capsys):
    assert main(["new", str(tmp_path / "plain")]) == 0
    assert Package.load(tmp_path / "plain").profile == "lite"
    assert not (tmp_path / "plain/planning/06-project-plan.md").exists()


def test_a_manifest_without_a_profile_is_full(tmp_path: Path, capsys):
    assert main(["new", str(tmp_path / "old"), "--profile", "full"]) == 0
    manifest = tmp_path / "old/package.yaml"
    manifest.write_text("\n".join(line for line in manifest.read_text().split("\n") if not line.startswith("profile:")))
    assert Package.load(tmp_path / "old").profile == "full"


def test_new_lite_package_has_only_the_lite_docs(tmp_path: Path, capsys):
    dest = tmp_path / "lite"
    assert main(["new", str(dest), "--profile", "lite"]) == 0
    assert not (dest / "planning/02-features.md").exists() and not (dest / "planning/06-project-plan.md").exists()
    assert "profile: lite" in (dest / "package.yaml").read_text()
    agents = (dest / "AGENTS.md").read_text()
    assert "02-features.md" not in agents and "3. `07-backlog.md`" in agents
    assert (dest / ".gitignore").read_text().strip().endswith("planning/backlog.json")
    errors = validate(Package.load(dest), check_links_current=False).errors
    assert not any("missing" in e or "never defined" in e or "broken link" in e for e in errors), errors


def test_lite_package_validates_and_scaffolds_with_only_requirements_approved(mini: Path, tmp_path: Path):
    (mini / "planning/02-features.md").unlink()
    (mini / "planning/06-project-plan.md").unlink()
    manifest = mini / "package.yaml"
    manifest.write_text(manifest.read_text() + "profile: lite\n")
    write(mini, "planning/03-requirements.md", "requirements", "Requirements",
          "# Requirements\n\n## Feature: Thing\n\n### FR-001 — Do it\n**Supports:** G-1 · **Priority:** Must\n", "vision")
    backlog = mini / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text().replace("**Milestone:** M1", "**Milestone:** —"))
    write(mini, "planning/00-idea.md", "idea", "Idea", "# Idea\n")
    (mini / "AGENTS.md").write_text("".join(f"## {s}\n" for s in (
        "Project", "Source of truth", "Stack & commands", "Repository layout", "How to work", "Conventions",
        "Boundaries", "Playbooks")))
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text().replace("status: approved", "status: draft"))
    report = validate(Package.load(mini), check_links_current=False)
    assert report.errors == [] and report.ready, report.warnings
    assert not any("Milestone" in w for w in report.warnings)
    repo = scaffold(mini, tmp_path / "out", git=False).path
    assert Package.load(repo).profile == "lite"

    manifest.write_text(manifest.read_text().replace("profile: lite", "profile: tiny"))
    assert any("profile 'tiny' must be one of" in e for e in validate(Package.load(mini)).errors)
