"""`its validate --trace`: which Must acceptance criteria no test names.

Acceptance criteria are numbered by position under a requirement's **Acceptance criteria** list: the first bullet
of FR-001 is `FR-001 AC1`. A bullet that starts with its own label (`AC3:` or `**AC3**`) keeps that number instead.

A test covers a criterion when a test file mentions the requirement and the criterion, in any of the usual
spellings: `test_fr_001_ac2_rejects_duplicates`, `FR-001/AC2`, `fr001_ac2`, or a marker such as
`@pytest.mark.req("FR-001 AC2")`. A mention of the requirement alone covers all of its criteria, and is reported as
`by_requirement` so a reviewer can see the trace is coarse.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import gitx
from .gate import is_test_path
from .ids import ID_RE, field_value, id_kind, sections, strip_links, strip_strike
from .package import Package

REF_RE = re.compile(
    r"(?<![A-Za-z0-9])fr[-_ ]?(\d{3,})(?:\s*[-_./ ,(]?\s*ac[-_ ]?(\d+))?(?![0-9A-Za-z])", re.I)
CRITERIA_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:#+\s*|\*\*)?acceptance criteria\b", re.I)
BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
LABEL_RE = re.compile(r"^(?:\*\*)?AC[-_ ]?(\d+)(?:\*\*)?\s*[:.)-]?\s*(?:\*\*)?\s*", re.I)
BOLD_LABEL_RE = re.compile(r"^\s*(?:[-*+]\s+)?\*\*[^*]+\*\*")  # the next field, e.g. **Out of scope**
MAX_BYTES = 1_000_000


def _indent(text: str) -> int:
    return len(text.expandtabs(4))


def criteria(section: str) -> list[tuple[int, str]]:
    """(number, text) for each acceptance criterion in a requirement's section.

    Only bullets at the first criterion's indentation count: deeper bullets are details of the one above. The list
    ends at the next heading or bold label (`**Out of scope**`), or at an unindented paragraph after it."""
    out: list[tuple[int, str]] = []
    inside = False
    level: int | None = None
    for line in section.split("\n"):
        if not inside:
            if CRITERIA_RE.match(line):
                inside = True
            continue
        m = BULLET_RE.match(line)
        if m and (level is None or _indent(m.group(1)) <= level):
            if level is not None and _indent(m.group(1)) < level:
                break  # back out to an enclosing list
            level = _indent(m.group(1))
            text = m.group(2).strip()
            label = LABEL_RE.match(text)
            used = {n for n, _ in out}
            number = int(label.group(1)) if label else len(out) + 1
            if not label and number in used:  # a label earlier took this position's number
                number = max(used) + 1
            out.append((number, strip_links(text[label.end():] if label else text).replace("**", "").strip()))
        elif m:
            continue  # a sub-bullet: part of the criterion above
        elif line.lstrip().startswith("#") or (line.strip() and BOLD_LABEL_RE.match(line)):
            break
        elif out and line.strip() and not line.startswith((" ", "\t")):
            break  # the list ended
    return out


def must_requirements(pkg: Package) -> list[dict]:
    out = []
    for path in pkg.planning_files():
        body = path.read_text(encoding="utf-8")
        for heading, section in sections(body):
            first = ID_RE.match(strip_strike(strip_links(heading)).strip())
            if not first or id_kind(first.group(1)) != "FR" or "~~" in heading:
                continue
            priority = strip_links(field_value(section, "Priority") or "").strip().lower()
            if not priority.startswith("must"):
                continue
            title = strip_links(heading).split(first.group(1), 1)[1].strip(" —–:-")
            found = criteria(section) or [(1, title or first.group(1))]
            out.append({"id": first.group(1), "title": title, "file": pkg.rel(path),
                        "criteria": [{"ac": n, "text": t} for n, t in found]})
    return out


def test_files(pkg: Package) -> list[str]:
    root = pkg.root
    proc = gitx.run(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if proc.returncode == 0:
        paths = [p for p in proc.stdout.splitlines() if p]
    else:
        paths = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    return sorted(p for p in paths if is_test_path(pkg, p))


def references(pkg: Package) -> dict[tuple[str, int | None], list[dict]]:
    """Every FR (and FR + criterion) mention in the test files, with where it is."""
    refs: dict[tuple[str, int | None], list[dict]] = {}
    for rel in test_files(pkg):
        path = Path(pkg.root) / rel
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.split("\n"), start=1):
            for m in REF_RE.finditer(line):
                key = (f"FR-{m.group(1)}", int(m.group(2)) if m.group(2) else None)
                refs.setdefault(key, []).append({"file": rel, "line": lineno})
    return refs


def trace(pkg: Package) -> dict:
    """The trace from Must acceptance criteria to tests."""
    if not pkg.repo_layout:
        return {"checked": False, "reason": "this is a planning package, not a scaffolded repo", "requirements": [],
                "uncovered": [], "by_requirement": []}
    refs = references(pkg)
    reqs = must_requirements(pkg)
    uncovered, coarse = [], []
    for req in reqs:
        whole = refs.get((req["id"], None), [])
        for crit in req["criteria"]:
            exact = refs.get((req["id"], crit["ac"]), [])
            crit["tests"] = exact
            crit["by_requirement"] = bool(whole) and not exact
            entry = {"id": req["id"], "ac": crit["ac"], "text": crit["text"]}
            if not exact and not whole:
                uncovered.append(entry)
            elif not exact:
                coarse.append(entry)
    return {"checked": True, "requirements": reqs, "uncovered": uncovered, "by_requirement": coarse,
            "criteria": sum(len(r["criteria"]) for r in reqs)}
