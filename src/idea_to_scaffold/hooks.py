"""Opt-in Stop hooks: run the done gate when an agent tries to finish, so it runs even when the agent forgets.

`its hooks` writes a Stop hook for Claude Code (`.claude/settings.json`) and Codex (`.codex/hooks.json`) that runs
`its hook stop`. That command reads the hook's JSON on stdin, runs `its done --check` for the item being worked on,
and, when the gate fails, prints `{"decision": "block", "reason": ...}` so the agent keeps going with the reason as
its next instruction. It never blocks twice in a row (`stop_hook_active`), when no item is being worked on, or when
the item is blocked or dropped: an agent that can't finish honestly runs `its block` and may then stop.
"""
from __future__ import annotations

import json
from pathlib import Path

from .backlog import load_backlog
from .package import Package
from .work import SCHEMA, WorkError, done, item_here

HOOK_COMMAND = "its hook stop"
HOOK_TIMEOUT = 900  # seconds: the test suite runs inside the hook
HOOK_FILES = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json"}


def _add_stop_hook(path: Path) -> bool:
    """Add the Stop hook to a hooks file, keeping everything else in it. Returns False if it was already there."""
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise WorkError("bad_hooks_file", f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise WorkError("bad_hooks_file", f"{path} must hold a JSON object")
    stop = data.setdefault("hooks", {}).setdefault("Stop", [])
    if any(h.get("command") == HOOK_COMMAND for group in stop for h in group.get("hooks", [])):
        return False
    stop.append({"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def install_hooks(root: Path, agents: list[str]) -> dict:
    unknown = [a for a in agents if a not in HOOK_FILES]
    if unknown:
        raise WorkError("bad_agent", f"no Stop hook for {', '.join(unknown)} (supported: {', '.join(HOOK_FILES)})")
    written, unchanged = [], []
    for agent in agents:
        rel = HOOK_FILES[agent]
        (written if _add_stop_hook(Path(root) / rel) else unchanged).append(rel)
    return {"schema": SCHEMA, "ok": True, "command": HOOK_COMMAND, "written": written, "unchanged": unchanged}


def hook_agents(pkg: Package) -> list[str]:
    """The agents in package.yaml that have a Stop hook, else both."""
    agents = [a for a in pkg.agents if a in HOOK_FILES]
    return agents or list(HOOK_FILES)


def stop_hook(pkg: Package, stdin: str) -> dict | None:
    """What the Stop hook answers: a block decision, or None to let the agent stop."""
    try:
        event = json.loads(stdin) if stdin.strip() else {}
    except json.JSONDecodeError:
        event = {}
    if isinstance(event, dict) and event.get("stop_hook_active"):
        return None  # it already blocked once this turn; never trap the agent in a loop
    items = load_backlog(pkg)
    ident = item_here(pkg, items)
    item = next((i for i in items if i.id == ident), None)
    if item is None or item.status in ("blocked", "dropped", "proposed"):
        return None
    try:
        result = done(pkg, ident, check_only=True)
    except WorkError as err:
        if err.code not in ("check_failed", "test_changes", "outside_touches", "gate_error"):
            return None
        failed = [c for c in err.details.get("checks", []) if not c["passed"]]
        detail = "".join(f"\n\n{c['name']} failed (exit {c['exit_code']}): {c['command']}\n{c['output']}"
                         for c in failed)
        return {"decision": "block", "reason": (
            f"{err.message}{detail}\n\nFix it and run `its done {ident}`. If you can't finish it honestly, run "
            f"`its block {ident} --reason '...'` and stop.")}
    if result["status"] != "done":
        return {"decision": "block", "reason": (
            f"The checks for {ident} pass, but it isn't marked done: run `its done {ident}`, then commit.")}
    return None
