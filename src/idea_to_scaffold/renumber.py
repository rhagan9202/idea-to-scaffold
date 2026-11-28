"""`its renumber`: give proposed items with placeholder IDs (`T-new-...`) permanent ones, and rewrite every
mention of them."""
from __future__ import annotations

import re

from . import claims as cl
from .backlog import (
    BACKLOG,
    BacklogItem,
    load_backlog,
    load_proposals,
    proposal_problems,
    read_raw,
    write_raw,
)
from .contract import (
    SCHEMA,
    WorkError,
)
from .ids import META_FILES, is_placeholder
from .linker import link_package
from .locking import holding_lock
from .mirror import refresh_mirror
from .package import Package
from .view import integration_branch, shown

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
    with holding_lock(pkg):
        return _renumber(pkg, ids, dry_run=dry_run)


def _renumber(pkg: Package, ids: list[str] | None, *, dry_run: bool) -> dict:
    items = load_backlog(pkg)
    problems = proposal_problems(pkg)
    if problems:
        raise WorkError("bad_proposal", f"{problems[0]}" + (f" (and {len(problems) - 1} more)" if len(problems) > 1
                        else "") + "; fix the proposal files, then renumber", details={"problems": problems})
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
        appended = list(dict.fromkeys(path for path, item in proposals if item.id in renumbering))
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
        refresh_mirror(pkg)
    return {
        "schema": SCHEMA, "ok": True, "dry_run": dry_run,
        "renumbered": [{"from": old, "to": new} for old, new in pairs], "files": changed,
        "proposals_moved": moved,
    }
