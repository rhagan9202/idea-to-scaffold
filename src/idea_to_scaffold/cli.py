"""Command line interface: `its new | import | validate | scaffold`."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from .backlog import load_backlog
from .importer import ImportConflict, import_export
from .linker import link_package
from .package import BACKLOG_STATUSES, Package, PackageError, blank_package_dir
from .scaffold import ScaffoldError, scaffold
from .validate import validate


def cmd_new(args: argparse.Namespace) -> int:
    dest = Path(args.dir)
    if dest.exists() and any(dest.iterdir()):
        print(f"error: {dest} exists and is not empty", file=sys.stderr)
        return 1
    shutil.copytree(blank_package_dir(), dest, dirs_exist_ok=True)
    print(f"created blank planning package in {dest}")
    print("next: fill it in with the prompts in prompts/stages/, or run the plan-project skill")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        written, seeded = import_export(
            Path(args.export), Path(args.into), seed=not args.no_defaults, overwrite=args.overwrite
        )
    except (ValueError, OSError, ImportConflict) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"imported {len(written)} new or changed file(s) into {args.into}" + (":" if written else ""))
    for rel in written:
        print(f"  {rel}")
    if seeded:
        print(f"added {len(seeded)} default file(s): {', '.join(seeded)}")
    try:
        changed = link_package(Package.load(Path(args.into)))
        print(f"cross-linked {len(changed)} file(s) and wrote planning/README.md")
    except PackageError as exc:
        print(f"warning: could not cross-link: {exc}")
    return cmd_validate(argparse.Namespace(dir=args.into))


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        pkg = Package.load(Path(args.dir))
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    report = validate(pkg)
    for e in report.errors:
        print(f"ERROR   {e}")
    for w in report.warnings:
        print(f"warning {w}")
    if report.valid and pkg.repo_layout:
        print("planning docs are valid")
    elif report.ready:
        print("package is valid and ready to scaffold")
    elif report.valid:
        print("package is valid, but not ready to scaffold yet")
    else:
        print(f"package has {len(report.errors)} error(s)")
    return 0 if report.valid else 1


def _shown(pkg: Package, rel: str) -> str:
    """A logical path as the user sees it on disk (docs/planning/... in a scaffolded repo)."""
    try:
        return pkg.path_of(rel).relative_to(pkg.root).as_posix()
    except ValueError:
        return rel


def cmd_link(args: argparse.Namespace) -> int:
    try:
        pkg = Package.load(Path(args.dir))
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        changed = link_package(pkg, check=args.check)
    except UnicodeDecodeError as exc:
        print(f"error: a planning file is not valid UTF-8 text ({exc.reason})", file=sys.stderr)
        return 1
    changed = [_shown(pkg, rel) for rel in changed]
    if args.check:
        if changed:
            print("cross-links are out of date in:")
            for rel in changed:
                print(f"  {rel}")
            print("run `its link` and commit the result")
            return 1
        print("cross-links are up to date")
        return 0
    print(f"updated {len(changed)} file(s)" if changed else "already up to date")
    for rel in changed:
        print(f"  {rel}")
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    try:
        pkg = Package.load(Path(args.dir))
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    items = load_backlog(pkg)
    if args.status:
        items = [i for i in items if i.status == args.status]
    if not items:
        print(f"no backlog items with status {args.status}" if args.status else "no backlog items")
        return 0
    id_width = max(8, *(len(i.id) + 1 for i in items))
    print(f"{'ID':<{id_width}} {'STATUS':<12} {'MILESTONE':<10} TITLE")
    for i in items:
        print(f"{i.id:<{id_width}} {i.status:<12} {i.milestone or '—':<10} {i.title}")
    return 0


def _confirm_delete(target: Path, assume_yes: bool) -> bool:
    count = sum(1 for p in target.rglob("*") if p.is_file())
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            f"error: --force would delete {target} ({count} files). Re-run with --yes to confirm non-interactively.",
            file=sys.stderr,
        )
        return False
    answer = input(f"--force will delete {target} and everything in it ({count} files). Continue? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def cmd_scaffold(args: argparse.Namespace) -> int:
    if args.no_git and args.github is not None:
        print("error: --github needs git; drop --no-git or --github", file=sys.stderr)
        return 2
    github = None
    if args.github is not None:
        github = args.github  # "" means use the slug
    if args.force:
        try:
            slug = Package.load(Path(args.package)).slug
        except PackageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        target = Path(args.out) / slug
        if target.exists() and any(target.iterdir()) and not _confirm_delete(target, args.yes):
            print("aborted: nothing was changed", file=sys.stderr)
            return 1
    try:
        result = scaffold(
            Path(args.package),
            Path(args.out),
            force=args.force,
            git=not args.no_git,
            github=github,
            allow_unready=args.allow_unready,
        )
    except (ScaffoldError, PackageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"scaffolded {result.path} ({len(result.files)} files)")
    for w in result.warnings:
        print(f"warning {w}")
    if result.report and result.report.warnings:
        for w in result.report.warnings:
            print(f"warning {w}")
    if result.committed:
        print("git: initialised on main with the initial commit")
    if result.remote:
        print(f"github: pushed to {result.remote}")
    print(f"next: open {result.path / 'HANDOFF.md'} and give its kick-off prompt to your agent")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="its", description="idea-to-scaffold: planning package to agent-ready repo")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("new", help="create a blank planning package")
    s.add_argument("dir")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("import", help="import a chat export (Markdown with file markers) into a package")
    s.add_argument("export", help="the exported chat .md file")
    s.add_argument("--into", required=True, help="package folder to write into")
    s.add_argument("--no-defaults", action="store_true", help="don't add default playbooks and ADR-0001")
    s.add_argument(
        "--overwrite", action="store_true",
        help="replace existing files that differ from the export (keeps each original as <name>.orig)",
    )
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("validate", help="check a package (or scaffolded repo) against the spec")
    s.add_argument("dir", nargs="?", default=".")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("link", help="cross-link IDs, refresh nav blocks and the planning index")
    s.add_argument("dir", nargs="?", default=".", help="package folder or scaffolded repo (default: .)")
    s.add_argument("--check", action="store_true", help="don't write; exit 1 if anything is out of date (for CI)")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("backlog", help="list backlog items")
    s.add_argument("dir", nargs="?", default=".", help="package folder or scaffolded repo (default: .)")
    s.add_argument("--status", choices=BACKLOG_STATUSES, help="only items with this status")
    s.set_defaults(func=cmd_backlog)

    s = sub.add_parser("scaffold", help="create the project repo from a package")
    s.add_argument("package", help="planning package folder")
    s.add_argument("--out", default=".", help="parent folder for the new repo (default: current)")
    s.add_argument("--force", action="store_true", help="delete and replace an existing non-empty target folder")
    s.add_argument("--yes", "-y", action="store_true", help="don't ask before --force deletes the target folder")
    s.add_argument("--no-git", action="store_true", help="skip git init and the initial commit")
    s.add_argument(
        "--github", nargs="?", const="", default=None, metavar="OWNER/NAME",
        help="also create a private GitHub repo with gh and push (default name: the slug)",
    )
    s.add_argument("--allow-unready", action="store_true", help="scaffold even if key docs aren't approved")
    s.set_defaults(func=cmd_scaffold)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
