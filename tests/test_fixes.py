"""Regression tests for bugs found in the design review (linking, import paths, manifest types, validation)."""
from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

import pytest

from idea_to_scaffold.cli import main
from idea_to_scaffold.ids import scan_lines
from idea_to_scaffold.importer import ImportErrorBadPath, _safe_path, import_export
from idea_to_scaffold.linker import link_package
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import scaffold
from idea_to_scaffold.validate import validate

from .conftest import EXAMPLE, write


def errors_of(root: Path) -> list[str]:
    return validate(Package.load(root), check_links_current=False).errors


# --- linking --------------------------------------------------------------------------------------------------

def test_link_leaves_external_links_images_and_code_alone(mini: Path):
    vision = mini / "planning/01-vision.md"
    extra = (
        "\nTracked in [FR-001](https://jira.example.com/browse/FR-001).\n"
        "![FR-001](wireframes/x.png)\n"
        "Literal ``FR-001`` and <!-- FR-001 in a comment --> stay put.\n"
    )
    vision.write_text(vision.read_text() + extra)
    link_package(Package.load(mini))
    text = vision.read_text()
    assert extra.strip() in text


def test_link_round_trip_only_touches_ids_and_nav(plan: Path):
    """Linking a linked package changes nothing; un-linking then linking again restores it exactly."""
    pkg = Package.load(plan)
    assert link_package(pkg, check=True) == []
    backlog = plan / "planning/07-backlog.md"
    linked = backlog.read_text()
    backlog.write_text(re.sub(r"\[((?:T|FR|NFR|M)[-\w.]*)\]\([^)]*\)", r"\1", linked))
    link_package(pkg)
    assert backlog.read_text() == linked


def test_index_lists_real_ids_not_examples(plan: Path):
    index = (plan / "planning/README.md").read_text()
    assert "C-api" not in index and "S-home" not in index
    assert "Goals (G-1, G-2)" in index


# --- import ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["C:/Windows/x.md", "C:x.md", ".git/hooks/pre-commit"])
def test_safe_path_rejects_drive_and_git_paths(bad):
    with pytest.raises(ImportErrorBadPath):
        _safe_path(bad)
    assert PureWindowsPath(r"D:\pkg") / _safe_path("planning/01-vision.md") == PureWindowsPath(
        r"D:\pkg\planning\01-vision.md"
    )


def test_import_into_a_scaffolded_repo_is_refused(plan: Path, tmp_path: Path):
    repo = scaffold(plan, tmp_path / "out", git=False).path
    with pytest.raises(ValueError, match="scaffolded repo"):
        import_export(EXAMPLE, repo, overwrite=True)


def test_seeded_defaults_have_a_real_date(plan: Path):
    assert "YYYY-MM-DD" not in (plan / "playbooks/implement-task.md").read_text()


# --- manifest and documents -----------------------------------------------------------------------------------

def test_scalar_agents_value_still_generates_adapters(plan: Path, tmp_path: Path):
    manifest = plan / "package.yaml"
    manifest.write_text(re.sub(r"(?m)^agents:.*$", "agents: claude", manifest.read_text()))
    assert Package.load(plan).agents == ["claude"]
    repo = scaffold(plan, tmp_path / "out", git=False).path
    assert (repo / "CLAUDE.md").is_file()


def test_wrong_typed_commands_is_an_error_not_a_crash(plan: Path):
    manifest = plan / "package.yaml"
    manifest.write_text(re.sub(r"(?ms)^commands:\n(?:  .*\n)+", "commands: [uv sync]\n", manifest.read_text()))
    assert Package.load(plan).commands == {}
    assert any("commands must be a mapping" in e for e in errors_of(plan))


def test_document_problems_are_reported(plan: Path):
    vision = plan / "planning/01-vision.md"
    vision.write_text(vision.read_text().replace("status: approved", "status: aproved", 1))
    assert any("status 'aproved' must be one of" in e for e in errors_of(plan))
    vision.write_text(vision.read_text().replace("title:", "title: [unclosed", 1))
    assert any("front matter is not valid YAML" in e for e in errors_of(plan))
    (plan / "planning/00-idea.md").write_bytes(b"\xff\xfe bad bytes")
    assert any("not valid UTF-8" in e for e in errors_of(plan))


def test_conflict_markers_are_reported(plan: Path):
    backlog = plan / "planning/07-backlog.md"
    backlog.write_text(backlog.read_text() + "\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n")
    assert any("merge conflict marker" in e for e in errors_of(plan))


def test_plan_checklist_is_a_task_list(plan: Path):
    proj = plan / "planning/06-project-plan.md"
    proj.write_text(proj.read_text() + "\n- [ ] T-1.1 skeleton\n")
    assert any("task checklist" in e for e in errors_of(plan))


def test_duplicate_table_row_definition(mini: Path):
    write(mini, "planning/03-requirements.md", "requirements", "Requirements",
          "# Requirements\n\n### FR-001 — Do it\n**Feature:** F-01\n\n| ID | Req |\n| -- | --- |\n"
          "| NFR-002 | a |\n| NFR-002 | b |\n", "features")
    assert any(e.startswith("NFR-002: defined more than once") for e in errors_of(mini))


def test_retired_ids_stay_defined(mini: Path):
    feats = mini / "planning/02-features.md"
    feats.write_text(feats.read_text() + "\n## ~~F-02 — Bulk import~~ (dropped)\n**Supports:** G-1\n")
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text() + "\nF-02 was dropped.\n")
    assert not any("F-02" in e for e in errors_of(mini))


def test_blank_template_only_reports_its_placeholders(tmp_path: Path, capsys):
    main(["new", str(tmp_path / "blank")])
    errors = errors_of(tmp_path / "blank")
    assert not any("never defined" in e or "broken link" in e for e in errors), errors


def test_no_git_with_github_is_rejected(plan: Path, tmp_path: Path, capsys):
    assert main(["scaffold", str(plan), "--out", str(tmp_path / "o"), "--no-git", "--github", "me/x"]) == 2
    assert "--github needs git" in capsys.readouterr().err


# --- anchors and indented code --------------------------------------------------------------------------------

def test_repeated_headings_get_numbered_anchors():
    lines = scan_lines("# Doc\n\n## Notes\n\nx\n\n## Notes\n\ny\n\n## Notes 1\n")
    assert [ln.anchor for ln in lines if ln.heading] == ["doc", "notes", "notes-1", "notes-1-1"]
    assert lines[8].section_anchor == "notes-1"


def test_ids_under_a_repeated_heading_link_to_the_right_section(mini: Path):
    write(mini, "planning/01-vision.md", "vision", "Vision",
          "# Vision\n\n## Notes\nNothing here.\n\n## Notes\n- **G-1** — Be tiny.\n")
    pkg = Package.load(mini)
    link_package(pkg)
    feats = (mini / "planning/02-features.md").read_text()
    assert "(01-vision.md#notes-1)" in feats
    assert not any("heading that doesn't exist" in e for e in validate(pkg).errors)


def test_indented_code_is_not_linked_but_nested_list_items_are(mini: Path):
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text() + (
        "\nExample output:\n\n    $ tool --check FR-001\n    ok\n\n"
        "- Parent item\n\n    Continuation that mentions FR-001.\n\n"
        "1. Step\n    - nested bullet about FR-001\n"
    ))
    link_package(Package.load(mini))
    text = vision.read_text()
    assert "    $ tool --check FR-001\n" in text
    assert "Continuation that mentions [FR-001](" in text
    assert "nested bullet about [FR-001](" in text
