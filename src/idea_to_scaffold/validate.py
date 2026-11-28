"""Validate a planning package against spec section 8."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import blocks
from .backlog import bad_touches, find_cycles, load_backlog, proposal_problems
from .ids import (
    ID_RE,
    META_FILES,
    field_value,
    id_kind,
    scan_lines,
    sections,
    strip_links,
    strip_strike,
)
from .linker import link_package, scan_package
from .package import (
    AGENTS_SECTIONS,
    BACKLOG_STATUSES,
    DOC_STATUSES,
    PROFILES,
    SLUG_RE,
    SUPPORTED_MAJOR,
    Package,
    front_matter_error,
    parse_front_matter,
)

# Section-level traceability: (kind of the section heading, kinds it must reference, label)
TRACE_RULES = [
    ("F", ("G",), "feature must say which goal it supports (G-n)"),
    ("FR", ("F",), "functional requirement must name its feature (F-nn)"),
]
LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)")
NO_VALUE = {"", "—", "-", "–", "none", "n/a"}
TASK_HEADING_RE = re.compile(r"^\s*(?:T-\d+\.\d+|T-new-[a-z0-9-]+)\b")
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+")
CONFLICT_RE = re.compile(r"^(<{7}|={7}|>{7})(\s|$)", re.MULTILINE)


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
    profile = pkg.get("profile")
    if profile is not None and str(profile).strip().lower() not in PROFILES:
        r.errors.append(f"package.yaml: profile {profile!r} must be one of {', '.join(PROFILES)}")
    agents = pkg.get("agents")
    if agents is not None and not isinstance(agents, (list, str)):
        r.errors.append("package.yaml: agents must be a list, e.g. [codex, claude]")
    for key in ("commands", "artifacts", "stack", "project"):
        value = pkg.get(key)
        if value is not None and not isinstance(value, dict):
            r.errors.append(f"package.yaml: {key} must be a mapping (key: value lines), not {type(value).__name__}")


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
                    slug_cache[target] = {ln.anchor for ln in scan_lines(tbody) if ln.anchor}
                if anchor not in slug_cache[target]:
                    r.errors.append(f"{rel}: link {href} points to a heading that doesn't exist (run `its link` to re-point ID links)")


def _check_backlog(pkg: Package, defined: set[str], must_frs: set[str], r: Report) -> None:
    items = load_backlog(pkg)
    if pkg.path_of("planning/07-backlog.md").is_file() and not items:
        r.warnings.append("planning/07-backlog.md: no backlog items (### T-n.m headings) found")
    ids = {i.id for i in items}
    by_id = {i.id: i for i in items}
    seen: set[str] = set()
    for item in items:
        ident = item.id
        if ident in seen:
            continue
        seen.add(ident)
        status = item.status
        if status not in BACKLOG_STATUSES:
            hint = ""
            if "status" not in item.fields:
                hint = " (write the field as `- **Status:** todo`)"
            r.errors.append(
                f"{ident}: Status {status or '(missing)'!r} must be one of {', '.join(BACKLOG_STATUSES)}{hint}"
            )
        if item.placeholder and status not in ("proposed", "dropped"):
            r.errors.append(
                f"{ident}: placeholder IDs (T-new-...) are only for proposed items; give it a permanent T-n.m ID "
                "before scheduling it"
            )
        milestone = item.milestone.strip()
        if milestone.lower() in NO_VALUE:
            if status in ("todo", "in-progress") and pkg.profile != "lite":
                r.warnings.append(f"{ident}: is {status} but has no Milestone")
        elif not ID_RE.fullmatch(milestone) or milestone not in defined:
            r.errors.append(f"{ident}: Milestone {milestone!r} is not a milestone defined in the project plan")
        sat_kinds = {id_kind(s) for s in item.satisfies}
        if status not in ("proposed", "dropped") and not sat_kinds & {"FR", "NFR"}:
            r.errors.append(f"{ident}: backlog item must list what it satisfies (FR-/NFR-)")
        if status in ("todo", "in-progress", "blocked") and item.done_when.lower() in NO_VALUE and not item.verify:
            r.warnings.append(f"{ident}: has no Done when or Verify, so nobody can tell when it is finished")
        for dep in item.depends_on:
            if dep == ident:
                continue  # reported as a cycle below
            if dep not in ids:
                r.errors.append(f"{ident}: depends on {dep}, which is not in the backlog")
            elif by_id[dep].status == "dropped" and status not in ("dropped", "done"):
                r.errors.append(f"{ident}: depends on {dep}, which is dropped; remove or replace the dependency")
            elif status == "done" and by_id[dep].status != "done":
                r.warnings.append(f"{ident}: is done but its dependency {dep} is {by_id[dep].status or 'not done'}")
        for other in item.other_dependencies:
            r.warnings.append(
                f"{ident}: Depends on lists {other}; only backlog items (T-...) are dependencies, so it is ignored"
            )
        for path in item.touches:
            if bad_touches(path):
                r.errors.append(f"{ident}: Touches {path!r} must be a relative path inside the repository")
    for cycle in find_cycles(items):
        r.errors.append(f"dependency cycle in the backlog: {' -> '.join(cycle)} (none of these can ever start)")
    covered = {s for i in items if i.status != "dropped" for s in i.satisfies}
    for fr in sorted(must_frs - covered, key=lambda x: int(x.split("-")[1])):
        r.errors.append(f"{fr}: is a Must requirement but no backlog item satisfies it")


def _must_requirements(body: str) -> set[str]:
    """Requirements whose section says `**Priority:** Must`."""
    out: set[str] = set()
    for heading, section in sections(body):
        first = ID_RE.match(strip_strike(strip_links(heading)).strip())
        if first and id_kind(first.group(1)) == "FR":
            priority = field_value(section, "Priority") or ""
            if strip_links(priority).strip().lower().startswith("must"):
                out.add(first.group(1))
    return out


def _check_plan_tasks(plan: str, r: Report) -> None:
    """The project plan defines milestones only; work items live in the backlog."""
    rel = "planning/06-project-plan.md"
    for line in scan_lines(plan):
        if line.in_code:
            continue
        if line.heading is not None and TASK_HEADING_RE.match(strip_links(line.heading)):
            r.errors.append(f"{rel}: defines tasks; move them to 07-backlog.md (the only list of work items)")
            return
        if CHECKBOX_RE.match(line.text):
            r.errors.append(
                f"{rel}: line {line.number + 1} is a task checklist; move tasks to 07-backlog.md "
                "(the only list of work items)"
            )
            return


def validate(pkg: Package, *, check_links_current: bool = True) -> Report:
    r = Report()
    _check_manifest(pkg, r)
    if not pkg.repo_layout and (pkg.root / "docs/planning/package.yaml").is_file():
        r.warnings.append(
            "both package.yaml and docs/planning/package.yaml exist; the root one is used and docs/planning/ is "
            "ignored. Remove the root package files if this is a scaffolded repo"
        )

    for rel in pkg.required_files:
        if not pkg.path_of(rel).is_file():
            r.errors.append(f"{rel}: missing")

    docs: dict[str, str] = {}
    for path in pkg.planning_files() + pkg.playbooks():
        try:
            docs[pkg.rel(path)] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            r.errors.append(f"{pkg.rel(path)}: is not valid UTF-8 text")
    if any(e.endswith("is not valid UTF-8 text") for e in r.errors):
        return r  # the scans below read every file
    bodies: dict[str, str] = {}
    for rel, text in docs.items():
        for m in CONFLICT_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            r.errors.append(f"{rel}: line {line} has a merge conflict marker ({m.group(1)}); resolve the conflict")
            break
        fm, body = parse_front_matter(text)
        bodies[rel] = body
        if rel in META_FILES:
            continue
        yaml_error = front_matter_error(text)
        if yaml_error:
            r.errors.append(f"{rel}: front matter is not valid YAML ({yaml_error})")
            continue
        missing = [k for k in ("id", "title", "status") if k not in fm]
        if missing:
            r.errors.append(f"{rel}: front matter missing {', '.join(missing)}")
        elif str(fm["status"]) not in DOC_STATUSES:
            r.errors.append(f"{rel}: status {fm['status']!r} must be one of {', '.join(DOC_STATUSES)}")
        artifacts = pkg.get("artifacts") or {}
        manifest_status = artifacts.get(rel) if isinstance(artifacts, dict) else None
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
        elif not headings and len(sites) > 1:
            r.errors.append(f"{ident}: defined more than once in {files[0]} (e.g. two table rows)")

    # Traceability
    for body in planning.values():
        for heading, section in sections(body):
            first = ID_RE.match(strip_strike(strip_links(heading)).strip())
            if not first:
                continue
            ident = first.group(1)
            for kind, needs, label in TRACE_RULES:
                if kind == "FR" and pkg.profile == "lite":
                    continue  # lite folds features into the requirements doc, so FRs name no F-nn
                if id_kind(ident) == kind:
                    kinds_found = {id_kind(m.group(1)) for m in ID_RE.finditer(strip_links(section))}
                    if not kinds_found & set(needs):
                        r.errors.append(f"{ident}: {label}")
    must = _must_requirements(planning.get("planning/03-requirements.md", ""))
    _check_backlog(pkg, defined, must, r)
    r.errors += proposal_problems(pkg)
    _check_plan_tasks(planning.get("planning/06-project-plan.md", ""), r)

    # Links
    slug_cache: dict = {}
    for rel, body in planning.items():
        _check_links(pkg, rel, body, r, slug_cache)
    if check_links_current and not r.errors:
        stale = link_package(pkg, check=True, commands=True)
        links = [x for x in stale if x not in blocks.FILES]
        if links:
            r.warnings.append(f"cross-links are out of date in {', '.join(links)} (run `its link`)")
        commands = [x for x in stale if x in blocks.FILES]
        if commands:
            r.warnings.append(f"the commands in {', '.join(commands)} don't match package.yaml (run `its link`)")
    for line in blocks.disagreements(pkg):
        r.warnings.append(f"{line}; package.yaml is the source (put the list between its:commands markers and run "
                          "`its link` to keep them in step)")

    # AGENTS.md sections
    agents = pkg.path_of("AGENTS.md")
    if agents.is_file():
        headings = {
            ln.heading.strip().lower()
            for ln in scan_lines(agents.read_text(encoding="utf-8"))
            if ln.heading is not None
        }
        for section in AGENTS_SECTIONS:
            if section.lower() not in headings:
                r.errors.append(f"AGENTS.md: missing section '## {section}'")

    # Readiness
    needs_approval = pkg.ready_requires_approved
    not_approved = [rel for rel in needs_approval if pkg.status_of(rel) != "approved"]
    for rel in not_approved:
        r.warnings.append(f"{rel}: not approved yet (needed before scaffolding)")
    for rel in pkg.required_files:
        if rel.startswith("planning/") and rel not in needs_approval:
            status = pkg.status_of(rel)
            if status not in (None, "approved"):
                r.warnings.append(f"{rel}: still {status}")
    r.ready = r.valid and not not_approved
    return r
