"""Edge cases for `its scaffold`."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from idea_to_scaffold.cli import main
from idea_to_scaffold.scaffold import ScaffoldError, scaffold


def test_scaffold_from_a_scaffolded_repo(plan: Path, tmp_path: Path):
    first = scaffold(plan, tmp_path / "one", git=False).path
    second = scaffold(first, tmp_path / "two", git=False).path
    for rel in ["AGENTS.md", "docs/planning/07-backlog.md", "docs/planning/package.yaml", "docs/playbooks/write-adr.md"]:
        assert (second / rel).read_text() == (first / rel).read_text(), rel


def test_scaffold_refuses_to_target_its_own_source(plan: Path, tmp_path: Path):
    repo = scaffold(plan, tmp_path / "one", git=False).path
    with pytest.raises(ScaffoldError, match="overlaps"):
        scaffold(repo, tmp_path / "one", git=False, force=True)
    assert (repo / "AGENTS.md").exists()


def test_unknown_stack_warns(plan: Path, tmp_path: Path):
    manifest = plan / "package.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["stack"]["language"] = "typescript"
    manifest.write_text(yaml.safe_dump(data))
    result = scaffold(plan, tmp_path / "out", git=False)
    assert any("no skeleton for stack.language 'typescript'" in w for w in result.warnings)
    assert not (result.path / "pyproject.toml").exists()


def test_force_needs_confirmation_when_not_interactive(plan: Path, tmp_path: Path, monkeypatch, capsys):
    out = tmp_path / "out"
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git"]) == 0
    marker = out / "streak" / "keep.txt"
    marker.write_text("mine")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git", "--force"]) == 1
    assert marker.exists()
    assert "--yes" in capsys.readouterr().err
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git", "--force", "--yes"]) == 0
    assert not marker.exists()


def test_force_prompt_declined(plan: Path, tmp_path: Path, monkeypatch):
    out = tmp_path / "out"
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git"]) == 0
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git", "--force"]) == 1
    assert (out / "streak" / "AGENTS.md").exists()


def test_handoff_skips_items_waiting_on_dependencies(plan: Path, tmp_path: Path):
    backlog = plan / "planning/07-backlog.md"
    text = backlog.read_text()
    # Put T-2.1 (depends on T-1.1) first; HANDOFF must still start at T-1.1.
    start, mid = text.index("### T-1.1"), text.index("### T-2.1")
    end = text.index("### T-2.2")
    backlog.write_text(text[:start] + text[mid:end] + text[start:mid] + text[end:])
    repo = scaffold(plan, tmp_path / "out", git=False).path
    handoff = (repo / "HANDOFF.md").read_text()
    assert "### T-1.1 — Project setup verified" in handoff
    assert "**Next up:** [T-1.1" in (repo / "docs/planning/README.md").read_text()
