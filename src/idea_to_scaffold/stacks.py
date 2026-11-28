"""The test runners whose results `its` reads, so a passing check means more than an exit code.

A check's exit code comes from the code under test (see `checks.py`). So for each runner it knows, `its` has the
runner write a JUnit XML report of its own, through an environment variable the agent's shell can't set, and reads
it back after the check:

- **pytest** (Python): `PYTEST_ADDOPTS` adds `--junitxml`, and `--ignore` for the held-out tests of items that
  aren't done. A command that names its own `--junitxml` gets that report read instead.
- **node --test** (Node's built-in runner, run directly or by an npm, pnpm or yarn script that runs it):
  `NODE_OPTIONS` adds a JUnit reporter beside the usual spec output. Node's report doesn't say which file a test is
  in, so a skip counts only when the Verify command ran it, and held-out folders aren't left out (`its land` leaves
  them out with a sparse checkout, which works for any runner). A test file that ends before its tests run
  (`process.exit(0)` in the code it imports) shows in the report as a passing test named for the file, which counts
  as an error. A test only counts as that when its name is the path of a file in the check's folder, so a test
  called "loads config.js" is still a test.

Any other runner (Jest, Vitest, `go test`, `cargo test`) is gated on its exit code, and on the static checks of
`tamper.py`, which read test files and test settings in every language.
"""
from __future__ import annotations

import json
import re
import shlex
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# A command that runs pytest: `pytest`, `py.test`, `uv run pytest`, `python -m pytest`, `.venv/bin/pytest`.
PYTEST_RE = re.compile(r"(?:^|[\s/\\;&|(\"'])(?:py\.test|pytest)(?:\.exe)?(?=$|[\s;&|)\"'])|-m\s+pytest\b")
# A report the command asks for itself; pytest keeps the last --junitxml it is given, so that one wins over ours.
JUNIT_OPTION_RE = re.compile(r"--junit-?xml(?:=|\s+)(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")
# `node --test`, `node --import tsx --test test/`.
NODE_TEST_RE = re.compile(r"(?:^|[\s/\\;&|(\"'])node(?:\.exe)?\s+(?:[^;&|]*?\s)?--test(?=$|[\s=;&|)\"'])")
# A package-manager script: `npm test`, `npm t`, `npm run test:unit`, `pnpm test`, `yarn test`.
SCRIPT_RE = re.compile(r"(?:^|[\s;&|(])(?:npm|pnpm|yarn)(?:\.cmd)?\s+(?:run(?:-script)?\s+)?(?P<script>test(?::[\w:.-]+)?|t)"
                       r"(?=$|[\s;&|)])")
# How node's JUnit report names a test file that ran as one test, because it ended before its own tests ran.
NODE_FILE_RE = re.compile(r"\.(?:[cm]?js|[cm]?ts|jsx|tsx)$")


@dataclass(frozen=True)
class Runner:
    """A test runner `its` asks for a report. `label` is how messages name it; `early_exit` is the way code under
    test usually stops it before it reports."""

    name: str
    label: str
    early_exit: str

    def runs(self, command: str, cwd: Path) -> bool:
        raise NotImplementedError

    def prepare(self, env: dict[str, str], report: Path, ignore: list[str]) -> None:
        """Set the variables that make the runner write its report to `report` (and leave out `ignore`)."""
        raise NotImplementedError

    def report_path(self, command: str, cwd: Path, ours: Path) -> Path:
        """Where the report is: ours, unless the command asked the runner for one of its own."""
        return ours

    def cases(self, root: ET.Element) -> Iterator[tuple[str, str, ET.Element]]:
        """(test id, test file, testcase) for each test in the report; the file is "" when the report doesn't say."""
        raise NotImplementedError


class Pytest(Runner):
    def runs(self, command: str, cwd: Path) -> bool:
        return bool(PYTEST_RE.search(command))

    def prepare(self, env: dict[str, str], report: Path, ignore: list[str]) -> None:
        # pytest splits PYTEST_ADDOPTS with shlex, so the path is quoted for it and uses forward slashes on Windows
        env["PYTEST_ADDOPTS"] = " ".join([f"--junitxml={shlex.quote(report.as_posix())} -o junit_family=xunit1",
                                          *(f"--ignore={shlex.quote(p)}" for p in ignore)])

    def report_path(self, command: str, cwd: Path, ours: Path) -> Path:
        if ours.is_file():
            return ours
        named = JUNIT_OPTION_RE.findall(command)
        if named:
            path = Path(named[-1].strip("\"'"))
            return path if path.is_absolute() else cwd / path
        return ours

    def cases(self, root: ET.Element) -> Iterator[tuple[str, str, ET.Element]]:
        for case in root.iter("testcase"):  # written with junit_family=xunit1, which names the file
            name = case.get("name", "")
            file = (case.get("file") or "").replace("\\", "/")
            classname = case.get("classname", "")
            if not file:
                yield (f"{classname}::{name}" if classname else name), "", case
                continue
            module = file[:-3].replace("/", ".") if file.endswith(".py") else ""
            cls = classname[len(module) + 1:] if module and classname.startswith(module + ".") else ""
            yield "::".join(p for p in (file, cls, name) if p), file, case


class NodeTest(Runner):
    def runs(self, command: str, cwd: Path) -> bool:
        if NODE_TEST_RE.search(command):
            return True
        script = SCRIPT_RE.search(command)
        if not script:
            return False
        name = "test" if script["script"] == "t" else script["script"]
        return bool(NODE_TEST_RE.search(package_scripts(cwd).get(name, "")))

    def prepare(self, env: dict[str, str], report: Path, ignore: list[str]) -> None:
        # Node splits NODE_OPTIONS on spaces outside double quotes; the spec reporter keeps the usual output
        env["NODE_OPTIONS"] = (f"--test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit "
                               f"--test-reporter-destination=\"{report.as_posix()}\"")

    def cases(self, root: ET.Element) -> Iterator[tuple[str, str, ET.Element]]:
        def walk(node: ET.Element, suites: tuple[str, ...]) -> Iterator[tuple[str, str, ET.Element]]:
            for child in node:
                if child.tag == "testsuite":
                    yield from walk(child, (*suites, child.get("name", "")))
                elif child.tag == "testcase":
                    yield " > ".join((*suites, child.get("name", ""))), (child.get("file") or "").replace("\\", "/"), child
        yield from walk(root, ())


PYTEST = Pytest("pytest", "pytest", "os._exit in the code under test")
NODE_TEST = NodeTest("node-test", "node --test", "process.exit in the code under test")
RUNNERS = (PYTEST, NODE_TEST)


def runner_for(command: str, cwd: Path) -> Runner | None:
    """The runner a check's command runs, if `its` can read its results."""
    return next((r for r in RUNNERS if r.runs(command, cwd)), None)


def package_scripts(cwd: Path) -> dict[str, str]:
    """The `scripts` of the package.json in `cwd`, or {}."""
    try:
        data = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}


def ended_early(runner: Runner, case: ET.Element, cwd: Path | None = None) -> bool:
    """Whether a testcase is node's stand-in for a test file that ended before its own tests ran: a passing test
    whose name is the file's path, relative to the folder node ran in (with `\\` on Windows) or absolute."""
    name = case.get("name", "").replace("\\", "/")
    if runner is not NODE_TEST or len(case) or not NODE_FILE_RE.search(name):
        return False
    return cwd is None or (cwd / name).is_file()  # `cwd / name` is `name` when it is absolute
