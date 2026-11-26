"""Unit tests for scanning, linking and backlog logic, on small hand-built packages."""
from __future__ import annotations

from pathlib import Path

from idea_to_scaffold.backlog import blocked_by, load_backlog, next_ready, set_status
from idea_to_scaffold.linker import link_package, scan_package
from idea_to_scaffold.package import Package
from idea_to_scaffold.validate import validate


def test_prose_lookalikes_are_not_ids(mini: Path):
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text() + (
        "\nBuilt on an M3 Mac for the C-suite. Like an F-16, it parses S-expressions; see G-7 notes.\n"
        "```mermaid\nflowchart LR\n  C-fake --> S-nope\n```\n"
    ))
    scan = scan_package(Package.load(mini))
    for word in ("M3", "C-suite", "F-16", "S-expressions", "G-7", "C-fake", "S-nope"):
        assert word not in scan.references
    errors = validate(Package.load(mini), check_links_current=False).errors
    assert not [e for e in errors if "never defined" in e], errors


def test_lookalikes_in_trace_fields_are_still_checked(mini: Path):
    feats = mini / "planning/02-features.md"
    feats.write_text(feats.read_text().replace("**Supports:** G-1", "**Supports:** G-9"))
    errors = validate(Package.load(mini), check_links_current=False).errors
    assert any(e.startswith("G-9: referenced") for e in errors)


def test_strict_ids_are_checked_anywhere(mini: Path):
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text() + "\nSee FR-042 for details.\n")
    errors = validate(Package.load(mini), check_links_current=False).errors
    assert any(e.startswith("FR-042: referenced") for e in errors)


def test_defined_ambiguous_id_in_prose_is_linked(mini: Path):
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text() + "\nWe ship M1 first.\n")
    link_package(Package.load(mini))
    assert "[M1](06-project-plan.md#milestones)" in vision.read_text()


def test_backticks_escape_linking(mini: Path):
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text() + "\nThe `M1` chip is unrelated.\n")
    link_package(Package.load(mini))
    assert "The `M1` chip" in vision.read_text()


def test_duplicate_definition_across_files(mini: Path):
    vision = mini / "planning/01-vision.md"
    vision.write_text(vision.read_text() + "\n### FR-001 — Stray copy\n")
    # FR-001 heading in vision isn't in its owning file, so it's a reference, not a second definition...
    assert not any("more than one" in e for e in validate(Package.load(mini), check_links_current=False).errors)
    req = mini / "planning/03-requirements.md"
    req.write_text(req.read_text() + "\n### FR-001 — Again\n**Feature:** F-01\n")
    errors = validate(Package.load(mini), check_links_current=False).errors
    assert any(e.startswith("FR-001: defined in more than one heading") for e in errors)


def test_next_item_respects_dependencies(mini: Path):
    pkg = Package.load(mini)
    items = load_backlog(pkg)
    assert [i.id for i in items] == ["T-1.2", "T-1.1"]
    assert next_ready(items).id == "T-1.1"  # T-1.2 is first but waits on T-1.1
    assert blocked_by(items[0], items) == ["T-1.1"]
    set_status(pkg, "T-1.1", "done")
    assert next_ready(load_backlog(pkg)).id == "T-1.2"
    set_status(pkg, "T-1.2", "blocked")
    assert next_ready(load_backlog(pkg)) is None


def test_index_has_no_live_backlog_state(mini: Path):
    link_package(Package.load(mini))
    index = (mini / "planning/README.md").read_text()
    assert "Next up" not in index and "| todo |" not in index
    assert "Run `its next`" in index


def test_link_is_idempotent_and_check_reports_drift(mini: Path):
    pkg = Package.load(mini)
    assert link_package(pkg)  # first run changes things
    assert link_package(pkg, check=True) == []
    assert link_package(pkg) == []
    req = mini / "planning/03-requirements.md"
    req.write_text(req.read_text().replace("### FR-001 — Do it", "### FR-001 — Do it well"))
    assert set(link_package(pkg, check=True)) >= {"planning/02-features.md", "planning/07-backlog.md"}


def test_backlinks_in_nav(mini: Path):
    link_package(Package.load(mini))
    req = (mini / "planning/03-requirements.md").read_text()
    assert "**Referenced by:** [Features](02-features.md), [Backlog](07-backlog.md)" in req
    assert "**Upstream:** [Features](02-features.md)" in req
