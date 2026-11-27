"""Create a project repository from a planning package."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from .gate import DEFAULT_MODES
from .linker import link_package
from .package import GENERATED_MARKER, Package, parse_front_matter, templates_dir
from .validate import Report, validate

GATE_BLOCK = (
    "\n# The done gate. A new scaffold enforces every check, including the ones that start in warn mode for older\n"
    "# projects. Set one to warn (reported, never refusing) if it misfires on honest work here.\n"
    "gate:\n  checks:\n" + "".join(f"    {kind}: enforce\n" for kind in sorted(DEFAULT_MODES))
)

ITS_REPO = "https://github.com/rhagan9202/idea-to-scaffold"  # where scaffolded CI installs `its` from
ITS_SOURCE = f"git+{ITS_REPO}"


def _its_commit() -> str | None:
    """The commit of the `its` running now, when it can be known: from pip's record of a git install, or from the
    git checkout an editable install runs from (only when that checkout is clean and its origin is ITS_REPO)."""
    from importlib import metadata
    try:
        dist = metadata.distribution("idea-to-scaffold")
        raw = dist.read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        raw = None
    if raw:
        try:
            commit = json.loads(raw).get("vcs_info", {}).get("commit_id")
        except (ValueError, AttributeError):
            commit = None
        if commit:
            return str(commit)
    here = Path(__file__).resolve().parent

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(["git", *args], cwd=here, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    origin = (git("remote", "get-url", "origin") or "").removesuffix(".git").rstrip("/")
    if not origin.replace(":", "/").endswith("/" + ITS_REPO.removeprefix("https://github.com/")):
        return None
    if git("status", "--porcelain", "--untracked-files=no") != "":
        return None
    head = git("rev-parse", "HEAD")
    contained = git("branch", "-r", "--contains", head) if head else None
    return head if contained else None  # only a commit that has been pushed is installable


def its_source(pkg: Package, warnings: list[str] | None = None) -> str:
    """What scaffolded CI installs `its` from: `its_source` in package.yaml, else ITS_SOURCE pinned to the commit
    of the `its` doing the scaffolding, so CI keeps checking with the same rules until someone moves the pin."""
    configured = pkg.get("its_source")
    if configured:
        return str(configured)
    commit = _its_commit()
    if commit:
        return f"{ITS_SOURCE}@{commit}"
    if warnings is not None:
        warnings.append("CI installs its unpinned, from the latest commit: set its_source in package.yaml to "
                        f"{ITS_SOURCE}@<tag or commit> to pin it")
    return ITS_SOURCE


class ScaffoldError(Exception):
    pass


@dataclass
class Result:
    path: Path
    files: list[str] = field(default_factory=list)
    report: Report | None = None
    committed: bool = False
    remote: str | None = None
    warnings: list[str] = field(default_factory=list)


def _marker(source: str) -> str:
    return GENERATED_MARKER.format(source=source)


def _variables(pkg: Package, warnings: list[str] | None = None) -> dict[str, str]:
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
        "its_source": its_source(pkg, warnings),
    }


def _write(root: Path, rel: str, content: str, written: list[str]) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    written.append(rel)


def _copy_tree(src: Path, dest_root: Path, dest_prefix: str, written: list[str]) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_file() and path.suffix != ".orig" and path.name != "backlog.json":  # the mirror is generated
            rel = f"{dest_prefix}/{path.relative_to(src).as_posix()}"
            target = dest_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            written.append(rel)


def available_stacks() -> list[str]:
    stacks = templates_dir() / "stacks"
    return sorted(p.name for p in stacks.iterdir() if p.is_dir())


def _render_stack(pkg: Package, root: Path, written: list[str], warnings: list[str]) -> None:
    stacks = templates_dir() / "stacks"
    stack = stacks / pkg.language
    if not stack.is_dir():
        warnings.append(
            f"no skeleton for stack.language {pkg.language!r} (available: {', '.join(available_stacks())}); "
            "used the generic skeleton (empty src/ and tests/, no build or CI files)"
        )
        stack = stacks / "generic"
    pin_warnings: list[str] = []
    variables = _variables(pkg, pin_warnings)
    for path in sorted(stack.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(stack).as_posix().replace("__module__", variables["module"])
        if rel.endswith(".tmpl"):
            rel = rel[: -len(".tmpl")]
        text = path.read_text(encoding="utf-8")
        if "${its_source}" in text:
            warnings.extend(w for w in pin_warnings if w not in warnings)  # only when CI installs its
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


def _handoff(pkg: Package, report: Report) -> str:
    """The first-session brief. It names no item's status: `its next` answers that live, so this file never goes
    stale as the backlog moves."""
    first = str(pkg.get("first_task") or "").strip()
    cmds = pkg.commands
    cmd_lines = "\n".join(f"- **{k.title()}:** `{v}`" for k, v in cmds.items()) or "- (none set in package.yaml)"
    open_items = "\n".join(f"- {w}" for w in report.warnings) or "- None."
    if first:
        pick = f"Start with {first}, as the plan asks."
        prompt = f"Read HANDOFF.md and AGENTS.md, then follow docs/playbooks/implement-task.md for {first}."
    else:
        pick = "Work the backlog in order:"
        prompt = (
            "Read HANDOFF.md and AGENTS.md, run its next to pick the next ready backlog item, "
            "then follow docs/playbooks/implement-task.md for it."
        )
    return f"""# Handoff: {pkg.name}

{pkg.summary}

This repository was scaffolded from an approved planning package. Everything an
agent needs is in the repo; there is no other context.

## Read first
1. `AGENTS.md`: rules for every agent
2. `docs/planning/README.md`: the planning index. It links every doc and says where each ID is defined
3. `docs/planning/07-backlog.md`: the only list of work items
4. `docs/playbooks/`: repeatable procedures

## Commands
{cmd_lines}

## Start here
{pick}

- `its next` prints the next ready item: the first `todo` whose dependencies are `done` (`--json` for agents).
- `its start <ID>` marks it `in-progress`; `its done <ID>` runs its checks and marks it `done`.
- Without `its`, read `docs/planning/07-backlog.md` and apply the same rule by hand.

## Kick-off prompt
Use the same prompt with any agent:

> {prompt}

- **Codex CLI:** `codex "{prompt}"`
- **Claude Code:** `claude "{prompt}"` (playbooks are also available as skills)
- **Copilot:** open an issue with the prompt above and assign it to Copilot, or paste it into Copilot Chat in agent mode
- **Cursor:** paste the prompt into the Agent panel

## Open items when this repo was scaffolded
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
    hooks: bool = False,
) -> Result:
    pkg = Package.load(Path(package_dir))
    report = validate(pkg)
    if not report.valid:
        raise ScaffoldError("package is not valid:\n  " + "\n  ".join(report.errors))
    if not report.ready and not allow_unready:
        names = ", ".join(rel.split("/")[-1] for rel in pkg.ready_requires_approved)
        raise ScaffoldError(
            f"package is valid but not ready ({names} must be approved). "
            "Use --allow-unready to scaffold anyway.\n  " + "\n  ".join(report.warnings)
        )

    root = Path(out_dir) / pkg.slug
    src_root = pkg.root.resolve()
    target = root.resolve()
    if target == src_root or src_root.is_relative_to(target) or target.is_relative_to(src_root):
        raise ScaffoldError(
            f"the target {root} overlaps the package being scaffolded ({pkg.root}); choose a different --out"
        )
    if root.exists() and any(root.iterdir()):
        if not force:
            raise ScaffoldError(f"{root} already exists and is not empty (use --force to replace it)")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    result = Result(path=root, report=report)
    written = result.files

    _render_stack(pkg, root, written, result.warnings)
    _copy_tree(pkg.planning_dir, root, "docs/planning", written)
    if "docs/planning/package.yaml" not in written:
        shutil.copy2(pkg.manifest_path, root / "docs/planning/package.yaml")
        written.append("docs/planning/package.yaml")
    if "gate" not in pkg.manifest:
        manifest = root / "docs/planning/package.yaml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text + ("" if text.endswith("\n") else "\n") + GATE_BLOCK, encoding="utf-8")
    if pkg.playbooks_dir.is_dir():
        _copy_tree(pkg.playbooks_dir, root, "docs/playbooks", written)
    agents_md = pkg.path_of("AGENTS.md").read_text(encoding="utf-8")
    _write(root, "AGENTS.md", _fill_agents_md(pkg, agents_md), written)
    _adapters(pkg, root, written)
    repo_pkg = Package.load(root)
    link_package(repo_pkg)  # cross-link docs in their repo locations and write docs/planning/README.md
    if "docs/planning/README.md" not in written:
        written.append("docs/planning/README.md")
    _write(root, "HANDOFF.md", _handoff(repo_pkg, report), written)
    if not (root / "README.md").exists():
        _write(root, "README.md", _readme(pkg), written)
    if hooks:
        from .hooks import hook_agents, install_hooks
        written += install_hooks(root, hook_agents(pkg))["written"]

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
