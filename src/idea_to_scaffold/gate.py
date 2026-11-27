"""The done gate's integrity checks: what a branch changed, measured against what its item allows.

`its done` runs the item's Verify command, but an agent can edit the files that command reads. So before any check
runs, `inspect` compares the branch with the integration branch:

- **The rules come from the integration branch.** The item's Verify and Touches, and the package's test and lint
  commands and `test_paths`, are read from there, never from the branch being checked. A branch that edits
  `package.yaml` is flagged (`policy_changed`).
- **The plan is diffed item by item.** A branch may change its own item's Status, Verified, Blocked and Override
  fields and add proposals. Changing its Verify or Touches, any other field, any other item, or other planning
  docs is flagged.
- **Test tampering is flagged** (see `tamper.py`). Deleted test files, test files that lose assertions, new skips,
  assertions that can't fail, tests that do nothing, changes to test collection (`conftest.py`, pytest or Jest
  settings) and changes to the held-out acceptance tests. So are changes to agent configuration (`AGENTS.md`,
  `CLAUDE.md`, `.claude/`, `.codex/`, CI workflows), whatever the item's Touches say.
- **New checks warn first.** The checks in DEFAULT_MODES are reported with `mode: warn` and refuse nothing until
  the integration branch's package.yaml sets `gate: {checks: {<kind>: enforce}}`.
- **The diff is checked against Touches.** Files changed outside the item's Touches are flagged, including files
  outside the package in a monorepo.

The diff runs from the fork point to the working tree, so uncommitted and untracked files count too, or to a given
ref (what `its land` checks before merging). On a claim branch the fork point is the merge base with the integration
branch. On the integration branch itself it is the commit recorded when the item was started, so work committed
there is still checked.

The gate fails closed: if git can't answer (no integration branch, a missing commit, a shallow clone), it raises
`GateError` instead of passing.

Findings fall in two groups, so each can be waived on its own: test and plan changes, and scope changes.
"""
from __future__ import annotations

import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import claims as cl
from . import tamper
from .backlog import BACKLOG, BacklogItem, parse_backlog
from .ids import META_FILES, line_fields
from .package import Package
from .touches import covered

TEST_KINDS = ("test_deleted", "assertions_removed", "skip_added", "verify_changed", "policy_changed",
              "item_changed", "other_item_changed", "agent_config_changed", "assertions_weakened", "test_disabled",
              "test_config_changed", "acceptance_changed", "tests_pass_on_parent")
SCOPE_KINDS = ("outside_touches", "touches_changed", "no_changes")
OWN_FIELDS = {"status", "verified", "blocked", "override"}  # what a branch may change on its own item
MODES = ("enforce", "warn", "off")
# Checks added in phase 7 start in warn mode: they are reported but don't refuse the item until a person sets
# `gate: {checks: {<kind>: enforce}}` in package.yaml. Every other check enforces, and none can be turned off
# except these.
DEFAULT_MODES = {"assertions_weakened": "warn", "test_disabled": "warn", "test_config_changed": "warn",
                 "tests_pass_on_parent": "warn"}
DEFAULT_ACCEPTANCE = "tests/acceptance/"

DEFAULT_TEST_PATHS = re.compile(
    r"(^|/)(tests?|__tests__)/"           # tests/, test/, __tests__/
    r"|(^|/)test_[^/]+$"                  # test_store.py
    r"|_test\.[^/]+$|_spec\.[^/]+$"       # store_test.go, store_spec.rb
    r"|\.(test|spec)\.[^/]+$"             # store.test.ts, store.spec.js
)
AGENT_CONFIG = re.compile(
    r"(^|/)(AGENTS|CLAUDE|GEMINI)\.md$"
    r"|(^|/)\.(claude|codex|cursor|gemini|its)/"
    r"|(^|/)\.mcp\.json$"
    r"|(^|/)\.github/workflows/"
)
class GateError(Exception):
    """The gate couldn't compare the branch with the integration branch, so it refuses instead of passing."""


@dataclass
class Change:
    status: str            # A, M, D, R (renamed) or C (copied)
    path: str              # package-relative (`../x` for a file outside the package), after the change
    old_path: str | None = None
    untracked: bool = False
    git_path: str = ""     # repository-relative, for asking git about it
    git_old_path: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "path": self.path, "old_path": self.old_path}


@dataclass
class Policy:
    """The package settings the gate trusts: the integration branch's copy of package.yaml."""
    commands: dict[str, str] = field(default_factory=dict)
    test_paths: list[str] | None = None
    source: str = "working tree"
    modes: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODES))
    acceptance: str = DEFAULT_ACCEPTANCE   # held-out tests: run by land, hidden from agents' worktrees

    @classmethod
    def of(cls, manifest: dict, source: str) -> Policy:
        cmds = manifest.get("commands") if isinstance(manifest, dict) else None
        cmds = {str(k): str(v) for k, v in cmds.items() if v} if isinstance(cmds, dict) else {}
        globs = manifest.get("test_paths") if isinstance(manifest, dict) else None
        globs = [str(g) for g in globs] if isinstance(globs, list) and globs else None
        gate = manifest.get("gate") if isinstance(manifest, dict) else None
        gate = gate if isinstance(gate, dict) else {}
        modes = dict(DEFAULT_MODES)
        checks = gate.get("checks")
        if isinstance(checks, dict):
            for kind, mode in checks.items():
                if kind in DEFAULT_MODES and str(mode) in MODES:  # only the phase 7 checks can be relaxed
                    modes[str(kind)] = str(mode)
        acceptance = gate.get("acceptance_path")
        acceptance = str(acceptance).strip().strip("/") + "/" if acceptance else DEFAULT_ACCEPTANCE
        return cls(cmds, globs, source, modes, acceptance)

    def mode(self, kind: str) -> str:
        return self.modes.get(kind, "enforce")


@dataclass
class Inspection:
    base: str | None = None            # the integration branch ref compared against
    merge_base: str | None = None      # the fork point
    head: str | None = None            # the ref inspected, or None for the working tree
    changes: list[Change] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    verify_command: str | None = None  # the Verify command to run: always the integration branch's
    verify_source: str = "working tree"
    touches: list[str] = field(default_factory=list)
    touches_checked: bool = False
    policy: Policy = field(default_factory=Policy)
    planned: BacklogItem | None = None  # the item as the integration branch has it

    def blocking(self, *, allow_test_changes: bool = False, allow_outside_touches: bool = False) -> list[dict]:
        allowed = (TEST_KINDS if allow_test_changes else ()) + (SCOPE_KINDS if allow_outside_touches else ())
        return [f for f in self.findings if f["kind"] not in allowed and f.get("mode") != "warn"]

    @property
    def warnings(self) -> list[dict]:
        return [f for f in self.findings if f.get("mode") == "warn"]

    def add(self, finding: dict) -> None:
        """Record a finding in the mode the integration branch's policy sets for its kind (`off` drops it)."""
        mode = self.policy.mode(finding["kind"])
        if mode != "off":
            self.findings.append({**finding, "mode": mode})

    def to_dict(self) -> dict:
        return {"base": self.base, "merge_base": self.merge_base, "verify_source": self.verify_source,
                "touches_checked": self.touches_checked, "findings": self.findings,
                "changed_files": [c.to_dict() for c in self.changes]}


# --- git, failing closed ---------------------------------------------------------------------------------------

def run_git(cwd: Path, *args: str, check: bool = True, input_text: str | None = None) -> str:
    """Run git with unquoted UTF-8 paths. With `check`, a failure raises GateError instead of reading as "no
    changes"."""
    proc = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", input=input_text)
    if check and proc.returncode != 0:
        raise GateError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip() or proc.returncode}")
    return proc.stdout if proc.returncode == 0 else ""


def _nul(text: str) -> list[str]:
    return [p for p in text.split("\0") if p]


def is_test_path(pkg: Package, path: str, globs: list[str] | None = None) -> bool:
    """Whether a path is a test file: the `test_paths` globs, else common conventions. The gate passes the
    integration branch's globs (`[]` for none); `None` reads the working tree's package.yaml."""
    if globs is None:
        configured = pkg.get("test_paths")
        globs = [str(g) for g in configured] if isinstance(configured, list) and configured else None
    if globs:
        return covered(path, globs)
    return bool(DEFAULT_TEST_PATHS.search(path))


def in_touches(path: str, touches: list[str]) -> bool:
    return covered(path, touches)


def _prefix(pkg: Package) -> str:
    """The package root's path inside the repository, with a trailing slash ("" at the top)."""
    return run_git(pkg.root, "rev-parse", "--show-prefix").strip()


def _local(prefix: str, git_path: str) -> str:
    if not prefix or git_path.startswith(prefix):
        return git_path[len(prefix):]
    return posixpath.relpath(git_path, prefix.rstrip("/"))


def changed_files(pkg: Package, merge_base: str, head: str | None) -> list[Change]:
    """Every file changed in the whole repository since `merge_base`, not just inside the package."""
    top = cl.toplevel(pkg.root)
    prefix = _prefix(pkg)
    tokens = _nul(run_git(top, "diff", "-z", "--name-status", "-M", "--no-color", merge_base, *([head] if head else [])))
    out: list[Change] = []
    i = 0
    while i < len(tokens):
        code = tokens[i][:1]
        if code in "RC" and i + 2 < len(tokens):
            old, new = tokens[i + 1], tokens[i + 2]
            out.append(Change(code, _local(prefix, new), _local(prefix, old), git_path=new, git_old_path=old))
            i += 3
        else:
            path = tokens[i + 1] if i + 1 < len(tokens) else ""
            out.append(Change(code, _local(prefix, path), git_path=path))
            i += 2
    if head is None:
        for path in _nul(run_git(top, "ls-files", "-z", "--others", "--exclude-standard")):
            out.append(Change("A", _local(prefix, path), untracked=True, git_path=path))
    return out


def _before(pkg: Package, merge_base: str, change: Change) -> str | None:
    """A changed file's content at the fork point, or None if it is new."""
    if change.status == "A" and change.git_old_path is None:
        return None
    path = change.git_old_path or change.git_path
    proc = subprocess.run(["git", "show", f"{merge_base}:{path}"], cwd=cl.toplevel(pkg.root), capture_output=True)
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def after_content(pkg: Package, head: str | None, change: Change) -> str | None:
    """A changed file's content on the branch (the working tree, or `head`), or None if it was deleted."""
    if change.status == "D":
        return None
    top = cl.toplevel(pkg.root)
    if head is None:
        try:
            return (top / change.git_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    proc = subprocess.run(["git", "show", f"{head}:{change.git_path}"], cwd=top, capture_output=True)
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


# --- the plan --------------------------------------------------------------------------------------------------

def _items(text: str | None) -> dict[str, BacklogItem]:
    return {i.id: i for i in parse_backlog(text)} if text else {}


def _item_body(text: str, item: BacklogItem, drop: set[str] = frozenset()) -> str:
    """An item's lines, without the lines holding any of the `drop` fields, for comparing two copies."""
    lines = text.split("\n")[item.heading_line:item.end_line]
    kept = [ln.rstrip() for ln in lines if not {f.name.lower() for f in line_fields(ln)} & drop]
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept)


def _planning_prefix(pkg: Package) -> str:
    try:
        return pkg.planning_dir.resolve().relative_to(pkg.root.resolve()).as_posix().rstrip("/") + "/"
    except ValueError:
        return ""


def manifest_at(pkg: Package, ref: str) -> dict | None:
    text = cl.read_at(pkg.root, ref, pkg.manifest_path)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _plan_findings(pkg: Package, ident: str, texts: dict[str, str | None], result: Inspection) -> None:
    """Compare the backlog the branch has with the one it forked from, item by item."""
    mine_text, fork_text, plan_text = texts["mine"], texts["fork"], texts["planned"]
    mine, forked, planned = _items(mine_text), _items(fork_text), _items(plan_text)
    rel = pkg.rel(pkg.path_of(BACKLOG))
    me, was, plan = mine.get(ident), forked.get(ident), planned.get(ident)
    if me is not None and was is not None:
        if me.verify != was.verify and (plan is None or me.verify != plan.verify):
            result.add({
                "kind": "verify_changed", "file": rel,
                "message": f"the branch changed {ident}'s Verify from {(plan or was).verify or '(none)'!r} to "
                           f"{me.verify or '(none)'!r}; change acceptance tests on {result.base} first",
                "planned": (plan or was).verify, "branch": me.verify})
        if me.touches != was.touches and (plan is None or me.touches != plan.touches):
            result.add({
                "kind": "touches_changed", "file": rel,
                "message": f"the branch changed {ident}'s Touches; change the plan on {result.base} first",
                "planned": (plan or was).touches, "branch": me.touches})
        drop = OWN_FIELDS | {"verify", "touches"}
        if _item_body(mine_text or "", me, drop) != _item_body(fork_text or "", was, drop) and (
                plan is None or _item_body(mine_text or "", me, drop) != _item_body(plan_text or "", plan, drop)):
            result.add({
                "kind": "item_changed", "file": rel,
                "message": f"the branch changed {ident}'s plan (only its Status, Verified and Blocked fields are its "
                           f"own); change the plan on {result.base} first"})
    changed = []
    for other in sorted(set(mine) | set(forked)):
        if other == ident:
            continue
        a, b = mine.get(other), forked.get(other)
        body_a = _item_body(mine_text or "", a) if a else None
        body_b = _item_body(fork_text or "", b) if b else None
        if body_a == body_b:
            continue
        c = planned.get(other)
        if c is not None and body_a == _item_body(plan_text or "", c):
            continue  # the integration branch made the same change: merged in, not made here
        changed.append(other)
    if changed:
        result.add({
            "kind": "other_item_changed", "file": rel, "items": changed,
            "message": f"the branch changed other backlog items ({', '.join(changed)}); only {ident} is its own"})


# --- the gate --------------------------------------------------------------------------------------------------

def inspect(pkg: Package, ident: str, base_ref: str | None, *, head: str | None = None,
            fork: str | None = None) -> Inspection:
    """Compare the branch (the working tree, or `head`) with `base_ref`, for the item `ident`. `fork` is the fork
    point when it isn't the merge base: the commit an item started from on the integration branch itself."""
    root = pkg.root
    backlog = pkg.path_of(BACKLOG)
    if head is None:
        branch_text = backlog.read_text(encoding="utf-8") if backlog.is_file() else None
    else:
        branch_text = cl.read_at(root, head, backlog)
    mine = _items(branch_text).get(ident)
    result = Inspection(base=base_ref, head=head, policy=Policy.of(pkg.manifest, "working tree"))
    if mine is not None:
        result.verify_command, result.touches = mine.verify_command, mine.touches
    if not cl.in_git(root):
        return result
    if base_ref is None:
        raise GateError("there is no integration branch to compare with; set integration_branch in package.yaml "
                        "to the branch work lands on")
    if run_git(root, "rev-parse", "--is-shallow-repository", check=False).strip() == "true":
        raise GateError("this is a shallow clone, so the fork point can't be trusted; fetch the full history "
                        "(git fetch --unshallow)")
    if fork is not None:
        merge_base = run_git(root, "rev-parse", "--verify", f"{fork}^{{commit}}").strip()
        policy_ref = merge_base
    else:
        merge_base = run_git(root, "merge-base", base_ref, head or "HEAD").strip()
        policy_ref = base_ref
    if not merge_base:
        raise GateError(f"the branch has no common history with {base_ref}")
    result.merge_base = merge_base
    manifest = manifest_at(pkg, policy_ref)
    if manifest is not None:
        result.policy = Policy.of(manifest, policy_ref if fork is None else "the item's start")
    plan_text = cl.read_at(root, policy_ref, backlog)
    fork_text = cl.read_at(root, merge_base, backlog)
    planned = _items(plan_text).get(ident)
    result.planned = planned
    if planned is not None:
        result.verify_command, result.touches = planned.verify_command, planned.touches
        result.verify_source = base_ref if fork is None else f"{base_ref} when {ident} started"
    _plan_findings(pkg, ident, {"mine": branch_text, "fork": fork_text, "planned": plan_text}, result)

    result.changes = changed_files(pkg, merge_base, head)
    planning = _planning_prefix(pkg)
    globs = result.policy.test_paths or []  # [] means the default conventions, never the branch's own setting
    manifest_rel = posixpath.relpath(pkg.manifest_path.resolve().as_posix(), pkg.root.resolve().as_posix())
    generated = {pkg.rel(pkg.path_of(m)) for m in META_FILES} | {"planning/backlog.json"}
    proposals = pkg.rel(pkg.proposals_dir).rstrip("/") + "/"
    outside: set[str] = set()
    config: set[str] = set()
    acceptance = result.policy.acceptance
    for change in result.changes:
        path = change.path
        if path == manifest_rel:
            result.add({"kind": "policy_changed", "file": path,
                                    "message": f"the branch changed {path}, which sets the gate's own rules; "
                                               f"change it on {base_ref} first"})
            continue
        if AGENT_CONFIG.search(change.git_path) or (change.git_old_path and AGENT_CONFIG.search(change.git_old_path)):
            config.add(path)
        if planning and path.startswith(planning):
            logical = "planning/" + path[len(planning):]
            if logical == pkg.rel(backlog) or logical in generated or logical.startswith(proposals):
                continue  # the backlog is diffed item by item above; proposals and generated files are free
            if not in_touches(path, result.touches):
                outside.add(path)
            continue
        was_test = change.old_path is not None and is_test_path(pkg, change.old_path, globs)
        if change.status == "D" and is_test_path(pkg, path, globs):
            result.add({"kind": "test_deleted", "file": path, "message": f"test file {path} was deleted"})
            continue
        if was_test and not is_test_path(pkg, path, globs):
            result.add({"kind": "test_deleted", "file": change.old_path,
                                    "message": f"test file {change.old_path} was moved out of the tests (to {path})"})
        if covered(path, [acceptance]) or (change.old_path and covered(change.old_path, [acceptance])):
            result.add({"kind": "acceptance_changed", "file": path,
                        "message": f"the branch changed {path}, a held-out acceptance test; only a person changes "
                                   "those, on the integration branch"})
            continue
        if tamper.is_test_config(path):
            for finding in tamper.config_findings(path, _before(pkg, merge_base, change),
                                                  after_content(pkg, head, change)):
                result.add(finding)
        if is_test_path(pkg, path, globs):
            for finding in tamper.compare(path, _before(pkg, merge_base, change), after_content(pkg, head, change)):
                result.add(finding)
        if result.touches and path != ".gitignore" and not in_touches(path, result.touches):
            outside.add(path)
        if change.old_path and result.touches and not in_touches(change.old_path, result.touches):
            outside.add(change.old_path)
    if pkg.repo_layout and not any(not (planning and c.path.startswith(planning)) for c in result.changes):
        result.add({
            "kind": "no_changes",
            "message": f"the branch changes no files outside the planning folder since {merge_base[:10]}, so "
                       f"nothing shows {ident} was implemented"})
    if config:
        files = sorted(config)
        result.add({"kind": "agent_config_changed", "files": files,
                                "message": f"the branch changed agent configuration ({', '.join(files)}), which "
                                           "steers every later agent; change it on the integration branch first"})
    if result.touches:
        result.touches_checked = True
        if outside:
            files = sorted(outside)
            result.add({
                "kind": "outside_touches", "files": files,
                "message": f"changed outside {ident}'s Touches ({', '.join(result.touches)}): {', '.join(files)}"})
    return result


def diff_text(pkg: Package, inspection: Inspection, limit: int,
              exclude: frozenset[str] | set[str] = frozenset()) -> tuple[str, bool]:
    """The diff the inspection covered, cut at `limit` characters. Untracked files are shown as new files.
    `exclude` names repository-relative paths to leave out."""
    if inspection.merge_base is None:
        return "", False
    top = cl.toplevel(pkg.root)
    skip = [f":(exclude,literal){p}" for p in sorted(exclude)]
    text = run_git(top, "diff", "-M", "--no-color", inspection.merge_base,
                   *([inspection.head] if inspection.head else []), *(["--", ".", *skip] if skip else []))
    if inspection.head is None:
        for change in inspection.changes:
            if not change.untracked or change.git_path in exclude:
                continue
            try:
                body = (top / change.git_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            path = change.git_path
            text += f"diff --git a/{path} b/{path}\nnew file (untracked)\n--- /dev/null\n+++ b/{path}\n"
            text += "".join(f"+{x}\n" for x in body.splitlines())
    if len(text) > limit:
        return text[:limit], True
    return text, False
