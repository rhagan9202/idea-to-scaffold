"""IDs, their owning documents, and Markdown scanning helpers shared by the validator and the linker."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Backlog items have a permanent ID (`T-2.4`) or, while proposed on a branch, a placeholder (`T-new-plant-aliases`)
# that is renumbered when it is merged, so agents on parallel branches never mint the same ID.
PLACEHOLDER_PATTERN = r"T-new-[a-z0-9]+(?:-[a-z0-9]+)*"
TASK_PATTERN = rf"{PLACEHOLDER_PATTERN}|T-\d+\.\d+"
ID_PATTERN = (
    rf"NFR-\d{{3,}}|FR-\d{{3,}}|ADR-\d{{4}}|F-\d{{2,}}|G-\d+|{TASK_PATTERN}|M\d+|"
    r"S-[a-z0-9]+(?:-[a-z0-9]+)*|C-[a-z0-9]+(?:-[a-z0-9]+)*"
)
ID_RE = re.compile(rf"(?<![\w/#-])({ID_PATTERN})(?![\w-])")
PLACEHOLDER_RE = re.compile(PLACEHOLDER_PATTERN)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# Which logical files may define each kind of ID. A trailing "/" means "any file in this folder".
OWNERS: dict[str, tuple[str, ...]] = {
    "G": ("planning/01-vision.md",),
    "F": ("planning/02-features.md",),
    "FR": ("planning/03-requirements.md",),
    "NFR": ("planning/03-requirements.md",),
    "C": ("planning/04-architecture.md",),
    "S": ("planning/wireframes/", "planning/05-design.md"),
    "M": ("planning/06-project-plan.md",),
    "T": ("planning/07-backlog.md",),
    "ADR": ("planning/adr/",),
}

KIND_LABELS = {
    "G": "Goal", "F": "Feature", "FR": "Functional requirement", "NFR": "Non-functional requirement",
    "C": "Component", "S": "Screen", "M": "Milestone", "T": "Backlog item", "ADR": "Decision",
}
KIND_EXAMPLES = {
    "G": "G-1", "F": "F-01", "FR": "FR-001", "NFR": "NFR-001", "C": "C-api", "S": "S-home",
    "M": "M1", "T": "T-1.1", "ADR": "ADR-0001",
}

# Kinds whose format can't be confused with ordinary prose. Other kinds ("M1 Mac", "C-suite", "F-16",
# "S-expression", "G-7") only count as references when they are defined somewhere, or when they appear in a
# traceability field such as `**Supports:**`, where nothing but IDs is expected.
STRICT_KINDS = {"FR", "NFR", "ADR", "T"}
TRACE_FIELDS = (
    "Supports", "Feature", "Requirements", "Screens", "Serves", "Satisfies", "Depends on", "Milestone",
)
TRACE_FIELD_RE = re.compile(rf"\*\*(?:{'|'.join(re.escape(f) for f in TRACE_FIELDS)}):\*\*(.*)")
TABLE_FIRST_CELL_RE = re.compile(r"^\s*\|\s*(?:\[([^\]]+)\]\([^)]*\)|([^|]+?))\s*\|")
BOLD_LEAD_RE = re.compile(r"^\s*(?:[-*+]\s+)?\*\*\[?([^*\]]+)\]?(?:\([^)]*\))?\*\*")

# Generated / meta files that are not scanned for IDs.
META_FILES = {"planning/README.md", "planning/wireframes/README.md"}


def is_placeholder(identifier: str) -> bool:
    return PLACEHOLDER_RE.fullmatch(identifier) is not None


def id_kind(identifier: str) -> str:
    for prefix in ("NFR-", "FR-", "ADR-", "F-", "G-", "T-", "S-", "C-"):
        if identifier.startswith(prefix):
            return prefix.rstrip("-")
    return "M"


def definition_on_line(line: Line) -> str | None:
    """The ID this line *defines*, if any: a heading that starts with the ID, a table row whose first cell is the
    ID, or a list item / paragraph that starts with the ID in bold (`- **G-1** — ...`)."""
    if line.in_code:
        return None
    if line.heading is not None:
        m = ID_RE.match(strip_strike(strip_links(line.heading)).strip())
        return m.group(1) if m else None
    for regex in (TABLE_FIRST_CELL_RE, BOLD_LEAD_RE):
        m = regex.match(line.text)
        if m:
            cell = strip_strike(next(g for g in m.groups() if g is not None)).strip()
            if ID_RE.fullmatch(cell):
                return cell
    return None


def trace_field_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of traceability-field values on a line (where every ID-shaped token is an ID)."""
    return [(m.start(1), m.end(1)) for m in TRACE_FIELD_RE.finditer(text)]


def owned_by(kind: str, rel: str) -> bool:
    return any(rel == o or (o.endswith("/") and rel.startswith(o)) for o in OWNERS[kind])


def github_slug(heading: str) -> str:
    """Anchor GitHub (and VS Code / Cursor previews) generate for a heading."""
    text = re.sub(r"<[^>]+>", "", heading)  # drop inline HTML
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> text
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


@dataclass
class Line:
    number: int
    text: str
    in_code: bool       # inside a fenced block (or the fence line itself)
    heading: str | None  # heading text if this line is a heading
    section: str | None  # text of the nearest heading above (or this heading)


def scan_lines(body: str) -> list[Line]:
    out: list[Line] = []
    fence: str | None = None
    section: str | None = None
    for i, text in enumerate(body.split("\n")):
        m = FENCE_RE.match(text)
        if fence:
            out.append(Line(i, text, True, None, section))
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(Line(i, text, True, None, section))
            continue
        h = HEADING_RE.match(text)
        heading = h.group(2) if h else None
        if heading is not None:
            section = heading
        out.append(Line(i, text, False, heading, section))
    return out


def sections(body: str) -> list[tuple[str, str]]:
    """(heading text, section body) pairs; a section ends at the next heading of equal or higher level."""
    lines = scan_lines(body)
    heads = [(ln, len(HEADING_RE.match(ln.text).group(1))) for ln in lines if ln.heading is not None]
    raw = body.split("\n")
    out = []
    for idx, (ln, level) in enumerate(heads):
        end = len(raw)
        for nxt, nlevel in heads[idx + 1:]:
            if nlevel <= level:
                end = nxt.number
                break
        out.append((ln.heading or "", "\n".join(raw[ln.number + 1:end])))
    return out


FIELD_RE = re.compile(r"\*\*([A-Za-z][A-Za-z ]*?)(?::\*\*|\*\*:)")
TRAILING_SEPARATOR_RE = re.compile(r"\s+·\s*$")


@dataclass
class FieldSpan:
    """A `**Name:** value` field found on one line. `start`/`end` are the column span of the raw value."""

    name: str
    raw: str
    line: int
    start: int
    end: int


def line_fields(text: str, number: int = 0) -> list[FieldSpan]:
    """Every `**Name:** value` field on one line (also `**Name**: value`). A value runs to the end of the line or
    to the next field (`**Milestone:** M1 · **Status:** todo`); it never continues onto the next line, so an
    empty field reads as empty instead of swallowing the line below it."""
    text = text.rstrip("\r")
    out: list[FieldSpan] = []
    matches = list(FIELD_RE.finditer(text))
    for i, m in enumerate(matches):
        limit = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = TRAILING_SEPARATOR_RE.sub("", text[m.end():limit])
        lead = len(segment) - len(segment.lstrip())
        start = m.end() + lead
        raw = segment.strip()
        out.append(FieldSpan(m.group(1).strip(), raw, number, start, start + len(raw)))
    return out


def field_value(section: str, name: str) -> str | None:
    """Value of a `**Name:** value` field inside a section (first match), or None if the field is absent."""
    for lineno, line in enumerate(section.split("\n")):
        for f in line_fields(line, lineno):
            if f.name.lower() == name.lower():
                return f.raw
    return None


def strip_strike(text: str) -> str:
    """`~~F-07 Bulk import~~` -> `F-07 Bulk import`: retired items are struck through but still defined."""
    return text.replace("~~", "")


def strip_links(text: str) -> str:
    """`[FR-001](x.md#y)` -> `FR-001`, so values can be read whether or not they are linked."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
