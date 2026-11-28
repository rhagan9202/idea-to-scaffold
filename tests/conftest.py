from __future__ import annotations

from pathlib import Path

import pytest

from idea_to_scaffold.cli import main

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "streak" / "chat-export.md"


@pytest.fixture(autouse=True)
def state_home(tmp_path_factory, monkeypatch) -> Path:
    """Keep each test's `its` state (claims, fork points, ledger, launch files) out of the real home folder."""
    folder = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("XDG_STATE_HOME", str(folder))
    return folder


@pytest.fixture
def plan(tmp_path: Path) -> Path:
    """The Streak example, imported (and therefore cross-linked) into a fresh package folder."""
    dest = tmp_path / "streak-plan"
    assert main(["import", str(EXAMPLE), "--into", str(dest)]) == 0
    return dest


FM = "---\nid: {id}\ntitle: {title}\nstatus: approved\nversion: 1.0\ndepends_on: [{deps}]\n---\n\n"


def write(root: Path, rel: str, doc_id: str, title: str, body: str, deps: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FM.format(id=doc_id, title=title, deps=deps) + body, encoding="utf-8")


@pytest.fixture
def mini(tmp_path: Path) -> Path:
    root = tmp_path / "mini"
    root.mkdir()
    (root / "package.yaml").write_text(
        'spec_version: "0.1"\nproject: {name: Mini, slug: mini, summary: Tiny.}\nstack: {language: python}\n'
    )
    write(root, "planning/01-vision.md", "vision", "Vision", "# Vision\n\n## Goals\n- **G-1** — Be tiny.\n")
    write(root, "planning/02-features.md", "features", "Features",
          "# Features\n\n## F-01 — Thing\n**Supports:** G-1\n**Requirements:** FR-001\n", "vision")
    write(root, "planning/03-requirements.md", "requirements", "Requirements",
          "# Requirements\n\n### FR-001 — Do it\n**Feature:** F-01\n", "features")
    write(root, "planning/06-project-plan.md", "project-plan", "Project Plan",
          "# Project Plan\n\n## Milestones\n| ID | Name |\n| -- | ---- |\n| M1 | First |\n")
    write(root, "planning/07-backlog.md", "backlog", "Backlog", (
        "# Backlog\n\n## Items\n\n"
        "### T-1.2 — Second\n- **Milestone:** M1\n- **Status:** todo\n- **Satisfies:** FR-001\n- **Depends on:** T-1.1\n\n"
        "### T-1.1 — First\n- **Milestone:** M1\n- **Status:** todo\n- **Satisfies:** FR-001\n- **Depends on:** —\n"
    ), "project-plan")
    return root


@pytest.fixture(autouse=True)
def overrides_allowed(monkeypatch):
    """Tests run without a terminal, so they stand in for the person who may waive the gate."""
    monkeypatch.setenv("ITS_ALLOW_OVERRIDES", "1")
