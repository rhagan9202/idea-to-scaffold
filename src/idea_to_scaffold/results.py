"""Headless runs: the launcher scripts `its start` writes, and the result an agent hands back to `its done --result`.

`its` never launches an agent. It writes a launcher script per item and agent and prints the line that runs it:

- **The Stop hook runs anyway.** The Claude line passes `--settings` with a settings file `its` writes to the
  repository's state folder, outside the repository and its git folder (`locking.state_dir`), holding the Stop hook
  and the sandbox settings, and `--setting-sources user`, so the worktree's own `.claude/` settings (which an agent
  can edit) aren't read. It doesn't pass `--bare`, which skips every hook in settings, `--settings` included, and
  only takes an API key. Claude's sandbox doesn't let the agent write the state folder, and `its done --result`
  refuses a run whose launch files no longer match what `its` wrote (`launch_settings_changed`).
- **The sandbox is on and credentials stay out.** Claude runs with its OS sandbox enabled (network denied by
  default); Codex runs with `--sandbox workspace-write`. Push and registry tokens (`GH_TOKEN`, `GITHUB_TOKEN`, ...)
  are unset for the agent alone, since `its` does every push itself. Nothing printed skips permissions.
- **A budget, and no prompts that would hang.** `--max-budget-usd` (`launch.budget_usd` in package.yaml, default
  5) and `--permission-prompts none` for Claude.
- **A structured result, always handed back.** The agent ends with one JSON object matching RESULT_SCHEMA
  (`--json-schema` for Claude, `--output-schema` for Codex), saved to a result file the script deletes first. The
  script then runs `its done --result FILE` whatever the agent's exit status, so a failed or over-budget run is in
  the ledger too, and exits with its status. `done` runs the gate and checks, `blocked` or `needs_human` runs
  `its block` with the agent's reason, and either way the claim branch is committed and pushed (`edits.apply_result`).
- **One script per shell.** `T-n.m-claude.sh` for sh; `T-n.m-claude.ps1` for PowerShell, which also sets the
  argument passing that keeps the JSON schema's quotes on Windows PowerShell 5.1 and on PowerShell 7; and
  `T-n.m-claude.cmd`, which runs the PowerShell one, for cmd. The same for Codex. The schema goes on the command
  line as one word with no whitespace (`compact_schema`), so no shell's quoting rules can split it.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
from pathlib import Path

from .locking import state_dir
from .package import Package

RESULT_SCHEMA = {
    "type": "object",
    "required": ["id", "status", "summary"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "description": "the backlog item, e.g. T-2.1"},
        "status": {"enum": ["done", "blocked", "needs_human"]},
        "summary": {"type": "string", "description": "what changed, in a sentence or two"},
        "reason": {"type": "string", "description": "for blocked or needs_human: what stops the work"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "commit_message": {"type": "string",
                           "description": "for done: the commit message, in the convention AGENTS.md gives"},
    },
}
SCRUBBED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "GL_TOKEN", "BITBUCKET_TOKEN", "NPM_TOKEN",
                "NODE_AUTH_TOKEN", "PYPI_TOKEN", "TWINE_PASSWORD", "UV_PUBLISH_TOKEN", "CARGO_REGISTRY_TOKEN",
                "DOCKER_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
DEFAULT_BUDGET = 5.0


class ResultError(Exception):
    def __init__(self, code: str, message: str, run: dict | None = None):
        super().__init__(message)
        self.code = code
        self.run = run or {}


def _quote(arg: str) -> str:
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


def cd_command(folder: str) -> str:
    """`cd` into `folder`, for this platform's shell. cmd.exe needs /d to change drive as well as folder. The lines
    join their steps with &&, which cmd.exe has too, so an agent never starts in the wrong folder after a failed cd."""
    return f"cd /d {_quote(folder)}" if os.name == "nt" else f"cd {_quote(folder)}"


def budget(pkg: Package) -> float:
    value = pkg.get("launch.budget_usd")
    try:
        return float(value) if value is not None else DEFAULT_BUDGET
    except (TypeError, ValueError):
        return DEFAULT_BUDGET


def prompt(ident: str) -> str:
    """The task an unattended agent gets. It matches the playbook's headless note: check with `--check`, and leave
    the status and the commit to `its done --result`. It holds no quote, percent sign or shell operator, so it reads
    the same in every launcher."""
    return (f"Read AGENTS.md, then follow docs/playbooks/implement-task.md for {ident}. Run `its done {ident} --check` "
            f"until it passes. Do not set the status or commit: when you end, `its done --result` checks the work "
            "again, marks the item done and commits it. If you cannot finish honestly, never weaken a test: end with "
            "status blocked or needs_human and say why in reason. End with the result JSON.")


HOOK_TIMEOUT = 900  # seconds: the test suite runs inside the Stop hook
LAUNCH_FILES = {"settings": "claude-settings.json", "schema": "result.schema.json"}


def claude_settings() -> dict:
    """The settings file the headless Claude line passes: the Stop hook, and the sandbox, which must start or the
    run stops instead of running unsandboxed (`failIfUnavailable`), with no way for a command to step outside it
    (`allowUnsandboxedCommands: false`)."""
    return {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "its hook stop --headless", "timeout": HOOK_TIMEOUT}]}]},
        "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "failIfUnavailable": True,
                    "allowUnsandboxedCommands": False},
    }


def _launch_texts() -> dict[str, str]:
    return {"settings": json.dumps(claude_settings(), indent=2) + "\n",
            "schema": json.dumps(RESULT_SCHEMA, indent=2) + "\n"}


def agent_files(pkg: Package) -> dict[str, Path] | None:
    """Write the settings and schema files the headless commands use, in the state folder's `launch/`, outside the
    repository and its git folder. None outside git."""
    store = state_dir(pkg.root)
    if store is None:
        return None
    folder = store / "launch"
    folder.mkdir(parents=True, exist_ok=True)
    files = {key: folder / name for key, name in LAUNCH_FILES.items()}
    for key, text in _launch_texts().items():
        files[key].write_text(text, encoding="utf-8", newline="\n")
    files["results"] = store / "runs"
    return files


def launch_changes(pkg: Package) -> list[str]:
    """The launch files whose content isn't what `its` writes: an agent rewrote its own Stop hook or sandbox
    setting, for this run or the next one. [] when they are intact, or were never written."""
    store = state_dir(pkg.root)
    if store is None:
        return []
    changed = []
    for key, text in _launch_texts().items():
        path = store / "launch" / LAUNCH_FILES[key]
        try:
            if path.read_text(encoding="utf-8") != text:
                changed.append(str(path))
        except FileNotFoundError:
            continue
        except OSError:
            changed.append(str(path))
    return changed


def compact_schema() -> str:
    """RESULT_SCHEMA as one command-line word: compact JSON with each space written `\\u0020`, which JSON reads back
    as a space. Windows PowerShell 5.1 decides whether an argument needs quotes by counting its double quotes, and
    JSON's quotes make it pass the schema unquoted, so a space would split it in two."""
    return json.dumps(RESULT_SCHEMA, separators=(",", ":")).replace(" ", "\\u0020")


def _sh(arg: str) -> str:
    return shlex.quote(arg)


def _ps(arg: str) -> str:
    return "'" + arg.replace("'", "''") + "'"


def _cmd_path(path: str) -> str:
    return '"' + path.replace("%", "%%") + '"'


def _sh_script(ident: str, agent: str, argv: list[str], worktree: str | None, result: str, stdout: bool) -> str:
    lines = ["#!/bin/sh", f"# its: unattended {AGENT_NAMES[agent]} run for {ident}. `its start` writes it again each time.",
             f"command -v {argv[0]} >/dev/null 2>&1 || {{ echo '{argv[0]} is not installed' >&2; exit 127; }}"]
    if worktree:
        lines.append(f"cd {_sh(worktree)} || exit 1")
    lines.append(f"rm -f {_sh(result)}")
    unset = " ".join(f"-u {name}" for name in SCRUBBED_ENV)
    run = f"env {unset} " + " ".join(_sh(a) for a in argv)
    lines.append(run + (f" > {_sh(result)}" if stdout else ""))
    lines.append(f"its done {ident} --result {_sh(result)}")
    return "\n".join(lines) + "\n"


def _ps_script(ident: str, agent: str, argv: list[str], worktree: str | None, result: str, stdout: bool) -> str:
    """PowerShell: Windows PowerShell 5.1 passes a native argument's double quotes unescaped, and PowerShell 7.3+
    does so for a .cmd shim (npm's `claude.cmd`) unless told otherwise, so the JSON schema would lose its quotes.
    7.3+ is set to Standard passing; on older versions the script escapes them itself, which is enough because the
    schema has no whitespace for 5.1 to quote, or fail to quote (`compact_schema`)."""
    names = ", ".join(_ps(n) for n in SCRUBBED_ENV)
    args = []
    for a in argv[1:]:
        args.append("$schema" if a.startswith("{") else _ps(a))
    lines = [f"# its: unattended {AGENT_NAMES[agent]} run for {ident}. `its start` writes it again each time.",
             "$ErrorActionPreference = 'Stop'",
             "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
             f"if (-not (Get-Command {argv[0]} -ErrorAction SilentlyContinue)) {{ "
             f"[Console]::Error.WriteLine('{argv[0]} is not installed'); exit 127 }}"]
    if worktree:
        lines.append(f"Set-Location -LiteralPath {_ps(worktree)}")
    lines += [f"$result = {_ps(result)}",
              "if (Test-Path -LiteralPath $result) { Remove-Item -LiteralPath $result -Force }"]
    schema = next((a for a in argv if a.startswith("{")), None)
    if schema is not None:
        lines += [f"$schema = {_ps(schema)}",
                  "if ($PSVersionTable.PSVersion -ge [version]'7.3') { $PSNativeCommandArgumentPassing = 'Standard' } "
                  "else { $schema = $schema.Replace('\"', '\\\"') }"]
    lines += ["$saved = @{}",
              f"foreach ($name in @({names})) {{",
              "    $saved[$name] = [Environment]::GetEnvironmentVariable($name)",
              "    [Environment]::SetEnvironmentVariable($name, $null)",
              "}",
              "$ErrorActionPreference = 'Continue'",
              "try {",
              f"    & {argv[0]} {' '.join(args)}" + (" | Out-File -LiteralPath $result -Encoding utf8" if stdout else ""),
              "} finally {",
              "    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }",
              "}",
              f"& its done {ident} --result $result",
              "exit $LASTEXITCODE"]
    return "\r\n".join(lines) + "\r\n"


def _cmd_script(ps1: Path) -> str:
    return "\r\n".join(["@echo off", f"powershell -NoProfile -ExecutionPolicy Bypass -File {_cmd_path(str(ps1))}",
                         "exit /b %ERRORLEVEL%"]) + "\r\n"


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def run_line(script: dict[str, str]) -> str:
    """The line that runs a launcher on this platform. The PowerShell line works from cmd and PowerShell alike."""
    if os.name == "nt":
        return f"powershell -NoProfile -ExecutionPolicy Bypass -File {subprocess.list2cmdline([script['ps1']])}"
    return f"sh {_sh(script['sh'])}"


def headless_commands(pkg: Package, ident: str, worktree: str | None) -> dict | None:
    """Write the unattended launchers for Claude Code and Codex, and return the lines that run them, plus the same
    commands as argv for a program to run without a shell. None outside git."""
    files = agent_files(pkg)
    if files is None:
        return None
    files["results"].mkdir(parents=True, exist_ok=True)
    result = str(files["results"] / f"{ident}-result.json")
    task = prompt(ident)
    spend = budget(pkg)
    argv = {
        "claude": ["claude", "-p", task, "--setting-sources", "user", "--settings", str(files["settings"]),
                   "--output-format", "json", "--json-schema", compact_schema(),
                   "--max-budget-usd", f"{spend:g}", "--permission-mode", "acceptEdits",
                   "--permission-prompts", "none"],
        "codex": ["codex", "exec", "--sandbox", "workspace-write", "--output-schema", str(files["schema"]),
                  "-o", result, task],
    }
    folder = files["settings"].parent
    scripts: dict[str, dict[str, str]] = {}
    for agent, args in argv.items():
        stdout = agent == "claude"  # claude prints its result; codex writes it with -o
        paths = {kind: folder / f"{ident}-{agent}.{kind}" for kind in ("sh", "ps1", "cmd")}
        paths["sh"].write_text(_sh_script(ident, agent, args, worktree, result, stdout), encoding="utf-8",
                               newline="\n")
        paths["ps1"].write_text("\ufeff" + _ps_script(ident, agent, args, worktree, result, stdout),
                                encoding="utf-8", newline="")
        paths["cmd"].write_text(_cmd_script(paths["ps1"]), encoding="utf-8", newline="")
        with contextlib.suppress(OSError):
            paths["sh"].chmod(0o755)
        scripts[agent] = {kind: str(path) for kind, path in paths.items()}
    done = f"its done {ident} --result {_quote(result)}"
    return {
        "claude": run_line(scripts["claude"]), "codex": run_line(scripts["codex"]), "scripts": scripts,
        "argv": argv, "cwd": worktree, "result_file": result, "settings": str(files["settings"]),
        "schema": str(files["schema"]), "budget_usd": spend, "env_unset": list(SCRUBBED_ENV),
        "then": done, "then_argv": ["its", "done", ident, "--result", result],
    }


def read_result_file(path: str) -> str:
    """A result file's text. PowerShell 5.1 writes UTF-8 with a byte order mark, and its `>` writes UTF-16, so both
    are read as well as plain UTF-8."""
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def parse_result(text: str) -> tuple[dict, dict]:
    """The agent's result, and what its run reported (cost, turns, session). Accepts the `claude -p --output-format
    json` envelope (the result is its `structured_output`, which is absent when the run failed or declined), a Codex
    `-o` file, or the bare object."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ResultError("bad_result", f"the result file isn't JSON: {exc}") from exc
    run: dict = {}
    envelope = isinstance(data, dict) and (data.get("type") == "result" or "structured_output" in data
                                           or "total_cost_usd" in data)
    if envelope:
        run = {"agent": "claude", "cost_usd": data.get("total_cost_usd"), "turns": data.get("num_turns"),
               "duration_ms": data.get("duration_ms"), "session_id": data.get("session_id"),
               "agent_error": data.get("subtype") if data.get("is_error") else None}
        inner = data.get("structured_output")
        if inner is None:
            why = data.get("subtype") or "no structured_output"
            raise ResultError("no_result", f"the agent's run ended without a result ({why}); nothing was marked",
                              {k: v for k, v in run.items() if v is not None})
        data = inner
    elif isinstance(data, dict):
        run = {"agent": "codex" if "status" in data else None}
    problems = validate_result(data)
    if problems:
        raise ResultError("bad_result", "the result doesn't match the schema: " + "; ".join(problems),
                          {k: v for k, v in run.items() if v is not None})
    return data, {k: v for k, v in run.items() if v is not None}


def validate_result(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["it isn't a JSON object"]
    problems = []
    if not isinstance(data.get("id"), str) or not data.get("id"):
        problems.append("id must name the item")
    if data.get("status") not in ("done", "blocked", "needs_human"):
        problems.append("status must be done, blocked or needs_human")
    if not isinstance(data.get("summary"), str):
        problems.append("summary must be a string")
    for key in ("files_changed", "tests_run"):
        if key in data and not (isinstance(data[key], list) and all(isinstance(x, str) for x in data[key])):
            problems.append(f"{key} must be a list of strings")
    if data.get("status") in ("blocked", "needs_human") and not str(data.get("reason") or "").strip():
        problems.append("a blocked or needs_human result needs a reason")
    extra = set(data) - set(RESULT_SCHEMA["properties"])
    if extra:
        problems.append(f"unexpected field(s): {', '.join(sorted(extra))}")
    return problems
