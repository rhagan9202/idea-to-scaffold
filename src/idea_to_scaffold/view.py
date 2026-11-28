"""What the backlog and the claims say, read-only: the backlog and next payloads, `its show`, the integration
branch and its remote, and which checkout this is.

Every function returns a plain dict in the versioned JSON contract (the workflow spec, section 16), so the CLI can print it as
JSON for agents or as text for people. Failures raise `WorkError`, with a stable error code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import claims as cl
from . import gate, gitx
from .backlog import (
    BACKLOG,
    BacklogItem,
    blocked_by,
    check_unique,
    find_cycles,
    load_backlog,
    load_proposals,
    parse_backlog,
    read_raw,
    ready_items,
)
from .contract import (
    SCHEMA,
    WorkError,
)
from .ids import KIND_LABELS, definition_on_line, id_kind, scan_lines, strip_links
from .linker import NAV_BLOCK_RE, Doc, Target, _load_docs, scan_package
from .locking import git_common_dir, git_store_dir
from .package import Package


def shown(pkg: Package, rel: str) -> str:
    """A logical path as it is on disk, relative to the package or repo root (docs/planning/... in a repo)."""
    try:
        return pkg.path_of(rel).relative_to(pkg.root).as_posix()
    except ValueError:
        return rel


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
    behind: int = 0                  # on the integration branch: commits its remote copy has that it lacks

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


STALE_FETCH_SECONDS = 3600


def remote_freshness(pkg: Package) -> dict | None:
    """When a remote exists and wasn't fetched in the last hour: `{remote, fetched_at}` (an ISO time, or None if
    never fetched), so `next` and `status` can say that other machines' claims may be missing. None otherwise."""
    if not cl.in_git(pkg.root):
        return None
    remote = remote_of(pkg)
    if remote is None:
        return None
    store = git_store_dir(pkg.root)
    if store is None:
        return None
    fetch_head = store.parent / "FETCH_HEAD"
    try:
        mtime = fetch_head.stat().st_mtime
    except OSError:
        return {"remote": remote, "fetched_at": None}
    if time.time() - mtime < STALE_FETCH_SECONDS:
        return None
    return {"remote": remote, "fetched_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")}


def stale_note(stale: dict | None) -> str | None:
    if not stale:
        return None
    when = f"last fetched {stale['fetched_at'][:16].replace('T', ' ')} UTC" if stale["fetched_at"] else "never fetched"
    return f"note: {stale['remote']} {when}; claims made on other machines may be missing (use --fetch)"


def view(pkg: Package, base: str | None = None) -> View:
    """Read the backlog for readiness. On the integration branch, or outside git, that is the working tree. On any
    other branch it is the integration branch's copy, so a dependency counts as done only once it is merged.

    On the integration branch when it is only behind its remote copy (another machine landed work, and this one
    fetched but didn't pull), it is the remote copy too, with `behind` set: what landed there is done, and nothing
    is offered or claimed again from the stale copy."""
    items = load_backlog(pkg)
    if not cl.in_git(pkg.root):
        return View(items, "working tree")
    name = base or integration_branch(pkg)
    base_ref = cl.resolve_base(pkg.root, name, remote_of(pkg) or "origin")
    branch = cl.current_branch(pkg.root)
    source = "working tree"
    behind = 0
    if base_ref is not None and branch == name and base_ref != name:
        behind = cl.ahead(pkg.root, base_ref, name)
    if base_ref is not None and (branch not in (name, base_ref) or behind):
        text = cl.read_at(pkg.root, base_ref, pkg.path_of(BACKLOG))
        if text is not None:
            items, source = parse_backlog(text), base_ref
    claims = cl.active_claims(cl.list_claims(pkg.root, closed_ids(items), {i.id for i in items}))
    return View(items, source, base_ref, branch, claims, behind)


def closed_ids(items: list[BacklogItem]) -> set[str]:
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


def next_payload(pkg: Package, *, base: str | None = None, parallel: int | None = None,
                 skip: set[str] | frozenset[str] = frozenset()) -> dict:
    """The next item that can start now, every ready item, and why the rest are waiting. Claimed items are never
    offered, and neither is an item that might overlap running work, so `its start` never refuses what `its next`
    offers. `skip` counts as claimed too: items another machine just won."""
    v = view(pkg, base)
    items = v.items
    check_unique(items)
    claimed = set(v.claimed) | set(skip)
    ready = ready_items(items, claimed)
    waiting = [
        {"id": i.id, "title": i.title, "blocked_by": blocked_by(i, items)}
        for i in items if i.status == "todo" and i.id not in claimed and i not in ready
    ]
    startable, _ = _parallel(v, ready, len(ready) or 1)
    first = startable[0] if startable else None
    payload = {
        "schema": SCHEMA,
        "ok": True,
        "source": v.source,
        "behind": v.behind or None,
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
    wip = wip_warning(pkg, v)
    payload["warnings"] = [wip] if wip else []
    return payload


def waiting_on_lead(pkg: Package, v: View) -> list[str]:
    """The claimed items whose work is finished and waits for the lead to review or land it: done on the claim
    branch, or an adopted claim with a candidate that has commits."""
    if v.base is None:
        return []
    out = []
    for c in v.claims:
        if c.finished or c.orphaned:
            continue
        if c.candidates:
            if any(cl.ref_exists(pkg.root, x["ref"]) and cl.ahead(pkg.root, x["ref"], v.base) for x in c.candidates):
                out.append(c.id)
            continue
        ref = cl.claim_ref(c)
        if not cl.ahead(pkg.root, ref, v.base):
            continue
        text = cl.read_at(pkg.root, ref, pkg.path_of(BACKLOG))
        item = next((i for i in parse_backlog(text) if i.id == c.id), None) if text else None
        if item is not None and item.status == "done":
            out.append(c.id)
    return out


def wip_warning(pkg: Package, v: View) -> dict | None:
    """`gate.max_unreviewed` on the integration branch: a warning once that many finished items wait for the lead,
    since agents can finish work faster than a person can review and land it. Nothing is refused."""
    if v.base is None:
        return None
    limit = gate.Policy.of(gate.manifest_at(pkg, v.base) or {}, v.base).max_unreviewed
    waiting = waiting_on_lead(pkg, v) if limit else []
    if not limit or len(waiting) < limit:
        return None
    return {"kind": "wip_limit", "limit": limit, "items": waiting,
            "message": f"{len(waiting)} finished item(s) wait for you to review or land ({', '.join(waiting)}), and "
                       f"gate.max_unreviewed is {limit}: land or review them before starting more (its status)"}


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


def base_ref(pkg: Package) -> str | None:
    """The integration branch's ref for comparing against (its remote-tracking ref when local is behind)."""
    if not cl.in_git(pkg.root):
        return None
    return cl.resolve_base(pkg.root, integration_branch(pkg), remote_of(pkg) or "origin")


def item_here(pkg: Package, items: list[BacklogItem]) -> str | None:
    """The item this checkout is working on: the claim branch's or worktree's, else the only in-progress item."""
    if cl.in_git(pkg.root):
        ident = cl.item_of_branch(cl.current_branch(pkg.root)) or cl.claim_of_worktree(pkg.root)
        if ident:
            return ident
    active = [i.id for i in items if i.status == "in-progress"]
    return active[0] if len(active) == 1 else None


def on_base_checkout(pkg: Package) -> bool:
    """Whether this is the lead's checkout of the integration branch: on that branch, in the repository's main
    worktree (not a linked one, where agents work), and not a claim's."""
    if not cl.in_git(pkg.root) or cl.current_branch(pkg.root) != integration_branch(pkg):
        return False
    if cl.claim_of_worktree(pkg.root):
        return False
    git_dir = gitx.out(pkg.root, "rev-parse", "--absolute-git-dir", check=False)
    common = git_common_dir(pkg.root)
    return bool(git_dir) and common is not None and Path(git_dir).resolve() == common.resolve()


def off_integration_branch(pkg: Package) -> bool:
    """Whether work here only counts once merged: a git checkout that isn't on the integration branch."""
    if not cl.in_git(pkg.root):
        return False
    return cl.current_branch(pkg.root) != integration_branch(pkg)
