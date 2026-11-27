"""What a change does to a test file or to the test runner's configuration, for the done gate.

Python test files are read with `ast`, so comments, strings and formatting can't hide anything: an assertion turned
into `pass  # assert later` is a lost assertion, `pytestmark = pytest.mark.skip(...)` and `from unittest import
skip; @skip` are skips, `assert x or True` is a weakened assertion, and a test whose body starts with `return` is
disabled. Other languages fall back to line patterns, with comments stripped first.

Test configuration is compared too: a `conftest.py` that deselects or ignores tests, or a change to the pytest,
Jest or Vitest settings, can drop a failing test without touching it.
"""
from __future__ import annotations

import ast
import json
import posixpath
import re
from dataclasses import dataclass, field

SKIP_NAMES = {"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure", "importorskip"}
LINE_SKIP_RE = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail|importorskip)\(|\bpytestmark\s*="
    r"|@(?:pytest\.)?mark\.(skip|skipif|xfail)\b"
    r"|@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b|\.skipTest\("
    r"|\b(it|test|describe|context)\.(skip|todo)\(|\b(xit|xdescribe|xtest)\("
    r"|\bt\.Skip(Now|f)?\(|#\[ignore\]|@(Disabled|Ignore)\b"
)
LINE_ASSERT_RE = re.compile(
    r"\bassert\w*\b|\bexpect\(|\.should\b|\bt\.(Error|Errorf|Fatal|Fatalf|Fail|FailNow)\(|\brequire\.\w+\("
    r"|\bassert_\w+!?\(|\b(EXPECT|ASSERT)_\w+\("
)
LINE_TRIVIAL_RE = re.compile(
    r"\bassert\s*\(?\s*(True|1|not\s+False)\s*\)?\s*(?:,.*)?$|\bor\s+True\s*\)?\s*$"
    r"|\bexpect\(\s*(true|1)\s*\)\.(toBe|toEqual)\(\s*(true|1)\s*\)|\bassert\.ok\(\s*true\s*\)"
)
COLLECTION_RE = re.compile(
    r"\bcollect_ignore(_glob)?\b|\bpytest_(collection_modifyitems|ignore_collect|collect_file|pycollect_makeitem|"
    r"deselected|runtest_setup|runtest_call)\b|--deselect\b|--ignore\b|\bitems\.(remove|pop|clear)\b|\bskip\b"
)
TEST_CONFIG_FILES = re.compile(
    r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|package\.json|\.mocharc(\.\w+)?"
    r"|(jest|vitest|playwright|karma|cypress)\.config\.\w+|karma\.conf\.\w+)$"
)
PY_SECTION_RE = re.compile(r"^\[(tool:pytest|pytest|tool\.pytest(\.[\w.]+)?)\]\s*$", re.M)
SECTION_RE = re.compile(r"^\[[^\]]+\]\s*$", re.M)


@dataclass
class Facts:
    """What a test file asserts and skips, counted so that a before and an after can be compared."""
    asserts: int = 0
    trivial: int = 0
    skips: int = 0
    disabled: set[str] = field(default_factory=set)
    tests: set[str] = field(default_factory=set)
    exact: bool = True  # read with a parser, not with line patterns


# --- Python, with ast ------------------------------------------------------------------------------------------

def _name(node: ast.AST) -> str:
    """The last part of a dotted name: `pytest.mark.skip(...)` and `skip` both give `skip`."""
    if isinstance(node, ast.Call):
        return _name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_trivial(test: ast.AST) -> bool:
    """`assert True`, `assert 1`, `assert x or True`, `assert 1 == 1`: an assertion that can't fail."""
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Constant):
        return not test.operand.value
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_is_trivial(v) for v in test.values)
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.Is)):
        return ast.dump(test.left) == ast.dump(test.comparators[0])
    return False


def _assert_call(node: ast.Call) -> bool:
    name = _name(node.func)
    return name.startswith(("assert", "assert_")) or name in ("fail", "raises", "warns")


def _trivial_call(node: ast.Call) -> bool:
    """`self.assertTrue(True)`, `self.assertEqual(1, 1)`."""
    name = _name(node.func)
    if name in ("assertTrue", "assert_") and node.args:
        return _is_trivial(node.args[0])
    if name in ("assertEqual", "assertIs") and len(node.args) >= 2:
        return ast.dump(node.args[0]) == ast.dump(node.args[1])
    return False


def _body_disabled(body: list[ast.stmt]) -> bool:
    """A test whose body does nothing: it starts with `return`, or is only `pass`, `...` or a docstring."""
    stmts = [s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not stmts:
        return True
    first = stmts[0]
    if isinstance(first, ast.Return):
        return True
    return all(isinstance(s, ast.Pass) for s in stmts)


def _skip_decorator(node: ast.AST) -> bool:
    return _name(node) in SKIP_NAMES


def python_facts(text: str) -> Facts | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    facts = Facts()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            facts.asserts += 1
            facts.trivial += _is_trivial(node.test)
        elif isinstance(node, ast.Call):
            if _assert_call(node):
                facts.asserts += 1
                facts.trivial += _trivial_call(node)
            if _name(node.func) in ("skip", "xfail", "importorskip", "skipTest"):
                facts.skips += 1
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets) and node.value is not None:
                marks = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                facts.skips += sum(_skip_decorator(m) for m in marks)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            facts.skips += sum(_skip_decorator(d) for d in node.decorator_list)
    for scope, prefix in [(tree, "")] + [(c, c.name + "::") for c in tree.body if isinstance(c, ast.ClassDef)]:
        for node in scope.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                facts.tests.add(prefix + node.name)
                if _body_disabled(node.body):
                    facts.disabled.add(prefix + node.name)
    return facts


# --- other languages, with line patterns ----------------------------------------------------------------------

def strip_comment(line: str, path: str) -> str:
    """The code part of a line: `#` comments for Python, Ruby and shell, `//` and one-line `/* */` otherwise.
    Quotes are respected, so `"#"` in a string is kept."""
    markers = ("#",) if path.endswith((".py", ".rb", ".sh", ".pl", ".r", ".R")) else ("//", "/*")
    out, quote, i = [], "", 0
    while i < len(line):
        c = line[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
            out.append(c)
        elif any(line.startswith(m, i) for m in markers):
            if line.startswith("/*", i):
                end = line.find("*/", i + 2)
                if end != -1:
                    i = end + 2
                    continue
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).rstrip()


def line_facts(text: str, path: str) -> Facts:
    facts = Facts(exact=False)
    for raw in text.splitlines():
        line = strip_comment(raw, path)
        if not line.strip():
            continue
        facts.asserts += bool(LINE_ASSERT_RE.search(line))
        facts.trivial += bool(LINE_TRIVIAL_RE.search(line))
        facts.skips += bool(LINE_SKIP_RE.search(line))
    return facts


def facts(text: str | None, path: str) -> Facts:
    if text is None:
        return Facts()
    if path.endswith(".py"):
        parsed = python_facts(text)
        if parsed is not None:
            return parsed
    return line_facts(text, path)


def compare(path: str, before: str | None, after: str | None) -> list[dict]:
    """The findings for one test file, from its content at the fork point and on the branch. A new file is compared
    with nothing, so a weakened or disabled test the branch itself added is still found."""
    old, new = facts(before, path), facts(after, path)
    out: list[dict] = []
    lost = old.asserts - new.asserts
    if lost > 0:
        out.append({"kind": "assertions_removed", "file": path, "count": lost,
                    "message": f"{path} has {lost} fewer assertion(s)"})
    if new.trivial > old.trivial:
        n = new.trivial - old.trivial
        out.append({"kind": "assertions_weakened", "file": path, "count": n,
                    "message": f"{path} adds {n} assertion(s) that can't fail (assert True, ... or True, 1 == 1)"})
    if new.skips > old.skips:
        out.append({"kind": "skip_added", "file": path, "count": new.skips - old.skips,
                    "message": f"{path} adds a skip or xfail"})
    disabled = sorted(new.disabled - old.disabled)
    if disabled:
        out.append({"kind": "test_disabled", "file": path, "tests": disabled,
                    "message": f"{path}: {', '.join(disabled)} does nothing (returns at once, or only passes)"})
    return out


# --- test configuration ----------------------------------------------------------------------------------------

def is_test_config(path: str) -> bool:
    return posixpath.basename(path) == "conftest.py" or bool(TEST_CONFIG_FILES.search(path))


def _pytest_section(text: str | None) -> str:
    if not text:
        return ""
    m = PY_SECTION_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = SECTION_RE.search(rest)
    while nxt and PY_SECTION_RE.match(nxt.group(0)):  # [tool.pytest.ini_options] then [tool.pytest.x]
        nxt = SECTION_RE.search(rest, nxt.end())
    return " ".join((rest[:nxt.start()] if nxt else rest).split())


def _package_json_tests(text: str | None) -> str:
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return text or ""
    if not isinstance(data, dict):
        return ""
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    keep = {k: data.get(k) for k in ("jest", "vitest", "mocha", "ava")} | {
        k: v for k, v in scripts.items() if k == "test" or k.startswith("test:")}
    return json.dumps(keep, sort_keys=True)


def config_findings(path: str, before: str | None, after: str | None) -> list[dict]:
    """A change to how tests are collected or run: a conftest.py that deselects or ignores tests, or changed
    pytest, Jest, Vitest or test-script settings."""
    name = posixpath.basename(path)
    if name == "conftest.py":
        old = [x for x in (before or "").splitlines() if COLLECTION_RE.search(strip_comment(x, path))]
        new = [x for x in (after or "").splitlines() if COLLECTION_RE.search(strip_comment(x, path))]
        changed = sorted(set(new) - set(old))
        if not changed:
            return []
        return [{"kind": "test_config_changed", "file": path, "lines": [x.strip() for x in changed[:5]],
                 "message": f"{path} changes which tests are collected or run: {changed[0].strip()}"}]
    if name in ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini"):
        if name == "pytest.ini":
            same = (before or "").split() == (after or "").split()
        else:
            same = _pytest_section(before) == _pytest_section(after)
        if same:
            return []
        return [{"kind": "test_config_changed", "file": path,
                 "message": f"{path} changes the pytest settings, which decide which tests run"}]
    if name == "package.json":
        if _package_json_tests(before) == _package_json_tests(after):
            return []
        return [{"kind": "test_config_changed", "file": path,
                 "message": f"{path} changes the test script or test-runner settings"}]
    if (before or "") == (after or ""):
        return []
    return [{"kind": "test_config_changed", "file": path,
             "message": f"{path} is test-runner configuration, which decides which tests run"}]
