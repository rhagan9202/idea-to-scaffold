"""The JSON contract: every error code the source raises is registered in contract.py with the exit codes it is raised
with, and the spec's error and exit-code tables are the ones contract.py generates."""
from __future__ import annotations

import ast
from pathlib import Path

from idea_to_scaffold import contract, gate, work
from idea_to_scaffold.contract import ERRORS, EXIT_CODES, EXIT_REFUSED

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "idea_to_scaffold"
SPEC = ROOT / "spec" / "PACKAGE_SPEC.md"
EXIT_NAMES = {name: value for name, value in vars(contract).items() if name.startswith("EXIT_") and name != "EXIT_CODES"}
# Raises whose code isn't a literal: gate_error() returns one of gate.REFUSALS, and apply_result re-raises a
# ResultError's code.
COMPUTED = {"code", "exc.code"}


def _raises(cls: str) -> list[tuple[str, set[int], str]]:
    """(code, exit codes, where) for each `cls(...)` call in the source. A code that isn't a literal is the
    unparsed expression, such as "code" or "exc.code"; a literal code is never one of those (they aren't codes)."""
    out = []
    for path in sorted(p for p in SRC.rglob("*.py") if "templates" not in p.relative_to(SRC).parts):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            func = getattr(node, "func", None)
            if not isinstance(node, ast.Call) or getattr(func, "id", getattr(func, "attr", None)) != cls:
                continue
            first = node.args[0]
            code = first.value if isinstance(first, ast.Constant) else ast.unparse(first)
            exit_node = None
            if cls == "WorkError":  # a ResultError has no exit code: apply_result raises it as a WorkError
                exit_node = node.args[2] if len(node.args) > 2 else next(
                    (k.value for k in node.keywords if k.arg == "exit_code"), None)
            if exit_node is None:  # the code's usual exit, from the registry
                exits = {ERRORS[code].exits[0]} if code in ERRORS else {contract.EXIT_ERROR}
            else:
                exits = _exit_values(exit_node, f"{path.name}:{node.lineno}")
            out.append((code, exits, f"{path.name}:{node.lineno}"))
    return out


def _exit_values(node: ast.AST, where: str) -> set[int]:
    values = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id.startswith("EXIT_"):
            values.add(EXIT_NAMES[sub.id])
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, int) and not isinstance(sub.value, bool):
            values.add(sub.value)
    assert values, f"{where}: can't tell the exit code of {ast.unparse(node)}"
    return values


def test_every_raised_error_code_is_registered_with_its_exit_codes():
    problems = []
    for code, exits, where in _raises("WorkError"):
        if code in COMPUTED:
            continue
        if code not in ERRORS:
            problems.append(f"{where}: {code} isn't in contract.ERRORS")
        elif not exits <= set(ERRORS[code].exits):
            problems.append(f"{where}: {code} exits {sorted(exits)}, but the registry says {ERRORS[code].exits}")
    computed = {code for code, _, _ in _raises("WorkError") if not code.isidentifier() or code in COMPUTED}
    assert computed <= COMPUTED, f"a WorkError with a computed code the test doesn't know: {computed - COMPUTED}"
    for code, _, where in _raises("ResultError"):
        if code not in ERRORS:
            problems.append(f"{where}: the result error {code} isn't in contract.ERRORS")
    for code in gate.REFUSALS:
        if ERRORS.get(code) is None or ERRORS[code].exits != (EXIT_REFUSED,):
            problems.append(f"the gate refusal {code} must be registered with exit {EXIT_REFUSED}")
    assert not problems, "\n".join(problems)


def test_every_registered_code_is_raised_somewhere():
    raised = {code for code, _, _ in _raises("WorkError") + _raises("ResultError")} | set(gate.REFUSALS)
    assert not set(ERRORS) - raised, f"registered but never raised: {sorted(set(ERRORS) - raised)}"


def test_every_exit_code_has_a_meaning():
    assert set(EXIT_NAMES.values()) == set(EXIT_CODES)
    assert {e for entry in ERRORS.values() for e in entry.exits} <= set(EXIT_CODES)
    assert all(entry.meaning and not entry.meaning.endswith(".") for entry in ERRORS.values())


def test_the_spec_tables_are_generated_from_the_contract():
    text = SPEC.read_text(encoding="utf-8")
    regions = contract.spec_regions(text)
    assert None not in regions.values(), f"the spec is missing a table's markers: {regions}"
    assert regions["errors"] == contract.error_table()
    assert regions["exit-codes"] == contract.exit_table()
    assert contract.render_spec(text) == text, "run python -m idea_to_scaffold.contract spec/PACKAGE_SPEC.md"


def test_the_generator_rewrites_and_checks(tmp_path):
    spec = tmp_path / "SPEC.md"
    stale = ("# Spec\n\n<!-- contract:errors -->\nold\n<!-- /contract:errors -->\n\ntext between\n\n"
             "<!-- contract:exit-codes -->\n| 0 | ok |\n<!-- /contract:exit-codes -->\n")
    spec.write_text(stale, encoding="utf-8")
    assert contract.main([str(spec), "--check"]) == 1
    assert spec.read_text(encoding="utf-8") == stale  # --check writes nothing
    assert contract.main([str(spec)]) == 0
    text = spec.read_text(encoding="utf-8")
    assert "| `usage` | 2 | the command line is wrong |  |" in text and "text between" in text
    assert "| 7 | `land`" in text
    assert contract.main([str(spec), "--check"]) == 0
    spec.write_text("# no markers\n", encoding="utf-8")
    assert contract.main([str(spec)]) == 1
    assert contract.main([]) == 2


def test_work_still_exports_the_contract_names():
    assert work.WorkError is contract.WorkError and work.SCHEMA == contract.SCHEMA == 1
    assert work.EXIT_CHECK_FAILED == contract.EXIT_CHECK_FAILED
