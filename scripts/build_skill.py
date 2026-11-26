"""Assemble the Claude skill folder and zip from the canonical prompts, spec and templates.

Usage: python scripts/build_skill.py  ->  dist/plan-project/ and dist/plan-project.zip
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "prompts" / "claude-skill" / "plan-project"
DIST = ROOT / "dist"
OUT = DIST / "plan-project"


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    refs = OUT / "references"
    refs.mkdir(parents=True)
    shutil.copy2(SRC / "SKILL.md", OUT / "SKILL.md")
    shutil.copy2(ROOT / "spec" / "PACKAGE_SPEC.md", refs / "PACKAGE_SPEC.md")
    shutil.copytree(ROOT / "prompts" / "stages", refs / "stages")
    shutil.copytree(ROOT / "src" / "idea_to_scaffold" / "templates" / "package", refs / "templates")
    shutil.copytree(ROOT / "src" / "idea_to_scaffold" / "templates" / "lite", refs / "templates-lite")
    archive = shutil.make_archive(str(DIST / "plan-project"), "zip", DIST, "plan-project")
    print(f"built {OUT} and {archive}")


if __name__ == "__main__":
    main()
