"""Working the backlog: `its next`, `show`, `start` and `done`, and the `backlog.json` mirror.

Every function returns a plain dict in the versioned JSON contract (spec section 16), so the CLI can print it as
JSON for agents or as text for people, and a future MCP server can return it unchanged. Failures raise `WorkError`,
which carries a stable error code and the exit code the CLI should use.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .backlog import (
    BACKLOG,
    BacklogItem,
    blocked_by,
    find_cycles,
    load_backlog,
    ready_items,
    set_status,
)
from .ids import KIND_LABELS, definition_on_line, id_kind, scan_lines, strip_links
from .linker import NAV_BLOCK_RE, Doc, Target, _load_docs, scan_package
from .package import Package

SCHEMA = 1
BACKLOG_JSON = "planning/backlog.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING_READY = 3
EXIT_BLOCKED = 4
EXIT_CHECK_FAILED = 5

OUTPUT_TAIL = 40  # lines of a failed check's output to keep


@dataclass
class WorkError(Exception):
    code: str
    message: str
    exit_code: int = EXIT_ERROR
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


def shown(pkg: Package, rel: str) -> str:
    """A logical path as it is on disk, relative to the package or repo root (docs/planning/... in a repo)."""
    try:
        return pkg.path_of(rel).relative_to(pkg.root).as_posix()
    except ValueError:
        return rel


# --- payloads -------------------------------------------------------------------------------------------------

def item_payload(pkg: Package, item: BacklogItem, items: list[BacklogItem]) -> dict:
    waiting = blocked_by(item, items)
    return {
        **item.to_dict(),
        "ready": item.status == "todo" and not waiting,
        "blocked_by": waiting,
        "file": shown(pkg, BACKLOG),
        "line": item.heading_line + 1,
    }


def backlog_payload(pkg: Package) -> dict:
    """The whole backlog as JSON: the content of `backlog.json`."""
    items = load_backlog(pkg)
    return {
        "schema": SCHEMA,
        "ok": True,
        "backlog": shown(pkg, BACKLOG),
        "items": [item_payload(pkg, i, items) for i in items],
        "cycles": find_cycles(items),
    }


def next_payload(pkg: Package) -> dict:
    """The next ready item, every ready item, and why the rest are waiting."""
    items = load_backlog(pkg)
    ready = ready_items(items)
    waiting = [
        {"id": i.id, "title": i.title, "blocked_by": blocked_by(i, items)}
        for i in items if i.status == "todo" and i not in ready
    ]
    return {
        "schema": SCHEMA,
        "ok": True,
        "item": item_payload(pkg, ready[0], items) if ready else None,
        "ready": [item_payload(pkg, i, items) for i in ready],
        "in_progress": [i.id for i in items if i.status == "in-progress"],
        "waiting": waiting,
        "cycles": find_cycles(items),
    }


def _definition(doc: Doc, ident: str, target: Target) -> tuple[int, str]:
    """(1-based line in the file, text) of an ID's definition: its whole section if it has a heading."""
    offset = doc.front.count("\n")
    body = doc.body
    lines = body.split("\n")
    scanned = scan_lines(body)
    if target.anchor is None:  # an ADR or wireframe: the file is the definition
        text = NAV_BLOCK_RE.sub("\n", body).strip()
        return offset + 1, "\n".join(strip_links(ln) for ln in text.split("\n"))
    for ln in scanned:
        if ln.in_code or definition_on_line(ln) != ident:
            continue
        if ln.heading is None:
            return offset + ln.number + 1, strip_links(ln.text).strip()
        if ln.anchor != target.anchor:
            continue
        level = len(ln.text) - len(ln.text.lstrip("#"))
        end = len(lines)
        for nxt in scanned[ln.number + 1:]:
            if nxt.heading is not None and not nxt.in_code and len(nxt.text) - len(nxt.text.lstrip("#")) <= level:
                end = nxt.number
                break
        section = "\n".join(strip_links(x) for x in lines[ln.number:end]).strip()
        return offset + ln.number + 1, section
    return offset + 1, ""


def show_payload(pkg: Package, ident: str) -> dict:
    """Where an ID is defined, its text, and the docs that mention it (the ones to revisit if it changes)."""
    docs = _load_docs(pkg)
    scan = scan_package(pkg, docs)
    target = scan.index.get(ident)
    if target is None:
        mentioned = sorted(scan.references.get(ident, set()))
        hint = f"; it is mentioned in {', '.join(shown(pkg, r) for r in mentioned)}" if mentioned else ""
        raise WorkError("not_found", f"{ident} is not defined in the planning docs{hint}",
                        details={"id": ident, "mentioned_in": [shown(pkg, r) for r in mentioned]})
    line, text = _definition(docs[target.rel], ident, target)
    referenced_by = sorted(r for r in scan.references.get(ident, set()) if r != target.rel)
    payload = {
        "schema": SCHEMA,
        "ok": True,
        "id": ident,
        "kind": KIND_LABELS[id_kind(ident)],
        "file": shown(pkg, target.rel),
        "anchor": target.anchor,
        "line": line,
        "text": text,
        "referenced_by": [shown(pkg, r) for r in referenced_by],
    }
    if id_kind(ident) == "T":
        items = load_backlog(pkg)
        item = next((i for i in items if i.id == ident), None)
        if item is not None:
            payload["item"] = item_payload(pkg, item, items)
    return payload


# --- status changes -------------------------------------------------------------------------------------------

def _find(items: list[BacklogItem], ident: str) -> BacklogItem:
    item = next((i for i in items if i.id == ident), None)
    if item is None:
        raise WorkError("not_found", f"{ident} is not in the backlog", details={"id": ident})
    return item


def _refresh_mirror(pkg: Package) -> None:
    """Keep `backlog.json` current after a status change, if someone generated it."""
    if pkg.path_of(BACKLOG_JSON).is_file():
        write_backlog_json(pkg)


def start(pkg: Package, ident: str, *, force: bool = False) -> dict:
    """Set a ready `todo` item to `in-progress`. `force` also starts an item whose dependencies aren't done, or a
    `blocked` one."""
    items = load_backlog(pkg)
    item = _find(items, ident)
    if item.placeholder:
        raise WorkError("placeholder", f"{ident} has a placeholder ID; give it a permanent T-n.m ID before starting it",
                        details={"id": ident})
    if item.status == "in-progress":
        raise WorkError("already_started", f"{ident} is already in-progress", details={"id": ident})
    if item.status != "todo" and not (force and item.status == "blocked"):
        raise WorkError("bad_status", f"{ident} is {item.status or 'missing a status'}; only todo items can be started",
                        details={"id": ident, "status": item.status})
    waiting = blocked_by(item, items)
    if waiting and not force:
        raise WorkError(
            "blocked", f"{ident} depends on {', '.join(waiting)}, which {'is' if len(waiting) == 1 else 'are'} not done",
            EXIT_BLOCKED, {"id": ident, "blocked_by": waiting},
        )
    previous = set_status(pkg, ident, "in-progress")
    _refresh_mirror(pkg)
    items = load_backlog(pkg)
    return {
        "schema": SCHEMA,
        "ok": True,
        "id": ident,
        "previous_status": previous,
        "status": "in-progress",
        "forced": bool(waiting) or previous != "todo",
        "item": item_payload(pkg, _find(items, ident), items),
    }


@dataclass
class Check:
    name: str
    command: str


def checks_for(pkg: Package, item: BacklogItem) -> list[Check]:
    """The item's Verify command, then the package's test and lint commands, without repeats."""
    out: list[Check] = []
    candidates = [("verify", item.verify_command or "")]
    candidates += [(name, pkg.commands.get(name, "")) for name in ("test", "lint")]
    for name, command in candidates:
        command = command.strip()
        if command and command != "true" and all(c.command != command for c in out):
            out.append(Check(name, command))
    return out


def run_check(check: Check, cwd: Path) -> dict:
    proc = subprocess.run(check.command, shell=True, cwd=cwd, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).rstrip("\n").split("\n")
    return {
        "name": check.name,
        "command": check.command,
        "exit_code": proc.returncode,
        "passed": proc.returncode == 0,
        "output": "\n".join(output[-OUTPUT_TAIL:]) if proc.returncode else "",
    }


def done(
    pkg: Package,
    ident: str | None = None,
    *,
    verify: bool = True,
    on_check: Callable[[Check], None] | None = None,
) -> dict:
    """Run the item's checks and, if they pass, set it `done`. With no ID, uses the only `in-progress` item.

    Checks run only in a scaffolded repo (a planning package has no code to test).
    """
    items = load_backlog(pkg)
    if ident is None:
        active = [i.id for i in items if i.status == "in-progress"]
        if len(active) != 1:
            why = "no item is in-progress" if not active else f"more than one item is in-progress ({', '.join(active)})"
            raise WorkError("which_item", f"{why}; name the item, e.g. `its done T-1.1`", details={"in_progress": active})
        ident = active[0]
    item = _find(items, ident)
    if item.status == "done":
        raise WorkError("already_done", f"{ident} is already done", details={"id": ident})
    if item.status in ("proposed", "dropped") or item.placeholder:
        raise WorkError("bad_status", f"{ident} is {item.status}; it can't be done before it is scheduled",
                        details={"id": ident, "status": item.status})
    results: list[dict] = []
    skipped = None
    if not verify:
        skipped = "--no-verify"
    elif not pkg.repo_layout:
        skipped = "this is a planning package, not a scaffolded repo"
    else:
        for check in checks_for(pkg, item):
            if on_check:
                on_check(check)
            results.append(run_check(check, pkg.root))
        failed = [r["name"] for r in results if not r["passed"]]
        if failed:
            raise WorkError(
                "check_failed", f"{ident} is not done: {', '.join(failed)} failed", EXIT_CHECK_FAILED,
                {"id": ident, "checks": results},
            )
    manual = item.verify if item.verify and not item.verify_command else None
    previous = set_status(pkg, ident, "done")
    _refresh_mirror(pkg)
    items = load_backlog(pkg)
    return {
        "schema": SCHEMA,
        "ok": True,
        "id": ident,
        "previous_status": previous,
        "status": "done",
        "checks": results,
        "checks_skipped": skipped,
        "manual_verify": manual,
        "now_ready": [i.id for i in ready_items(items) if ident in i.depends_on],
    }


# --- backlog.json ---------------------------------------------------------------------------------------------

def write_backlog_json(pkg: Package) -> Path:
    """Write the backlog mirror next to the backlog. It is generated, gitignored and never edited by hand."""
    path = pkg.path_of(BACKLOG_JSON)
    path.write_text(json.dumps(backlog_payload(pkg), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
