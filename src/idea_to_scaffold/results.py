"""Headless runs: the launch commands `its start` prints, and the result an agent hands back to `its done --result`.

`its` never launches an agent. It prints commands that do it safely:

- **The Stop hook runs anyway.** `claude --bare -p` (soon the default for `-p`) skips the project's hooks, so the
  command passes `--settings` with a settings file `its` writes to the repository's state folder, outside the
  repository and its git folder (`locking.state_dir`), holding the Stop hook and the sandbox settings. Claude's
  sandbox doesn't let the agent write there, and `its done --result` refuses a run whose launch files no longer
  match what `its` wrote (`launch_settings_changed`).
- **The sandbox is on and credentials stay out.** Claude runs with its OS sandbox enabled (network denied by
  default); Codex runs with `--sandbox workspace-write`. Push and registry tokens (`GH_TOKEN`, `GITHUB_TOKEN`, ...)
  are unset for the agent, since `its` does every push itself. Nothing printed skips permissions.
- **A budget, and no prompts that would hang.** `--max-budget-usd` (`launch.budget_usd` in package.yaml, default
  5) and `--permission-prompts none` for Claude.
- **A structured result.** The agent ends with one JSON object matching RESULT_SCHEMA (`--json-schema` for
  Claude, `--output-schema` for Codex), saved to a result file. `its done --result FILE` reads it: `done` runs the
  gate and checks as usual, `blocked` or `needs_human` runs `its block` with the agent's reason, and the run's cost
  and turns go to the run ledger.
"""
from __future__ import annotations

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
    return (f"Read AGENTS.md, then follow docs/playbooks/implement-task.md for {ident}. Read the code and reproduce "
            f"the problem before you edit. Run `its done {ident} --check` before you finish, and if you can't "
            f"finish honestly, run `its block {ident} --reason '...'`. End with the result JSON.")


HOOK_TIMEOUT = 900  # seconds: the test suite runs inside the Stop hook
LAUNCH_FILES = {"settings": "claude-settings.json", "schema": "result.schema.json"}


def claude_settings() -> dict:
    """The settings file the headless Claude line passes: the Stop hook, and the sandbox, which must start or the
    run stops instead of running unsandboxed (`failIfUnavailable`), with no way for a command to step outside it
    (`allowUnsandboxedCommands: false`)."""
    return {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "its hook stop", "timeout": HOOK_TIMEOUT}]}]},
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


def headless_commands(pkg: Package, ident: str, worktree: str | None) -> dict | None:
    """The unattended commands for Claude Code and Codex, as shell lines and as argv. None outside git."""
    files = agent_files(pkg)
    if files is None:
        return None
    files["results"].mkdir(parents=True, exist_ok=True)
    result = files["results"] / f"{ident}-result.json"
    task = prompt(ident)
    spend = budget(pkg)
    claude = ["claude", "--bare", "-p", task, "--settings", str(files["settings"]), "--output-format", "json",
              "--json-schema", json.dumps(RESULT_SCHEMA, separators=(",", ":")), "--max-budget-usd", f"{spend:g}",
              "--permission-mode", "acceptEdits", "--permission-prompts", "none"]
    codex = ["codex", "exec", "--sandbox", "workspace-write", "--output-schema", str(files["schema"]),
             "-o", str(result), task]
    cd = f"{cd_command(worktree)} && " if worktree else ""
    if os.name == "nt":
        # cmd.exe has no env(1): the line unsets the tokens first (`set NAME=`, joined with &, which binds more
        # loosely than &&), and a program that runs argv drops env_unset itself
        start = "".join(f"set {name}=& " for name in SCRUBBED_ENV) + cd
        env_prefix: list[str] = []
    else:
        start = cd + "env " + " ".join(f"-u {name}" for name in SCRUBBED_ENV) + " "
        env_prefix = ["env", *[x for name in SCRUBBED_ENV for x in ("-u", name)]]
    done = f"its done {ident} --result {_quote(str(result))}"
    line = " ".join(_quote(a) for a in claude)
    return {
        "claude": f"{start}{line} > {_quote(str(result))} && {done}",
        "codex": f"{start}{' '.join(_quote(a) for a in codex)} && {done}",
        "argv": {"claude": env_prefix + claude, "codex": env_prefix + codex},
        "cwd": worktree, "result_file": str(result), "settings": str(files["settings"]),
        "schema": str(files["schema"]), "budget_usd": spend, "env_unset": list(SCRUBBED_ENV),
        "then": done,
    }


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
