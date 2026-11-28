"""The backlog (`planning/07-backlog.md`): parsing items, readiness, dependency cycles, and status writes.

Items are parsed with the position of every field, so a status change rewrites only that field's value and leaves
links, formatting and everything else in the file untouched.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from .ids import (
    ID_RE,
    FieldSpan,
    id_kind,
    is_placeholder,
    line_fields,
    scan_lines,
    strip_links,
    strip_strike,
)
from .locking import LockTimeout, package_lock
from .package import BACKLOG_STATUSES, Package

BACKLOG = "planning/07-backlog.md"
ITEM_HEADING_RE = re.compile(r"^(T-new-[a-z0-9]+(?:-[a-z0-9]+)*|T-\d+\.\d+)(?![\w-])\s*(?:[—–:-]+\s*)?(.*)$")
TRAILING_COMMENT_RE = re.compile(r"\s+#\s.*$")
NO_VALUE = {"", "—", "-", "–", "none", "n/a"}
VERIFY_COMMAND_RE = re.compile(r"`([^`]+)`")


class BacklogError(Exception):
    """Raised when the backlog can't be changed as asked."""


@dataclass
class BacklogItem:
    id: str
    title: str
    level: int                     # heading level (3 for `### T-1.1`)
    heading_line: int              # 0-based line number of the heading in the file
    end_line: int                  # first line after the item's section
    fields: dict[str, FieldSpan] = field(default_factory=dict)  # lower-cased name -> first occurrence

    def get(self, name: str) -> str:
        """A field's value with links stripped ("" if absent)."""
        f = self.fields.get(name.lower())
        return strip_links(f.raw).strip() if f else ""

    @property
    def status(self) -> str:
        value = TRAILING_COMMENT_RE.sub("", self.get("Status")).strip("`* ").lower()
        return value

    @property
    def milestone(self) -> str:
        return TRAILING_COMMENT_RE.sub("", self.get("Milestone")).strip("` ")

    @property
    def satisfies(self) -> list[str]:
        return [m.group(1) for m in ID_RE.finditer(self.get("Satisfies"))]

    @property
    def depends_raw(self) -> str:
        return self.get("Depends on")

    @property
    def depends_on(self) -> list[str]:
        """Backlog items this item depends on (other ID kinds in the field are ignored; validate reports them)."""
        return [m.group(1) for m in ID_RE.finditer(self.depends_raw) if id_kind(m.group(1)) == "T"]

    @property
    def other_dependencies(self) -> list[str]:
        return [m.group(1) for m in ID_RE.finditer(self.depends_raw) if id_kind(m.group(1)) != "T"]

    @property
    def done_when(self) -> str:
        return self.get("Done when")

    @property
    def verify(self) -> str:
        """How to check the item is done: a command (in backticks), a test ID, or `manual: ...`."""
        return self.get("Verify")

    @property
    def verify_command(self) -> str | None:
        """The command `its done` runs: only when the whole Verify value is one backticked span, e.g.
        `` `uv run pytest tests/test_store.py` ``. A `manual:` note, or prose that merely mentions a command in
        backticks, is never run."""
        value = TRAILING_COMMENT_RE.sub("", self.verify).strip()
        if value.lower().startswith("manual"):
            return None
        m = VERIFY_COMMAND_RE.fullmatch(value)
        return m.group(1).strip() if m and m.group(1).strip() else None

    @property
    def touches(self) -> list[str]:
        """Paths or globs the item is expected to change, relative to the repository root."""
        value = self.get("Touches")
        if value.strip().lower() in NO_VALUE:
            return []
        return [p.strip().strip("`").strip() for p in split_outside_braces(value) if p.strip().strip("`").strip()]

    @property
    def placeholder(self) -> bool:
        return is_placeholder(self.id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "milestone": self.milestone,
            "satisfies": self.satisfies,
            "depends_on": self.depends_on,
            "done_when": self.done_when,
            "verify": self.verify,
            "touches": self.touches,
            "verified": self.get("Verified") or None,
            "placeholder": self.placeholder,
        }


def split_outside_braces(value: str) -> list[str]:
    """Split a Touches list on commas, keeping brace globs such as `src/{a,b}.py` whole."""
    parts, depth, current = [], 0, []
    for ch in value:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def parse_backlog(text: str) -> list[BacklogItem]:
    """Parse backlog Markdown (front matter included or not) into items, in file order."""
    lines = text.replace("\r\n", "\n").split("\n")
    scanned = scan_lines("\n".join(lines))
    heads = []
    for ln in scanned:
        if ln.heading is None:
            continue
        level = len(ln.text) - len(ln.text.lstrip("#"))
        m = ITEM_HEADING_RE.match(strip_strike(strip_links(ln.heading)).strip())
        heads.append((ln.number, level, m))
    items: list[BacklogItem] = []
    for idx, (number, level, m) in enumerate(heads):
        if not m:
            continue
        end = len(lines)
        for nxt_number, nxt_level, nxt_m in heads[idx + 1:]:
            # An item ends at the next heading of the same or higher level, or at the next item at any level, so a
            # missing field is never read from the item below it.
            if nxt_level <= level or nxt_m:
                end = nxt_number
                break
        item = BacklogItem(m.group(1), m.group(2).strip().strip("~").strip(), level, number, end)
        for ln in scanned[number + 1:end]:
            if ln.in_code or ln.heading is not None:
                continue
            for f in line_fields(ln.text, ln.number):
                item.fields.setdefault(f.name.lower(), f)
        items.append(item)
    return items


def load_backlog(pkg: Package) -> list[BacklogItem]:
    path = pkg.path_of(BACKLOG)
    if not path.is_file():
        return []
    return parse_backlog(_read(path))


def item_section(pkg: Package, item_id: str) -> str:
    """The Markdown of one item (heading and body), or "" if it isn't in the backlog."""
    path = pkg.path_of(BACKLOG)
    if not path.is_file():
        return ""
    text = _read(path).replace("\r\n", "\n")
    lines = text.split("\n")
    for item in parse_backlog(text):
        if item.id == item_id:
            return "\n".join(lines[item.heading_line:item.end_line]).strip()
    return ""


# --- readiness and dependencies ------------------------------------------------------------------------------

def ready_items(items: list[BacklogItem], claimed: set[str] | frozenset[str] = frozenset()) -> list[BacklogItem]:
    """Every `todo` item whose dependencies are all `done`, in backlog (priority) order, minus claimed items."""
    status = {i.id: i.status for i in items}
    return [
        i for i in items
        if i.status == "todo" and i.id not in claimed and all(status.get(d) == "done" for d in i.depends_on)
    ]


def next_ready(items: list[BacklogItem], claimed: set[str] | frozenset[str] = frozenset()) -> BacklogItem | None:
    ready = ready_items(items, claimed)
    return ready[0] if ready else None


def blocked_by(item: BacklogItem, items: list[BacklogItem]) -> list[str]:
    """Dependencies of `item` that are not done yet."""
    status = {i.id: i.status for i in items}
    return [d for d in item.depends_on if status.get(d) != "done"]


def find_cycles(items: list[BacklogItem]) -> list[list[str]]:
    """Dependency cycles, each as a list of IDs (`[A, B, A]`). A self-dependency is a cycle of one."""
    graph: dict[str, list[str]] = {}
    for i in items:
        graph.setdefault(i.id, []).extend(i.depends_on)
    cycles: list[list[str]] = []
    seen_cycles: set[frozenset[str]] = set()
    state: dict[str, int] = {}  # 1 = on the stack, 2 = finished
    # Iterative depth-first search, so a long dependency chain can't hit Python's recursion limit.
    for root in graph:
        if state.get(root) is not None:
            continue
        stack: list[str] = [root]
        todo: list[list[str]] = [list(graph[root])]
        state[root] = 1
        while stack:
            if not todo[-1]:
                state[stack.pop()] = 2
                todo.pop()
                continue
            dep = todo[-1].pop(0)
            if dep not in graph:
                continue
            if state.get(dep) == 1:
                cycle = stack[stack.index(dep):] + [dep]
                key = frozenset(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cycle)
            elif state.get(dep) is None:
                state[dep] = 1
                stack.append(dep)
                todo.append(list(graph[dep]))
    return cycles


def bad_touches(path: str) -> bool:
    """A Touches entry must be a relative path or glob inside the repository."""
    posix = PurePosixPath(path.replace("\\", "/"))
    return posix.is_absolute() or ".." in posix.parts or bool(PureWindowsPath(path).drive)


# --- writing -------------------------------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def set_field(pkg: Package, item_id: str, name: str, value: str, *, add: bool = False,
              path: Path | None = None, apart: bool = False) -> str:
    """Replace one field's value in place, keeping the rest of the file byte for byte. Returns the old value ("" if
    the field was added). With `add`, a missing field is added on a new line after the item's Status, or with
    `apart` at the end of the item (or its start), away from the fields a branch changes on its own item, so a field
    the lead adds on the integration branch merges cleanly with a branch's `its done`.

    The read, change and write happen under the package lock, so two commands changing different items at the same
    moment can't lose either change. `path` names another file holding the item (a proposal)."""
    path = path or pkg.path_of(BACKLOG)
    if not path.is_file():
        raise BacklogError(f"{pkg.rel(path) if path.is_relative_to(pkg.root) else path} not found")
    try:
        with package_lock(pkg.root):
            return _set_field(path, item_id, name, value, add, apart)
    except LockTimeout as exc:
        raise BacklogError(str(exc)) from None


def remove_field(pkg: Package, item_id: str, name: str, *, path: Path | None = None) -> str | None:
    """Delete a field's line if the line holds only that field. Returns the old value, or None if absent."""
    path = path or pkg.path_of(BACKLOG)
    try:
        with package_lock(pkg.root):
            text = _read(path)
            item = _one(text, item_id)
            span = item.fields.get(name.lower())
            if span is None:
                return None
            lines = text.split("\n")
            if len(line_fields(lines[span.line], span.line)) == 1:
                del lines[span.line]
            else:
                line = lines[span.line]
                lines[span.line] = line[: span.start] + line[span.end:]
            _atomic_write(path, "\n".join(lines))
            return span.raw
    except LockTimeout as exc:
        raise BacklogError(str(exc)) from None


def _one(text: str, item_id: str) -> BacklogItem:
    items = [i for i in parse_backlog(text) if i.id == item_id]
    if not items:
        raise BacklogError(f"{item_id} is not in the backlog")
    if len(items) > 1:
        raise BacklogError(f"{item_id} is defined more than once in the backlog; fix that first")
    return items[0]


RISKS = ("low", "normal", "high")  # an item's optional Risk; high needs an approving review to land


def risk_of(item: BacklogItem | None) -> str:
    """An item's Risk: low, normal (the default, and what an unknown value counts as) or high."""
    value = item.get("Risk").lower() if item is not None else ""
    return value if value in RISKS else "normal"


BRANCH_FIELDS = ("status", "verified", "blocked", "override")  # what `its done` and `its block` change


def _set_field(path: Path, item_id: str, name: str, value: str, add: bool = False, apart: bool = False) -> str:
    text = _read(path)
    item = _one(text, item_id)
    lines = text.split("\n")  # keeps any "\r" at line ends, so line endings are preserved
    span = item.fields.get(name.lower())
    if span is None:
        anchor = item.fields.get("status")
        if not add or anchor is None:
            raise BacklogError(f"{item_id} has no **{name}:** field")
        at = anchor.line + 1
        if apart:
            used = sorted({f.line for f in item.fields.values()})
            own = {item.fields[k].line for k in BRANCH_FIELDS if k in item.fields}
            at = used[-1] + 1 if used[-1] not in own or used[0] in own else used[0]
        neighbour = lines[anchor.line]
        prefix = neighbour[: neighbour.index("**")] if "**" in neighbour else "- "
        eol = "\r" if neighbour.endswith("\r") else ""
        lines.insert(at, f"{prefix}**{name}:** {value}{eol}")
        _atomic_write(path, "\n".join(lines))
        return ""
    line = lines[span.line]
    lines[span.line] = line[: span.start] + value + line[span.end:]
    _atomic_write(path, "\n".join(lines))
    return span.raw


def set_status(pkg: Package, item_id: str, status: str, *, path: Path | None = None) -> str:
    """Set an item's Status. Returns the previous status."""
    status = status.strip().lower()
    if status not in BACKLOG_STATUSES:
        raise BacklogError(f"status {status!r} must be one of {', '.join(BACKLOG_STATUSES)}")
    old = set_field(pkg, item_id, "Status", status, path=path)
    return TRAILING_COMMENT_RE.sub("", strip_links(old)).strip("`* ").lower()


# --- proposals -----------------------------------------------------------------------------------------------

PROPOSAL_ONLY_ITS = ("verified", "override", "waiver", "blocked")  # fields a proposal can't carry yet


def check_proposal(rel: str, stem: str, text: str) -> list[str]:
    """What is wrong with one proposal file, as messages: it must hold exactly one item, whose placeholder ID is the
    file's name, with status `proposed` and none of the fields `its` sets on scheduled work. A proposal skips the
    gate's item-by-item diff, so anything else would enter the backlog unchecked: an item already done, or one that
    takes a real item's ID."""
    items = parse_backlog(text)
    if len(items) != 1:
        return [f"{rel}: holds {len(items)} items; a proposal file holds exactly one"]
    item = items[0]
    out = []
    if not item.placeholder:
        out.append(f"{rel}: holds {item.id}, a permanent ID; a proposal has a placeholder ID (T-new-<name>)")
    elif item.id != stem:
        out.append(f"{rel}: holds {item.id}, but the file is named {stem}; a proposal's file is named after its ID")
    if item.status != "proposed":
        out.append(f"{rel}: {item.id} is {item.status or 'missing a status'}; a proposal is proposed until its promote "
                   "or its renumber schedules it")
    extra = [name.capitalize() for name in PROPOSAL_ONLY_ITS if name in item.fields]
    if extra:
        out.append(f"{rel}: {item.id} has {', '.join(extra)}, which only scheduled items carry")
    return out


def proposal_problems(pkg: Package) -> list[str]:
    """`check_proposal` for every proposal file."""
    out: list[str] = []
    for path in pkg.proposal_files():
        try:
            out += check_proposal(pkg.rel(path), path.stem, _read(path))
        except (OSError, UnicodeDecodeError) as exc:
            out.append(f"{pkg.rel(path)}: can't be read ({exc})")
    return out


def load_proposals(pkg: Package) -> list[tuple[Path, BacklogItem]]:
    """Proposed items waiting in `planning/proposals/`, one per file, in file-name order. A proposal lives in its own
    file so two branches that each propose items never edit the same lines; `its renumber` moves them into the
    backlog with permanent IDs."""
    out: list[tuple[Path, BacklogItem]] = []
    for path in pkg.proposal_files():
        for item in parse_backlog(_read(path)):
            out.append((path, item))
    return out
