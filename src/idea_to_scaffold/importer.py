"""Import a chat export (Markdown with file markers) into a package folder."""
from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath

from .package import blank_package_dir

FILE_BLOCK_RE = re.compile(
    r"<!--\s*file:\s*(?P<path>[^\s>]+)\s*-->[ \t]*\n(?P<body>.*?)\n?[ \t]*<!--\s*end file\s*-->",
    re.DOTALL,
)
OUTER_FENCE_RE = re.compile(
    r"\A\s*(?P<fence>`{3,}|~{3,})[ \t]*(?:markdown|md|yaml|yml)?[ \t]*\n(?P<inner>.*)\n(?P=fence)[ \t]*\s*\Z",
    re.DOTALL,
)


class ImportErrorBadPath(ValueError):
    pass


def _safe_path(raw: str) -> str:
    path = PurePosixPath(raw.strip().replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ImportErrorBadPath(f"unsafe path in export: {raw!r}")
    return path.as_posix()


def strip_outer_fence(body: str) -> str:
    match = OUTER_FENCE_RE.match(body)
    if match:
        return match.group("inner")
    return body


def parse_export(text: str) -> dict[str, str]:
    """Return {relative path: content}. Later occurrences of a path win."""
    files: dict[str, str] = {}
    # Normalise Windows newlines so the regex is predictable.
    text = text.replace("\r\n", "\n")
    for match in FILE_BLOCK_RE.finditer(text):
        path = _safe_path(match.group("path"))
        content = strip_outer_fence(match.group("body"))
        if not content.endswith("\n"):
            content += "\n"
        files[path] = content
    return files


def seed_defaults(dest: Path) -> list[str]:
    """Copy template files the chat usually doesn't produce (playbooks, ADR-0001, wireframe README)."""
    copied: list[str] = []
    blank = blank_package_dir()
    for rel in [
        "playbooks/implement-task.md",
        "playbooks/update-plan.md",
        "playbooks/write-adr.md",
        "planning/adr/ADR-0001-record-architecture-decisions.md",
        "planning/wireframes/README.md",
    ]:
        target = dest / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(blank / rel, target)
            copied.append(rel)
    return copied


def import_export(export_file: Path, dest: Path, *, seed: bool = True) -> tuple[list[str], list[str]]:
    """Write files from a chat export into dest. Returns (written, seeded)."""
    files = parse_export(Path(export_file).read_text(encoding="utf-8"))
    if not files:
        raise ValueError(
            "no <!-- file: ... --> blocks found. Did you export the chat after stage 8?"
        )
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for rel, content in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    seeded = seed_defaults(dest) if seed else []
    return written, seeded
