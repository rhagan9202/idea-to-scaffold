"""`backlog.json`, the generated mirror of the backlog."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .package import Package
from .view import backlog_payload

BACKLOG_JSON = "planning/backlog.json"


def refresh_mirror(pkg: Package) -> None:
    """Keep `backlog.json` current after a status change, if someone generated it."""
    if pkg.path_of(BACKLOG_JSON).is_file():
        write_backlog_json(pkg)


def write_backlog_json(pkg: Package) -> Path:
    """Write the backlog mirror next to the backlog. It is generated, gitignored and never edited by hand."""
    path = pkg.path_of(BACKLOG_JSON)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # written whole, then swapped in: never half a file
    temp.write_text(json.dumps(backlog_payload(pkg), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)
    return path

