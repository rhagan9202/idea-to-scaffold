r"""Where `its` keeps its own state, and the one lock for everything that reads then writes shared state.

**State.** Claims (`claims.json`), fork points (`forks.json`), the run ledger (`runs/`), the headless launch files
(`launch/`), the trusted integration-branch commits (`trust.json`), the lockfiles `its start`'s setup wrote
(`setup.json`) and review verdicts (`reviews.json`) live in a folder per repository *outside* the repository and its
git folder (`state_dir`):

- `$XDG_STATE_HOME/its/<repo>` when `XDG_STATE_HOME` is set, on any OS;
- otherwise `~/.local/state/its/<repo>` on Linux, `~/Library/Application Support/its/<repo>` on macOS and
  `%LOCALAPPDATA%\its\<repo>` on Windows.

An agent in a linked worktree can write the repository's shared git folder, even inside Claude Code's sandbox, so
state kept there (as it was before) is state the agent can rewrite: moving its own fork point, or editing the launch
settings of the next headless run. A sandbox that limits writes to the working folders blocks this folder. An agent
without a sandbox can still write anything its user can. State found in the old place (`<git common dir>/its/`) is
copied over once.

**The lock** is an OS file lock (`flock` on POSIX, `msvcrt.locking` on Windows) on a file that is never deleted. The
OS drops it when the holder exits, even after a crash, so there is no "stale lock" rule to get wrong and no way for
two processes to hold it at once. In a git repository it stays in ``$(git rev-parse --git-common-dir)/its/``,
where an agent's own `its done` (which takes it to change the backlog) can still reach it; the worst an agent can do
with it is hold it. Outside git it lives in the system temp folder, keyed by the package's path.

The lock is re-entrant within a thread: a function that holds it can call another that takes it again.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import gitx

LOCK_WAIT_SECONDS = 60.0
LOCK_NAME = "claims.lock"

_held = threading.local()


class LockTimeout(Exception):
    """Another `its` command held the lock for longer than LOCK_WAIT_SECONDS."""


def git_common_dir(cwd: Path) -> Path | None:
    """The repository's shared git folder (the main `.git`, also for linked worktrees), or None outside git."""
    proc = gitx.run(cwd, "rev-parse", "--git-common-dir")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = (Path(cwd) / common).resolve()
    return common


def git_store_dir(cwd: Path) -> Path | None:
    """`<git common dir>/its`, where the lock lives, or None outside a git repository."""
    common = git_common_dir(cwd)
    return common / "its" if common is not None else None


MIGRATED = ("claims.json", "forks.json")


def state_root() -> Path:
    """The folder that holds every repository's state folder (see the module docstring)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "its"
    home = Path.home()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / "its"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "its"
    return home / ".local" / "state" / "its"


def state_dir(cwd: Path) -> Path | None:
    """This repository's state folder, outside the repository (see the module docstring). None outside git.

    It is named after the repository's folder plus a hash of its git folder's real path, so every worktree of one
    repository shares it and two repositories never do. Nothing is created here: writers make the folder."""
    common = git_common_dir(cwd)
    if common is None:
        return None
    real = os.path.realpath(common)
    name = Path(real).parent.name if Path(real).name == ".git" else Path(real).name
    key = hashlib.sha256(os.path.normcase(real).encode("utf-8")).hexdigest()[:12]
    folder = state_root() / f"{_safe(name)}-{key}"
    old = common / "its"
    if not folder.exists() and any((old / f).is_file() for f in (*MIGRATED, "runs")):
        _migrate(old, folder)
    return folder


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in name)[:40] or "repo"


def _migrate(old: Path, new: Path) -> None:
    """Copy the state an older `its` kept in the git folder. Best effort: a failure leaves the new folder empty."""
    try:
        new.mkdir(parents=True, exist_ok=True)
        for name in MIGRATED:
            if (old / name).is_file():
                shutil.copy2(old / name, new / name)
        if (old / "runs").is_dir():
            shutil.copytree(old / "runs", new / "runs", dirs_exist_ok=True)
    except OSError:
        pass


def lock_path(cwd: Path) -> Path:
    store = git_store_dir(cwd)
    if store is not None:
        return store / LOCK_NAME
    key = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"its-{key}.lock"


def _try_lock(fd: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def locked(path: Path, wait: float | None = None) -> Iterator[Path]:
    """Hold an exclusive lock on `path` (created if missing). Yields the lock file's folder."""
    path = Path(path)
    held: dict[str, int] = getattr(_held, "paths", None) or {}
    _held.paths = held
    key = os.path.realpath(path)
    if held.get(key):
        held[key] += 1
        try:
            yield path.parent
        finally:
            held[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + (LOCK_WAIT_SECONDS if wait is None else wait)
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise LockTimeout(f"another `its` command is still running (it holds {path}); try again when it ends")
            time.sleep(0.02)
        held[key] = 1
        try:
            yield path.parent
        finally:
            held.pop(key, None)
            _unlock(fd)
    finally:
        os.close(fd)


@contextmanager
def package_lock(cwd: Path, wait: float | None = None) -> Iterator[Path]:
    """The lock for the repository (or package folder) that `cwd` is in."""
    with locked(lock_path(cwd), wait) as folder:
        yield folder
