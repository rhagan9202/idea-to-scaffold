"""The `its` command line. It only parses arguments and renders what the service layer (`service.py`) returns:

- `parser.py`: the commands, grouped in `its --help`, and their options;
- `commands.py`: one function per command, from its arguments to one service call;
- `render.py`: each payload as text, or as JSON with `--json`;
- `app.py`: `main`, which runs a command and keeps the output contract whatever happens.
"""
from .app import main
from .parser import build_parser

__all__ = ["build_parser", "main"]
