from __future__ import annotations

import shutil
from collections.abc import Callable
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
    for name in ("GITHUB_BASE_REF", "GITHUB_HEAD_REF"):  # set when this suite runs in a pull request's CI
        monkeypatch.delenv(name, raising=False)
    return folder


Golden = Callable[[str, Callable[[Path], Path], Path], Path]


@pytest.fixture(scope="session")
def golden(tmp_path_factory) -> Golden:
    """Starting points that are slow to make (an imported plan, a scaffolded repo with commits), made once per
    session and copied for each test: `golden(key, build, tmp_path)` runs `build(folder)` the first time `key` is
    asked for, keeps what it made in `folder`, and copies all of it into `tmp_path`, returning the copy of the path
    `build` returned. Each test gets its own copy, so what one test changes never reaches another. A build must not
    depend on anything a test changed, nor leave state outside its folder."""
    base = tmp_path_factory.mktemp("golden")
    made: dict[str, Path] = {}

    def copy(key: str, build: Callable[[Path], Path], into: Path) -> Path:
        if key not in made:
            folder = base / key
            folder.mkdir()
            made[key] = build(folder).relative_to(folder)
        shutil.copytree(base / key, into, dirs_exist_ok=True, symlinks=True)
        return into / made[key]

    return copy


@pytest.fixture
def plan(tmp_path: Path, golden: Golden) -> Path:
    """The Streak example, imported (and therefore cross-linked) into a fresh package folder."""
    def build(folder: Path) -> Path:
        dest = folder / "streak-plan"
        assert main(["import", str(EXAMPLE), "--into", str(dest)]) == 0
        return dest
    return golden("plan", build, tmp_path)


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
