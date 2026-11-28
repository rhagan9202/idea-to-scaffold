"""The commands, from one source: blocks that `its link` writes from package.yaml into the files that repeat them.

`package.yaml`'s `commands` (and `stack`) are the only place to change a command. These files carry a copy between
markers, which `its link` rewrites and `its link --check` and `its validate` compare:

- `AGENTS.md`, the commands in the *Stack & commands* list: ``<!-- its:commands ... -->`` to
  ``<!-- /its:commands -->``;
- `HANDOFF.md`, the *Commands* list, between the same markers;
- `.github/workflows/ci.yml`, the setup, lint and test steps: ``# its:commands`` to ``# /its:commands`` comment
  lines, the steps indented like the opening marker.

Text outside the markers is the author's and is left alone. `its link` (not `--check`) adds the markers to an AGENTS.md
or HANDOFF.md command list that has none, when every line in it is a package.yaml command; otherwise the file is left
alone and `its validate` says when its **Test:** or other command lines disagree. A CI workflow gets its markers by
hand. The acceptance command isn't listed for agents: they can't see the held-out tests it runs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .package import Package

MD_START = "<!-- its:commands (generated from package.yaml by `its link`; edit package.yaml, not this block) -->"
MD_END = "<!-- /its:commands -->"
MD_RE = re.compile(r"<!-- its:commands\b[^>]*-->[ \t]*\n.*?<!-- /its:commands -->", re.S)
YAML_START = "# its:commands (generated from package.yaml by `its link`; edit package.yaml, not these steps)"
YAML_END = "# /its:commands"
YAML_RE = re.compile(r"^(?P<indent>[ \t]*)# its:commands\b[^\n]*\n(?:(?![ \t]*# /its:commands).*\n)*[ \t]*# /its:commands[^\n]*$",
                     re.M)
STANDARD = ("setup", "test", "lint", "run")
CI_ORDER = ("setup", "lint", "test")
FILES = ("AGENTS.md", "HANDOFF.md", ".github/workflows/ci.yml")
LINE_RE = re.compile(r"^- \*\*(?P<name>[A-Za-z][\w -]*):\*\* `+(?P<cmd>.*?)`+\s*$")


def _code(cmd: str) -> str:
    """Inline code for a command, with a fence longer than any run of backticks in it."""
    longest = max((len(m) for m in re.findall(r"`+", cmd)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {cmd} {fence}" if longest else f"`{cmd}`"


def listed(pkg: Package) -> list[tuple[str, str]]:
    """The commands agents are told about, as (name, command): the standard ones in order, then the rest."""
    cmds = {k: v for k, v in pkg.commands.items() if k != "acceptance"}
    return [(k, cmds[k]) for k in STANDARD if k in cmds] + [(k, v) for k, v in cmds.items() if k not in STANDARD]


def command_lines(pkg: Package) -> list[str]:
    """The Markdown list of commands, for AGENTS.md and HANDOFF.md."""
    return [f"- **{k.title()}:** {_code(v)}" for k, v in listed(pkg)] or ["- (none set in package.yaml)"]


def _yaml_scalar(text: str) -> str:
    plain = (text == text.strip() and text and text[0] not in "!&*[]{}|>'\"%@`#,?:-"
             and ": " not in text and " #" not in text and not text.endswith(":"))
    return text if plain else json.dumps(text)  # JSON's string syntax is valid YAML


def ci_lines(pkg: Package) -> list[str]:
    cmds = pkg.commands
    return [f"- run: {_yaml_scalar(cmds[k])}" for k in CI_ORDER if cmds.get(k) and cmds[k].strip() != "true"]


def md_block(lines: list[str]) -> str:
    return "\n".join([MD_START, *lines, MD_END])


def _rewrite(rel: str, text: str, pkg: Package) -> str:
    if rel.endswith(".yml"):
        def ci(m: re.Match) -> str:
            indent = m.group("indent")
            return "\n".join(indent + line for line in (YAML_START, *ci_lines(pkg), YAML_END))
        return YAML_RE.sub(ci, text)
    return MD_RE.sub(lambda _: md_block(command_lines(pkg)), text)


def adopt(rel: str, text: str, pkg: Package) -> str:
    """Put the markers around an unmarked command list, so `its link` keeps it in step from now on: in AGENTS.md
    the run of command lines under *Stack & commands*, in HANDOFF.md the list under *Commands*. Only when every line
    in that run names a command package.yaml has, so regenerating it loses nothing."""
    if rel not in ("AGENTS.md", "HANDOFF.md") or MD_RE.search(text):
        return text
    names = {k.lower() for k, _ in listed(pkg)}
    lines = text.split("\n")
    section = False
    for i, line in enumerate(lines):
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            section = ("command" in title and ("stack" in title or rel == "HANDOFF.md"))
            continue
        m = LINE_RE.match(line)
        if not (section and m and m.group("name").strip().lower() in names):
            continue
        end = i
        while end < len(lines) and (m := LINE_RE.match(lines[end])) and m.group("name").strip().lower() in names:
            end += 1
        if end < len(lines) and lines[end].startswith("- "):
            return text  # the list goes on with lines that aren't package.yaml commands: leave it to its author
        return "\n".join([*lines[:i], MD_START, *lines[i:end], MD_END, *lines[end:]])
    return text


def sync(pkg: Package, *, check: bool = False, adopt_unmarked: bool = False) -> list[str]:
    """Rewrite the generated blocks from package.yaml. Returns the files that changed (or would, with `check`).
    `adopt_unmarked` first adds the markers to a command list that has none (`its link`, import and scaffold do)."""
    changed = []
    for rel in FILES:
        path = pkg.path_of(rel)
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        new = _rewrite(rel, adopt(rel, text, pkg) if adopt_unmarked else text, pkg)
        if new == text:
            continue
        changed.append(rel)
        if not check:
            _write(path, new.replace("\n", "\r\n") if crlf else new)
    return changed


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def disagreements(pkg: Package) -> list[str]:
    """For an AGENTS.md or HANDOFF.md without the markers: command lines (`- **Test:** `...``) that say something
    other than package.yaml does."""
    cmds = {k.lower(): v for k, v in pkg.commands.items()}
    out = []
    for rel in ("AGENTS.md", "HANDOFF.md"):
        path = pkg.path_of(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if MD_RE.search(text):
            continue
        for line in text.splitlines():
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            name, cmd = m.group("name").strip().lower(), m.group("cmd").strip()
            if name in cmds and cmd != cmds[name].strip():
                out.append(f"{rel} says {m.group('name')}: `{cmd}`, but package.yaml has `{cmds[name]}`")
    return out
