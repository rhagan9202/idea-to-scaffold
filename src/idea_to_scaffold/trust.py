"""Which commits on the integration branch `its` trusts, so that `its land` notices a main that moved without it.

A linked worktree shares its refs with the lead's checkout, so an agent can run `git update-ref refs/heads/main
<commit>` and move main past the gate (the round 4 review's S2). The gate reads its rules from main too (Verify,
Touches, the test commands, the waivers), so a moved main can also weaken the rules for the next land.

A commit on the integration branch is trusted when either:

- it is on the remote's copy of the branch (`origin/main`): pushed work, which a protected branch on the remote
  guards; or
- it is behind a tip that `its` recorded itself in the lead's checkout: the result of a land, a waiver, or a
  planning commit (`promote`, `renumber`, `block --commit`, ...). A commit `its` makes on top of untrusted work is
  not recorded, so it can't vouch for that work.

The record lives in the per-user state folder (`locking.state_dir`), which the agent's sandbox can't write. With no
record yet, the branch's tip is trusted as it is (trust on first use).

`its land` refuses (`base_untrusted`) while the integration branch has a commit that is neither, and lists them;
after a person has looked, `--accept-base` trusts them. `its status` warns (`main_moved`). A commit the lead made by
hand on main counts as untrusted until it is pushed or accepted: from inside git, nothing tells it apart from one
an agent made.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import gitx
from .locking import state_dir

FILE = "trust.json"
LIMIT = 200  # tips kept per branch, newest last
LIST_LIMIT = 20


def _file(cwd: Path) -> Path | None:
    folder = state_dir(cwd)
    return folder / FILE if folder else None


def _load(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(t) for t in v if isinstance(t, str)] for k, v in data.items() if isinstance(v, list)}


def tips(cwd: Path, branch: str) -> list[str]:
    return _load(_file(cwd)).get(branch, [])


def _commit(cwd: Path, ref: str) -> str | None:
    return gitx.out(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) or None


def untrusted(cwd: Path, branch: str, ref: str = "HEAD", remote: str | None = None) -> list[str] | None:
    """The commits on `ref` that aren't trusted, newest first, as "<short sha> <subject>" (at most LIST_LIMIT, the
    last line saying how many more). None when nothing is recorded for `branch` yet: the caller trusts it on first
    use."""
    known = tips(cwd, branch)
    if not known:
        return None
    negative = list(known)
    if remote and gitx.ok(cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"):
        negative.append(f"refs/remotes/{remote}/{branch}")
    proc = gitx.run(cwd, "rev-list", "--ignore-missing", "--oneline", ref, "--not", *negative)
    if proc.returncode != 0:
        raise gitx.GitError(proc.stderr.strip() or f"git rev-list {ref} failed")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) > LIST_LIMIT:
        lines = lines[:LIST_LIMIT] + [f"... and {len(lines) - LIST_LIMIT} more"]
    return lines


def record(cwd: Path, branch: str, ref: str = "HEAD", *, after: str | None = None,
           remote: str | None = None) -> bool:
    """Trust `ref` (and everything behind it) on `branch`. With `after`, the tip before `its` made the commit, only
    if that tip was trusted already, so a commit made on top of untrusted work doesn't vouch for it. Best effort: an
    unwritable state folder (an agent's sandbox) records nothing. Returns whether it recorded."""
    path = _file(cwd)
    commit = _commit(cwd, ref)
    if path is None or commit is None:
        return False
    if after is not None:
        left = untrusted(cwd, branch, after, remote)
        if left:  # None (nothing recorded yet) is first use: trusted
            return False
    data = _load(path)
    known = [t for t in data.get(branch, []) if t != commit] + [commit]
    data[branch] = known[-LIMIT:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{FILE}.{os.getpid()}")
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(temp, path)
    except OSError:
        return False
    return True
