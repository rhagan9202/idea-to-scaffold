"""IDs, their owning documents, and Markdown scanning helpers shared by the validator and the linker."""
from __future__ import annotations

import re
from dataclasses import dataclass

ID_PATTERN = (
    r"NFR-\d{3,}|FR-\d{3,}|ADR-\d{4}|F-\d{2,}|G-\d+|T-\d+\.\d+|M\d+|"
    r"S-[a-z0-9]+(?:-[a-z0-9]+)*|C-[a-z0-9]+(?:-[a-z0-9]+)*"
)
ID_RE = re.compile(rf"(?<![\w/#-])({ID_PATTERN})(?![\w-])")
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

# Generated / meta files that are not scanned for IDs.
META_FILES = {"planning/README.md", "planning/wireframes/README.md"}


def id_kind(identifier: str) -> str:
    for prefix in ("NFR-", "FR-", "ADR-", "F-", "G-", "T-", "S-", "C-"):
        if identifier.startswith(prefix):
            return prefix.rstrip("-")
    return "M"


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


def field_value(section: str, name: str) -> str | None:
    """Value of a `**Name:** value` field inside a section (first match)."""
    m = re.search(rf"\*\*{re.escape(name)}:\*\*\s*(.+)", section)
    if not m:
        return None
    value = m.group(1)
    # Stop at an inline separator like " · **Other:**"
    value = re.split(r"\s+·\s+\*\*", value)[0]
    return value.strip()


def strip_links(text: str) -> str:
    """`[FR-001](x.md#y)` -> `FR-001`, so values can be read whether or not they are linked."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
