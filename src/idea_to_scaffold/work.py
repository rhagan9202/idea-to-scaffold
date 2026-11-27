"""Working the backlog: `its next`, `show`, `start` and `done`, and the `backlog.json` mirror.

Every function returns a plain dict in the versioned JSON contract (spec section 16), so the CLI can print it as
JSON for agents or as text for people, and a future MCP server can return it unchanged. Failures raise `WorkError`,
which carries a stable error code and the exit code the CLI should use.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import claims as cl
from .backlog import (
    BACKLOG,
    BacklogError,
    BacklogItem,
    blocked_by,
    find_cycles,
    load_backlog,
    load_proposals,
    parse_backlog,
    ready_items,
    set_status,
)
from .ids import KIND_LABELS, META_FILES, definition_on_line, id_kind, is_placeholder, scan_lines, strip_links
from .linker import NAV_BLOCK_RE, Doc, Target, _load_docs, link_package, scan_package
from .locking import LockTimeout, package_lock
from .package import Package

SCHEMA = 1
BACKLOG_JSON = "planning/backlog.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING_READY = 3
EXIT_BLOCKED = 4
EXIT_CHECK_FAILED = 5
EXIT_OVERLAP = 6
EXIT_NOT_LANDED = 7

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
        "proposals": [{**i.to_dict(), "file": shown(pkg, pkg.rel(p))} for p, i in load_proposals(pkg)],
    }


@dataclass
class View:
    """The backlog as readiness sees it: from the integration branch when working on another branch, plus claims."""
    items: list[BacklogItem]
    source: str                      # "working tree" or the ref the backlog was read from
    base: str | None = None          # the integration branch's ref, if this is a git repo that has one
    branch: str | None = None        # the checked-out branch
    claims: list[cl.Claim] = field(default_factory=list)  # active claims (unmerged claim branches), orphans included

    @property
    def claimed(self) -> set[str]:
        return {c.id for c in self.claims}


def integration_branch(pkg: Package) -> str:
    return str(pkg.get("integration_branch") or "main")


def remote_of(pkg: Package) -> str | None:
    """The remote that claims are shared through, or None if there is none."""
    if not cl.in_git(pkg.root):
        return None
    return cl.default_remote(pkg.root, pkg.get("remote"))


def fetch(pkg: Package, base: str | None = None) -> str | None:
    """`--fetch`: bring the integration branch and every claim branch up to date from the remote. Returns the
    remote fetched from, or None if there is none."""
    remote = remote_of(pkg)
    if remote is None:
        return None
    try:
        cl.fetch(pkg.root, remote, base or integration_branch(pkg))
    except cl.GitError as exc:
        raise WorkError("fetch_failed", str(exc), details={"remote": remote}) from exc
    return remote


def view(pkg: Package, base: str | None = None) -> View:
    """Read the backlog for readiness. On the integration branch, or outside git, that is the working tree. On any
    other branch it is the integration branch's copy, so a dependency counts as done only once it is merged."""
    items = load_backlog(pkg)
    if not cl.in_git(pkg.root):
        return View(items, "working tree")
    name = base or integration_branch(pkg)
    base_ref = cl.resolve_base(pkg.root, name, remote_of(pkg) or "origin")
    branch = cl.current_branch(pkg.root)
    source = "working tree"
    if base_ref is not None and branch not in (name, base_ref):
        text = cl.read_at(pkg.root, base_ref, pkg.path_of(BACKLOG))
        if text is not None:
            items, source = parse_backlog(text), base_ref
    claims = cl.active_claims(cl.list_claims(pkg.root, _closed(items), {i.id for i in items}))
    return View(items, source, base_ref, branch, claims)


def _closed(items: list[BacklogItem]) -> set[str]:
    return {i.id for i in items if i.status in ("done", "dropped")}


def _running(v: View) -> list[tuple[str, list[str]]]:
    """(ID, Touches) of everything being worked on: active claims and in-progress items. A claim for an ID that
    isn't in the backlog (orphaned) is left out: `its claims` reports it, and it must not block all parallel work."""
    by_id = {i.id: i for i in v.items}
    running = [(c.id, by_id[c.id].touches) for c in v.claims if c.id in by_id]
    seen = {ident for ident, _ in running}
    running += [(i.id, i.touches) for i in v.items if i.status == "in-progress" and i.id not in seen]
    return running


def overlaps_with_running(v: View, item: BacklogItem) -> list[str]:
    """Running items that `item` might share files with. Only items confirmed apart by their Touches can run
    together, so an item with no Touches overlaps everything."""
    return [ident for ident, paths in _running(v) if ident != item.id and cl.touches_overlap(item.touches, paths)]


def _parallel(v: View, ready: list[BacklogItem], limit: int) -> tuple[list[BacklogItem], list[dict]]:
    """Ready items that can run alongside the active claims and each other without touching the same files."""
    busy = _running(v)
    chosen: list[BacklogItem] = []
    held: list[dict] = []
    for item in ready:
        clash = [ident for ident, paths in busy if cl.touches_overlap(item.touches, paths)]
        if clash or len(chosen) >= limit:
            if clash:
                held.append({"id": item.id, "overlaps": clash, "touches_listed": bool(item.touches)})
            continue
        chosen.append(item)
        busy.append((item.id, item.touches))
    return chosen, held


def _nothing_reason(items: list[BacklogItem], ready: list[BacklogItem], waiting: list[dict], v: View) -> str:
    """Why `its next` has no item: overlap, waiting, in_progress or complete."""
    if ready:
        return "overlap"        # items are ready, but each might touch files that running work touches
    if waiting:
        return "waiting"        # todo items wait on dependencies (or a cycle)
    if v.claims or any(i.status in ("in-progress", "blocked") for i in items):
        return "in_progress"    # nothing left to start; the rest is being worked on or is blocked
    return "complete"


def next_payload(pkg: Package, *, base: str | None = None, parallel: int | None = None) -> dict:
    """The next item that can start now, every ready item, and why the rest are waiting. Claimed items are never
    offered, and neither is an item that might overlap running work, so `its start` never refuses what `its next`
    offers."""
    v = view(pkg, base)
    items = v.items
    _check_unique(items)
    ready = ready_items(items, v.claimed)
    waiting = [
        {"id": i.id, "title": i.title, "blocked_by": blocked_by(i, items)}
        for i in items if i.status == "todo" and i.id not in v.claimed and i not in ready
    ]
    startable, _ = _parallel(v, ready, len(ready) or 1)
    first = startable[0] if startable else None
    payload = {
        "schema": SCHEMA,
        "ok": True,
        "source": v.source,
        "item": item_payload(pkg, first, items) if first else None,
        "reason": None if first else _nothing_reason(items, ready, waiting, v),
        "ready": [item_payload(pkg, i, items) for i in ready],
        "in_progress": [i.id for i in items if i.status == "in-progress"],
        "claimed": [c.to_dict() for c in v.claims],
        "waiting": waiting,
        "blocked": [{"id": i.id, "title": i.title, "reason": i.get("Blocked") or None}
                    for i in items if i.status == "blocked"],
        "cycles": find_cycles(items),
    }
    if parallel is not None:
        chosen, held = _parallel(v, ready, parallel)
        payload["parallel"] = [item_payload(pkg, i, items) for i in chosen]
        payload["held_back"] = held
    return payload


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
    proposal = next(((p, i) for p, i in load_proposals(pkg) if i.id == ident), None) if target is None else None
    if proposal is not None:
        path, item = proposal
        text = read_raw(path).replace("\r\n", "\n").strip()
        return {"schema": SCHEMA, "ok": True, "id": ident, "kind": KIND_LABELS[id_kind(ident)],
                "file": shown(pkg, pkg.rel(path)), "anchor": None, "line": 1, "text": strip_links(text),
                "referenced_by": [], "proposal": True, "item": item.to_dict()}
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
    found = [i for i in items if i.id == ident]
    if not found:
        raise WorkError("not_found", f"{ident} is not in the backlog", details={"id": ident})
    if len(found) > 1:
        raise WorkError("duplicate_id", f"{ident} is defined {len(found)} times in the backlog; give each a unique ID "
                        "first (its validate lists them)", details={"id": ident})
    return found[0]


def _check_unique(items: list[BacklogItem]) -> None:
    seen: set[str] = set()
    dupes = sorted({i.id for i in items if i.id in seen or seen.add(i.id)})
    if dupes:
        raise WorkError("duplicate_id", f"{', '.join(dupes)} {'is' if len(dupes) == 1 else 'are'} defined more than "
                        "once in the backlog; give each a unique ID first (its validate lists them)",
                        details={"ids": dupes})


@contextmanager
def _locked(pkg: Package):
    """Hold the package lock (the repository's, in git) for a check-then-write sequence."""
    try:
        with package_lock(pkg.root):
            yield
    except LockTimeout as exc:
        raise WorkError("locked", str(exc)) from None


def _refresh_mirror(pkg: Package) -> None:
    """Keep `backlog.json` current after a status change, if someone generated it."""
    if pkg.path_of(BACKLOG_JSON).is_file():
        write_backlog_json(pkg)


def _quote(arg: str) -> str:
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


def launch_commands(ident: str, worktree: str | None) -> dict:
    """Commands that start an agent on the item. its prints them; it never launches an agent itself.

    `claude` and `codex` are shell lines quoted for this platform (cmd.exe on Windows); `argv` and `cwd` give the same
    command unquoted, for a program to run without a shell."""
    prompt = f"Read AGENTS.md, then follow docs/playbooks/implement-task.md for {ident}."
    sep = " & " if os.name == "nt" else " && "
    cd = f"cd {_quote(worktree)}{sep}" if worktree else ""
    return {
        "claude": f"{cd}claude {_quote(prompt)}", "codex": f"{cd}codex {_quote(prompt)}",
        "cwd": worktree, "argv": {"claude": ["claude", prompt], "codex": ["codex", prompt]},
    }


def start(
    pkg: Package,
    ident: str | None,
    *,
    force: bool = False,
    branch: bool = False,
    worktree: str | None = None,
    agent: str | None = None,
    base: str | None = None,
    allow_overlap: bool = False,
    push: bool | None = None,
) -> dict:
    """Start an item. With no `ident`, start the next item that can start now (`start --next`): picking and claiming
    happen under one lock, so two agents asking at the same moment get different items.

    Without `branch` or `worktree`, set a ready `todo` item to `in-progress` in the backlog. With either, claim it
    instead: create the claim branch `its/<ID>-<slug>` from the integration branch (checked out here, or in a new
    worktree), and leave the backlog alone, since the branch is the claim. `force` also starts an item whose
    dependencies aren't done, or a `blocked` one.

    Items only run in parallel when their Touches confirm they change different files: starting an item that might
    overlap an active claim or an in-progress item is refused unless `allow_overlap`.

    A claim is pushed to the remote (`origin`, or `remote` in package.yaml) when there is one, so other machines see
    it; the push only succeeds if nobody claimed the item there first. `push=False` (`--no-push`), or
    `push_claims: false` in package.yaml, keeps it local.
    """
    previous_branch = cl.current_branch(pkg.root) if cl.in_git(pkg.root) else None
    with _locked(pkg):
        if ident is None:
            ident = _pick_next(pkg, base)
        payload = _start(pkg, ident, force=force, branch=branch, worktree=worktree, agent=agent, base=base,
                         allow_overlap=allow_overlap)
    claim = payload.get("claim")
    if push is None:
        push = pkg.get("push_claims") is not False
    payload["pushed"] = False
    if not claim or payload.get("claim_held_here") or not push:
        return payload
    remote = remote_of(pkg)
    if remote is None:
        return payload
    try:  # outside the lock: a slow network never holds up other agents on this machine
        cl.push_claim(pkg.root, remote, ident, claim["branch"])
    except cl.PushRejected as exc:
        with contextlib.suppress(cl.GitError):
            if claim["worktree"] is None and previous_branch:  # --branch checked it out here: go back first
                cl.git(pkg.root, "switch", "--quiet", previous_branch)
            cl.release_claim(pkg.root, ident, force=True)
        raise WorkError("claimed", f"{exc}; your local claim was undone", details={"id": ident, "remote": remote}) \
            from exc
    except cl.GitError as exc:
        payload["push_error"] = str(exc)
        return payload
    payload["pushed"] = True
    payload["remote"] = remote
    return payload


def _pick_next(pkg: Package, base: str | None) -> str:
    """The item `its next` would offer, for `start --next`."""
    out = next_payload(pkg, base=base)
    if out["item"] is None:
        raise WorkError("nothing_ready", f"nothing can start now ({out['reason']})", EXIT_NOTHING_READY,
                        {"reason": out["reason"]})
    return out["item"]["id"]


def _start(
    pkg: Package, ident: str, *, force: bool, branch: bool, worktree: str | None, agent: str | None,
    base: str | None, allow_overlap: bool,
) -> dict:
    """`start`, run under the lock: readiness, claims and overlap are checked and the claim made in one step."""
    claiming = branch or worktree is not None
    v = view(pkg, base)
    # Readiness and overlap always come from the integration branch (v.items). A plain start writes the working
    # tree's backlog, so the item's own status is read from there.
    items = v.items if claiming else load_backlog(pkg)
    item = _find(items, ident)
    if not claiming:
        _check_unique(items)
    if item.placeholder:
        raise WorkError("placeholder", f"{ident} has a placeholder ID; give it a permanent T-n.m ID before starting it",
                        details={"id": ident})
    claim = next((c for c in v.claims if c.id == ident), None)
    if claim is not None:
        here = v.branch == claim.branch or bool(
            claim.worktree and cl.toplevel(pkg.root).resolve() == Path(claim.worktree).resolve())
        if here:
            return {
                "schema": SCHEMA, "ok": True, "id": ident, "previous_status": item.status, "status": item.status,
                "forced": False, "claim": claim.to_dict(), "claim_held_here": True,
                "item": item_payload(pkg, item, items), "commands": launch_commands(ident, claim.worktree),
            }
        where = f" in {claim.worktree}" if claim.worktree else ""
        raise WorkError("claimed", f"{ident} is already claimed by branch {claim.branch}{where}",
                        details={"id": ident, "claim": claim.to_dict()})
    if item.status == "in-progress":
        raise WorkError("already_started", f"{ident} is already in-progress", details={"id": ident})
    if item.status != "todo" and not (force and item.status == "blocked"):
        raise WorkError("bad_status", f"{ident} is {item.status or 'missing a status'}; only todo items can be started",
                        details={"id": ident, "status": item.status})
    base_item = next((i for i in v.items if i.id == ident), item)
    waiting = blocked_by(base_item, v.items)
    if waiting and not force:
        where = f" on {v.source}" if v.source != "working tree" else ""
        raise WorkError(
            "blocked",
            f"{ident} depends on {', '.join(waiting)}, which {'is' if len(waiting) == 1 else 'are'} not done{where}",
            EXIT_BLOCKED, {"id": ident, "blocked_by": waiting},
        )
    running = dict(_running(v))
    clash = overlaps_with_running(v, item)
    if clash and not allow_overlap:
        others = "; ".join(f"{o} (touches {', '.join(running.get(o, [])) or 'anything: no Touches listed'})"
                           for o in clash)
        why = ("it lists no Touches, so it could change any file" if not item.touches
               else f"its Touches ({', '.join(item.touches)}) may change the same files")
        raise WorkError(
            "overlap",
            f"{ident} can't run alongside {others}: {why}. Finish those first, "
            "list non-overlapping Touches on both items, or pass --allow-overlap",
            EXIT_OVERLAP, {"id": ident, "overlaps": clash, "touches_listed": bool(item.touches),
                           "overlapping_touches": {other: running.get(other, []) for other in clash}},
        )
    forced = bool(waiting) or item.status != "todo" or bool(clash)
    if not claiming:
        try:
            previous = set_status(pkg, ident, "in-progress")
        except BacklogError as exc:
            raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
        _refresh_mirror(pkg)
        items = load_backlog(pkg)
        return {
            "schema": SCHEMA, "ok": True, "id": ident, "previous_status": previous, "status": "in-progress",
            "forced": forced, "claim": None, "item": item_payload(pkg, _find(items, ident), items),
        }
    if v.base is None:
        raise WorkError("no_base", f"can't claim {ident}: this is not a git repository with an integration branch "
                        f"({integration_branch(pkg) if base is None else base})", details={"id": ident})
    path = None
    if worktree is not None:
        top = cl.toplevel(pkg.root)
        path = Path(worktree) if worktree else top.parent / f"{top.name}-{ident}"
        path = path if path.is_absolute() else (Path.cwd() / path)
        if path.exists():
            raise WorkError("worktree_exists", f"{path} already exists; pass another path to --worktree",
                            details={"id": ident, "worktree": str(path)})
    try:
        claim = cl.create_claim(pkg.root, ident, item.title, base_ref=v.base, worktree=path, agent=agent,
                                closed=_closed(items))
    except cl.ClaimTaken as exc:
        raise WorkError("claimed", str(exc), details={"id": ident, "claim": exc.claim.to_dict()}) from exc
    except cl.GitError as exc:
        raise WorkError("git_error", str(exc), details={"id": ident}) from exc
    return {
        "schema": SCHEMA, "ok": True, "id": ident, "previous_status": item.status, "status": item.status,
        "forced": forced, "claim": claim.to_dict(), "item": item_payload(pkg, item, items),
        "commands": launch_commands(ident, claim.worktree),
    }


def claims_payload(pkg: Package, base: str | None = None) -> dict:
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "claims need a git repository")
    v = view(pkg, base)
    claims = cl.list_claims(pkg.root, _closed(v.items), {i.id for i in v.items})
    out = [c.to_dict() for c in claims]
    if v.base is not None:
        _add_conflicts(pkg, v.base, claims, out)
    return {"schema": SCHEMA, "ok": True, "base": v.base, "claims": out}


def _ref(claim: cl.Claim) -> str:
    return f"refs/remotes/{claim.branch}" if claim.remote else f"refs/heads/{claim.branch}"


def _add_conflicts(pkg: Package, base: str, claims: list[cl.Claim], out: list[dict]) -> None:
    """Dry-run merges (nothing changes): which active claims would conflict with the integration branch, or with
    each other, if merged. Only claims with commits of their own can conflict."""
    pairs = list(zip(claims, out, strict=True))
    for c, d in pairs:
        d["ahead"] = cl.ahead(pkg.root, _ref(c), base)
        d["conflicts_with_base"] = []
        d["conflicts_with"] = []
    active = [(c, d) for c, d in pairs if not c.finished and d["ahead"]]
    for c, d in active:
        d["conflicts_with_base"] = cl.conflicting_files(pkg.root, base, _ref(c)) or []
    for i, (a, da) in enumerate(active):
        for b, db in active[i + 1:]:
            files = cl.conflicting_files(pkg.root, _ref(a), _ref(b))
            if files:
                da["conflicts_with"].append({"id": b.id, "files": files})
                db["conflicts_with"].append({"id": a.id, "files": files})


def release(pkg: Package, ident: str, *, force: bool = False, remote: bool = False) -> dict:
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "claims need a git repository")
    v = view(pkg)
    try:
        result = cl.release_claim(pkg.root, ident, force=force, remote=remote, base_ref=v.base,
                                  closed=_closed(v.items))
    except cl.GitError as exc:
        raise WorkError("git_error", str(exc), details={"id": ident}) from exc
    return {"schema": SCHEMA, "ok": True, **result}


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
    proc = subprocess.run(check.command, shell=True, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL)
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
    """Run the item's checks and, if they pass, set it `done`. With no ID, uses the item of the claim branch that is
    checked out, else the only `in-progress` item.

    Checks run only in a scaffolded repo (a planning package has no code to test).
    """
    items = load_backlog(pkg)
    if ident is None and cl.in_git(pkg.root):
        ident = cl.item_of_branch(cl.current_branch(pkg.root)) or cl.claim_of_worktree(pkg.root)
    if ident is None:
        active = [i.id for i in items if i.status == "in-progress"]
        if len(active) != 1:
            why = "no item is in-progress" if not active else f"more than one item is in-progress ({', '.join(active)})"
            raise WorkError("which_item", f"{why}; name the item, e.g. `its done T-1.1`",
                            details={"in_progress": active})
        ident = active[0]
    item = _find(items, ident)
    if item.status == "done":
        raise WorkError("already_done", f"{ident} is already done", details={"id": ident})
    if item.status in ("proposed", "dropped") or item.placeholder:
        raise WorkError("bad_status", f"{ident} is {item.status}; it can't be done before it is scheduled",
                        details={"id": ident, "status": item.status})
    if "status" not in item.fields:
        raise WorkError("no_status", f"{ident} has no **Status:** field; add one, then run its done again",
                        details={"id": ident})
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
    try:
        previous = set_status(pkg, ident, "done")
    except BacklogError as exc:
        raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
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
        "merge_needed": _off_integration_branch(pkg),
    }


def _off_integration_branch(pkg: Package) -> bool:
    """Whether work here only counts once merged: a git checkout that isn't on the integration branch."""
    if not cl.in_git(pkg.root):
        return False
    return cl.current_branch(pkg.root) != integration_branch(pkg)


# --- backlog.json ---------------------------------------------------------------------------------------------

def write_backlog_json(pkg: Package) -> Path:
    """Write the backlog mirror next to the backlog. It is generated, gitignored and never edited by hand."""
    path = pkg.path_of(BACKLOG_JSON)
    path.write_text(json.dumps(backlog_payload(pkg), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# --- renumbering placeholders ---------------------------------------------------------------------------------

TASK_NUM_RE = re.compile(r"^T-(\d+)\.(\d+)$")
TASK_MENTION_RE = re.compile(r"(?<![\w-])T-(\d+)\.(\d+)(?!\d)")


def renumber(pkg: Package, ids: list[str] | None = None, *, dry_run: bool = False, force: bool = False) -> dict:
    """Give placeholder items (`T-new-...`) permanent `T-n.m` IDs and rewrite every mention in the planning docs.

    Run it on the integration branch after merging branches that proposed items, so numbers are picked in one place
    and never collide. `n` is the item's milestone (`M3` -> 3), or for an item with none, the milestone of the last
    numbered item; `m` is the next free number in it. Dropped placeholders are left alone unless named.
    """
    if not dry_run and not force and cl.in_git(pkg.root):
        here, target = cl.current_branch(pkg.root), integration_branch(pkg)
        if here != target:
            raise WorkError("not_integration_branch",
                            f"renumber picks permanent IDs, so run it on {target} (this is {here or 'a detached HEAD'}),"
                            " after merging the branches that proposed items; --force runs it here anyway",
                            details={"branch": here, "integration_branch": target})
    with _locked(pkg):
        return _renumber(pkg, ids, dry_run=dry_run)


def _renumber(pkg: Package, ids: list[str] | None, *, dry_run: bool) -> dict:
    items = load_backlog(pkg)
    proposals = load_proposals(pkg)
    proposal_of = {item.id: path for path, item in proposals}
    known = {i.id for i in items} | set(proposal_of)
    placeholders = [i.id for i in items if i.placeholder] + [i.id for _, i in proposals]
    dupes = sorted({x for x in placeholders if placeholders.count(x) > 1})
    if dupes:
        raise WorkError("duplicate_id", f"{', '.join(dupes)} {'is' if len(dupes) == 1 else 'are'} proposed more than "
                        "once; merge or rename the duplicates, then renumber", details={"ids": dupes})
    for ident in ids or []:
        if ident not in known:
            raise WorkError("not_found", f"{ident} is not in the backlog or the proposals", details={"id": ident})
        if not is_placeholder(ident):
            raise WorkError("not_placeholder", f"{ident} already has a permanent ID", details={"id": ident})
    highest: dict[int, int] = {}
    # Numbers already used anywhere count, not only those in the backlog: a deleted item's ID may still be mentioned
    # in other docs, and reusing it would make those mentions point at the wrong item.
    used = [(m.group(1), m.group(2)) for m in (TASK_NUM_RE.match(i.id) for i in items) if m]
    for path in pkg.planning_files() + pkg.proposal_files():
        try:
            used += TASK_MENTION_RE.findall(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    for major, minor in used:
        highest[int(major)] = max(highest.get(int(major), 0), int(minor))
    pairs: list[tuple[str, str]] = []
    last_major = 1

    def assign(item: BacklogItem) -> None:
        if ids is not None and item.id not in ids:
            return
        if ids is None and item.status == "dropped":
            return
        ms = re.fullmatch(r"M(\d+)", item.milestone.strip())
        major = int(ms.group(1)) if ms else last_major
        highest[major] = highest.get(major, 0) + 1
        pairs.append((item.id, f"T-{major}.{highest[major]}"))

    for item in items:
        m = TASK_NUM_RE.match(item.id)
        if m:
            last_major = int(m.group(1))
        else:
            assign(item)
    for _, item in proposals:  # proposals without a milestone go under the backlog's last milestone
        assign(item)
    changed: list[str] = []
    moved: list[str] = []
    if pairs and not dry_run:
        backlog_path = pkg.path_of(BACKLOG)
        renumbering = {old for old, _ in pairs}
        appended = [path for path, item in proposals if item.id in renumbering]
        if appended:  # move each renumbered proposal to the end of the backlog, then delete its file
            text = read_raw(backlog_path)
            eol = "\r\n" if "\r\n" in text else "\n"
            for path in appended:
                section = read_raw(path).replace("\r\n", "\n").strip("\n")
                text = text.rstrip("\r\n") + eol + eol + section.replace("\n", eol) + eol
            write_raw(backlog_path, text)
            for path in appended:
                path.unlink()
                moved.append(shown(pkg, pkg.rel(path)))
        patterns = [(re.compile(rf"(?<![\w/#-]){re.escape(old)}(?![\w-])"), new) for old, new in pairs]
        for path in pkg.planning_files() + pkg.proposal_files():
            if pkg.rel(path) in META_FILES:  # generated; `its link` rewrites it below
                continue
            text = read_raw(path)
            new_text = text
            for pattern, new in patterns:
                new_text = pattern.sub(new, new_text)
            if new_text != text or path == backlog_path and appended:
                if new_text != text:
                    write_raw(path, new_text)
                changed.append(shown(pkg, pkg.rel(path)))
        link_package(pkg)
        _refresh_mirror(pkg)
    return {
        "schema": SCHEMA, "ok": True, "dry_run": dry_run,
        "renumbered": [{"from": old, "to": new} for old, new in pairs], "files": changed,
        "proposals_moved": moved,
    }


def read_raw(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_raw(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
