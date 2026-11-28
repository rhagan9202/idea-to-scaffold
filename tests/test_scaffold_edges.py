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
    monkeypatch.setattr("idea_to_scaffold.cli.is_terminal", lambda fd: False)
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git", "--force"]) == 1
    assert marker.exists()
    assert "--yes" in capsys.readouterr().err
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git", "--force", "--yes"]) == 0
    assert not marker.exists()


def test_force_prompt_declined(plan: Path, tmp_path: Path, monkeypatch):
    out = tmp_path / "out"
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git"]) == 0
    monkeypatch.setattr("idea_to_scaffold.cli.is_terminal", lambda fd: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["scaffold", str(plan), "--out", str(out), "--no-git", "--force"]) == 1
    assert (out / "streak" / "AGENTS.md").exists()


def test_handoff_names_no_item_unless_the_plan_does(plan: Path, tmp_path: Path):
    repo = scaffold(plan, tmp_path / "out", git=False).path
    handoff = (repo / "HANDOFF.md").read_text()
    assert "T-1.1" not in handoff and "run its next to pick" in handoff  # HANDOFF names no item, so it can't go stale
    manifest = plan / "package.yaml"
    manifest.write_text(manifest.read_text() + 'first_task: "T-2.2"\n')
    repo = scaffold(plan, tmp_path / "out2", git=False).path
    assert "implement-task.md for T-2.2" in (repo / "HANDOFF.md").read_text()
