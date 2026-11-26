"""Unit tests for chat-export parsing and safe re-import."""
from __future__ import annotations

from pathlib import Path

import pytest

from idea_to_scaffold.cli import main
from idea_to_scaffold.importer import ImportConflict, import_export, normalise, parse_export

from .conftest import EXAMPLE


def test_parse_export_handles_crlf_and_unfenced_bodies():
    text = "<!-- file: a.md -->\r\nplain body\r\n<!-- end file -->\r\n"
    assert parse_export(text) == {"a.md": "plain body\n"}


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x.md", "a/../../x.md"])
def test_parse_export_rejects_unsafe_paths(bad):
    with pytest.raises(ValueError):
        parse_export(f"<!-- file: {bad} -->\nx\n<!-- end file -->")


def test_normalise_ignores_generated_links_and_nav():
    raw = "# T\n\nSee FR-001.\n"
    linked = "# T\n\n<!-- its:nav (generated) -->\n> nav\n<!-- /its:nav -->\n\nSee [FR-001](03-r.md#fr-001).\n"
    assert normalise(raw) == normalise(linked)


def test_reimport_same_export_is_a_no_op(plan: Path):
    written, _ = import_export(EXAMPLE, plan)
    assert written == []


def test_reimport_refuses_to_overwrite_hand_edits(plan: Path):
    vision = plan / "planning/01-vision.md"
    vision.write_text(vision.read_text() + "\nA hand-written note.\n")
    edited = vision.read_text()
    with pytest.raises(ImportConflict) as exc:
        import_export(EXAMPLE, plan)
    assert exc.value.paths == ["planning/01-vision.md"]
    assert vision.read_text() == edited  # nothing written
    assert main(["import", str(EXAMPLE), "--into", str(plan)]) == 1


def test_overwrite_keeps_original(plan: Path):
    vision = plan / "planning/01-vision.md"
    vision.write_text(vision.read_text() + "\nA hand-written note.\n")
    edited = vision.read_text()
    written, _ = import_export(EXAMPLE, plan, overwrite=True)
    assert written == ["planning/01-vision.md"]
    assert "hand-written" not in vision.read_text()
    assert (plan / "planning/01-vision.md.orig").read_text() == edited
