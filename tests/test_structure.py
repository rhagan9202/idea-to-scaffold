"""The shape of the code: the CLI only parses and renders, every command goes through one service call, `its --help`
lists every command in one group, and the modules import each other without cycles."""
from __future__ import annotations

import ast
from pathlib import Path

from idea_to_scaffold.cli import build_parser
from idea_to_scaffold.cli.parser import GROUPS, HIDDEN

SRC = Path(__file__).resolve().parents[1] / "src" / "idea_to_scaffold"
CLI = SRC / "cli"
# what the CLI may import from the rest of the package: the service layer, the error contract, and the terminal test
CLI_MAY_IMPORT = {"service", "contract", "terminal", ""}  # "" is `from .. import __version__`


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_the_cli_imports_only_the_service_layer():
    for path in sorted(CLI.glob("*.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("idea_to_scaffold") for a in node.names), f"{path.name}: {ast.unparse(node)}"
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 2:  # the package
                module = node.module or ""
                assert module.split(".")[0] in CLI_MAY_IMPORT, f"cli/{path.name} imports ..{module}: go through service"
                if not module:  # `from .. import x`: x is a module, or the version
                    names = {a.name for a in node.names}
                    assert names <= CLI_MAY_IMPORT | {"__version__"}, f"cli/{path.name}: {ast.unparse(node)}"
            assert not (node.module or "").startswith("idea_to_scaffold"), f"cli/{path.name}: {ast.unparse(node)}"


def _service_calls(fn: ast.FunctionDef) -> set[str]:
    return {node.func.attr for node in ast.walk(fn) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "service"}


def test_every_command_is_one_service_call():
    commands = {n.name: n for n in _tree(CLI / "commands.py").body if isinstance(n, ast.FunctionDef)
                and n.name.startswith("cmd_")}
    parser = build_parser()
    registered = parser._subparsers._group_actions[0].choices  # noqa: SLF001 - the parser's own table
    for name, sub in registered.items():
        func = sub.get_default("func")
        assert func.__module__ == "idea_to_scaffold.cli.commands" and func.__name__ in commands, name
        calls = _service_calls(commands[func.__name__])
        assert calls, f"{name}: {func.__name__} calls no service function"
        # review has three modes (--schema, --reply, the prompt), each its own service call; every other command has one
        assert len(calls) == 1 or name == "review", f"{name}: {func.__name__} calls {sorted(calls)}"


def test_help_lists_every_command_in_one_group():
    registered = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    grouped = [name for _purpose, names in GROUPS.values() for name in names.split()]
    assert len(grouped) == len(set(grouped)), "a command is in two groups"
    assert set(grouped) | set(HIDDEN) == registered
    text = build_parser().format_help()
    for group in GROUPS:
        assert f"\n{group}: " in text
    for name in HIDDEN:
        assert f"\n  {name} " not in text


def test_the_modules_import_each_other_without_cycles():
    """Only imports at the top of a module count: a cycle among them fails when the package is imported."""
    graph: dict[str, set[str]] = {}
    for path in SRC.glob("*.py"):
        deps = set()
        for node in _tree(path).body:
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                deps |= {node.module.split(".")[0]} if node.module else {a.name for a in node.names}
        graph[path.stem] = deps & {p.stem for p in SRC.glob("*.py")}
    done: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        assert name not in stack, "import cycle: " + " -> ".join([*stack[stack.index(name):], name])
        if name in done:
            return
        for dep in sorted(graph[name]):
            visit(dep, [*stack, name])
        done.add(name)

    for name in sorted(graph):
        visit(name, [])
