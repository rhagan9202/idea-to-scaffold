"""Running `its`: parse the command line, run the command, and keep the output contract whatever happens.

Output is UTF-8 on pipes and files, a reader that goes away (`its status | head`) ends the command quietly, and with
`--json` even a bug in `its` comes out as the JSON error envelope (spec section 16).
"""
from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
from pathlib import Path

from .. import __version__
from ..contract import EXIT_ERROR, EXIT_USAGE, WorkError
from ..terminal import is_terminal
from .parser import Parser, build_parser
from .render import fail


def _utf8_streams() -> None:
    """Read and write UTF-8 on the process's own pipes and files, whatever the locale says (cp1252 on most Windows
    machines), so `--json` output and the hook's stdin always mean the same thing. A console is left alone: Python
    already writes Unicode to it."""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not getattr(sys, f"__{name}__") or not hasattr(stream, "reconfigure"):
            continue  # replaced by a caller (a test, an embedding program): theirs to set
        try:
            if is_terminal(stream.fileno()) or (stream.encoding or "").lower().replace("-", "") == "utf8":
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def _stdout_closed() -> bool:
    """Whether stdout's reader has gone. When the failed write left its bytes in the buffer, flushing again fails the
    same way. When it left nothing (a write too big to buffer went straight to the pipe), only a real write shows
    it, so a pipe gets a newline: harmless on the error path this is on, and an EINVAL from anything else leaves
    the pipe writable."""
    try:
        sys.stdout.flush()
    except OSError:
        return True
    try:
        fd = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):  # not a real file (a test's capture): it can't have gone
        return False
    try:
        if stat.S_ISFIFO(os.fstat(fd).st_mode):
            os.write(fd, b"\n")
    except OSError:
        return True
    return False


def _reader_gone() -> int:
    with contextlib.suppress(OSError, ValueError, AttributeError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    argv = sys.argv[1:] if argv is None else list(argv)
    Parser.json_errors = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # usage errors, --help and --version: return the code rather than exiting
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    finally:
        Parser.json_errors = False
    if args.chdir:
        for name in ("dir", "into", "package"):
            value = getattr(args, name, None)
            if isinstance(value, str) and not Path(value).is_absolute():
                setattr(args, name, str(Path(args.chdir) / value))
    try:
        return args.func(args)
    except BrokenPipeError:  # `its status | head`: the reader stopped reading, so stop writing, quietly
        return _reader_gone()
    except WorkError as err:
        return fail(args, err)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # the JSON contract holds even for a bug: agents always get an error envelope
        if isinstance(exc, OSError) and os.name == "nt" and exc.errno == errno.EINVAL and _stdout_closed():
            return _reader_gone()  # on Windows a write to a closed pipe fails with EINVAL, not EPIPE
        if not getattr(args, "json", False):
            raise
        return fail(args, WorkError("internal_error", f"{type(exc).__name__}: {exc}",
                                     details={"version": __version__}))
