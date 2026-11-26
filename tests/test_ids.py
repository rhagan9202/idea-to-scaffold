"""Unit tests for ID recognition and definition detection."""
from __future__ import annotations

import pytest

from idea_to_scaffold.ids import (
    ID_RE,
    definition_on_line,
    github_slug,
    id_kind,
    scan_lines,
    sections,
    strip_links,
    trace_field_spans,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see FR-001 and NFR-002", ["FR-001", "NFR-002"]),
        ("T-2.3 depends on T-1.1.", ["T-2.3", "T-1.1"]),
        ("ADR-0002 chose SQLite", ["ADR-0002"]),
        ("[FR-003](03-requirements.md#fr-003--x)", ["FR-003"]),
        ("adr/ADR-0002-sqlite.md and wireframes/S-home.md", []),  # paths are not references
        ("NFR-001", ["NFR-001"]),  # not also FR-001
        ("XFR-001 FR-01", []),
    ],
)
def test_id_regex(text, expected):
    assert [m.group(1) for m in ID_RE.finditer(text)] == expected


@pytest.mark.parametrize(
    "ident, kind",
    [("G-1", "G"), ("F-01", "F"), ("FR-001", "FR"), ("NFR-001", "NFR"), ("C-api", "C"),
     ("S-home", "S"), ("M2", "M"), ("T-1.1", "T"), ("ADR-0001", "ADR")],
)
def test_id_kind(ident, kind):
    assert id_kind(ident) == kind


@pytest.mark.parametrize(
    "line, defines",
    [
        ("### FR-003 — Check in", "FR-003"),
        ("## F-01 — Manage habits", "F-01"),
        ("- **G-1** — Logging takes one command.", "G-1"),
        ("| C-store | Persist | sqlite3 | FR-001 |", "C-store"),
        ("| [F-01](#f-01--x) | Manage | Must |", "F-01"),
        ("| ID | Feature |", None),
        ("**Supports:** G-1", None),
        ("Built for the C-suite.", None),
        ("## Goals", None),
    ],
)
def test_definition_on_line(line, defines):
    assert definition_on_line(scan_lines(line)[0]) == defines


def test_definitions_inside_code_are_ignored():
    lines = scan_lines("```text\n| C-fake | x |\n```")
    assert [definition_on_line(ln) for ln in lines] == [None, None, None]


def test_trace_field_spans_cover_only_field_values():
    text = "**Priority:** Must · **Supports:** G-1, G-2"
    spans = trace_field_spans(text)
    covered = [m.group(1) for m in ID_RE.finditer(text) if any(a <= m.start() < b for a, b in spans)]
    assert covered == ["G-1", "G-2"]
    assert trace_field_spans("Runs on an M1 Mac") == []


@pytest.mark.parametrize(
    "heading, slug",
    [
        ("FR-003 — Check in for today", "fr-003--check-in-for-today"),
        ("T-1.1 — Project setup verified", "t-11--project-setup-verified"),
        ("Screens & navigation", "screens--navigation"),
        ("[F-01](x.md) — Linked", "f-01--linked"),
    ],
)
def test_github_slug(heading, slug):
    assert github_slug(heading) == slug


def test_scan_lines_tracks_fences_and_sections():
    body = "# Title\ntext\n````md\n```\n# not a heading\n```\n````\n## Next\nmore"
    lines = scan_lines(body)
    assert [ln.in_code for ln in lines] == [False, False, True, True, True, True, True, False, False]
    assert lines[4].heading is None
    assert lines[-1].section == "Next"


def test_sections_nest_by_level():
    body = "## A\na\n### A1\na1\n## B\nb"
    assert [(h, s.strip()) for h, s in sections(body)] == [("A", "a\n### A1\na1"), ("A1", "a1"), ("B", "b")]


def test_strip_links():
    assert strip_links("[FR-001](a.md#b), [x](y)") == "FR-001, x"
