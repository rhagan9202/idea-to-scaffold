"""The `Verified` mark: the date, the base commit and a hash of the item's Touches files, from disk or from a
commit."""
from __future__ import annotations

import hashlib
from datetime import date

from . import claims as cl
from . import gate
from .backlog import (
    set_field,
)
from .package import Package
from .touches import covered

SKIPPED_MARK = "skipped (--no-verify)"


def mark_verified(pkg: Package, ident: str, inspection: gate.Inspection, *, skipped: bool = False) -> str:
    """Write the item's `Verified` field: the date its checks passed, the integration-branch commit the work was
    built on, and a hash of the files in its Touches as they were checked. `its drift` compares that hash with the
    files now, so it needs no history, and re-verifying changed code always writes a new value. When the checks were
    skipped, it says that instead, so nothing reads the item as checked."""
    if skipped:
        set_field(pkg, ident, "Verified", SKIPPED_MARK, add=True)
        return SKIPPED_MARK
    base = inspection.merge_base
    value = date.today().isoformat() + (f", base {base[:10]}" if base else "")
    tree = touches_hash(pkg, inspection.touches)
    if tree:
        value += f", tree {tree}"
    set_field(pkg, ident, "Verified", value, add=True)
    return value


def touched_files(pkg: Package, touches: list[str]) -> list[str]:
    """The files (tracked or untracked, outside the planning folder) that an item's Touches cover, package-relative."""
    if not touches or not cl.in_git(pkg.root):
        return []
    out = gate.run_git(pkg.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", check=False)
    planning = gate._planning_prefix(pkg)
    files = {p for p in out.split("\0") if p and not (planning and p.startswith(planning))}
    return sorted(p for p in files if covered(p, touches) and (pkg.root / p).is_file())


def touches_hash(pkg: Package, touches: list[str]) -> str | None:
    """A short hash of the content of every file an item's Touches cover, as they are on disk now."""
    files = touched_files(pkg, touches)
    if not touches or not cl.in_git(pkg.root):
        return None
    blobs = gate.run_git(pkg.root, "hash-object", "--stdin-paths", input_text="\n".join(files) + "\n").split() \
        if files else []
    digest = hashlib.sha1()
    for path, blob in zip(files, blobs, strict=False):
        digest.update(f"{path}\0{blob}\n".encode())
    return digest.hexdigest()[:12]


def touches_hash_at(pkg: Package, touches: list[str], ref: str) -> str | None:
    """`touches_hash` of the files as committed at `ref` rather than on disk: the same digest when they match, since
    both use git's blob ids. Land marks a landed item with it, from the merged commit."""
    if not touches or not cl.in_git(pkg.root):
        return None
    out = gate.run_git(pkg.root, "ls-tree", "-r", "-z", ref, check=False)
    planning = gate._planning_prefix(pkg)
    blobs = {}
    for entry in out.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and not (planning and path.startswith(planning)):
            blobs[path] = parts[2]
    digest = hashlib.sha1()
    for path in sorted(p for p in blobs if covered(p, touches)):
        digest.update(f"{path}\0{blobs[path]}\n".encode())
    return digest.hexdigest()[:12]
