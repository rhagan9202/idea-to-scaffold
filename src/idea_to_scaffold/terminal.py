"""Whether a file descriptor is a terminal that a person types at."""
from __future__ import annotations

import os


def is_terminal(fd: int) -> bool:
    """Whether `fd` is a terminal that a person types at. On Windows `isatty` is true of any character device,
    NUL included, and NUL is what a tool usually gives a command as its stdin, so there the handle must also be a
    console."""
    try:
        if not os.isatty(fd):
            return False
        return os.name != "nt" or _windows_console(fd)
    except (OSError, ValueError):
        return False


def _windows_console(fd: int) -> bool:  # pragma: no cover - Windows only
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_mode = ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleMode
    get_mode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_mode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    return bool(get_mode(wintypes.HANDLE(msvcrt.get_osfhandle(fd)), ctypes.byref(mode)))
