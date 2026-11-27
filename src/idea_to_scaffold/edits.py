"""Changing the backlog from the CLI: `its propose`, `promote`, `block`, `unblock`, `drop` and `set-status`.

Agents and people change items through these commands instead of editing Markdown, so every change is one small,
locked, predictable edit. Each returns a dict in the versioned JSON contract (spec section 16).
"""
from __future__ import annotations

import re
from pathlib import Path

from .backlog import (
    NO_VALUE,
    BacklogError,
    BacklogItem,
    load_backlog,
    load_proposals,
    remove_field,
    set_field,
    set_status,
)
from .ids import is_placeholder
from .locking import LockTimeout, package_lock
from .package import BACKLOG_STATUSES, Package
from .work import SCHEMA, WorkError, _find, _refresh_mirror, item_payload, renumber, shown

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _slug(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    out = ""
    for word in words:
        if len(out) + len(word) + 1 > 40:
            break
        out = f"{out}-{word}" if out else word
    return out


def _clean(value: str | None) -> str:
    return (value or "").replace("\n", " ").strip()


def propose(
    pkg: Package,
    title: str,
    *,
    name: str | None = None,
    satisfies: str | None = None,
    depends_on: str | None = None,
    milestone: str | None = None,
    do: str | None = None,
    done_when: str | None = None,
    verify: str | None = None,
    touches: str | None = None,
) -> dict:
    """Write a new proposed item to `planning/proposals/T-new-<name>.md`.

    Each proposal is its own file, so branches that propose items at the same time never conflict when merged.
    `its renumber` (on the integration branch) gives it a permanent ID and moves it into the backlog."""
    title = _clean(title)
    if not title:
        raise WorkError("bad_title", "a proposal needs a title")
    name = (name or _slug(title)).lower()
    if not NAME_RE.fullmatch(name):
        raise WorkError("bad_name", f"{name!r} can't be a placeholder name: use lowercase letters, digits and hyphens",
                        details={"name": name})
    ident = f"T-new-{name}"
    path = pkg.proposals_dir / f"{ident}.md"
    taken = {i.id for i in load_backlog(pkg)} | {i.id for _, i in load_proposals(pkg)}
    if ident in taken or path.exists():
        raise WorkError("exists", f"{ident} is already proposed; pass --name to pick another name",
                        details={"id": ident})
    fields = [
        ("Milestone", _clean(milestone) or "—"),
        ("Status", "proposed"),
        ("Satisfies", _clean(satisfies) or "—"),
        ("Depends on", _clean(depends_on) or "—"),
        ("Do", _clean(do)),
        ("Done when", _clean(done_when)),
        ("Verify", _clean(verify)),
        ("Touches", _clean(touches)),
    ]
    body = f"### {ident} — {title}\n" + "".join(f"- **{k}:** {v}".rstrip() + "\n" for k, v in fields)
    try:
        with package_lock(pkg.root):
            if path.exists():
                raise WorkError("exists", f"{ident} is already proposed", details={"id": ident})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
    except LockTimeout as exc:
        raise WorkError("locked", str(exc)) from None
    item = next(i for p, i in load_proposals(pkg) if p == path)
    return {"schema": SCHEMA, "ok": True, "id": ident, "file": shown(pkg, pkg.rel(path)), "item": item.to_dict()}


def _locate(pkg: Package, ident: str) -> tuple[BacklogItem, Path | None, list[BacklogItem]]:
    """An item and the proposal file holding it (None when it is in the backlog)."""
    items = load_backlog(pkg)
    if any(i.id == ident for i in items):
        return _find(items, ident), None, items
    for path, item in load_proposals(pkg):
        if item.id == ident:
            return item, path, items
    raise WorkError("not_found", f"{ident} is not in the backlog or the proposals", details={"id": ident})


def _result(pkg: Package, ident: str, previous: str, status: str, **extra) -> dict:
    _refresh_mirror(pkg)
    items = load_backlog(pkg)
    item = next((i for i in items if i.id == ident), None)
    return {"schema": SCHEMA, "ok": True, "id": ident, "previous_status": previous, "status": status,
            "item": item_payload(pkg, item, items) if item else None, **extra}


def set_item_status(pkg: Package, ident: str, status: str, *, reason: str | None = None) -> dict:
    """Set any status by hand: the escape hatch behind promote, block and drop."""
    status = status.strip().lower()
    if status not in BACKLOG_STATUSES:
        raise WorkError("bad_status", f"status {status!r} must be one of {', '.join(BACKLOG_STATUSES)}",
                        details={"id": ident, "status": status})
    item, path, _ = _locate(pkg, ident)
    if is_placeholder(ident) and status not in ("proposed", "dropped"):
        raise WorkError("placeholder", f"{ident} has a placeholder ID; `its promote {ident}` gives it a permanent ID "
                        "and schedules it", details={"id": ident})
    try:
        previous = set_status(pkg, ident, status, path=path)
        if status == "blocked" and reason:
            set_field(pkg, ident, "Blocked", _clean(reason), add=True, path=path)
        elif status != "blocked":
            remove_field(pkg, ident, "Blocked", path=path)
        if status == "dropped" and reason:
            set_field(pkg, ident, "Dropped", _clean(reason), add=True, path=path)
    except BacklogError as exc:
        raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
    return _result(pkg, ident, previous, status, reason=_clean(reason) or None)


def block(pkg: Package, ident: str, reason: str) -> dict:
    """Mark an item blocked and say why. This is also how an agent stops honestly when it can't finish."""
    if not _clean(reason):
        raise WorkError("no_reason", "say why it is blocked: its block <ID> --reason '...'", details={"id": ident})
    item, _, _ = _locate(pkg, ident)
    if item.status in ("done", "dropped"):
        raise WorkError("bad_status", f"{ident} is {item.status}; it can't be blocked",
                        details={"id": ident, "status": item.status})
    return set_item_status(pkg, ident, "blocked", reason=reason)


def unblock(pkg: Package, ident: str) -> dict:
    item, _, _ = _locate(pkg, ident)
    if item.status != "blocked":
        raise WorkError("bad_status", f"{ident} is {item.status}, not blocked", details={"id": ident})
    return set_item_status(pkg, ident, "todo")


def drop(pkg: Package, ident: str, reason: str | None = None) -> dict:
    """Drop an item. A dropped proposal is simply deleted: it was never agreed."""
    item, path, _ = _locate(pkg, ident)
    if item.status == "done":
        raise WorkError("bad_status", f"{ident} is done; it can't be dropped", details={"id": ident})
    if path is not None:
        try:
            with package_lock(pkg.root):
                path.unlink()
        except LockTimeout as exc:
            raise WorkError("locked", str(exc)) from None
        return {"schema": SCHEMA, "ok": True, "id": ident, "previous_status": item.status, "status": "dropped",
                "item": None, "deleted_file": shown(pkg, pkg.rel(path)), "reason": _clean(reason) or None}
    return set_item_status(pkg, ident, "dropped", reason=reason)


def promote(pkg: Package, ident: str, *, force: bool = False, allow_incomplete: bool = False) -> dict:
    """Schedule a proposed item: set it `todo`. A placeholder is renumbered first (on the integration branch).

    An item with no `Done when` or no `Touches` is refused unless `allow_incomplete`: an agent would have nothing to
    finish against, and an item without Touches can't run alongside anything."""
    item, _, _ = _locate(pkg, ident)
    if item.status != "proposed":
        raise WorkError("bad_status", f"{ident} is {item.status}, not proposed", details={"id": ident})
    missing = [name for name, empty in (("Done when", item.done_when.strip().lower() in NO_VALUE),
                                        ("Touches", not item.touches)) if empty]
    if missing and not allow_incomplete:
        raise WorkError("incomplete", f"{ident} has no {' or '.join(missing)}; fill "
                        f"{'them' if len(missing) > 1 else 'it'} in before scheduling it, or pass --allow-incomplete",
                        details={"id": ident, "missing": missing})
    renumbered = None
    if item.placeholder:
        out = renumber(pkg, [ident], force=force)
        renumbered = out["renumbered"][0]["to"]
        ident = renumbered
    result = set_item_status(pkg, ident, "todo")
    result["renumbered_from"] = item.id if renumbered else None
    return result
