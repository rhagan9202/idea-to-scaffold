"""What a change does to a test file or to the test runner's configuration, for the done gate.

Python test files are read with `ast`, so comments, strings and formatting can't hide anything: an assertion turned
into `pass  # assert later` is a lost assertion, `pytestmark = pytest.mark.skip(...)`, `from unittest import skip;
@skip` and `@pytest.mark.parametrize("x", [])` are skips, `assert x or True` and `assert [x == 1]` are weakened
assertions, and a test whose body starts with `return` is disabled. Other languages fall back to line patterns, with
comments stripped first.

Assertions are counted test by test, so commenting out one test's assertion and adding two elsewhere is still a
lost assertion, and an assertion that can't fail doesn't count. A test that is no longer there (deleted, or renamed
so pytest stops collecting it) is `test_removed`.

Test configuration is compared too: a `conftest.py` that deselects or ignores tests or rewrites their results, a
change to the pytest, Jest or Vitest settings, a pytest plugin entry point, or a `sitecustomize.py` can drop a
failing test without touching it.
"""
from __future__ import annotations

import ast
import json
import operator
import posixpath
import re
from dataclasses import dataclass, field

SKIP_NAMES = {"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure", "importorskip"}
LINE_SKIP_RE = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail|importorskip)\(|\bpytestmark\s*="
    r"|@(?:pytest\.)?mark\.(skip|skipif|xfail)\b"
    r"|@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b|\.skipTest\("
    r"|\b(it|test|describe|context|suite|t|ctx)\.(skip|todo)\s*\(|\b(xit|xdescribe|xtest)\s*\("
    r"|\b(it|test|describe|context|suite)\.only\s*\(|\b(fit|fdescribe)\s*\(|\.(skipIf|runIf)\s*\("
    r"|\b(it|test|describe|suite)\s*\(.*\{[^{}]*\b(skip|todo)\s*:(?!\s*(false|undefined|null|0)\b)"
    r"|\{[^{}]*\bonly\s*:\s*true\b"
    r"|\bt\.Skip(Now|f)?\(|#\[ignore\]|@(Disabled|Ignore)\b"
)
# The start of one test, for counting assertions test by test without a parser: `it("adds", ...)`, `test('x', ...)`,
# `def test_add(`.
LINE_TEST_RE = re.compile(
    r"\b(?:it|test|specify)(?:\.(?:only|concurrent|skip|todo))?\s*\(\s*(['\"`])(?P<title>.+?)\1"
    r"|^\s*(?:async\s+)?def\s+(?P<name>test\w*)\s*\("
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
    # hooks and calls that rewrite results or the exit status after the tests ran
    r"|\bpytest_(runtest_makereport|report_teststatus|runtest_protocol|pyfunc_call|sessionfinish|sessionstart|"
    r"runtest_logreport|terminal_summary|runtestloop|configure|unconfigure|plugin_registered|cmdline_main|"
    r"cmdline_parse|load_initial_conftests)\b|\bexitstatus\b|\bhookwrapper\b|\bwrapper\s*=\s*True\b"
    r"|\bos\._exit\b|\bsys\.exit\b|\bpytest_plugins\b|\bpluginmanager\b|\.outcome\b|\bforce_(result|exception)\b"
    r"|\batexit\b"
)
TEST_CONFIG_FILES = re.compile(
    r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|setup\.py|pyproject\.toml|package\.json|\.mocharc(\.\w+)?"
    r"|(jest|vitest|playwright|karma|cypress)\.config\.\w+|karma\.conf\.\w+"
    r"|sitecustomize\.py|usercustomize\.py|[^/]+\.pth)$"
)
PYTEST11_RE = re.compile(r"pytest11")
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
    per_test: dict[str, int] = field(default_factory=dict)  # assertions that can fail, in each test
    helpers: int = 0    # assertions that can fail, outside any test (helpers, fixtures)
    runner: set[str] = field(default_factory=set)  # statements that end the run or load plugins


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


# Nodes through which an assertion can depend on the code under test. One with none of them is made of constants.
DEPENDS = (ast.Name, ast.Call, ast.Attribute, ast.Subscript, ast.Starred, ast.Await, ast.NamedExpr, ast.Yield,
           ast.YieldFrom, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
           ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


def _is_trivial(test: ast.AST) -> bool:
    """An assertion that can't fail: `assert True`, `assert 1`, `assert x or True`, `assert 1 == 1`, `assert 2 > 1`,
    and anything always truthy, such as `assert [x == 1]`, `assert (x, "message")` or `assert f"{x}"`."""
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Constant):
        return not test.operand.value
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_is_trivial(v) for v in test.values)
    if isinstance(test, (ast.List, ast.Tuple, ast.Set)):
        return bool(test.elts)
    if isinstance(test, ast.Dict):
        return bool(test.keys)
    if isinstance(test, ast.JoinedStr):
        return any(isinstance(v, ast.Constant) and v.value for v in test.values)
    if isinstance(test, ast.Lambda):
        return True
    if not any(isinstance(n, DEPENDS) for n in ast.walk(test)):  # `assert 1 + 1 == 2`: only constants
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, right = test.left, test.comparators[0]
        if isinstance(test.ops[0], (ast.Eq, ast.Is, ast.LtE, ast.GtE)) and ast.dump(left) == ast.dump(right):
            return True
        if isinstance(left, ast.Constant) and isinstance(right, ast.Constant) and type(test.ops[0]) in COMPARE:
            try:
                return bool(COMPARE[type(test.ops[0])](left.value, right.value))
            except TypeError:
                return False
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


def _empty_parametrize(node: ast.AST) -> bool:
    """`@pytest.mark.parametrize("x", [])`: a test with no cases, which pytest reports as skipped."""
    if not (isinstance(node, ast.Call) and _name(node.func) == "parametrize"):
        return False
    values = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "argvalues"),
                                                          None)
    return isinstance(values, (ast.List, ast.Tuple, ast.Set)) and not values.elts


def _skip_decorator(node: ast.AST) -> bool:
    return _name(node) in SKIP_NAMES or _empty_parametrize(node)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def _counted(node: ast.AST) -> tuple[int, int]:
    """(assertions, assertions that can't fail) in `node` and everything under it."""
    asserts = trivial = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            asserts += 1
            trivial += _is_trivial(sub.test)
        elif isinstance(sub, ast.Call) and _assert_call(sub):
            asserts += 1
            trivial += _trivial_call(sub)
    return asserts, trivial


def python_facts(text: str) -> Facts | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    facts = Facts()
    facts.asserts, facts.trivial = _counted(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _name(node.func) in ("skip", "xfail", "importorskip", "skipTest"):
                facts.skips += 1
            if _dotted(node.func) == "os._exit":
                facts.runner.add("os._exit")
            elif _dotted(node.func) == "atexit.register":
                facts.runner.add("atexit.register")
            if _name(node.func) == "param":  # pytest.param(..., marks=pytest.mark.skip)
                for kw in node.keywords:
                    if kw.arg == "marks":
                        facts.skips += sum(_name(m) in SKIP_NAMES for m in ast.walk(kw.value)
                                           if isinstance(m, (ast.Attribute, ast.Name)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets) and node.value is not None:
                marks = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                facts.skips += sum(_skip_decorator(m) for m in marks)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            facts.skips += sum(_skip_decorator(d) for d in node.decorator_list)
    for stmt in tree.body:  # what runs when pytest imports the module
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            if _dotted(stmt.value.func) in ("sys.exit", "exit", "quit"):
                facts.runner.add("exit at import")
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in targets):
                facts.runner.add("pytest_plugins")
    in_tests = 0
    for scope, prefix in [(tree, "")] + [(c, c.name + "::") for c in tree.body if isinstance(c, ast.ClassDef)]:
        for node in scope.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                name = prefix + node.name
                facts.tests.add(name)
                asserts, trivial = _counted(node)
                facts.per_test[name] = facts.per_test.get(name, 0) + asserts - trivial
                in_tests += asserts - trivial
                if _body_disabled(node.body):
                    facts.disabled.add(name)
    facts.helpers = facts.asserts - facts.trivial - in_tests
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
    """Counts from line patterns. A test runs from the line that starts it to the line that starts the next one."""
    facts = Facts(exact=False)
    current: str | None = None
    for raw in text.splitlines():
        line = strip_comment(raw, path)
        if not line.strip():
            continue
        start = LINE_TEST_RE.search(line)
        if start:
            current = start.group("title") or start.group("name")
            facts.tests.add(current)
            facts.per_test.setdefault(current, 0)
        asserts, trivial = bool(LINE_ASSERT_RE.search(line)), bool(LINE_TRIVIAL_RE.search(line))
        facts.asserts += asserts
        facts.trivial += trivial
        if asserts and not trivial:
            if current is None:
                facts.helpers += 1
            else:
                facts.per_test[current] += 1
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


def test_names(text: str | None, path: str) -> set[str]:
    """The tests a file defines, by bare name (`test_add`, not `TestStore::test_add`)."""
    return {t.rsplit("::", 1)[-1] for t in facts(text, path).tests}


def compare(path: str, before: str | None, after: str | None) -> list[dict]:
    """The findings for one test file, from its content at the fork point and on the branch. A new file is compared
    with nothing, so a weakened or disabled test the branch itself added is still found.

    Assertions that can fail are counted per test: a test that keeps fewer of them than it had is a loss, whatever
    the branch adds elsewhere, and so is a helper outside the tests that loses some. A test that is gone is
    `test_removed`; the gate drops the finding when the test turns up in another test file the branch changed."""
    old, new = facts(before, path), facts(after, path)
    out: list[dict] = []
    common = old.tests & new.tests
    lost_in = {t: old.per_test.get(t, 0) - new.per_test.get(t, 0) for t in sorted(common)}
    lost_in = {t: n for t, n in lost_in.items() if n > 0}
    lost = sum(lost_in.values()) + max(0, old.helpers - new.helpers)
    if not old.tests and not new.tests:  # no tests found: compare the whole file
        lost = (old.asserts - old.trivial) - (new.asserts - new.trivial)
    if lost > 0:
        where = f" (in {', '.join(lost_in)})" if lost_in else ""
        out.append({"kind": "assertions_removed", "file": path, "count": lost, "tests": sorted(lost_in),
                    "message": f"{path} has {lost} fewer assertion(s) that can fail{where}"})
    removed = sorted(t for t in old.tests - new.tests if old.per_test.get(t, 0) or not old.exact)  # one that
    # asserted nothing that can fail showed nothing, so it can go
    if removed and after is not None:
        out.append({"kind": "test_removed", "file": path, "tests": removed,
                    "message": f"{path} no longer has {', '.join(removed[:5])}, so it no longer runs (deleted, or "
                               "renamed so the runner doesn't collect it)"})
    runner = sorted(new.runner - old.runner)
    if runner and posixpath.basename(path) != "conftest.py":  # config_findings reads conftest.py line by line
        out.append({"kind": "test_config_changed", "file": path, "lines": runner,
                    "message": f"{path} adds {', '.join(runner)}, which can end the test run early or load plugins "
                               "that change its results"})
    if new.trivial > old.trivial:
        n = new.trivial - old.trivial
        out.append({"kind": "assertions_weakened", "file": path, "count": n,
                    "message": f"{path} adds {n} assertion(s) that can't fail (assert True, ... or True, 1 == 1)"})
    if new.skips > old.skips:
        out.append({"kind": "skip_added", "file": path, "count": new.skips - old.skips,
                    "message": f"{path} adds a skip, an xfail, an empty parametrize or an .only"})
    disabled = sorted(new.disabled - old.disabled)
    if disabled:
        out.append({"kind": "test_disabled", "file": path, "tests": disabled,
                    "message": f"{path}: {', '.join(disabled)} does nothing (returns at once, or only passes)"})
    return out


# --- test configuration ----------------------------------------------------------------------------------------

def is_test_config(path: str) -> bool:
    return posixpath.basename(path) == "conftest.py" or bool(TEST_CONFIG_FILES.search(path))


def _pytest11(text: str | None) -> str:
    """The pytest plugin entry points a pyproject.toml, setup.cfg or setup.py declares (`pytest11`)."""
    if not text:
        return ""
    out, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            inside = bool(PYTEST11_RE.search(stripped))
            if inside:
                out.append(stripped)
            continue
        if inside or PYTEST11_RE.search(line):
            out.append(" ".join(stripped.split()))
    return "\n".join(x for x in out if x)


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
    if name in ("pyproject.toml", "setup.cfg", "setup.py") and _pytest11(before) != _pytest11(after):
        return [{"kind": "test_config_changed", "file": path,
                 "message": f"{path} changes the pytest plugins it installs (pytest11 entry points), which can "
                            "rewrite test results"}]
    if name == "setup.py":
        return []
    if name in ("sitecustomize.py", "usercustomize.py") or name.endswith(".pth"):
        if (before or "") == (after or ""):
            return []
        return [{"kind": "test_config_changed", "file": path,
                 "message": f"{path} runs when Python starts, before any test, so it can change how the tests run"}]
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
