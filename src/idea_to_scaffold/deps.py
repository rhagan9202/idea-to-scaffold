"""Dependency changes on a branch, which `its land` would install as the lead.

`its land` runs `commands.setup` (`uv sync`, `npm ci`, ...) in the merge worktree before the checks, as the lead.
Setup installs whatever the branch's manifests and lockfiles name, and installing a package runs its build hooks.
So a branch that adds a dependency, changes where one comes from, or rewrites a lockfile, is reported
(`dependency_added`) for a person to look at; `its waive <ID> --dependencies` lets it through.

What counts, by file name:

- **Manifests**, compared entry by entry, so only what the branch adds or changes is reported: `pyproject.toml`
  (`[project] dependencies`, optional dependencies, `[dependency-groups]`, `[tool.poetry.*]`, `[tool.uv*]`),
  `setup.cfg` (`install_requires`, extras), `requirements*.txt` and `constraints*.txt`, `Pipfile`, `package.json`
  (the dependency maps, `overrides`, `resolutions` and the install scripts), `Cargo.toml` (the dependency tables,
  `[patch]`, `[replace]`) and `go.mod` (`require`, `replace`).
- **Whole files**, where any change counts: lockfiles (`uv.lock`, `poetry.lock`, `package-lock.json`,
  `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `go.sum`, ...), `setup.py` (code that can declare anything), and
  package-index settings (`uv.toml`, `pip.conf`, `.npmrc`, `.yarnrc.yml`).

Removing a dependency isn't reported: it installs nothing new.
"""
from __future__ import annotations

import json
import posixpath
import re

LOCKFILES = frozenset({"uv.lock", "poetry.lock", "pipfile.lock", "pdm.lock", "package-lock.json",
                       "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "cargo.lock",
                       "go.sum", "composer.lock", "gemfile.lock"})
WHOLE_FILES = frozenset({"setup.py", "uv.toml", "pip.conf", "pip.ini", ".npmrc", ".yarnrc", ".yarnrc.yml"})
REQUIREMENTS_RE = re.compile(r"^(requirements|constraints)([-_.][\w.-]*)?\.(txt|in)$", re.I)
NPM_MAPS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "bundleDependencies",
            "bundledDependencies", "overrides", "resolutions")
NPM_INSTALL_SCRIPTS = ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepack")
# TOML tables whose every entry is a dependency or a source of one.
PYPROJECT_TABLES = re.compile(r"^(project\.optional-dependencies|dependency-groups|tool\.poetry(\..*)?dependencies"
                              r"|tool\.poetry\.group\..*|tool\.poetry\.source|tool\.uv(\..*)?|tool\.pdm\.dev-dependencies"
                              r"|build-system)$")
CARGO_TABLES = re.compile(r"^((target\..*\.)?(dev-|build-)?dependencies(\..*)?|workspace\.dependencies(\..*)?"
                          r"|patch(\..*)?|replace)$")
PIPFILE_TABLES = re.compile(r"^(packages|dev-packages|source)$")
SETUP_CFG_KEYS = re.compile(r"^(install_requires|setup_requires|tests_require|dependency_links)\s*=")
LIST_LIMIT = 8


def kind_of(path: str) -> str | None:
    """How a file is compared: "lock", "whole", or the manifest's type; None for any other file."""
    name = posixpath.basename(path)
    low = name.lower()
    if low in LOCKFILES:
        return "lock"
    if low in WHOLE_FILES:
        return "whole"
    if low == "pyproject.toml":
        return "pyproject"
    if low == "setup.cfg":
        return "setup.cfg"
    if low == "package.json":
        return "package.json"
    if low == "cargo.toml":
        return "cargo"
    if low == "go.mod":
        return "go.mod"
    if low == "pipfile":
        return "pipfile"
    if REQUIREMENTS_RE.match(name):
        return "requirements"
    return None


def findings(path: str, before: str | None, after: str | None) -> list[dict]:
    """The `dependency_added` finding for one changed file, if the change adds or changes a dependency."""
    kind = kind_of(path)
    if kind is None or after is None:
        return []
    if kind in ("lock", "whole"):
        if before == after:
            return []
        what = "lockfile" if kind == "lock" else "file, which can declare dependencies or where they come from"
        verb = "adds the" if before is None else "changes the"
        return [_finding(path, [], f"the branch {verb} {what} {path}")]
    added = sorted(_entries(kind, after) - _entries(kind, before or ""))
    if not added:
        return []
    shown = ", ".join(added[:LIST_LIMIT]) + (f" and {len(added) - LIST_LIMIT} more" if len(added) > LIST_LIMIT else "")
    return [_finding(path, added, f"the branch adds or changes dependencies in {path}: {shown}")]


def _finding(path: str, added: list[str], what: str) -> dict:
    return {"kind": "dependency_added", "file": path, "added": added[:50],
            "message": f"{what}. its land installs dependencies as the lead (commands.setup), so a person looks at "
                       "them first and waives them with its waive <ID> --dependencies"}


def _entries(kind: str, text: str) -> set[str]:
    """A manifest's dependency entries as comparable strings: "<where>: <entry>"."""
    if kind == "package.json":
        return _npm(text)
    if kind == "requirements":
        return {f"requirement: {line}" for line in _lines(text, "#")}
    if kind == "go.mod":
        return _go(text)
    if kind == "setup.cfg":
        return _setup_cfg(text)
    tables = {"pyproject": PYPROJECT_TABLES, "cargo": CARGO_TABLES, "pipfile": PIPFILE_TABLES}[kind]
    out = set()
    for table, body in _toml_tables(text):
        if tables.match(table):
            out |= {f"[{table}] {line}" for line in _toml_values(body)}
        elif kind == "pyproject" and table == "project":
            out |= {f"[project] {line}" for line in _toml_values(_toml_key(body, "dependencies"))}
            out |= {f"[project] {line}" for line in _toml_values(_toml_key(body, "optional-dependencies"))}
    return out


def _lines(text: str, comment: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.split(f" {comment}", 1)[0].strip() if not raw.lstrip().startswith(comment) else ""
        if line:
            out.append(" ".join(line.split()))
    return out


TABLE_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _toml_tables(text: str) -> list[tuple[str, str]]:
    """(table name, its text) for each table of a TOML file, "" for the top level. Enough of TOML for comparing
    entries: it reads no values, so it needs no TOML parser (Python 3.10 has none)."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        m = TABLE_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            out.append((m.group(1).replace('"', "").replace("'", "").replace(" ", ""), []))
        else:
            out[-1][1].append(line)
    return [(name, "\n".join(lines)) for name, lines in out]


def _toml_key(body: str, key: str) -> str:
    """The text of one key's value in a table, an array or inline table over several lines included."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*)$", line)
        if not m:
            continue
        value, depth = [m.group(1)], _depth(m.group(1))
        j = i + 1
        while depth > 0 and j < len(lines):
            value.append(lines[j])
            depth += _depth(lines[j])
            j += 1
        return "\n".join(value)
    return ""


def _depth(line: str) -> int:
    return line.count("[") + line.count("{") - line.count("]") - line.count("}")


def _toml_values(body: str) -> set[str]:
    """Each non-empty, non-comment line of a table or value, with array punctuation trimmed: one entry per line in
    the way these files are written."""
    out = set()
    for line in _lines(body, "#"):
        line = line.strip()
        while _depth(line) < 0 and line[-1:] in ("]", "}"):  # the closing bracket of an array on an entry's line
            line = line[:-1].rstrip()
        line = line.rstrip(",").strip()
        if line not in ("[", "]", "{", "}", ""):
            out.add(line)
    return out


def _npm(text: str) -> set[str]:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return {"package.json: (not valid JSON)"}
    if not isinstance(data, dict):
        return set()
    out = set()
    for key in NPM_MAPS:
        value = data.get(key)
        if isinstance(value, dict):
            out |= {f"{key}: {name} {json.dumps(spec, sort_keys=True)}" for name, spec in value.items()}
        elif isinstance(value, list):
            out |= {f"{key}: {name}" for name in value}
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        out |= {f"scripts: {name}: {scripts[name]}" for name in NPM_INSTALL_SCRIPTS if name in scripts}
    return out


def _go(text: str) -> set[str]:
    out, block = set(), None
    for line in _lines(text, "//"):
        if block:
            if line == ")":
                block = None
            else:
                out.add(f"{block} {line}")
            continue
        m = re.match(r"^(require|replace)\s*(\(|.+)$", line)
        if m:
            if m.group(2) == "(":
                block = m.group(1)
            else:
                out.add(f"{m.group(1)} {m.group(2)}")
    return out


def _setup_cfg(text: str) -> set[str]:
    out, section, key = set(), "", None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith(("#", ";")) else ""
        if not line.strip():
            continue
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            section, key = m.group(1).strip(), None
            continue
        continuation = line[:1].isspace()
        if section == "options.extras_require":
            out.add(f"[{section}] {' '.join(line.split())}")
        elif section == "options" and not continuation:
            key = line if SETUP_CFG_KEYS.match(line.strip()) else None
            if key:
                out.add(f"[options] {' '.join(line.split())}")
        elif section == "options" and continuation and key:
            out.add(f"[options] {key.split('=')[0].strip()}: {line.strip()}")
    return out
