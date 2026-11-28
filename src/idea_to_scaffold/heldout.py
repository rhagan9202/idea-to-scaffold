"""Held-out acceptance tests, one folder per item.

The lead writes an item's held-out tests in `<acceptance_path>/<ID>/` (`tests/acceptance/T-2.1/` by default).
Files elsewhere in the acceptance folder are shared: fixtures and helpers, or tests that hold for the whole project.

- An agent's worktree never has the acceptance folder (`work.hide_acceptance`), and the gate refuses a branch that
  changes it.
- `its land` checks each merged result with the held-out tests of every item that is done on it, the landing item
  included, plus the shared files, and without the folders of items that aren't done yet. Two items landing in
  parallel can't fail on each other's held-out tests, whose code isn't merged yet: the deadlock of a single shared
  folder (B2). The folders are left out with a sparse checkout, so this works for any test runner.
- Elsewhere (`its done` in a checkout that has the folder, such as the plain flow on the integration branch), a check
  that runs pytest is told to `--ignore` the folders of items that aren't done.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

from . import gitx
from .backlog import BacklogItem

ITEM_DIR_RE = re.compile(r"^T-\d+\.\d+$")
FAILED_ITEM_RE = re.compile(r"(?:^|/)(T-\d+\.\d+)/")


def item_folders(checkout: Path, folder: str, ref: str = "HEAD") -> list[str]:
    """The IDs that have a held-out folder at `ref`: subfolders of `folder` (repository-relative) named for an item."""
    proc = gitx.run(checkout, "ls-tree", "-d", "--name-only", "--full-tree", ref, "--", folder.rstrip("/") + "/")
    if proc.returncode != 0:
        return []
    names = [posixpath.basename(line.rstrip("/")) for line in proc.stdout.splitlines() if line.strip()]
    return sorted(n for n in names if ITEM_DIR_RE.match(n))


def done_ids(items: list[BacklogItem]) -> set[str]:
    return {i.id for i in items if i.status == "done"}


def show_for(worktree: Path, folder: str, done: set[str]) -> dict | None:
    """Check `worktree` out without the held-out folders of items that aren't in `done`. Returns which folders are
    in and which are out, or None when there is no acceptance folder. Raises gitx.GitError if git can't do it."""
    names = item_folders(worktree, folder)
    if not names and not gitx.out(worktree, "ls-tree", "-d", "--name-only", "--full-tree", "HEAD", "--",
                                  folder.rstrip("/"), check=False):
        return None
    hidden = [n for n in names if n not in done]
    if hidden:
        patterns = ["/*", *[f"!/{folder.rstrip('/')}/{n}/" for n in hidden]]
        proc = gitx.run(worktree, "sparse-checkout", "set", "--no-cone", *patterns)
    else:
        proc = gitx.run(worktree, "sparse-checkout", "disable")
    if proc.returncode != 0:
        raise gitx.GitError(f"couldn't leave held-out tests out of {worktree}: {proc.stderr.strip()}")
    listed = gitx.out(worktree, "ls-files", "-v", "--full-name", "--", f":(top){folder}", check=False)
    files = any(line[:1] not in ("S", "s") for line in listed.splitlines() if line.strip())  # S: left out
    return {"folder": folder, "included": [n for n in names if n in done], "left_out": hidden, "runs": files}


def ignored_folders(pkg_root: Path, acceptance: str, items: list[BacklogItem]) -> list[str]:
    """For a checkout that has the acceptance folder on disk: the package-relative held-out folders of items that
    aren't done, for pytest's `--ignore`."""
    base = pkg_root / acceptance
    if not base.is_dir():
        return []
    done = done_ids(items)
    return sorted(f"{acceptance.rstrip('/')}/{p.name}" for p in base.iterdir()
                  if p.is_dir() and ITEM_DIR_RE.match(p.name) and p.name not in done)


def failing_items(check: dict, folder: str) -> list[str]:
    """The items whose held-out tests a failed check's test report names."""
    tail = "/" + folder.strip("/") + "/"
    out = set()
    for test in (check.get("results") or {}).get("failed_tests", []):
        path = "/" + test.split("::", 1)[0]
        if tail in path or path.startswith(tail):
            match = FAILED_ITEM_RE.search(path[path.find(tail) + len(tail) - 1:])
            if match:
                out.add(match.group(1))
    return sorted(out)
