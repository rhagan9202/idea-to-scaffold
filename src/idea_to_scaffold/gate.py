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
- **Tests that vanish are flagged.** A test that was on the fork point and is gone from the branch, deleted or
  renamed so the runner skips it, is `test_removed`, unless it turns up in another test file the branch changed.
- **Some checks warn first.** The checks in DEFAULT_MODES are reported with `mode: warn` and refuse nothing until
  the integration branch's package.yaml sets `gate: {checks: {<kind>: enforce}}`. A new scaffold enforces them all.
- **The diff is checked against Touches.** Files changed outside the item's Touches are flagged, including files
  outside the package in a monorepo.

The diff runs from the fork point to the working tree, so uncommitted and untracked files count too, or to a given
ref (what `its land` checks before merging). On a claim branch the fork point is the merge base with the integration
branch. On the integration branch itself it is the commit recorded when the item was started, so work committed
there is still checked.

The gate fails closed: if git can't answer (no integration branch, a missing commit, a shallow clone), it raises
`GateError` instead of passing.

Findings fall in groups (`WAIVER_GROUPS`) so that each can be waived on its own: test and plan changes, scope
changes, and dependency changes. Only the lead can waive: `its waive` writes a `Waiver` field on the item on the
integration branch, and the gate honours only the integration branch's copy (`Inspection.waiver`). A branch that
writes a Waiver on its own item changes the item (`item_changed`).
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import claims as cl
from . import deps, gitx, tamper
from .backlog import BACKLOG, BRANCH_FIELDS, BacklogItem, check_proposal, parse_backlog
from .checks import Settings
from .ids import META_FILES, line_fields
from .package import Package
from .touches import covered

TEST_KINDS = ("test_deleted", "assertions_removed", "skip_added", "verify_changed", "policy_changed",
              "item_changed", "other_item_changed", "agent_config_changed", "assertions_weakened", "test_disabled",
              "test_config_changed", "acceptance_changed", "tests_pass_on_parent", "test_removed",
              "no_test_report", "failures_hidden", "no_tests_ran", "tests_skipped", "proposal_invalid")
SCOPE_KINDS = ("outside_touches", "touches_changed", "no_changes")
DEPENDENCY_KINDS = ("dependency_added",)
# What `its waive --<group>` waives.
WAIVER_GROUPS = {"test-changes": TEST_KINDS, "outside-touches": SCOPE_KINDS, "dependencies": DEPENDENCY_KINDS}
WAIVER_FIELD = "Waiver"
REFUSALS = ("test_changes", "dependency_changes", "outside_touches")  # the error codes a gate refusal has


def refusal(blocking: list[dict]) -> str:
    """The error code for refused findings, by the group of the worst: tests first, then dependencies, then scope."""
    found = {f["kind"] for f in blocking}
    if found & set(TEST_KINDS):
        return "test_changes"
    return "dependency_changes" if found & set(DEPENDENCY_KINDS) else "outside_touches"
OWN_FIELDS = set(BRANCH_FIELDS)  # what a branch may change on its own item
MODES = ("enforce", "warn", "off")
# Checks that can misfire on honest work start in warn mode: they are reported but don't refuse the item until a
# person sets `gate: {checks: {<kind>: enforce}}` in package.yaml (a new scaffold sets that for all of them). Every
# other check enforces. Only the RELAXABLE checks can be set to warn or off; the rest always enforce.
DEFAULT_MODES = {"assertions_weakened": "warn", "test_disabled": "warn", "test_config_changed": "warn",
                 "tests_pass_on_parent": "warn", "no_test_report": "warn", "tests_skipped": "warn",
                 "dependency_added": "warn"}
RELAXABLE = set(DEFAULT_MODES) | {"test_removed", "failures_hidden", "no_tests_ran"}
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
    hidden: bool = False   # untracked and ignored by git, but a test runner reads it (see `hidden_test_config`)
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
    run: Settings = field(default_factory=Settings)  # how checks run: gate.env and gate.timeout

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
                if kind in RELAXABLE and str(mode) in MODES:
                    modes[str(kind)] = str(mode)
        acceptance = gate.get("acceptance_path")
        acceptance = str(acceptance).strip().strip("/") + "/" if acceptance else DEFAULT_ACCEPTANCE
        return cls(cmds, globs, source, modes, acceptance, Settings.of(gate))

    def mode(self, kind: str) -> str:
        return self.modes.get(kind, "enforce")


@dataclass(frozen=True)
class Waiver:
    """A waiver the lead recorded with `its waive`: `- **Waiver:** 2026-11-28; test-changes, dependencies; why`."""
    date: str
    groups: tuple[str, ...]
    reason: str

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(kind for group in self.groups for kind in WAIVER_GROUPS.get(group, ()))

    def render(self) -> str:
        return f"{self.date}; {', '.join(self.groups)}; {self.reason}"

    @classmethod
    def parse(cls, text: str | None) -> Waiver | None:
        parts = [p.strip() for p in (text or "").split(";", 2)]
        if len(parts) != 3 or not parts[2]:
            return None
        groups = tuple(g for g in re.split(r"[\s,]+", parts[1]) if g in WAIVER_GROUPS)
        return cls(parts[0], groups, parts[2]) if groups else None

    def to_dict(self) -> dict:
        return {"date": self.date, "groups": list(self.groups), "reason": self.reason}


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
    waiver_item: BacklogItem | None = None  # the item in the last waive commit, on the plain flow

    @property
    def waiver(self) -> Waiver | None:
        """The lead's waiver for the item: only ever read from the integration branch's copy of it (on the plain
        flow, from the last commit `its waive` made there)."""
        item = self.waiver_item or self.planned
        return Waiver.parse(item.get(WAIVER_FIELD)) if item else None

    def blocking(self) -> list[dict]:
        """The findings that refuse the item: every enforced finding the integration branch's waiver doesn't cover."""
        waived = self.waiver.kinds if self.waiver else frozenset()
        return [f for f in self.findings if f["kind"] not in waived and f.get("mode") != "warn"]

    @property
    def waived(self) -> list[dict]:
        """The enforced findings the waiver lets through."""
        waived = self.waiver.kinds if self.waiver else frozenset()
        return [f for f in self.findings if f["kind"] in waived and f.get("mode") != "warn"]

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
    proc = gitx.run(cwd, *args, input=input_text)
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
        for path in hidden_test_config(top):
            out.append(Change("A", _local(prefix, path), untracked=True, hidden=True, git_path=path))
    return out


# Test configuration a runner reads whether or not git ignores it, by name (`tamper.TEST_CONFIG_FILES`).
HIDDEN_CONFIG_NAMES = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "setup.py", "pyproject.toml",
                       "sitecustomize.py", "usercustomize.py", "*.pth", "package.json", ".mocharc*", "jest.config.*",
                       "vitest.config.*", "playwright.config.*", "cypress.config.*", "karma.conf.*")
# Folders that hold installed packages or tool caches, where such files are expected.
INSTALL_DIRS = {"venv", "env", "site-packages", "dist-packages", "node_modules", "__pycache__"}


def hidden_test_config(top: Path) -> list[str]:
    """Untracked test configuration that git ignores, so the ordinary untracked listing misses it: a `conftest.py`
    hidden through `.git/info/exclude` (which an agent's worktree can write), a global excludes file, or a
    `.gitignore`. The test runner reads it all the same. Files in dot-folders and package folders (`.venv`,
    `node_modules`, `site-packages`, ...) are left out."""
    specs = [f":(glob)**/{name}" for name in HIDDEN_CONFIG_NAMES]
    listed = _nul(run_git(top, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", *specs,
                          check=False))
    return [p for p in listed if tamper.is_test_config(p)
            and not any(part.startswith(".") or part in INSTALL_DIRS for part in p.split("/")[:-1])]


def _before(pkg: Package, merge_base: str, change: Change) -> str | None:
    """A changed file's content at the fork point, or None if it is new."""
    if change.status == "A" and change.git_old_path is None:
        return None
    path = change.git_old_path or change.git_path
    data = gitx.show(cl.toplevel(pkg.root), f"{merge_base}:{path}")
    return data.decode("utf-8", "replace") if data is not None else None


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
    data = gitx.show(top, f"{head}:{change.git_path}")
    return data.decode("utf-8", "replace") if data is not None else None


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
    """Compare the backlog the branch has with the one it forked from, item by item. A change the integration
    branch made too is merged in, not made here. On the plain flow, a Waiver field that `its waive` committed since
    the start (`texts["waived"]`, that commit's backlog) isn't the branch's change either."""
    mine_text, fork_text, plan_text = texts["mine"], texts["fork"], texts["planned"]
    mine, forked, planned, waived = _items(mine_text), _items(fork_text), _items(plan_text), _items(texts.get("waived"))
    rel = pkg.rel(pkg.path_of(BACKLOG))
    me, was, plan = mine.get(ident), forked.get(ident), planned.get(ident)

    def leaves(item_id: str, item: BacklogItem | None) -> set[str]:
        """{"waiver"} when the branch's Waiver on the item is the one `its waive` wrote, so it is left out."""
        lead = waived.get(item_id)
        value = item.get(WAIVER_FIELD) if item else ""
        return {"waiver"} if lead is not None and value and value == lead.get(WAIVER_FIELD) else set()

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
        drop = OWN_FIELDS | {"verify", "touches"} | leaves(ident, me)
        body = _item_body(mine_text or "", me, drop)
        if body != _item_body(fork_text or "", was, drop) and (
                plan is None or body != _item_body(plan_text or "", plan, drop)):
            result.add({
                "kind": "item_changed", "file": rel,
                "message": f"the branch changed {ident}'s plan (only its Status, Verified and Blocked fields are its "
                           f"own); change the plan on {result.base} first"})
    changed = []
    for other in sorted(set(mine) | set(forked)):
        if other == ident:
            continue
        a, b = mine.get(other), forked.get(other)
        skip = leaves(other, a)
        body_a = _item_body(mine_text or "", a, skip) if a else None
        body_b = _item_body(fork_text or "", b, skip) if b else None
        if body_a == body_b:
            continue
        c = planned.get(other)
        if c is not None and body_a == _item_body(plan_text or "", c, skip):
            continue  # the integration branch made the same change: merged in, not made here
        changed.append(other)
    if changed:
        result.add({
            "kind": "other_item_changed", "file": rel, "items": changed,
            "message": f"the branch changed other backlog items ({', '.join(changed)}); only {ident} is its own"})


# --- the gate --------------------------------------------------------------------------------------------------

def inspect(pkg: Package, ident: str, base_ref: str | None, *, head: str | None = None,
            fork: str | None = None, waiver_ref: str | None = None) -> Inspection:
    """Compare the branch (the working tree, or `head`) with `base_ref`, for the item `ident`. `fork` is the fork
    point when it isn't the merge base: the commit an item started from on the integration branch itself. On that
    plain flow the rules come from the fork point, and the waiver from `waiver_ref`, the last waiver `its waive`
    committed since (`trust.last_waiver`)."""
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
    waived_text = cl.read_at(root, waiver_ref, backlog) if fork is not None and waiver_ref else None
    if waived_text is not None:
        result.waiver_item = _items(waived_text).get(ident)
    if planned is not None:
        result.verify_command, result.touches = planned.verify_command, planned.touches
        result.verify_source = base_ref if fork is None else f"{base_ref} when {ident} started"
    _plan_findings(pkg, ident, {"mine": branch_text, "fork": fork_text, "planned": plan_text,
                                "waived": waived_text}, result)

    result.changes = changed_files(pkg, merge_base, head)
    planning = _planning_prefix(pkg)
    globs = result.policy.test_paths or []  # [] means the default conventions, never the branch's own setting
    manifest_rel = posixpath.relpath(pkg.manifest_path.resolve().as_posix(), pkg.root.resolve().as_posix())
    generated = {pkg.rel(pkg.path_of(m)) for m in META_FILES} | {"planning/backlog.json"}
    proposals = pkg.rel(pkg.proposals_dir).rstrip("/") + "/"
    outside: set[str] = set()
    config: set[str] = set()
    acceptance = result.policy.acceptance
    removed: list[dict] = []   # tests gone from a file, kept until every changed test file has been read
    present: set[str] = set()  # the tests every changed test file has on the branch
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
            if logical.startswith(proposals) and change.status != "D":
                text = after_content(pkg, head, change) or ""
                for problem in check_proposal(path, posixpath.splitext(posixpath.basename(path))[0], text):
                    result.add({"kind": "proposal_invalid", "file": path, "message": problem})
            if logical == pkg.rel(backlog) or logical in generated or logical.startswith(proposals):
                continue  # the backlog is diffed item by item above; valid proposals and generated files are free
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
        if deps.kind_of(path) and change.status != "D":
            for finding in deps.findings(path, _before(pkg, merge_base, change), after_content(pkg, head, change)):
                result.add(finding)
        if change.hidden:
            result.add({"kind": "test_config_changed", "file": path, "hidden": True,
                        "message": f"{path} is test configuration that git ignores (through .git/info/exclude, a "
                                   "global excludes file or a .gitignore), so the diff doesn't show it, but the test "
                                   "runner still reads it"})
        if tamper.is_test_config(path):
            for finding in tamper.config_findings(path, _before(pkg, merge_base, change),
                                                  after_content(pkg, head, change)):
                result.add(finding)
        if is_test_path(pkg, path, globs):
            after = after_content(pkg, head, change)
            present |= tamper.test_names(after, path)
            for finding in tamper.compare(path, _before(pkg, merge_base, change), after):
                if finding["kind"] == "test_removed":
                    removed.append(finding)
                else:
                    result.add(finding)
        if result.touches and path != ".gitignore" and not in_touches(path, result.touches):
            outside.add(path)
        if change.old_path and result.touches and not in_touches(change.old_path, result.touches):
            outside.add(change.old_path)
    for finding in removed:  # a test moved to another file the branch changed hasn't gone
        gone = [name for name in finding["tests"] if name.rsplit("::", 1)[-1] not in present]
        if gone:
            result.add({**finding, "tests": gone,
                        "message": f"{finding['file']} no longer has {', '.join(gone[:5])}, and no other test file "
                                   "the branch changed has it, so it no longer runs (deleted, or renamed so the "
                                   "runner doesn't collect it)"})
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
