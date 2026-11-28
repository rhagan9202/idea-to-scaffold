"""Commands from one source: package.yaml, copied into AGENTS.md, HANDOFF.md and CI between markers by `its link`."""
from __future__ import annotations

import json
from pathlib import Path

from idea_to_scaffold import blocks
from idea_to_scaffold.cli import main
from idea_to_scaffold.package import Package
from idea_to_scaffold.scaffold import scaffold

from .test_landing import git_identity, repo  # noqa: F401 - fixtures

CI = ".github/workflows/ci.yml"


def run_json(capsys, *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def repo_of(plan: Path, tmp_path: Path) -> Path:
    return scaffold(plan, tmp_path / "out", git=False).path


def set_test_command(root: Path, command: str) -> None:
    manifest = root / "docs/planning/package.yaml"
    manifest.write_text(manifest.read_text().replace('test: "uv run pytest"', f"test: {json.dumps(command)}"))


def test_a_scaffold_carries_the_commands_between_markers(plan: Path, tmp_path: Path, capsys):
    root = repo_of(plan, tmp_path)
    for rel in ("AGENTS.md", "HANDOFF.md"):
        text = (root / rel).read_text()
        assert blocks.MD_START in text and "- **Test:** `uv run pytest`" in text, rel
    ci = (root / CI).read_text()
    assert "      # its:commands" in ci and "      - run: uv run pytest\n      # /its:commands" in ci
    code, out = run_json(capsys, "link", "--check", str(root))
    assert code == 0 and out["changed"] == []


def test_one_change_in_package_yaml_reaches_every_copy(plan: Path, tmp_path: Path, capsys):
    root = repo_of(plan, tmp_path)
    agents = root / "AGENTS.md"
    agents.write_text(agents.read_text().replace("\r\n", "\n").replace("\n", "\r\n"))  # a Windows checkout
    set_test_command(root, "node --test js/*.test.js")
    code, out = run_json(capsys, "link", "--check", str(root))
    assert code == 1 and out["error"]["code"] == "link_outdated"
    assert sorted(out["error"]["changed"]) == sorted(["AGENTS.md", "HANDOFF.md", CI])
    _, report = run_json(capsys, "validate", str(root))
    assert any("don't match package.yaml" in w for w in report["warnings"]), report["warnings"]
    before = agents.read_bytes()
    code, out = run_json(capsys, "link", str(root))
    assert code == 0 and sorted(out["changed"]) == sorted(["AGENTS.md", "HANDOFF.md", CI])
    after = agents.read_bytes()
    assert b"- **Test:** `node --test js/*.test.js`\r\n" in after and b"\n" not in after.replace(b"\r\n", b"")
    assert after.split(b"<!-- its:commands")[0] == before.split(b"<!-- its:commands")[0]  # the rest is untouched
    assert "- run: node --test js/*.test.js\n" in (root / CI).read_text()
    assert "- **Test:** `node --test js/*.test.js`" in (root / "HANDOFF.md").read_text()
    assert run_json(capsys, "link", "--check", str(root))[0] == 0


def test_ci_quotes_a_command_yaml_would_misread(plan: Path, tmp_path: Path, capsys):
    root = repo_of(plan, tmp_path)
    set_test_command(root, "echo 'a: b' # not a comment")
    assert main(["link", str(root)]) == 0
    assert '- run: "echo \'a: b\' # not a comment"\n' in (root / CI).read_text()


def test_an_unmarked_list_gets_markers_only_when_nothing_is_lost(plan: Path, tmp_path: Path, capsys):
    root = repo_of(plan, tmp_path)
    agents = root / "AGENTS.md"
    old = agents.read_text()
    plain = old.replace(blocks.MD_START + "\n", "").replace(blocks.MD_END + "\n", "")
    stale = plain.replace("- **Test:** `uv run pytest`", "- **Test:** `pytest -x`")
    agents.write_text(stale)  # an older repo: no markers, and a Test line that disagrees
    _, report = run_json(capsys, "validate", str(root))
    assert any("AGENTS.md says Test: `pytest -x`" in w for w in report["warnings"]), report["warnings"]
    assert run_json(capsys, "link", "--check", str(root))[0] == 0  # check mode doesn't add markers
    assert main(["link", str(root)]) == 0
    assert agents.read_text() == old  # markers added and the line regenerated
    custom = plain.replace("- **Run:** `uv run python -m streak --help`",
                           "- **Run:** `uv run python -m streak --help`\n- **Deploy:** ask Dana")
    agents.write_text(custom)  # a line package.yaml doesn't have: regenerating would lose it
    assert main(["link", str(root)]) == 0
    assert agents.read_text() == custom


def test_the_acceptance_command_isnt_listed_for_agents(plan: Path, tmp_path: Path):
    root = repo_of(plan, tmp_path)
    manifest = root / "docs/planning/package.yaml"
    manifest.write_text(manifest.read_text().replace("commands:\n", "commands:\n  acceptance: \"uv run pytest tests/acceptance\"\n", 1))
    pkg = Package.load(root)
    assert "acceptance" not in [k for k, _ in blocks.listed(pkg)]
    assert all("acceptance" not in line for line in blocks.command_lines(pkg))


def test_link_in_a_claim_worktree_leaves_the_commands_to_the_lead(repo: Path, capsys):  # noqa: F811
    # the repo fixture changed package.yaml's commands without `its link`: the lead's drift, not the agent's
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    wt = Path(out["claim"]["worktree"])
    before = (wt / "AGENTS.md").read_text()
    assert main(["link", str(wt)]) == 0
    assert (wt / "AGENTS.md").read_text() == before  # an agent's link never becomes an agent-config change
    code, out = run_json(capsys, "link", "--check", str(repo))  # on main the drift shows
    assert code == 1 and "AGENTS.md" in out["error"]["changed"]
