from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from idea_to_scaffold.backlog import load_backlog
from idea_to_scaffold.cli import main
from idea_to_scaffold.importer import parse_export
from idea_to_scaffold.linker import link_package
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import ScaffoldError, scaffold
from idea_to_scaffold.validate import validate


def test_parse_export_strips_fences_and_last_wins():
    text = (
        "chatter\n<!-- file: a.md -->\n```markdown\nfirst\n```\n<!-- end file -->\n"
        "<!-- file: a.md -->\n````markdown\nsecond\n```py\ncode\n```\n````\n<!-- end file -->\n"
    )
    files = parse_export(text)
    assert files == {"a.md": "second\n```py\ncode\n```\n"}


def test_parse_export_rejects_escaping_paths():
    with pytest.raises(ValueError):
        parse_export("<!-- file: ../evil.md -->\nx\n<!-- end file -->")


def test_example_package_is_ready(plan: Path):
    report = validate(Package.load(plan))
    assert report.errors == []
    assert report.ready


def test_validation_catches_broken_traceability(plan: Path):
    req = plan / "planning/03-requirements.md"
    text = req.read_text()
    req.write_text(re.sub(r"\*\*Feature:\*\* \[F-02\]\([^)]*\)", "**Feature:** none", text))
    feats = plan / "planning/02-features.md"
    feats.write_text(
        re.sub(r"\*\*Requirements:\*\* \[FR-003\]\([^)]*\)", "**Requirements:** FR-099", feats.read_text())
    )
    errors = validate(Package.load(plan)).errors
    assert any(e.startswith("FR-003: functional requirement must name") for e in errors)
    assert any(e.startswith("FR-099: referenced") for e in errors)


def test_scaffold_creates_agent_files_and_commits(plan: Path, tmp_path: Path):
    result = scaffold(plan, tmp_path / "out")
    repo = result.path
    for rel in [
        "AGENTS.md", "CLAUDE.md", "HANDOFF.md", ".github/copilot-instructions.md",
        ".cursor/rules/00-project.mdc", ".claude/skills/implement-task/SKILL.md",
        "docs/planning/03-requirements.md", "docs/planning/07-backlog.md", "docs/planning/README.md",
        "docs/playbooks/write-adr.md",
        "pyproject.toml", "src/streak/__init__.py", "tests/test_smoke.py",
    ]:
        assert (repo / rel).is_file(), rel
    agents = (repo / "AGENTS.md").read_text()
    assert "`uv run pytest`" in agents and "<test>" not in agents
    assert "run its next to pick" in (repo / "HANDOFF.md").read_text()
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True)
    assert "scaffold streak" in log.stdout
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout == ""


def test_generated_project_tests_pass(plan: Path, tmp_path: Path):
    repo = scaffold(plan, tmp_path / "out", git=False).path
    proc = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_scaffold_refuses_unready_and_existing(plan: Path, tmp_path: Path):
    vision = plan / "planning/01-vision.md"
    vision.write_text(vision.read_text().replace("status: approved", "status: draft", 1))
    with pytest.raises(ScaffoldError, match="not ready"):
        scaffold(plan, tmp_path / "out")
    scaffold(plan, tmp_path / "out", allow_unready=True, git=False)
    with pytest.raises(ScaffoldError, match="already exists"):
        scaffold(plan, tmp_path / "out", allow_unready=True, git=False)


def test_new_creates_blank_package(tmp_path: Path):
    dest = tmp_path / "blank"
    assert main(["new", str(dest)]) == 0
    assert (dest / "planning/06-project-plan.md").is_file()
    assert (dest / "playbooks/implement-task.md").is_file()


def test_import_cross_links_ids_and_adds_nav(plan: Path):
    backlog = (plan / "planning/07-backlog.md").read_text()
    assert "[FR-001](03-requirements.md#fr-001--add-a-habit)" in backlog
    assert "[T-1.1](#t-11--project-setup-verified)" in backlog  # same-doc link to its own heading
    assert "<!-- its:nav" in backlog and "**Referenced by:**" in (plan / "planning/03-requirements.md").read_text()
    index = (plan / "planning/README.md").read_text()
    assert "Run `its next`" in index and "Next up" not in index
    assert "| [FR-003](03-requirements.md#fr-003--check-in-for-today) |" in index
    assert link_package(Package.load(plan), check=True) == []  # idempotent


def test_heading_rename_is_detected_and_repaired(plan: Path):
    req = plan / "planning/03-requirements.md"
    req.write_text(req.read_text().replace("### FR-003 — Check in for today", "### FR-003 — Mark done today"))
    pkg = Package.load(plan)
    errors = validate(pkg).errors
    assert any("fr-003--check-in-for-today" in e and "heading that doesn't exist" in e for e in errors)
    assert "planning/07-backlog.md" in link_package(pkg, check=True)
    link_package(pkg)
    assert validate(Package.load(plan)).errors == []
    assert "#fr-003--mark-done-today" in (plan / "planning/07-backlog.md").read_text()


def test_status_change_leaves_generated_files_alone(plan: Path):
    """The index carries no backlog status, so a status change on a task branch can't make it stale or conflict."""
    backlog = plan / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text().replace("- **Status:** todo", "- **Status:** done", 1))
    assert link_package(Package.load(plan), check=True) == []
    assert main(["link", "--check", str(plan)]) == 0


def test_backlog_rules(plan: Path):
    items = load_backlog(Package.load(plan))
    assert [i.id for i in items] == ["T-1.1", "T-2.1", "T-2.2", "T-2.3", "T-9.1"]
    assert items[-1].status == "proposed"
    backlog = plan / "planning/07-backlog.md"
    text = backlog.read_text()
    text = text.replace("- **Status:** todo", "- **Status:** finished", 1)
    text = re.sub(r"- \*\*Milestone:\*\* \[M2\]\([^)]*\)", "- **Milestone:** M7", text, count=1)
    backlog.write_text(text)
    errors = validate(Package.load(plan)).errors
    assert any(e.startswith("T-1.1: Status 'finished'") for e in errors)
    assert any(e.startswith("T-2.1: Milestone 'M7'") for e in errors)


def test_plan_must_not_hold_tasks(plan: Path):
    planf = plan / "planning/06-project-plan.md"
    planf.write_text(planf.read_text() + "\n### T-5.1 — Sneaky task\n- **Satisfies:** FR-001\n")
    errors = validate(Package.load(plan)).errors
    assert any("move them to 07-backlog.md" in e for e in errors)


def test_scaffolded_repo_validates_in_place(plan: Path, tmp_path: Path):
    repo = scaffold(plan, tmp_path / "out", git=False).path
    pkg = Package.load(repo)
    assert pkg.repo_layout
    assert validate(pkg).errors == []
    assert link_package(pkg, check=True) == []
    assert "docs/planning/backlog.json" in (repo / ".gitignore").read_text()
