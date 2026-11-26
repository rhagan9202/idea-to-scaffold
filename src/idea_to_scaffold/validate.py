"""Validate a planning package against spec section 8."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ids import (
    HEADING_RE,
    ID_RE,
    META_FILES,
    github_slug,
    id_kind,
    scan_lines,
    sections,
    strip_links,
)
from .linker import backlog_items, link_package, scan_package
from .package import (
    AGENTS_SECTIONS,
    BACKLOG_STATUSES,
    READY_REQUIRES_APPROVED,
    REQUIRED_FILES,
    SLUG_RE,
    SUPPORTED_MAJOR,
    Package,
    parse_front_matter,
)

# Section-level traceability: (kind of the section heading, kinds it must reference, label)
TRACE_RULES = [
    ("F", ("G",), "feature must say which goal it supports (G-n)"),
    ("FR", ("F",), "functional requirement must name its feature (F-nn)"),
]
LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)")
NO_VALUE = {"", "—", "-", "–", "none", "n/a"}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ready: bool = False

    @property
    def valid(self) -> bool:
        return not self.errors


def _check_manifest(pkg: Package, r: Report) -> None:
    version = str(pkg.get("spec_version", ""))
    if not version:
        r.errors.append("package.yaml: spec_version is missing")
    else:
        try:
            if int(version.split(".")[0]) > SUPPORTED_MAJOR:
                r.errors.append(f"package.yaml: spec_version {version} is newer than this tool supports")
        except ValueError:
            r.errors.append(f"package.yaml: spec_version {version!r} is not MAJOR.MINOR")
    for key in ("project.name", "project.slug", "project.summary", "stack.language"):
        value = pkg.get(key)
        if not value or str(value).startswith("<"):
            r.errors.append(f"package.yaml: {key} is required")
    if pkg.slug and not pkg.slug.startswith("<") and not SLUG_RE.match(pkg.slug):
        r.errors.append(f"package.yaml: project.slug {pkg.slug!r} must be lowercase letters, digits and hyphens")


def _check_links(pkg: Package, rel: str, body: str, r: Report, slug_cache: dict) -> None:
    """Every relative link in a planning doc must point at an existing file, and at an existing heading."""
    base = pkg.path_of(rel).parent
    for line in scan_lines(body):
        if line.in_code:
            continue
        for m in LINK_RE.finditer(line.text):
            href = m.group(1)
            if re.match(r"^[a-z][a-z0-9+.-]*:", href):  # http:, mailto:, ...
                continue
            path_part, _, anchor = href.partition("#")
            target = (base / path_part).resolve() if path_part else pkg.path_of(rel).resolve()
            if not target.exists():
                r.errors.append(f"{rel}: broken link to {href}")
                continue
            if anchor and target.suffix == ".md":
                if target not in slug_cache:
                    _, tbody = parse_front_matter(target.read_text(encoding="utf-8"))
                    slug_cache[target] = {github_slug(h) for h in
                                          (ln.heading for ln in scan_lines(tbody)) if h is not None}
                if anchor not in slug_cache[target]:
                    r.errors.append(f"{rel}: link {href} points to a heading that doesn't exist (run `its link` to re-point ID links)")


def _check_backlog(pkg: Package, defined: set[str], r: Report) -> None:
    items = backlog_items(pkg)
    if pkg.path_of("planning/07-backlog.md").is_file() and not items:
        r.warnings.append("planning/07-backlog.md: no backlog items (### T-n.m headings) found")
    seen: set[str] = set()
    for item in items:
        ident = item["id"]
        if ident in seen:
            continue
        seen.add(ident)
        status = item["status"]
        if status not in BACKLOG_STATUSES:
            r.errors.append(
                f"{ident}: Status {status or '(missing)'!r} must be one of {', '.join(BACKLOG_STATUSES)}"
            )
        milestone = item["milestone"].strip().lower()
        if milestone in NO_VALUE:
            if status in ("todo", "in-progress"):
                r.warnings.append(f"{ident}: is {status} but has no Milestone")
        elif not ID_RE.fullmatch(item["milestone"].strip()) or item["milestone"].strip() not in defined:
            r.errors.append(f"{ident}: Milestone {item['milestone']!r} is not a milestone defined in the project plan")
        sat_kinds = {id_kind(m.group(1)) for m in ID_RE.finditer(item["satisfies"])}
        if status not in ("proposed", "dropped") and not sat_kinds & {"FR", "NFR"}:
            r.errors.append(f"{ident}: backlog item must list what it satisfies (FR-/NFR-)")
        for dep in ID_RE.finditer(item["depends"]):
            if id_kind(dep.group(1)) == "T" and dep.group(1) not in {i["id"] for i in items}:
                r.errors.append(f"{ident}: depends on {dep.group(1)}, which is not in the backlog")


def validate(pkg: Package, *, check_links_current: bool = True) -> Report:
    r = Report()
    _check_manifest(pkg, r)

    for rel in REQUIRED_FILES:
        if not pkg.path_of(rel).is_file():
            r.errors.append(f"{rel}: missing")

    docs = {pkg.rel(p): p.read_text(encoding="utf-8") for p in pkg.planning_files() + pkg.playbooks()}
    bodies: dict[str, str] = {}
    for rel, text in docs.items():
        fm, body = parse_front_matter(text)
        bodies[rel] = body
        if rel in META_FILES:
            continue
        missing = [k for k in ("id", "title", "status") if k not in fm]
        if missing:
            r.errors.append(f"{rel}: front matter missing {', '.join(missing)}")
        manifest_status = (pkg.get("artifacts") or {}).get(rel)
        if manifest_status and "status" in fm and str(manifest_status) != str(fm["status"]):
            r.warnings.append(
                f"{rel}: status is {fm['status']!r} in front matter but {manifest_status!r} in package.yaml"
            )

    planning = {rel: b for rel, b in bodies.items() if rel.startswith("planning/") and rel not in META_FILES}

    # IDs: definitions vs references
    scan = scan_package(pkg)
    defined = set(scan.index)
    for ident, rels in sorted(scan.references.items()):
        if ident not in defined:
            r.errors.append(f"{ident}: referenced in {', '.join(sorted(rels))} but never defined")
    for ident, sites in sorted(scan.definitions.items()):
        files = sorted({rel for rel, _ in sites})
        headings = [rel for rel, is_heading in sites if is_heading]
        if len(files) > 1:
            r.errors.append(f"{ident}: defined in more than one file ({', '.join(files)})")
        elif len(headings) > 1:
            r.errors.append(f"{ident}: defined in more than one heading in {files[0]}")

    # Traceability
    for body in planning.values():
        for heading, section in sections(body):
            first = ID_RE.match(strip_links(heading))
            if not first:
                continue
            ident = first.group(1)
            for kind, needs, label in TRACE_RULES:
                if id_kind(ident) == kind:
                    kinds_found = {id_kind(m.group(1)) for m in ID_RE.finditer(strip_links(section))}
                    if not kinds_found & set(needs):
                        r.errors.append(f"{ident}: {label}")
    _check_backlog(pkg, defined, r)

    # Plan must not carry its own task list (the backlog is canonical)
    plan = planning.get("planning/06-project-plan.md", "")
    if re.search(r"^#{1,6}\s+T-\d+\.\d+", plan, re.MULTILINE):
        r.errors.append("planning/06-project-plan.md: defines tasks; move them to 07-backlog.md (the only list of work items)")

    # Links
    slug_cache: dict = {}
    for rel, body in planning.items():
        _check_links(pkg, rel, body, r, slug_cache)
    if check_links_current and not r.errors:
        stale = link_package(pkg, check=True)
        if stale:
            r.warnings.append(f"cross-links are out of date in {', '.join(stale)} (run `its link`)")

    # AGENTS.md sections
    agents = pkg.path_of("AGENTS.md")
    if agents.is_file():
        headings = {h.group(2).strip().lower() for h in HEADING_RE.finditer(agents.read_text(encoding="utf-8"))}
        for section in AGENTS_SECTIONS:
            if section.lower() not in headings:
                r.errors.append(f"AGENTS.md: missing section '## {section}'")

    # Readiness
    not_approved = [rel for rel in READY_REQUIRES_APPROVED if pkg.status_of(rel) != "approved"]
    for rel in not_approved:
        r.warnings.append(f"{rel}: not approved yet (needed before scaffolding)")
    for rel in REQUIRED_FILES:
        if rel.startswith("planning/") and rel not in READY_REQUIRES_APPROVED:
            status = pkg.status_of(rel)
            if status not in (None, "approved"):
                r.warnings.append(f"{rel}: still {status}")
    r.ready = r.valid and not not_approved
    return r
