"""Command line interface: `its new | import | validate | scaffold`."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from .importer import import_export
from .linker import backlog_items, link_package
from .package import Package, PackageError, blank_package_dir
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
        written, seeded = import_export(Path(args.export), Path(args.into), seed=not args.no_defaults)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"imported {len(written)} file(s) into {args.into}:")
    for rel in written:
        print(f"  {rel}")
    if seeded:
        print(f"added {len(seeded)} default file(s): {', '.join(seeded)}")
    try:
        changed = link_package(Package.load(Path(args.into)))
        print(f"cross-linked {len(changed)} file(s) and wrote planning/README.md")
    except PackageError as exc:
        print(f"warning: could not cross-link: {exc}")
    return cmd_validate(argparse.Namespace(dir=args.into, quiet=True))


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
    if report.ready:
        print("package is valid and ready to scaffold")
    elif report.valid:
        print("package is valid, but not ready to scaffold yet")
    else:
        print(f"package has {len(report.errors)} error(s)")
    return 0 if report.valid else 1


def cmd_link(args: argparse.Namespace) -> int:
    try:
        pkg = Package.load(Path(args.dir))
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    changed = link_package(pkg, check=args.check)
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
    items = backlog_items(pkg)
    if args.status:
        items = [i for i in items if i["status"] == args.status]
    if not items:
        print("no backlog items")
        return 0
    width = max(len(i["title"]) for i in items)
    print(f"{'ID':<8} {'STATUS':<12} {'MILESTONE':<10} TITLE")
    for i in items:
        print(f"{i['id']:<8} {i['status']:<12} {i['milestone'] or '—':<10} {i['title'][:width]}")
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    github = None
    if args.github is not None:
        github = args.github  # "" means use the slug
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
    s.add_argument("--status", help="only items with this status")
    s.set_defaults(func=cmd_backlog)

    s = sub.add_parser("scaffold", help="create the project repo from a package")
    s.add_argument("package", help="planning package folder")
    s.add_argument("--out", default=".", help="parent folder for the new repo (default: current)")
    s.add_argument("--force", action="store_true", help="replace an existing non-empty target folder")
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
