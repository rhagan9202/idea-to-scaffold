"""The one way `its` runs git, so that no git command it runs also runs code someone put in the repository's git
folder.

An agent in a linked worktree can write the repository's shared git folder: its refs always, its hooks and config
outside a sandbox that denies them. Git runs some of what is there on its own: `worktree add` fires `post-checkout`,
a merge fires `post-merge`, and `core.fsmonitor` names a program most commands start. So a git command in the
lead's `its land` could run agent-written code as the lead, with the lead's credentials. Every git command `its`
runs goes through `run`, which:

- points `core.hooksPath` at an empty folder, so no hook runs (the project's own hooks don't run on `its`'s commits
  either);
- turns `core.fsmonitor` off;
- drops `GIT_*` variables from the environment, except the commit identity and the SSH and askpass settings, so a
  `GIT_DIR` or `GIT_CONFIG_*` left in the caller's shell can't point git elsewhere;
- sets `GIT_TERMINAL_PROMPT=0`, so a missing credential fails instead of waiting for someone to type it;
- has a timeout: `LOCAL_TIMEOUT` seconds, or `NETWORK_TIMEOUT` for fetch, push and ls-remote;
- reads UTF-8 and prints paths unquoted.

A command that fails or times out comes back as a CompletedProcess with a non-zero `returncode` (124 for a timeout),
so each caller keeps deciding whether a failure means "no" or "stop". `out` raises `GitError` instead.

What this doesn't cover: other programs the repository's config can name (`filter.*`, `diff.external`) still run
if an agent can write that config. Claude Code's sandbox denies writes to it; an agent without a sandbox can write
anything the lead can.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

LOCAL_TIMEOUT = 300
NETWORK_TIMEOUT = 600
TIMED_OUT = 124
NETWORK_COMMANDS = frozenset({"fetch", "push", "ls-remote", "pull", "clone"})
KEEP_ENV = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_ASKPASS",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
})

_hooks: str | None = None


class GitError(Exception):
    """A git command failed, timed out, or the folder isn't in a git repository."""


def _no_hooks() -> str:
    """An empty folder for core.hooksPath, made once per process and removed when it exits."""
    global _hooks
    if _hooks is None or not os.path.isdir(_hooks):
        _hooks = tempfile.mkdtemp(prefix="its-no-hooks-")
        atexit.register(shutil.rmtree, _hooks, True)
    return _hooks


def env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment git runs in: the caller's, without GIT_* variables other than KEEP_ENV."""
    out = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.upper().startswith("GIT_") or k.upper() in KEEP_ENV}
    out["GIT_TERMINAL_PROMPT"] = "0"
    return out


def argv(*args: str) -> list[str]:
    """The full git command line for `args`, with the hardening options in front."""
    return ["git", "-c", f"core.hooksPath={_no_hooks()}", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false",
            *args]


def run(cwd: Path | str, *args: str, input: str | bytes | None = None, timeout: float | None = None,
        binary: bool = False) -> subprocess.CompletedProcess:
    """Run one git command in `cwd`. Never raises for a failure: check `returncode` (TIMED_OUT for a timeout)."""
    if timeout is None:
        timeout = NETWORK_TIMEOUT if args and args[0] in NETWORK_COMMANDS else LOCAL_TIMEOUT
    command = argv(*args)
    text = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, env=env(), timeout=timeout, input=input,
                              stdin=None if input is not None else subprocess.DEVNULL, **text)
    except subprocess.TimeoutExpired:
        empty = b"" if binary else ""
        message = f"git {' '.join(args[:2])} timed out after {timeout:g} s"
        return subprocess.CompletedProcess(command, TIMED_OUT, empty, message.encode() if binary else message)
    except OSError as exc:  # git isn't installed, or cwd is gone
        empty = b"" if binary else ""
        message = f"couldn't run git: {exc}"
        return subprocess.CompletedProcess(command, 127, empty, message.encode() if binary else message)


def ok(cwd: Path | str, *args: str, timeout: float | None = None) -> bool:
    """Whether a git command succeeds (for questions such as `merge-base --is-ancestor`)."""
    return run(cwd, *args, timeout=timeout).returncode == 0


def out(cwd: Path | str, *args: str, check: bool = True, timeout: float | None = None) -> str:
    """A git command's stripped output. With `check`, a failure raises GitError; without, it reads as ""."""
    proc = run(cwd, *args, timeout=timeout)
    if proc.returncode != 0:
        if check:
            raise GitError(proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed")
        return ""
    return proc.stdout.strip()


def show(cwd: Path | str, spec: str) -> bytes | None:
    """`git show REF:PATH` as bytes, or None if it doesn't exist there."""
    proc = run(cwd, "show", spec, binary=True)
    return proc.stdout if proc.returncode == 0 else None
