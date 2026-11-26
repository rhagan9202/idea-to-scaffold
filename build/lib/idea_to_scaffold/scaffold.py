"""Create a project repository from a planning package."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from .package import GENERATED_MARKER, Package, parse_front_matter, templates_dir
from .validate import Report, validate


class ScaffoldError(Exception):
    pass


@dataclass
class Result:
    path: Path
    files: list[str] = field(default_factory=list)
    report: Report | None = None
    committed: bool = False
    remote: str | None = None


def _marker(source: str) -> str:
    return GENERATED_MARKER.format(source=source)


def _variables(pkg: Package) -> dict[str, str]:
    runtime = str(pkg.get("stack.runtime", "3.12"))
    cmds = pkg.commands
    return {
        "name": pkg.name,
        "slug": pkg.slug,
        "summary": pkg.summary,
        "module": pkg.slug.replace("-", "_"),
        "runtime": runtime,
        "runtime_nodot": runtime.replace(".", ""),
        "cmd_setup": cmds.get("setup", "true"),
        "cmd_test": cmds.get("test", "true"),
        "cmd_lint": cmds.get("lint", "true"),
        "cmd_run": cmds.get("run", ""),
    }


def _write(root: Path, rel: str, content: str, written: list[str]) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    written.append(rel)


def _copy_tree(src: Path, dest_root: Path, dest_prefix: str, written: list[str]) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_file():
            rel = f"{dest_prefix}/{path.relative_to(src).as_posix()}"
            target = dest_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            written.append(rel)


def _render_stack(pkg: Package, root: Path, written: list[str]) -> None:
    stacks = templates_dir() / "stacks"
    stack = stacks / pkg.language
    if not stack.is_dir():
        stack = stacks / "generic"
    variables = _variables(pkg)
    for path in sorted(stack.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(stack).as_posix().replace("__module__", variables["module"])
        text = path.read_text(encoding="utf-8")
        _write(root, rel, Template(text).safe_substitute(variables), written)


def _fill_agents_md(pkg: Package, text: str) -> str:
    """Replace the template's angle-bracket placeholders with manifest values, leaving real content alone."""
    cmds = pkg.commands
    replacements = {
        "<Project Name>": pkg.name,
        "<language>": str(pkg.get("stack.language", "")),
        "<runtime>": str(pkg.get("stack.runtime", "")),
        "<framework>": str(pkg.get("stack.framework", "") or "none"),
        "<setup>": cmds.get("setup", ""),
        "<test>": cmds.get("test", ""),
        "<lint>": cmds.get("lint", ""),
        "<run>": cmds.get("run", ""),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _adapters(pkg: Package, root: Path, written: list[str]) -> None:
    agents = set(pkg.agents)
    if "claude" in agents:
        _write(root, "CLAUDE.md", f"{_marker('AGENTS.md')}\n@AGENTS.md\n", written)
        for playbook in pkg.playbooks():
            fm, _ = parse_front_matter(playbook.read_text(encoding="utf-8"))
            name = str(fm.get("id") or playbook.stem)
            description = str(fm.get("description") or fm.get("title") or name).replace("\n", " ")
            body = (
                "---\n"
                f"name: {name}\n"
                f"description: {json.dumps(description, ensure_ascii=False)}\n"
                "---\n"
                f"{_marker(f'docs/playbooks/{playbook.name}')}\n"
                f"Follow the procedure in `docs/playbooks/{playbook.name}`, and the rules in `AGENTS.md`.\n"
            )
            _write(root, f".claude/skills/{name}/SKILL.md", body, written)
    if "copilot" in agents:
        _write(
            root,
            ".github/copilot-instructions.md",
            f"{_marker('AGENTS.md')}\n"
            "Follow the instructions in [AGENTS.md](../AGENTS.md) at the repository root.\n"
            "Planning docs in `docs/planning/` are the source of truth; procedures are in `docs/playbooks/`.\n",
            written,
        )
    if "cursor" in agents:
        _write(
            root,
            ".cursor/rules/00-project.mdc",
            "---\ndescription: Project-wide rules\nalwaysApply: true\n---\n"
            f"{_marker('AGENTS.md')}\n"
            "Follow @AGENTS.md. Planning docs in `docs/planning/` are the source of truth; "
            "procedures are in `docs/playbooks/`.\n",
            written,
        )
    # Codex reads AGENTS.md natively; nothing to generate.


def _task_section(pkg: Package, task_id: str) -> str:
    plan = pkg.root / "planning/06-project-plan.md"
    if not plan.is_file():
        return ""
    _, body = parse_front_matter(plan.read_text(encoding="utf-8"))
    match = re.search(rf"^(#+)\s+{re.escape(task_id)}\b.*$", body, re.MULTILINE)
    if not match:
        return ""
    level = len(match.group(1))
    rest = body[match.end():]
    end = re.search(rf"^#{{1,{level}}}\s", rest, re.MULTILINE)
    section = match.group(0) + rest[: end.start() if end else len(rest)]
    return section.strip()


def _handoff(pkg: Package, report: Report) -> str:
    first = str(pkg.get("first_task", "T-1.1"))
    task = _task_section(pkg, first) or f"(Task {first} was not found in docs/planning/06-project-plan.md.)"
    task = re.sub(r"^#+\s+", "### ", task, count=1)
    cmds = pkg.commands
    cmd_lines = "\n".join(f"- **{k.title()}:** `{v}`" for k, v in cmds.items()) or "- (none set in package.yaml)"
    open_items = "\n".join(f"- {w}" for w in report.warnings) or "- None. All planning docs are approved."
    prompt = f"Read HANDOFF.md and AGENTS.md, then follow docs/playbooks/implement-task.md for {first}."
    return f"""# Handoff: {pkg.name}

{pkg.summary}

This repository was scaffolded from an approved planning package. Everything an
agent needs is in the repo; there is no other context.

## Read first
1. `AGENTS.md`: rules for every agent
2. `docs/planning/`: the plan, in numbered order (source of truth)
3. `docs/playbooks/`: repeatable procedures

## Commands
{cmd_lines}

## Start here
{task}

## Kick-off prompt
Use the same prompt with any agent:

> {prompt}

- **Codex CLI:** `codex "{prompt}"`
- **Claude Code:** `claude "{prompt}"` (playbooks are also available as skills)
- **Copilot:** open an issue with the prompt above and assign it to Copilot, or paste it into Copilot Chat in agent mode
- **Cursor:** paste the prompt into the Agent panel

## Open items from planning
{open_items}
"""


def _readme(pkg: Package) -> str:
    return f"""# {pkg.name}

{pkg.summary}

- Plan and requirements: [`docs/planning/`](docs/planning/)
- Agent instructions: [`AGENTS.md`](AGENTS.md)
- Getting started for agents: [`HANDOFF.md`](HANDOFF.md)
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _git_commit(root: Path, message: str) -> None:
    if _git(root, "init", "-b", "main").returncode != 0:
        # Older git without -b
        _git(root, "init")
        _git(root, "checkout", "-b", "main")
    _git(root, "add", "-A")
    identity: list[str] = []
    if not _git(root, "config", "user.email").stdout.strip():
        identity = ["-c", "user.name=idea-to-scaffold", "-c", "user.email=idea-to-scaffold@localhost"]
    result = subprocess.run(
        ["git", *identity, "commit", "-m", message], cwd=root, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ScaffoldError(f"git commit failed: {result.stderr.strip() or result.stdout.strip()}")


def scaffold(
    package_dir: Path,
    out_dir: Path,
    *,
    force: bool = False,
    git: bool = True,
    github: str | None = None,
    allow_unready: bool = False,
) -> Result:
    pkg = Package.load(Path(package_dir))
    report = validate(pkg)
    if not report.valid:
        raise ScaffoldError("package is not valid:\n  " + "\n  ".join(report.errors))
    if not report.ready and not allow_unready:
        raise ScaffoldError(
            "package is valid but not ready (vision, features and requirements must be approved). "
            "Use --allow-unready to scaffold anyway.\n  " + "\n  ".join(report.warnings)
        )

    root = Path(out_dir) / pkg.slug
    if root.exists() and any(root.iterdir()):
        if not force:
            raise ScaffoldError(f"{root} already exists and is not empty (use --force to replace it)")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    result = Result(path=root, report=report)
    written = result.files

    _render_stack(pkg, root, written)
    _copy_tree(pkg.root / "planning", root, "docs/planning", written)
    shutil.copy2(pkg.root / "package.yaml", root / "docs/planning/package.yaml")
    written.append("docs/planning/package.yaml")
    if (pkg.root / "playbooks").is_dir():
        _copy_tree(pkg.root / "playbooks", root, "docs/playbooks", written)
    _write(root, "AGENTS.md", _fill_agents_md(pkg, (pkg.root / "AGENTS.md").read_text(encoding="utf-8")), written)
    _adapters(pkg, root, written)
    _write(root, "HANDOFF.md", _handoff(pkg, report), written)
    if not (root / "README.md").exists():
        _write(root, "README.md", _readme(pkg), written)

    if git:
        _git_commit(
            root,
            f"chore: scaffold {pkg.slug} from planning package\n\n"
            "Generated by idea-to-scaffold. Planning docs in docs/planning/ are the source of truth.",
        )
        result.committed = True
        if github is not None:
            repo = github or pkg.slug
            proc = subprocess.run(
                ["gh", "repo", "create", repo, "--private", "--source", ".", "--remote", "origin", "--push"],
                cwd=root, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise ScaffoldError(
                    "scaffold and commit succeeded, but creating the GitHub repo failed: "
                    + (proc.stderr.strip() or proc.stdout.strip())
                )
            result.remote = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else repo
    return result
