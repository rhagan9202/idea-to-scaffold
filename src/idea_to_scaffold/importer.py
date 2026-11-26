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


NAV_RE = re.compile(r"\n?<!-- its:nav[^>]*-->.*?<!-- /its:nav -->\n?", re.DOTALL)
ID_LINK_RE = re.compile(r"\[([^\]\n]+)\]\([^)\s]*\)")


def normalise(text: str) -> str:
    """Content with generated parts removed (nav blocks, links), so a file that `its link` touched still compares
    equal to the export it came from."""
    text = NAV_RE.sub("\n", text.replace("\r\n", "\n"))
    text = ID_LINK_RE.sub(r"\1", text)
    return "\n".join(line.rstrip() for line in text.split("\n") if line.strip())


class ImportConflict(Exception):
    """Raised when an import would replace files that differ from the export."""

    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__(
            f"{len(paths)} existing file(s) differ from the export and would be overwritten: "
            + ", ".join(paths)
            + ". Nothing was written. Re-run with --overwrite to replace them (originals are kept as .orig)."
        )


def import_export(
    export_file: Path, dest: Path, *, seed: bool = True, overwrite: bool = False
) -> tuple[list[str], list[str]]:
    """Write files from a chat export into dest. Returns (written, seeded).

    Files that already exist and differ from the export (after ignoring generated links and nav blocks) are
    conflicts: the import is refused as a whole unless ``overwrite`` is set, in which case each replaced file's
    previous content is saved next to it as ``<name>.orig``. Files identical to the export are left untouched.
    """
    files = parse_export(Path(export_file).read_text(encoding="utf-8"))
    if not files:
        raise ValueError("no <!-- file: ... --> blocks found. Did you export the chat after stage 8?")
    dest = Path(dest)
    to_write: dict[str, str] = {}
    conflicts: list[str] = []
    for rel, content in files.items():
        target = dest / rel
        if target.exists():
            if normalise(target.read_text(encoding="utf-8")) == normalise(content):
                continue  # unchanged; keep the linked version on disk
            conflicts.append(rel)
        to_write[rel] = content
    if conflicts and not overwrite:
        raise ImportConflict(conflicts)

    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in to_write.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel in conflicts:
            target.with_name(target.name + ".orig").write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        target.write_text(content, encoding="utf-8")
    seeded = seed_defaults(dest) if seed else []
    return list(to_write), seeded
