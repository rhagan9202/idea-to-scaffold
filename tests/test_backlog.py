"""The backlog model: parsing with positions, status writes, readiness, cycles, placeholders, Verify and Touches."""
from __future__ import annotations

from pathlib import Path

import pytest

from idea_to_scaffold.backlog import (
    BacklogError,
    find_cycles,
    item_section,
    parse_backlog,
    ready_items,
    set_status,
)
from idea_to_scaffold.cli import main
from idea_to_scaffold.ids import ID_RE, field_value, is_placeholder
from idea_to_scaffold.package import Package
from idea_to_scaffold.validate import validate

from .conftest import write

ITEM = "### {id} — {title}\n- **Milestone:** M1\n- **Status:** {status}\n- **Satisfies:** FR-001\n- **Depends on:** {deps}\n"


def backlog(root: Path, *items: str) -> None:
    write(root, "planning/07-backlog.md", "backlog", "Backlog", "# Backlog\n\n## Items\n\n" + "\n".join(items),
          "project-plan")


def errors_of(root: Path) -> list[str]:
    return validate(Package.load(root), check_links_current=False).errors


def warnings_of(root: Path) -> list[str]:
    return validate(Package.load(root), check_links_current=False).warnings


# --- parsing --------------------------------------------------------------------------------------------------

def test_empty_field_does_not_swallow_the_next_line():
    text = (
        "### T-1.1 — A\n- **Milestone:**\n- **Status:** todo\n- **Satisfies:** NFR-002\n"
        "- **Depends on:**\n- **Do:** extend T-1.2 later\n"
    )
    [item] = parse_backlog(text)
    assert item.milestone == ""
    assert item.status == "todo"
    assert item.depends_on == []
    assert field_value("**Depends on:**\n- **Do:** x", "Depends on") == ""


def test_fields_on_one_line_and_colon_outside_bold():
    [item] = parse_backlog("### T-1.1 — A\n**Milestone:** M1 · **Status**: `todo`   # agreed\n")
    assert item.milestone == "M1"
    assert item.status == "todo"


def test_a_missing_field_is_not_read_from_a_nested_item():
    text = "## T-1.8 — Parent\n- **Status:** todo\n\n### T-1.9 — Child\n- **Milestone:** M2\n- **Status:** done\n"
    parent, child = parse_backlog(text)
    assert (parent.id, parent.milestone) == ("T-1.8", "")
    assert (child.id, child.milestone, child.status) == ("T-1.9", "M2", "done")


def test_retired_items_are_still_items():
    [item] = parse_backlog("### ~~T-1.7 — Retired~~\n- **Status:** dropped\n")
    assert (item.id, item.title, item.status) == ("T-1.7", "Retired", "dropped")


def test_verify_and_touches():
    [item] = parse_backlog(
        "### T-2.1 — Store\n- **Status:** todo\n- **Verify:** `uv run pytest tests/test_store.py`\n"
        "- **Touches:** `src/store/`, tests/test_store.py\n"
    )
    assert item.verify_command == "uv run pytest tests/test_store.py"
    assert item.touches == ["src/store/", "tests/test_store.py"]
    assert item.to_dict()["touches"] == ["src/store/", "tests/test_store.py"]


def test_placeholder_ids_are_backlog_ids():
    assert ID_RE.fullmatch("T-new-plant-aliases")
    assert is_placeholder("T-new-plant-aliases") and not is_placeholder("T-2.4")
    [item] = parse_backlog("### T-new-plant-aliases — Plant aliases\n- **Status:** proposed\n")
    assert item.id == "T-new-plant-aliases" and item.placeholder


# --- writing --------------------------------------------------------------------------------------------------

def test_set_status_changes_only_the_value(mini: Path):
    path = mini / "planning/07-backlog.md"
    before = path.read_text()
    assert set_status(Package.load(mini), "T-1.1", "in-progress") == "todo"
    after = path.read_text()
    assert after == before.replace(
        "### T-1.1 — First\n- **Milestone:** M1\n- **Status:** todo",
        "### T-1.1 — First\n- **Milestone:** M1\n- **Status:** in-progress",
    )


def test_set_status_keeps_crlf_and_rejects_bad_input(mini: Path):
    path = mini / "planning/07-backlog.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    pkg = Package.load(mini)
    set_status(pkg, "T-1.2", "done")
    raw = path.read_bytes()
    assert b"- **Status:** done\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    with pytest.raises(BacklogError, match="must be one of"):
        set_status(pkg, "T-1.2", "finished")
    with pytest.raises(BacklogError, match="not in the backlog"):
        set_status(pkg, "T-9.9", "done")


def test_item_section(mini: Path):
    section = item_section(Package.load(mini), "T-1.2")
    assert section.startswith("### T-1.2 — Second") and "T-1.1 — First" not in section


# --- readiness and cycles -------------------------------------------------------------------------------------

def test_ready_items_returns_all_ready_and_skips_claimed():
    items = parse_backlog(
        ITEM.format(id="T-1.1", title="A", status="done", deps="—")
        + ITEM.format(id="T-2.1", title="B", status="todo", deps="T-1.1")
        + ITEM.format(id="T-2.2", title="C", status="todo", deps="T-1.1")
        + ITEM.format(id="T-2.3", title="D", status="todo", deps="T-2.1")
    )
    assert [i.id for i in ready_items(items)] == ["T-2.1", "T-2.2"]
    assert [i.id for i in ready_items(items, claimed={"T-2.1"})] == ["T-2.2"]


def test_cycles_are_found_and_reported(mini: Path):
    items = parse_backlog(
        ITEM.format(id="T-1.1", title="A", status="todo", deps="T-1.2")
        + ITEM.format(id="T-1.2", title="B", status="todo", deps="T-1.1")
        + ITEM.format(id="T-1.3", title="C", status="todo", deps="T-1.3")
    )
    assert sorted(len(c) for c in find_cycles(items)) == [2, 3]
    backlog(mini, ITEM.format(id="T-1.1", title="A", status="todo", deps="T-1.2"),
            ITEM.format(id="T-1.2", title="B", status="todo", deps="T-1.1"))
    assert any("dependency cycle" in e for e in errors_of(mini))


def test_dependency_rules(mini: Path):
    backlog(
        mini,
        ITEM.format(id="T-1.1", title="A", status="dropped", deps="—"),
        ITEM.format(id="T-1.2", title="B", status="todo", deps="T-1.1, FR-001"),
        ITEM.format(id="T-1.3", title="C", status="todo", deps="—"),
        ITEM.format(id="T-1.4", title="D", status="done", deps="T-1.3"),
    )
    errors, warnings = errors_of(mini), warnings_of(mini)
    assert any(e.startswith("T-1.2: depends on T-1.1, which is dropped") for e in errors)
    assert any(w.startswith("T-1.2: Depends on lists FR-001") for w in warnings)
    assert any(w.startswith("T-1.4: is done but its dependency T-1.3 is todo") for w in warnings)


def test_placeholder_and_touches_rules(mini: Path):
    backlog(
        mini,
        ITEM.format(id="T-1.1", title="A", status="todo", deps="—") + "- **Touches:** ../outside, /etc/x, src/ok\n",
        ITEM.format(id="T-new-idea", title="Idea", status="proposed", deps="—"),
        ITEM.format(id="T-new-sched", title="Sched", status="todo", deps="—"),
    )
    errors = errors_of(mini)
    assert any("T-new-sched: placeholder IDs" in e for e in errors)
    assert not any("T-new-idea" in e for e in errors)
    assert sum("Touches" in e for e in errors) == 2


def test_must_requirements_need_a_backlog_item(mini: Path):
    req = mini / "planning/03-requirements.md"
    req.write_text(req.read_text() + "\n### FR-002 — Also\n**Feature:** F-01 · **Priority:** Must\n")
    assert any(e.startswith("FR-002: is a Must requirement") for e in errors_of(mini))
    backlog(mini, ITEM.format(id="T-1.1", title="A", status="todo", deps="—").replace("FR-001", "FR-001, FR-002"))
    assert not any("Must requirement" in e for e in errors_of(mini))


def test_cli_backlog_rejects_unknown_status(mini: Path, capsys):
    assert main(["backlog", str(mini), "--status", "bogus"]) == 2
    assert main(["backlog", str(mini), "--status", "done"]) == 0
    assert "no backlog items with status done" in capsys.readouterr().out
