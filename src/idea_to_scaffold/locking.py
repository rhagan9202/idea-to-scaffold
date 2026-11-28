"""One lock for everything that reads then writes shared state: claims, and the backlog's Status fields.

The lock is an OS file lock (`flock` on POSIX, `msvcrt.locking` on Windows) on a file that is never deleted. The OS
drops it when the holder exits, even after a crash, so there is no "stale lock" rule to get wrong and no way for two
processes to hold it at once.

In a git repository the lock file is ``$(git rev-parse --git-common-dir)/its/claims.lock``, shared by every worktree.
Outside git it lives in the system temp folder, keyed by the package's path, so it never shows up in the package.

The lock is re-entrant within a thread: a function that holds it can call another that takes it again.
"""
from __future__ import annotations

import hashlib
import os
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


def git_store_dir(cwd: Path) -> Path | None:
    """`<git common dir>/its`, or None outside a git repository."""
    proc = gitx.run(cwd, "rev-parse", "--git-common-dir")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = (cwd / common).resolve()
    return common / "its"


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
