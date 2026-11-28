"""Running an item's checks, and reading what the test runner says it ran.

A check's exit code comes from the code under test, so on its own it proves little: `os._exit(0)` in a module the
tests import ends pytest during collection with status 0, and a `conftest.py` can set the session's exit status
after the tests fail. So when a check runs a test runner `its` knows (pytest, or Node's `node --test`: see
`stacks.py`), `run_check` asks the runner for a JUnit XML report, into a fresh temporary folder, and reads it back.
`result_findings` then compares the report with the exit code:

- `no_test_report`: the runner ran but wrote no report. It stopped before the end of the session.
- `failures_hidden`: the report has failed tests or errors, but the command exited 0.
- `no_tests_ran`: the report has no tests, or every test was skipped.
- `tests_skipped`: tests the item's Verify ran, or tests in test files the branch changed, were skipped.

A check runs code from the branch, and `its land` runs it as the lead. So a check gets only the environment
variables on an allow-list (`ALLOWED_ENV`: the path, the locale, temporary folders, the OS's own variables, proxies
and certificate bundles, toolchain homes), plus the names the integration branch's `gate.env` adds. A token in the
lead's shell, such as `GH_TOKEN`, never reaches the code under test. `PYTEST_ADDOPTS`, `PYTEST_PLUGINS` and
`NODE_OPTIONS` are never passed, so an agent can't switch pytest to `--collect-only`, or load code into node, from
its own shell.

A check also has a time limit (`gate.timeout`, `DEFAULT_TIMEOUT` seconds by default). It runs in a process group of
its own (a new session on POSIX, a new process group on Windows), and when the limit passes the whole group is
killed, not only the shell, so a test that started a server or a `sleep` in the background doesn't outlive it. Its
output goes to a temporary file rather than a pipe, so a background process that keeps the output open can't hold
the check up after the shell exits.

This raises the cost of faking a pass; it doesn't make faking one impossible. Code that runs inside the test
process can still write a report of its own, and it can still read whatever the lead's account can read on disk,
such as `~/.config/gh` or `~/.ssh`: the allow-list keeps secrets out of the environment, not off the file system.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .backlog import BacklogItem
from .package import Package
from .stacks import PYTEST, RUNNERS, Runner, ended_early, runner_for

OUTPUT_TAIL = 40          # lines of a failed check's output to keep
REPORT_LIMIT = 64 << 20   # bytes of JUnit XML read at most
LIST_LIMIT = 20           # test names kept in a finding or a summary
OUTPUT_READ = 256 << 10   # bytes of a check's output read back (its end)
DEFAULT_TIMEOUT = 1200    # seconds a check may run before its process tree is killed
KILL_GRACE = 5            # seconds between asking a timed-out check to stop and killing it
TIMED_OUT = 124           # the exit code a timed-out check reports
NEVER_ENV = frozenset({"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "NODE_OPTIONS"})  # set by run_check itself, never passed
# The variables a check gets from the caller's environment. Names compare without case (Windows has no case in
# them; on POSIX the lower-case proxy names are the usual ones).
ALLOWED_ENV = frozenset(name.upper() for name in (
    # POSIX basics
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR", "LANG", "LANGUAGE", "COLUMNS", "LINES",
    "NO_COLOR", "FORCE_COLOR", "CI", "__CF_USER_TEXT_ENCODING",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    # Python and other toolchains
    "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
    "JAVA_HOME", "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "GOFLAGS", "CARGO_HOME", "RUSTUP_HOME",
    # the network: proxies and certificate bundles
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "PIP_CERT",
    # the commit identity, for tests that make commits
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    # Windows
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERNAME", "USERPROFILE",
    "USERDOMAIN", "COMPUTERNAME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE",
    "PUBLIC", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
))
# Families of variables a check gets, except a name that looks like it holds a secret.
ALLOWED_PREFIXES = ("LC_", "UV_")
SECRET_RE = re.compile(r"TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY|AUTH", re.I)
RESULT_KINDS = ("no_test_report", "failures_hidden", "no_tests_ran", "tests_skipped")
RUNNER_NAMES = {r.name: r for r in RUNNERS}

@dataclass
class Check:
    name: str
    command: str


@dataclass(frozen=True)
class Settings:
    """How checks run: the integration branch's `gate.env` (extra variables to pass) and `gate.timeout`."""
    env: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def of(cls, gate: dict) -> Settings:
        names = gate.get("env")
        names = tuple(str(n).strip() for n in names if str(n).strip()) if isinstance(names, list) else ()
        timeout = gate.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            timeout = DEFAULT_TIMEOUT
        return cls(names, float(timeout))


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
    return PYTEST.runs(command, Path("."))


def check_env(base: dict[str, str] | None = None, extra: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """The environment a check runs in: the caller's variables that are on the allow-list (`ALLOWED_ENV`, or in an
    `ALLOWED_PREFIXES` family without a secret-looking name), or that `extra` names (the project's `gate.env`)."""
    wanted = {n.upper() for n in extra} - NEVER_ENV
    out = {}
    for name, value in (os.environ if base is None else base).items():
        key = name.upper()
        if key in NEVER_ENV:
            continue
        if (key in ALLOWED_ENV or key in wanted
                or (key.startswith(ALLOWED_PREFIXES) and not SECRET_RE.search(key))):
            out[name] = value
    return out


def run_check(check: Check, cwd: Path, settings: Settings | None = None, *, ignore: list[str] = ()) -> dict:
    """Run one check in `cwd`, with the allow-listed environment and the time limit from `settings`. When the
    command runs a test runner `its` knows (`stacks.py`), the result also carries `results`, the summary of the
    JUnit report it wrote (`{"report": False}` when it wrote none), and the runner skips the `ignore` paths (held-out
    tests of items that aren't done: `heldout.py`) if it can. A check that runs out of time has `timed_out: True`
    and fails."""
    settings = settings or Settings()
    env = check_env(extra=settings.env)
    folder = report = None
    runner = runner_for(check.command, cwd)
    if runner is not None:
        folder = Path(tempfile.mkdtemp(prefix="its-report-"))
        report = folder / "junit.xml"
        runner.prepare(env, report, list(ignore))
    try:
        code, output, timed_out = run_command(check.command, cwd, env, settings.timeout)
        result = {
            "name": check.name,
            "command": check.command,
            "exit_code": code,
            "passed": code == 0 and not timed_out,
            "output": output if code or timed_out else "",
        }
        if timed_out:
            result["timed_out"] = True
            result["output"] = (output + "\n" if output else "") + (
                f"[its] {check.name} ran for more than {settings.timeout:g} s; its process tree was killed "
                "(gate.timeout in package.yaml sets the limit)")
            return result
        if runner is not None:
            result["results"] = read_report(runner.report_path(check.command, cwd, report), runner)
        return result
    finally:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)


def run_command(command: str, cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str, bool]:
    """Run a shell command in a process group of its own: (exit code, the end of its output, whether it timed
    out). Past `timeout` seconds, or if `its` itself is interrupted, the whole group is killed."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    with tempfile.TemporaryFile() as sink:
        proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=sink,
                                stderr=subprocess.STDOUT, **group)
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            code = proc.returncode if proc.returncode is not None else TIMED_OUT
        except BaseException:
            kill_tree(proc)
            raise
        return (TIMED_OUT if timed_out else code), _tail(sink), timed_out


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a check's process and everything it started: the process group on POSIX (TERM, then KILL after
    `KILL_GRACE` seconds), `taskkill /T /F` on Windows."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        return
    for sig, wait in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=wait)
            if sig == signal.SIGTERM:  # the shell is gone; make sure nothing else in its group is left
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            return
        except subprocess.TimeoutExpired:
            continue


def _tail(sink) -> str:
    """The last `OUTPUT_TAIL` lines of what a check wrote, read from the end of its output file."""
    size = sink.seek(0, os.SEEK_END)
    sink.seek(max(0, size - OUTPUT_READ))
    data = sink.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = data.rstrip("\n").split("\n")
    if size > OUTPUT_READ:
        lines = lines[1:]  # the first line was cut part way through
    return "\n".join(lines[-OUTPUT_TAIL:])


def read_report(path: Path, runner: Runner = PYTEST) -> dict:
    """A summary of the JUnit XML report `runner` wrote: counts, plus the failed and skipped tests by name."""
    head = {"runner": runner.name}
    try:
        if path.stat().st_size > REPORT_LIMIT:
            return {**head, "report": False, "reason": f"the test report is larger than {REPORT_LIMIT >> 20} MB"}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {**head, "report": False, "reason": f"{runner.label} wrote no test report"}
    if "<!DOCTYPE" in text or "<!ENTITY" in text:  # no runner writes either; a report that has one was forged
        return {**head, "report": False,
                "reason": f"the test report declares a DOCTYPE or entities, which {runner.label} never writes"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {**head, "report": False, "reason": f"the test report isn't valid XML ({exc})"}
    counts = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failed: list[str] = []
    skipped: list[dict] = []
    files: set[str] = set()
    for ident, file, case in runner.cases(root):
        counts["tests"] += 1
        if file:
            files.add(file)
        tags = {child.tag for child in case}
        if ended_early(runner, case):  # node: the file ran as one test, because it ended before its own tests ran
            counts["errors"] += 1
            failed.append(f"{ident} (ended before its tests ran)")
        elif "failure" in tags or "error" in tags:
            counts["failed" if "failure" in tags else "errors"] += 1
            failed.append(ident)
        elif "skipped" in tags:
            counts["skipped"] += 1
            mark = case.find("skipped")
            skipped.append({"id": ident, "file": file,
                            "reason": ((mark.get("message") if mark is not None else "") or "")[:200]})
        else:
            counts["passed"] += 1
    return {**head, "report": True, **counts, "failed_tests": failed[:LIST_LIMIT], "skipped_tests": skipped,
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
        runner = RUNNER_NAMES.get(summary.get("runner", ""), PYTEST)
        if not summary.get("report"):
            out.append({
                "kind": "no_test_report", "check": name, "command": command,
                "message": f"{name} ran {runner.label} and exited {r['exit_code']}, but "
                           f"{summary.get('reason', 'wrote no report')}, so nothing shows the tests ran: the run "
                           f"stopped before {runner.label} finished (for example {runner.early_exit})"})
            continue
        bad = summary["failed"] + summary["errors"]
        if bad:
            names = summary["failed_tests"]
            out.append({
                "kind": "failures_hidden", "check": name, "command": command, "tests": names,
                "message": f"{name} exited 0, but {runner.label} reported {summary['failed']} failed and {summary['errors']} "
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
