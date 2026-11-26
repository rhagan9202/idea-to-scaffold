"""Validate a planning package against spec section 8."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .package import (
    AGENTS_SECTIONS,
    READY_REQUIRES_APPROVED,
    REQUIRED_FILES,
    SLUG_RE,
    SUPPORTED_MAJOR,
    Package,
    parse_front_matter,
)

ID_RE = re.compile(
    r"(?<![\w-])("
    r"NFR-\d{3,}|FR-\d{3,}|ADR-\d{4}|F-\d{2,}|G-\d+|T-\d+\.\d+|M\d+|"
    r"S-[a-z0-9]+(?:-[a-z0-9]+)*|C-[a-z0-9]+(?:-[a-z0-9]+)*"
    r")(?![\w-])"
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def id_kind(identifier: str) -> str:
    for prefix in ("NFR-", "FR-", "ADR-", "F-", "G-", "T-", "S-", "C-"):
        if identifier.startswith(prefix):
            return prefix.rstrip("-")
    return "M"


# Which files may define each kind of ID.
OWNERS: dict[str, tuple[str, ...]] = {
    "G": ("planning/01-vision.md",),
    "F": ("planning/02-features.md",),
    "FR": ("planning/03-requirements.md",),
    "NFR": ("planning/03-requirements.md",),
    "C": ("planning/04-architecture.md",),
    "S": ("planning/05-design.md", "planning/wireframes/"),
    "M": ("planning/06-project-plan.md",),
    "T": ("planning/06-project-plan.md",),
    "ADR": ("planning/adr/",),
}

# Section-level traceability: (kind of the section heading, kinds it must reference, label)
TRACE_RULES = [
    ("F", ("G",), "feature must say which goal it supports (G-n)"),
    ("FR", ("F",), "functional requirement must name its feature (F-nn)"),
    ("T", ("FR", "NFR"), "task must list what it satisfies (FR-/NFR-)"),
]

SKIP_SCAN = {"planning/wireframes/README.md"}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ready: bool = False

    @property
    def valid(self) -> bool:
        return not self.errors


def _owned_by(kind: str, rel: str) -> bool:
    return any(rel == o or (o.endswith("/") and rel.startswith(o)) for o in OWNERS[kind])


def _sections(body: str) -> list[tuple[str, str]]:
    """Return (heading text, section body) pairs, each section ending at the next heading of equal or higher level."""
    heads = list(HEADING_RE.finditer(body))
    out = []
    for i, h in enumerate(heads):
        level = len(h.group(1))
        end = len(body)
        for nxt in heads[i + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        out.append((h.group(2).strip(), body[h.end():end]))
    return out


def validate(pkg: Package) -> Report:
    r = Report()

    # Manifest
    version = str(pkg.get("spec_version", ""))
    if not version:
        r.errors.append("package.yaml: spec_version is missing")
    else:
        try:
            major = int(version.split(".")[0])
            if major > SUPPORTED_MAJOR:
                r.errors.append(f"package.yaml: spec_version {version} is newer than this tool supports")
        except ValueError:
            r.errors.append(f"package.yaml: spec_version {version!r} is not MAJOR.MINOR")
    for key in ("project.name", "project.slug", "project.summary", "stack.language"):
        value = pkg.get(key)
        if not value or str(value).startswith("<"):
            r.errors.append(f"package.yaml: {key} is required")
    if pkg.slug and not pkg.slug.startswith("<") and not SLUG_RE.match(pkg.slug):
        r.errors.append(f"package.yaml: project.slug {pkg.slug!r} must be lowercase letters, digits and hyphens")

    # Required files and front matter
    for rel in REQUIRED_FILES:
        if not (pkg.root / rel).is_file():
            r.errors.append(f"{rel}: missing")
    docs = {pkg.rel(p): p.read_text(encoding="utf-8") for p in pkg.planning_files() + pkg.playbooks()}
    bodies: dict[str, str] = {}
    for rel, text in docs.items():
        fm, body = parse_front_matter(text)
        bodies[rel] = body
        if rel in SKIP_SCAN:
            continue
        missing = [k for k in ("id", "title", "status") if k not in fm]
        if missing:
            r.errors.append(f"{rel}: front matter missing {', '.join(missing)}")
        manifest_status = (pkg.get("artifacts") or {}).get(rel)
        if manifest_status and "status" in fm and str(manifest_status) != str(fm["status"]):
            r.warnings.append(
                f"{rel}: status is {fm['status']!r} in front matter but {manifest_status!r} in package.yaml"
            )

    # IDs: definitions vs references
    defined: set[str] = set()
    heading_defs: dict[str, list[str]] = {}
    referenced: dict[str, set[str]] = {}
    for rel, body in bodies.items():
        if not rel.startswith("planning/") or rel in SKIP_SCAN:
            continue
        for match in ID_RE.finditer(body):
            ident = match.group(1)
            if _owned_by(id_kind(ident), rel):
                defined.add(ident)
            else:
                referenced.setdefault(ident, set()).add(rel)
        for heading, _ in _sections(body):
            first = ID_RE.match(heading)
            if first and _owned_by(id_kind(first.group(1)), rel):
                heading_defs.setdefault(first.group(1), []).append(rel)
        if rel.startswith("planning/adr/"):
            fm, _ = parse_front_matter(docs[rel])
            if fm.get("id"):
                defined.add(str(fm["id"]))
    for ident, rels in sorted(referenced.items()):
        if ident not in defined:
            r.errors.append(f"{ident}: referenced in {', '.join(sorted(rels))} but never defined")
    for ident, rels in sorted(heading_defs.items()):
        if len(rels) > 1:
            r.errors.append(f"{ident}: defined in more than one heading ({', '.join(rels)})")

    # Traceability
    for rel, body in bodies.items():
        if not rel.startswith("planning/"):
            continue
        for heading, section in _sections(body):
            first = ID_RE.match(heading)
            if not first:
                continue
            ident = first.group(1)
            for kind, needs, label in TRACE_RULES:
                if id_kind(ident) != kind:
                    continue
                kinds_found = {id_kind(m.group(1)) for m in ID_RE.finditer(section)}
                if not kinds_found.intersection(needs):
                    r.errors.append(f"{ident}: {label}")

    # AGENTS.md sections
    agents = pkg.root / "AGENTS.md"
    if agents.is_file():
        headings = {h.group(2).strip().lower() for h in HEADING_RE.finditer(agents.read_text(encoding="utf-8"))}
        for section in AGENTS_SECTIONS:
            if section.lower() not in headings:
                r.errors.append(f"AGENTS.md: missing section '## {section}'")

    # Readiness
    not_approved = [rel for rel in READY_REQUIRES_APPROVED if pkg.status_of(rel) != "approved"]
    for rel in not_approved:
        r.warnings.append(f"{rel}: not approved yet (needed before scaffolding)")
    drafts = [
        rel for rel in REQUIRED_FILES
        if rel.startswith("planning/") and rel not in READY_REQUIRES_APPROVED
        and pkg.status_of(rel) not in (None, "approved")
    ]
    for rel in drafts:
        r.warnings.append(f"{rel}: still {pkg.status_of(rel)}")
    r.ready = r.valid and not not_approved
    return r
