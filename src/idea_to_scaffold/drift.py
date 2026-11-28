"""`its drift`: done items whose code changed after it was verified, and the requirements that are affected.

When `its done` (or `its verify`) passes, it writes the item's `Verified` field. The commit that brought that value
into the checked-out branch, found by walking its first-parent history, is the item's verified commit: its tree is
the code the checks ran on (for a landed item, the merge `its land` tested). Drift is any difference between that
tree and the working tree in the item's Touches. An item done before `Verified` existed uses the commit where it
became done.

Since phase 5, `Verified` also records a hash of the files in the item's Touches as they were checked
(`tree <hash>`). When that hash still matches the files on disk the item is clean, with no history needed. When it
doesn't, the history walk above says which files and commits changed. An item verified before the hash existed
needs the history, so in a shallow clone it is reported as unknown, never as clean.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import claims as cl
from .backlog import BACKLOG, BacklogItem, load_backlog, parse_backlog
from .gate import _planning_prefix
from .package import Package
from .touches import covered
from .work import SCHEMA, WorkError, touched_files, touches_hash

COMMIT_LIMIT = 10
TREE_RE = re.compile(r"\btree ([0-9a-f]{6,40})\b")


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return proc.stdout if proc.returncode == 0 else ""


def _key(item: BacklogItem | None) -> tuple | None:
    if item is None or item.status != "done":
        return None
    return ("done", item.get("Verified"))


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
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=pkg.root, capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout.strip() == "true"
    drifted, unknown, no_touches, clean = [], [], [], []
    for item in items:
        if item.status != "done":
            continue
        if not item.touches:
            no_touches.append(item.id)
            continue
        recorded = TREE_RE.search(item.get("Verified") or "")
        if recorded and touches_hash(pkg, item.touches) == recorded.group(1)[:12]:
            clean.append(item.id)
            continue
        commit = history.verified_commit(item)
        if shallow and not recorded:
            unknown.append({"id": item.id, "reason": "shallow",
                            "message": "this is a shallow clone and its Verified mark has no file hash; fetch the "
                                       "full history (fetch-depth: 0) or re-verify it"})
            continue
        if recorded and commit is not None and shallow:
            commit = None  # the history can't be trusted to say which files: the hash already says they changed
        if commit is None and recorded:
            drifted.append({
                "id": item.id, "title": item.title, "satisfies": item.satisfies,
                "verified": item.get("Verified") or None, "verified_commit": None,
                "files": touched_files(pkg, item.touches), "commits": [],
                "uncommitted": True, "message": "its files changed since it was verified (the history to say "
                                                "which is missing)"})
            continue
        if commit is None:
            if history.commits:
                unknown.append({"id": item.id, "reason": "uncommitted",
                                "message": "its verification isn't committed yet"})
            else:
                unknown.append({"id": item.id, "reason": "no_history",
                                "message": "no git history for the backlog (a shallow clone?)"})
            continue
        changed = _git(pkg.root, "-c", "core.quotePath=false", "diff", "-z", "--name-only", "--relative", commit)
        untracked = _git(pkg.root, "-c", "core.quotePath=false", "ls-files", "-z", "--others", "--exclude-standard")
        files = sorted({f for f in changed.split("\0") + untracked.split("\0")
                        if f and not (planning and f.startswith(planning)) and covered(f, item.touches)})
        if not files and not recorded:
            clean.append(item.id)
            continue
        if not files:  # the hash differs but the history shows no change since: the verified commit is older
            files = touched_files(pkg, item.touches)
        log = _git(pkg.root, "log", f"--max-count={COMMIT_LIMIT}", "--format=%h %s", f"{commit}..HEAD", "--",
                   *[f":(literal){f}" for f in files])
        status = _git(pkg.root, "status", "--porcelain", "--", *[f":(literal){f}" for f in files])
        drifted.append({
            "id": item.id, "title": item.title, "satisfies": item.satisfies, "verified": item.get("Verified") or None,
            "verified_commit": commit, "files": sorted(files),
            "commits": [line for line in log.split("\n") if line],
            "uncommitted": bool(status.strip()),
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
        "clean": clean, "unknown": unknown, "no_touches": no_touches, "shallow": shallow,
    }
