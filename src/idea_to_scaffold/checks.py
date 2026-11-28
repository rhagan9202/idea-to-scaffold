"""Running an item's checks, and reading what the test runner says it ran.

A check's exit code comes from the code under test, so on its own it proves little: `os._exit(0)` in a module the
tests import ends pytest during collection with status 0, and a `conftest.py` can set the session's exit status
after the tests fail. So when a check runs pytest, `run_check` asks pytest for a JUnit XML report (through
`PYTEST_ADDOPTS`, into a fresh temporary folder) and reads it back. `result_findings` then compares the report with
the exit code:

- `no_test_report`: pytest ran but wrote no report. It stopped before the end of the session.
- `failures_hidden`: the report has failed tests or errors, but the command exited 0.
- `no_tests_ran`: the report has no tests, or every test was skipped.
- `tests_skipped`: tests the item's Verify ran, or tests in test files the branch changed, were skipped.

The checks run without any `PYTEST_ADDOPTS` or `PYTEST_PLUGINS` from the caller's environment, so an agent can't
switch pytest to `--collect-only` from its own shell.

This raises the cost of faking a pass; it doesn't make faking one impossible. Code that runs inside the test
process can still write a report of its own, and the checks still run with the caller's environment otherwise and
without a time limit.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .backlog import BacklogItem
from .package import Package

OUTPUT_TAIL = 40          # lines of a failed check's output to keep
REPORT_LIMIT = 64 << 20   # bytes of JUnit XML read at most
LIST_LIMIT = 20           # test names kept in a finding or a summary
SCRUBBED_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
RESULT_KINDS = ("no_test_report", "failures_hidden", "no_tests_ran", "tests_skipped")

# A command that runs pytest: `pytest`, `py.test`, `uv run pytest`, `python -m pytest`, `.venv/bin/pytest`.
PYTEST_RE = re.compile(r"(?:^|[\s/\\;&|(\"'])(?:py\.test|pytest)(?:\.exe)?(?=$|[\s;&|)\"'])|-m\s+pytest\b")
# A report the command asks for itself; pytest keeps the last --junitxml it is given, so that one wins over ours.
JUNIT_OPTION_RE = re.compile(r"--junit-?xml(?:=|\s+)(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")


@dataclass
class Check:
    name: str
    command: str


def checks_for(pkg: Package, item: BacklogItem, verify: str | None | bool = True,
               commands: dict[str, str] | None = None, *, acceptance: bool = False) -> list[Check]:
    """The item's Verify command, then the package's test and lint commands, without repeats. `verify` replaces the
    item's own Verify command and `commands` the package's (both the integration branch's, from `gate.inspect`).
    `acceptance` adds `commands.acceptance`, the held-out tests, which only `its land` runs."""
    out: list[Check] = []
    command = item.verify_command if verify is True else verify
    commands = pkg.commands if commands is None else commands
    candidates = [("verify", command or "")]
    candidates += [(name, commands.get(name, "")) for name in ("test", "lint") + (("acceptance",) if acceptance else ())]
    for name, command in candidates:
        command = command.strip()
        if command and command != "true" and all(c.command != command for c in out):
            out.append(Check(name, command))
    return out


def runs_pytest(command: str) -> bool:
    return bool(PYTEST_RE.search(command))


def check_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a check runs in: the caller's, without the variables that change what pytest runs."""
    env = dict(os.environ if base is None else base)
    for name in SCRUBBED_ENV:
        env.pop(name, None)
    return env


def run_check(check: Check, cwd: Path) -> dict:
    """Run one check in `cwd`. When the command runs pytest, the result also carries `results`, the summary of the
    JUnit report it wrote (`{"report": False}` when it wrote none)."""
    env = check_env()
    folder = report = None
    if runs_pytest(check.command):
        folder = Path(tempfile.mkdtemp(prefix="its-report-"))
        report = folder / "junit.xml"
        # pytest splits PYTEST_ADDOPTS with shlex, so the path is quoted for it and uses forward slashes on Windows
        env["PYTEST_ADDOPTS"] = f"--junitxml={shlex.quote(report.as_posix())} -o junit_family=xunit1"
    try:
        proc = subprocess.run(check.command, shell=True, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, env=env)
        output = (proc.stdout + proc.stderr).rstrip("\n").split("\n")
        result = {
            "name": check.name,
            "command": check.command,
            "exit_code": proc.returncode,
            "passed": proc.returncode == 0,
            "output": "\n".join(output[-OUTPUT_TAIL:]) if proc.returncode else "",
        }
        if report is not None:
            result["results"] = read_report(_report_path(check.command, cwd, report))
        return result
    finally:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)


def _report_path(command: str, cwd: Path, ours: Path) -> Path:
    """Our report, or the one the command names with its own --junitxml when pytest wrote that one instead."""
    if ours.is_file():
        return ours
    named = JUNIT_OPTION_RE.findall(command)
    if named:
        path = Path(named[-1].strip("\"'"))
        return path if path.is_absolute() else cwd / path
    return ours


def _test_id(case: ET.Element) -> tuple[str, str]:
    """(test id, test file) for a JUnit testcase written by pytest with junit_family=xunit1."""
    name = case.get("name", "")
    file = (case.get("file") or "").replace("\\", "/")
    classname = case.get("classname", "")
    if not file:
        return (f"{classname}::{name}" if classname else name), ""
    module = file[:-3].replace("/", ".") if file.endswith(".py") else ""
    cls = classname[len(module) + 1:] if module and classname.startswith(module + ".") else ""
    return "::".join(p for p in (file, cls, name) if p), file


def read_report(path: Path) -> dict:
    """A summary of a JUnit XML report: counts, plus the failed and skipped tests by name."""
    try:
        if path.stat().st_size > REPORT_LIMIT:
            return {"report": False, "reason": f"the test report is larger than {REPORT_LIMIT >> 20} MB"}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"report": False, "reason": "pytest wrote no test report"}
    if "<!DOCTYPE" in text or "<!ENTITY" in text:  # pytest never writes either; a report that has one was forged
        return {"report": False, "reason": "the test report declares a DOCTYPE or entities, which pytest never writes"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"report": False, "reason": f"the test report isn't valid XML ({exc})"}
    counts = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failed: list[str] = []
    skipped: list[dict] = []
    files: set[str] = set()
    for case in root.iter("testcase"):
        ident, file = _test_id(case)
        counts["tests"] += 1
        if file:
            files.add(file)
        tags = {child.tag for child in case}
        if "failure" in tags or "error" in tags:
            counts["failed" if "failure" in tags else "errors"] += 1
            failed.append(ident)
        elif "skipped" in tags:
            counts["skipped"] += 1
            mark = case.find("skipped")
            skipped.append({"id": ident, "file": file,
                            "reason": ((mark.get("message") if mark is not None else "") or "")[:200]})
        else:
            counts["passed"] += 1
    return {"report": True, **counts, "failed_tests": failed[:LIST_LIMIT], "skipped_tests": skipped,
            "files": sorted(files)}


def _in(file: str, paths: set[str]) -> bool:
    """Whether a report's test file is one of `paths` (package-relative); pytest's rootdir may sit above or below."""
    return bool(file) and any(p == file or p.endswith("/" + file) or file.endswith("/" + p) for p in paths)


def result_findings(results: list[dict], changed_tests: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """What the test reports say about checks that passed. `changed_tests` are the test files the branch changed."""
    out: list[dict] = []
    for r in results:
        summary = r.get("results")
        if summary is None or not r["passed"]:
            continue
        name, command = r["name"], r["command"]
        if not summary.get("report"):
            out.append({
                "kind": "no_test_report", "check": name, "command": command,
                "message": f"{name} ran pytest and exited {r['exit_code']}, but {summary.get('reason', 'wrote no report')}, "
                           "so nothing shows the tests ran: the run stopped before pytest finished (for example "
                           "os._exit in the code under test)"})
            continue
        bad = summary["failed"] + summary["errors"]
        if bad:
            names = summary["failed_tests"]
            out.append({
                "kind": "failures_hidden", "check": name, "command": command, "tests": names,
                "message": f"{name} exited 0, but pytest reported {summary['failed']} failed and {summary['errors']} "
                           f"errored test(s) ({', '.join(names[:5])}); something changed the exit status"})
        if summary["passed"] == 0 and not bad:
            what = ("collected no tests" if summary["tests"] == 0
                    else f"skipped every test it collected ({summary['tests']})")
            out.append({
                "kind": "no_tests_ran", "check": name, "command": command,
                "message": f"{name} {what}, so it shows nothing about the code"})
        skips = [s for s in summary["skipped_tests"] if name == "verify" or _in(s["file"], set(changed_tests))]
        if skips and summary["passed"]:
            ids = [s["id"] for s in skips]
            out.append({
                "kind": "tests_skipped", "check": name, "tests": ids[:LIST_LIMIT],
                "message": f"{name} skipped {len(ids)} test(s) "
                           f"{'it was meant to run' if name == 'verify' else 'in files the branch changed'}: "
                           f"{', '.join(ids[:5])}" + (f" ({skips[0]['reason']})" if skips[0]["reason"] else "")})
    return out


def failure_report(checks: list[dict]) -> str:
    """The failed checks for a person or an agent to read. Each failure's output is shown, except when it is the
    same as output already shown (the Verify command and the test command often fail on the same test)."""
    parts: list[str] = []
    shown: list[str] = []
    for c in checks:
        if c["passed"]:
            continue
        head = f"{c['name']} failed (exit {c['exit_code']}): {c['command']}"
        if c["output"] and c["output"] not in shown:
            parts.append(f"{head}\n{c['output']}")
            shown.append(c["output"])
        else:
            parts.append(head + ("  (same output as above)" if c["output"] else ""))
    return "\n\n".join(parts)
