"""The one meaning of a Touches entry, shared by the done gate, `its land` and `its drift`.

An entry names files relative to the package root (the repository root in a scaffolded repo):

- A plain path names that file, or everything under it if it is a folder: `src/store.py`, `src/store/`.
- A glob uses `*` (any characters except `/`), `**` (any characters, `/` included), `?` (one character other than
  `/`), `[abc]` (one of the characters) and `{a,b}` (either alternative). A glob that names a folder covers
  everything under it too, so `src/*` covers `src/store/db.py`.
- An entry is also tried as a literal path, so a folder really named `app/[id]/` works without escaping.
"""
from __future__ import annotations

import functools
import re

GLOB_CHARS = re.compile(r"[*?\[{]")


def norm(entry: str) -> str:
    entry = entry.replace("\\", "/").strip()
    while entry.startswith("./"):
        entry = entry[2:]
    return entry


def _translate(glob: str) -> str:
    out, i, n = [], 0, len(glob)
    while i < n:
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            end = glob.find("]", i + 2 if glob.startswith("[!", i) or glob.startswith("[^", i) else i + 1)
            if end == -1:
                out.append(re.escape(c))
                i += 1
                continue
            body = glob[i + 1:end]
            if body[:1] in "!^":
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        elif c == "{":
            end = glob.find("}", i)
            if end == -1:
                out.append(re.escape(c))
                i += 1
                continue
            out.append("(?:" + "|".join(_translate(alt) for alt in glob[i + 1:end].split(",")) + ")")
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


@functools.lru_cache(maxsize=1024)
def _pattern(entry: str) -> re.Pattern[str]:
    return re.compile(_translate(entry.rstrip("/")) + "(?:/.*)?")


def matches(path: str, entry: str) -> bool:
    """Whether a package-relative path is covered by one Touches entry."""
    entry = norm(entry)
    if not entry:
        return False
    literal = entry.rstrip("/")
    if path == literal or path.startswith(literal + "/") or literal == ".":
        return True
    if GLOB_CHARS.search(entry):
        try:
            return _pattern(entry).fullmatch(path) is not None
        except re.error:
            return False
    return False


def covered(path: str, touches: list[str]) -> bool:
    """Whether any of an item's Touches covers the path."""
    return any(matches(path, entry) for entry in touches)
