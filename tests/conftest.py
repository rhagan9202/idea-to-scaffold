from __future__ import annotations

from pathlib import Path

import pytest

from idea_to_scaffold.cli import main

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "streak" / "chat-export.md"


@pytest.fixture
def plan(tmp_path: Path) -> Path:
    """The Streak example, imported (and therefore cross-linked) into a fresh package folder."""
    dest = tmp_path / "streak-plan"
    assert main(["import", str(EXAMPLE), "--into", str(dest)]) == 0
    return dest
