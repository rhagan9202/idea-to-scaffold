"""The done gate's integrity checks: what a branch changed, measured against what its item allows.

`its done` runs the item's Verify command, but an agent can edit the files that command reads. So before any check
runs, `inspect` compares the branch with the integration branch:

- **Verify and Touches come from the integration branch.** The item's acceptance test is the one that was planned,
  not one the branch rewrote. A branch that changes its own Verify or Touches is flagged.
- **Test tampering is flagged.** Deleted test files, test files that lose assertions, and new skip or xfail markers.
- **The diff is checked against Touches.** Files changed outside the item's Touches are flagged (the planning folder
  is exempt: status changes and proposals live there).

Findings fall in two groups, so each can be allowed on its own: test changes (`--allow-test-changes`) and scope
changes (`--allow-outside-touches`). The diff runs from the merge base to the working tree, so uncommitted and
untracked files count too, or to a given ref (what `its land` checks before merging).
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import claims as cl
from .backlog import BACKLOG, BacklogItem, parse_backlog
from .package import Package

TEST_KINDS = ("test_deleted", "assertions_removed", "skip_added", "verify_changed")
SCOPE_KINDS = ("outside_touches", "touches_changed")

DEFAULT_TEST_PATHS = re.compile(
    r"(^|/)(tests?|__tests__)/"           # tests/, test/, __tests__/
    r"|(^|/)test_[^/]+$"                  # test_store.py
    r"|_test\.[^/]+$|_spec\.[^/]+$"       # store_test.go, store_spec.rb
    r"|\.(test|spec)\.[^/]+$"             # store.test.ts, store.spec.js
)
SKIP_RE = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail|importorskip)\("
    r"|@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b|\.skipTest\("
    r"|\b(it|test|describe|context)\.(skip|todo)\(|\b(xit|xdescribe|xtest)\("
    r"|\bt\.Skip(Now|f)?\(|#\[ignore\]|@(Disabled|Ignore)\b"
)
ASSERT_RE = re.compile(
    r"\bassert\w*\b|\bexpect\(|\.should\b|\bt\.(Error|Errorf|Fatal|Fatalf|Fail|FailNow)\(|\brequire\.\w+\("
    r"|\bassert_\w+!?\(|\b(EXPECT|ASSERT)_\w+\("
)


@dataclass
class Change:
    status: str            # A, M, D, R (renamed) or C (copied)
    path: str              # repo-relative, after the change
    old_path: str | None = None
    untracked: bool = False

    def to_dict(self) -> dict:
        return {"status": self.status, "path": self.path, "old_path": self.old_path}


@dataclass
class Inspection:
    base: str | None = None            # the integration branch ref compared against
    merge_base: str | None = None
    head: str | None = None            # the ref inspected, or None for the working tree
    changes: list[Change] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    verify_command: str | None = None  # the Verify command to run: the integration branch's, unless allowed
    verify_source: str = "working tree"
    touches: list[str] = field(default_factory=list)
    touches_checked: bool = False

    def blocking(self, *, allow_test_changes: bool = False, allow_outside_touches: bool = False) -> list[dict]:
        allowed = (TEST_KINDS if allow_test_changes else ()) + (SCOPE_KINDS if allow_outside_touches else ())
        return [f for f in self.findings if f["kind"] not in allowed]

    def to_dict(self) -> dict:
        return {"base": self.base, "merge_base": self.merge_base, "verify_source": self.verify_source,
                "touches_checked": self.touches_checked, "findings": self.findings,
                "changed_files": [c.to_dict() for c in self.changes]}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def is_test_path(pkg: Package, path: str) -> bool:
    """Whether a repo-relative path is a test file: `test_paths` globs in package.yaml, else common conventions."""
    globs = pkg.get("test_paths")
    if isinstance(globs, list) and globs:
        return any(fnmatch.fnmatchcase(path, str(g)) for g in globs)
    return bool(DEFAULT_TEST_PATHS.search(path))


def in_touches(path: str, touches: list[str]) -> bool:
    for entry in touches:
        entry = cl._norm(entry)
        if cl.GLOB_CHARS.search(entry):
            if fnmatch.fnmatchcase(path, entry) or fnmatch.fnmatchcase(path, entry.rstrip("/") + "/*"):
                return True
        elif path == entry.rstrip("/") or path.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def changed_files(root: Path, merge_base: str, head: str | None) -> list[Change]:
    args = ["diff", "--name-status", "-M", "--relative", "--no-color", merge_base] + ([head] if head else [])
    out: list[Change] = []
    for line in _git(root, *args).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0][:1]
        if code in "RC" and len(parts) >= 3:
            out.append(Change(code, parts[2], parts[1]))
        else:
            out.append(Change(code, parts[-1]))
    if head is None:
        for path in _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines():
            if path.strip():
                out.append(Change("A", path.strip(), untracked=True))
    return out


def _added_removed(root: Path, merge_base: str, head: str | None, change: Change) -> tuple[list[str], list[str]]:
    """Added and removed lines of one changed file."""
    if change.untracked:
        try:  # untracked: every line is new
            return (root / change.path).read_text(encoding="utf-8", errors="replace").splitlines(), []
        except OSError:
            return [], []
    paths = [change.path] + ([change.old_path] if change.old_path else [])
    args = ["diff", "-U0", "-M", "--relative", "--no-color", merge_base] + ([head] if head else []) + ["--", *paths]
    added, removed = [], []
    for line in _git(root, *args).stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return added, removed


def _item(text: str | None, ident: str) -> BacklogItem | None:
    if text is None:
        return None
    return next((i for i in parse_backlog(text) if i.id == ident), None)


def _planning_prefix(pkg: Package) -> str:
    try:
        return pkg.planning_dir.resolve().relative_to(pkg.root.resolve()).as_posix().rstrip("/") + "/"
    except ValueError:
        return ""


def inspect(pkg: Package, ident: str, base_ref: str | None, *, head: str | None = None) -> Inspection:
    """Compare the branch (the working tree, or `head`) with `base_ref`, for the item `ident`."""
    root = pkg.root
    backlog = pkg.path_of(BACKLOG)
    if head is None:
        branch_text = backlog.read_text(encoding="utf-8") if backlog.is_file() else None
    else:
        branch_text = cl.read_at(root, head, backlog)
    mine = _item(branch_text, ident)
    result = Inspection(base=base_ref, head=head)
    if mine is not None:
        result.verify_command, result.touches = mine.verify_command, mine.touches
    if base_ref is None or not cl.in_git(root):
        return result
    merge_base = _git(root, "merge-base", base_ref, head or "HEAD").stdout.strip()
    if not merge_base:
        return result
    result.merge_base = merge_base
    planned = _item(cl.read_at(root, base_ref, backlog), ident)
    forked = _item(cl.read_at(root, merge_base, backlog), ident)  # what the branch started from
    if planned is not None:
        result.verify_command, result.touches, result.verify_source = planned.verify_command, planned.touches, base_ref
        if mine is not None and forked is not None and mine.verify != forked.verify and mine.verify != planned.verify:
            result.findings.append({
                "kind": "verify_changed", "file": pkg.rel(backlog),
                "message": f"the branch changed {ident}'s Verify from {planned.verify or '(none)'!r} to "
                           f"{mine.verify or '(none)'!r}; change acceptance tests on {base_ref} first",
                "planned": planned.verify, "branch": mine.verify})
        if mine is not None and forked is not None and mine.touches != forked.touches and mine.touches != planned.touches:
            result.findings.append({
                "kind": "touches_changed", "file": pkg.rel(backlog),
                "message": f"the branch changed {ident}'s Touches; change the plan on {base_ref} first",
                "planned": planned.touches, "branch": mine.touches})

    result.changes = changed_files(root, merge_base, head)
    planning = _planning_prefix(pkg)
    for change in result.changes:
        was_test = change.old_path is not None and is_test_path(pkg, change.old_path)
        if change.status == "D" and is_test_path(pkg, change.path):
            result.findings.append({"kind": "test_deleted", "file": change.path,
                                    "message": f"test file {change.path} was deleted"})
            continue
        if was_test and not is_test_path(pkg, change.path):
            result.findings.append({"kind": "test_deleted", "file": change.old_path,
                                    "message": f"test file {change.old_path} was moved out of the tests "
                                               f"(to {change.path})"})
        if is_test_path(pkg, change.path):
            added, removed = _added_removed(root, merge_base, head, change)
            lost = sum(bool(ASSERT_RE.search(x)) for x in removed) - sum(bool(ASSERT_RE.search(x)) for x in added)
            if lost > 0:
                result.findings.append({"kind": "assertions_removed", "file": change.path, "count": lost,
                                        "message": f"{change.path} has {lost} fewer assertion line(s)"})
            skips = [x.strip() for x in added if SKIP_RE.search(x)]
            skips_removed = sum(bool(SKIP_RE.search(x)) for x in removed)
            if len(skips) > skips_removed:
                result.findings.append({"kind": "skip_added", "file": change.path, "lines": skips[:5],
                                        "message": f"{change.path} adds a skip or xfail: {skips[0]}"})
    if result.touches:
        result.touches_checked = True
        outside = sorted({c.path for c in result.changes
                          if not (planning and c.path.startswith(planning)) and not in_touches(c.path, result.touches)
                          and c.path != ".gitignore"})
        if outside:
            result.findings.append({
                "kind": "outside_touches", "files": outside,
                "message": f"changed outside {ident}'s Touches ({', '.join(result.touches)}): {', '.join(outside)}"})
    return result


def diff_text(pkg: Package, inspection: Inspection, limit: int) -> tuple[str, bool]:
    """The diff the inspection covered, cut at `limit` characters. Untracked files are listed, not shown."""
    if inspection.merge_base is None:
        return "", False
    args = ["diff", "-M", "--relative", "--no-color", inspection.merge_base]
    if inspection.head:
        args.append(inspection.head)
    text = _git(pkg.root, *args).stdout
    if inspection.head is None:
        untracked = [p for p in _git(pkg.root, "ls-files", "--others", "--exclude-standard").stdout.splitlines() if p]
        for path in untracked:
            try:
                body = (pkg.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = body.splitlines()
            text += f"diff --git a/{path} b/{path}\nnew file (untracked)\n--- /dev/null\n+++ b/{path}\n"
            text += "".join(f"+{x}\n" for x in lines)
    if len(text) > limit:
        return text[:limit], True
    return text, False
