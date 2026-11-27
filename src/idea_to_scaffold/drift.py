"""`its drift`: done items whose code changed after it was verified, and the requirements that are affected.

When `its done` (or `its verify`) passes, it writes the item's `Verified` field. The commit that brought that value
into the checked-out branch, found by walking its first-parent history, is the item's verified commit: its tree is
the code the checks ran on (for a landed item, the merge `its land` tested). Drift is any difference between that
tree and the working tree in the item's Touches. An item done before `Verified` existed uses the commit where it
became done.

This reads git history, so a shallow clone (CI's default) can't tell: fetch with full history (`fetch-depth: 0`).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import claims as cl
from .backlog import BACKLOG, BacklogItem, load_backlog, parse_backlog
from .gate import _planning_prefix
from .package import Package
from .work import SCHEMA, WorkError

COMMIT_LIMIT = 10


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return proc.stdout if proc.returncode == 0 else ""


def _key(item: BacklogItem | None) -> tuple | None:
    if item is None or item.status != "done":
        return None
    return ("done", item.get("Verified"))


def pathspecs(touches: list[str]) -> list[str]:
    out = []
    for entry in touches:
        entry = cl._norm(entry)
        out.append(f":(glob){entry}" if cl.GLOB_CHARS.search(entry) else entry.rstrip("/") or ".")
    return out


class History:
    """The backlog at each first-parent commit that changed it, newest first, parsed once."""

    def __init__(self, pkg: Package):
        self.pkg = pkg
        backlog = pkg.path_of(BACKLOG)
        rel = backlog.resolve().relative_to(pkg.root.resolve()).as_posix()
        self.commits = _git(pkg.root, "log", "--first-parent", "--format=%H", "--", rel).split()
        self._items: dict[str, dict[str, BacklogItem]] = {}

    def items_at(self, commit: str) -> dict[str, BacklogItem]:
        if commit not in self._items:
            text = cl.read_at(self.pkg.root, commit, self.pkg.path_of(BACKLOG))
            self._items[commit] = {i.id: i for i in parse_backlog(text)} if text else {}
        return self._items[commit]

    def verified_commit(self, item: BacklogItem) -> str | None:
        """The oldest commit of the newest run of commits whose copy of the item matches the working tree's: the
        commit that recorded its current verification. None if that isn't committed (or history is missing)."""
        current = _key(item)
        found = None
        for commit in self.commits:
            if _key(self.items_at(commit).get(item.id)) == current:
                found = commit
            else:
                break
        return found


def drift(pkg: Package) -> dict:
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "drift is read from git history; this isn't a git repository")
    items = load_backlog(pkg)
    history = History(pkg)
    planning = _planning_prefix(pkg)
    drifted, unknown, no_touches, clean = [], [], [], []
    for item in items:
        if item.status != "done":
            continue
        if not item.touches:
            no_touches.append(item.id)
            continue
        commit = history.verified_commit(item)
        if commit is None:
            if history.commits:
                unknown.append({"id": item.id, "reason": "uncommitted",
                                "message": "its verification isn't committed yet"})
            else:
                unknown.append({"id": item.id, "reason": "no_history",
                                "message": "no git history for the backlog (a shallow clone?)"})
            continue
        specs = pathspecs(item.touches)
        files = [f for f in _git(pkg.root, "diff", "--name-only", "--relative", commit, "--", *specs).split("\n")
                 if f and not (planning and f.startswith(planning))]
        files += [f for f in _git(pkg.root, "ls-files", "--others", "--exclude-standard", "--", *specs).split("\n")
                  if f and f not in files]
        if not files:
            clean.append(item.id)
            continue
        log = _git(pkg.root, "log", f"--max-count={COMMIT_LIMIT}", "--format=%h %s", f"{commit}..HEAD", "--", *specs)
        drifted.append({
            "id": item.id, "title": item.title, "satisfies": item.satisfies, "verified": item.get("Verified") or None,
            "verified_commit": commit, "files": sorted(files),
            "commits": [line for line in log.split("\n") if line],
            "uncommitted": bool(_git(pkg.root, "status", "--porcelain", "--", *specs).strip()),
        })
    requirements: dict[str, dict] = {}
    for entry in drifted:
        for req in entry["satisfies"]:
            r = requirements.setdefault(req, {"id": req, "items": [], "files": []})
            r["items"].append(entry["id"])
            r["files"] = sorted(set(r["files"]) | set(entry["files"]))
    return {
        "schema": SCHEMA, "ok": True, "drifted": drifted,
        "requirements": sorted(requirements.values(), key=lambda r: r["id"]),
        "clean": clean, "unknown": unknown, "no_touches": no_touches,
    }
