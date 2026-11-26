from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from idea_to_scaffold.cli import main
from idea_to_scaffold.importer import parse_export
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import ScaffoldError, scaffold
from idea_to_scaffold.validate import validate

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "streak" / "chat-export.md"


@pytest.fixture
def plan(tmp_path: Path) -> Path:
    dest = tmp_path / "streak-plan"
    assert main(["import", str(EXAMPLE), "--into", str(dest)]) == 0
    return dest


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
    req.write_text(req.read_text().replace("**Feature:** F-02", "**Feature:** none"))
    feats = plan / "planning/02-features.md"
    feats.write_text(feats.read_text().replace("**Requirements:** FR-003", "**Requirements:** FR-099"))
    errors = validate(Package.load(plan)).errors
    assert any(e.startswith("FR-003: functional requirement must name") for e in errors)
    assert any(e.startswith("FR-099: referenced") for e in errors)


def test_scaffold_creates_agent_files_and_commits(plan: Path, tmp_path: Path):
    result = scaffold(plan, tmp_path / "out")
    repo = result.path
    for rel in [
        "AGENTS.md", "CLAUDE.md", "HANDOFF.md", ".github/copilot-instructions.md",
        ".cursor/rules/00-project.mdc", ".claude/skills/implement-task/SKILL.md",
        "docs/planning/03-requirements.md", "docs/playbooks/write-adr.md",
        "pyproject.toml", "src/streak/__init__.py", "tests/test_smoke.py",
    ]:
        assert (repo / rel).is_file(), rel
    agents = (repo / "AGENTS.md").read_text()
    assert "`uv run pytest`" in agents and "<test>" not in agents
    assert "T-1.1 — Project setup verified" in (repo / "HANDOFF.md").read_text()
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
