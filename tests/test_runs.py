"""Phase 8: headless launch commands, `its done --result`, the run ledger and `its stats`, and the sizing lint."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from idea_to_scaffold import ledger
from idea_to_scaffold.cli import main
from idea_to_scaffold.package import Package, new_package
from idea_to_scaffold.results import RESULT_SCHEMA, SCRUBBED_ENV, ResultError, parse_result

from .test_gate import TEST, claim  # noqa: F401 - fixtures
from .test_landing import git_identity, py, remote, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json, set_field


def envelope(result: dict | None, **extra) -> str:
    data = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.42, "num_turns": 7,
            "duration_ms": 1234, "session_id": "s-1", **extra}
    if result is not None:
        data["structured_output"] = result
    return json.dumps(data)


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "result.json"
    path.write_text(text)
    return str(path)


# -- launch commands ---------------------------------------------------------------------------------------------

def test_start_writes_launchers_that_keep_the_guards(repo: Path, capsys, state_home: Path):  # noqa: F811
    code, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    assert code == 0
    h = out["commands"]["headless"]
    claude, codex = h["argv"]["claude"], h["argv"]["codex"]
    assert h["env_unset"] == list(SCRUBBED_ENV) and claude[0] == "claude" and codex[:2] == ["codex", "exec"]
    for flag in ("-p", "--settings", "--json-schema", "--max-budget-usd", "--permission-prompts"):
        assert flag in claude
    assert "--bare" not in claude  # it would skip the Stop hook in --settings, and only take an API key
    assert claude[claude.index("--setting-sources") + 1] == "user"  # not the worktree's own .claude/ settings
    assert claude[claude.index("--max-budget-usd") + 1] == "5"
    assert json.loads(claude[claude.index("--json-schema") + 1]) == RESULT_SCHEMA
    # one word for every shell: no whitespace for Windows PowerShell 5.1 to misquote, nothing cmd.exe (npm's
    # claude.cmd shim) would act on
    assert not set(claude[claude.index("--json-schema") + 1]) & set(" \t\r\n&|<>^%!")
    assert codex[codex.index("--sandbox") + 1] == "workspace-write" and "--output-schema" in codex
    worktree = Path(out["claim"]["worktree"]).resolve()
    for agent in ("claude", "codex"):
        scripts = {kind: Path(path) for kind, path in h["scripts"][agent].items()}
        sh, ps1, cmd = (scripts[k].read_text(encoding="utf-8-sig") for k in ("sh", "ps1", "cmd"))
        for text in (sh, ps1, cmd, " ".join(h["argv"][agent])):
            assert "dangerously" not in text and "danger-full-access" not in text and "bypassPermissions" not in text
        assert f"cd {shlex.quote(str(out['claim']['worktree']))} || exit 1" in sh
        assert all(f"-u {name}" in sh for name in SCRUBBED_ENV) and all(f"'{name}'" in ps1 for name in SCRUBBED_ENV)
        # the result file is removed first, and `its done --result` runs whatever the agent's exit status
        run = sh.index(f"-u {SCRUBBED_ENV[-1]} {agent} ")  # the tokens are unset for the agent alone
        assert sh.index("rm -f ") < run < sh.index("its done T-2.1 --result")
        assert sh.rstrip().splitlines()[-1] == f"its done T-2.1 --result {shlex.quote(h['result_file'])}"
        assert "Set-Location -LiteralPath" in ps1 and "& its done T-2.1 --result $result" in ps1
        assert ps1.rstrip().endswith("exit $LASTEXITCODE") and str(scripts["ps1"]) in cmd
        line = h[agent]
        assert line == (f"powershell -NoProfile -ExecutionPolicy Bypass -File {subprocess.list2cmdline([str(scripts['ps1'])])}"
                        if os.name == "nt" else f"sh {shlex.quote(str(scripts['sh']))}")
        assert not scripts["sh"].resolve().is_relative_to(worktree)

    settings = Path(h["settings"]).resolve()
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    for folder in (worktree, repo.resolve(), common):  # out of the agent's reach: not in the repo or its git folder
        assert not settings.is_relative_to(folder)
    assert settings.is_relative_to(state_home.resolve())
    data = json.loads(settings.read_text())
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "its hook stop --headless"
    assert data["sandbox"]["enabled"] is True and data["sandbox"]["failIfUnavailable"] is True
    assert git(repo, "status", "--porcelain") == ""  # nothing written into the checkout


def test_budget_comes_from_package_yaml(repo: Path, capsys):  # noqa: F811
    manifest = repo / "docs/planning/package.yaml"
    manifest.write_text(manifest.read_text() + "launch:\n  budget_usd: 1.5\n")
    git(repo, "commit", "-qam", "budget")
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    assert out["commands"]["headless"]["budget_usd"] == 1.5
    assert "'--max-budget-usd' '1.5'" in Path(out["commands"]["headless"]["scripts"]["claude"]["ps1"]).read_text(
        encoding="utf-8-sig")


# -- parsing results ---------------------------------------------------------------------------------------------

def test_parse_result_reads_the_envelope_codex_and_bare_objects():
    result = {"id": "T-2.1", "status": "done", "summary": "added the store"}
    data, run = parse_result(envelope(result))
    assert data == result and run == {"agent": "claude", "cost_usd": 0.42, "turns": 7, "duration_ms": 1234,
                                      "session_id": "s-1"}
    data, run = parse_result(json.dumps(result))
    assert data == result and run == {"agent": "codex"}


@pytest.mark.parametrize("bad, problem", [
    ({"id": "T-2.1", "status": "finished", "summary": "x"}, "status"),
    ({"id": "T-2.1", "status": "blocked", "summary": "x"}, "reason"),
    ({"id": "T-2.1", "status": "done", "summary": "x", "extra": 1}, "unexpected"),
    ({"id": "T-2.1", "status": "done", "summary": "x", "tests_run": "all"}, "tests_run"),
    ([1, 2], "object"),
])
def test_parse_result_refuses_what_the_schema_refuses(bad, problem):
    with pytest.raises(ResultError) as exc:
        parse_result(json.dumps(bad))
    assert exc.value.code == "bad_result" and problem in str(exc.value)


def test_a_run_without_structured_output_is_no_result():
    with pytest.raises(ResultError) as exc:
        parse_result(envelope(None, subtype="error_max_budget_usd", is_error=True))
    assert exc.value.code == "no_result" and exc.value.run["cost_usd"] == 0.42
    with pytest.raises(ResultError):
        parse_result("not json")


# -- its done --result -------------------------------------------------------------------------------------------

def test_done_result_runs_the_gate_and_records_the_run(claim: Path, tmp_path: Path, capsys):  # noqa: F811
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 0 and out["status"] == "done" and out["run"]["cost_usd"] == 0.42
    entry = [e for e in ledger.read(Package.load(claim)) if e["event"] == "done"][-1]
    assert entry["outcome"] == "passed" and entry["agent"] == "claude" and entry["turns"] == 7


def test_done_result_still_refuses_a_cheat(claim: Path, tmp_path: Path, capsys):  # noqa: F811
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    (claim / TEST).write_text("def test_add():\n    assert True\n")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 12 and out["error"]["code"] == "test_changes"


def test_a_blocked_result_blocks_and_commits(claim: Path, tmp_path: Path, capsys):  # noqa: F811
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "needs_human", "summary": "stuck",
                                     "reason": "the schema is undecided"}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 0 and out["status"] == "blocked" and out["committed"]
    assert "the schema is undecided" in out["reason"]
    entry = [e for e in ledger.read(Package.load(claim)) if e["event"] == "block"][-1]
    assert entry["cost_usd"] == 0.42


def test_done_result_refuses_a_mismatch_or_missing_result(claim: Path, tmp_path: Path, capsys):  # noqa: F811
    path = write(tmp_path, json.dumps({"id": "T-9.9", "status": "done", "summary": "x"}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 1 and out["error"]["code"] == "result_mismatch"
    path = write(tmp_path, envelope(None, subtype="error_max_turns", is_error=True))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 1 and out["error"]["code"] == "no_result"
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", str(tmp_path / "missing.json"))
    assert out["error"]["code"] == "bad_result"
    _, status = run_json(capsys, "status", str(claim))
    assert next(c for c in status["claims"] if c["id"] == "T-2.1")["state"] == "working"  # nothing was marked


# -- the ledger and its stats ------------------------------------------------------------------------------------

def test_stats_reports_first_try_rate_refusals_and_cost(claim: Path, repo: Path, tmp_path: Path,  # noqa: F811
                                                         capsys):
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    (claim / TEST).write_text("def test_add():\n    assert True\n")
    assert run_json(capsys, "done", "T-2.1", str(claim))[0] == 12  # refused: weakened test
    git(claim, "checkout", "--", TEST)
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}))
    code, out = run_json(capsys, "done", "T-2.1", str(claim), "--result", path)
    assert code == 0 and out["committed"]  # its commits a headless run's work itself
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 0

    code, out = run_json(capsys, "stats", str(repo))
    assert code == 0 and out["attempted"] == 1 and out["first_try"] == 0 and out["first_try_rate"] == 0.0
    item = next(i for i in out["items"] if i["id"] == "T-2.1")
    assert item["attempts"] == 2 and item["passed"] == 1 and item["landed"] and item["agents"] == ["claude"]
    assert len(item["runs"]) == 1 and out["first_try_interval"] == [0.0, 0.79]  # one run links start, done and land
    assert out["by_agent"]["claude"]["comparable"] is False and out["by_agent"]["claude"]["interval"] == [0.0, 0.79]
    assert out["refusals"] == {"assertions_removed": 1} and out["land"]["landed"] == 1
    assert out["cost_usd"] == 0.42 and out["by_size"]["2-3 files"]["items"] == 1
    assert git(repo, "status", "--porcelain") == ""  # the ledger lives outside the checkout

    capsys.readouterr()
    assert main(["stats", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "passed first time (0%, 95% interval 0%-79%)" in text and "assertions_removed 1" in text
    assert "too few to compare" in text


def test_check_only_runs_are_not_attempts(claim: Path, capsys):  # noqa: F811
    (claim / "src/streak/store.py").write_text("def add(): ...\n")
    assert run_json(capsys, "done", "T-2.1", str(claim), "--check")[0] == 0
    _, out = run_json(capsys, "stats", str(claim))
    assert out["attempted"] == 0 and out["records"] >= 2  # start + the check


def test_stats_outside_git_is_empty(tmp_path: Path, capsys):
    root = tmp_path / "plain"
    new_package(root)
    code, out = run_json(capsys, "stats", str(root))
    assert code == 0 and out["records"] == 0 and out["ledger"] is None


# -- sizing ------------------------------------------------------------------------------------------------------

def test_sizing_warns_about_big_or_vague_items_without_failing(repo: Path, capsys):  # noqa: F811
    set_field(repo, "T-2.1", "Touches", "`a.py`, `b.py`, `c.py`, `d.py`")
    set_field(repo, "T-2.2", "Verify", "manual: look at it")
    assert main(["propose", "Improve performance", "--dir", str(repo)]) == 0
    code, out = run_json(capsys, "validate", str(repo), "--sizing")
    assert code == 0 and out["valid"]
    kinds = {(w["id"], w["kind"]) for w in out["sizing"]["warnings"]}
    assert ("T-2.1", "too_many_touches") in kinds and ("T-2.2", "no_verify") in kinds
    assert ("T-new-improve-performance", "vague") in kinds
    assert "T-1.1" not in out["sizing"]["flagged"]  # done items aren't linted

    capsys.readouterr()
    assert main(["validate", str(repo), "--sizing"]) == 0
    assert "sizing  too_many_touches" in capsys.readouterr().out


def test_promote_shows_sizing_warnings(repo: Path, capsys):  # noqa: F811
    assert main(["propose", "Undo", "--dir", str(repo), "--milestone", "M1", "--done-when", "undo works",
                 "--touches", "a.py, b.py, c.py, d.py"]) == 0
    code, out = run_json(capsys, "promote", "T-new-undo", str(repo))
    assert code == 0 and {w["kind"] for w in out["sizing"]} >= {"too_many_touches", "no_verify"}


def test_wilson_intervals_and_one_agent_identity(repo: Path, tmp_path: Path, capsys):  # noqa: F811
    assert ledger.wilson(0, 0) is None and ledger.wilson(10, 10) == [0.72, 1.0] and ledger.wilson(5, 10) == [0.24, 0.76]
    _, out = run_json(capsys, "start", "T-2.1", str(repo), "--worktree", "--agent", "alice")  # a person starts it
    wt = Path(out["claim"]["worktree"])
    assert out["run"]
    (wt / "src/streak/store.py").write_text("def add(): ...\n")
    path = write(tmp_path, envelope({"id": "T-2.1", "status": "done", "summary": "store"}))
    assert run_json(capsys, "done", "T-2.1", str(wt), "--result", path)[0] == 0  # claude does the work
    _, stats = run_json(capsys, "stats", str(repo))
    item = next(i for i in stats["items"] if i["id"] == "T-2.1")
    assert item["agent"] == "claude" and item["started_by"] == "alice" and item["runs"] == [out["run"]]
    assert list(stats["by_agent"]) == ["claude"]  # counted once, as the agent that did the work
    assert {e["run"] for e in ledger.of(Package.find(repo), "T-2.1")} == {out["run"]}
