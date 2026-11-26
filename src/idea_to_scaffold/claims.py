"""Claims for parallel work: the branch is the claim, plus a lock so two agents on one machine can't race.

An item is claimed when a local or remote branch named ``its/<ID>`` or ``its/<ID>-<slug>`` exists. Branches already
travel to every place agents run (other worktrees, other machines once pushed, cloud agents), so they need no extra
state store, and an abandoned claim is just a branch to delete.

On one machine, ``its start`` takes a short lock in git's shared directory (``$(git rev-parse --git-common-dir)/its/``)
while it checks for an existing claim and creates the branch, so two agents starting at the same moment can't both
win. The same directory keeps ``claims.json``: who started each claim, where its worktree is, and when.

Readiness comes from the integration branch (``main`` unless ``integration_branch`` in ``package.yaml`` says
otherwise): an item's dependency counts as done when it is done there, that is, merged.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .ids import TASK_PATTERN

BRANCH_PREFIX = "its/"
BRANCH_RE = re.compile(rf"^(?:refs/heads/|refs/remotes/[^/]+/)?its/({TASK_PATTERN})(?:-[a-z0-9-]*)?$")
LOCK_STALE_SECONDS = 30
LOCK_WAIT_SECONDS = 10


class GitError(Exception):
    """A git command failed, or the folder isn't in a git repository."""


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def in_git(cwd: Path) -> bool:
    proc = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, capture_output=True, text=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def toplevel(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel"))


def current_branch(cwd: Path) -> str | None:
    """The checked-out branch, or None on a detached HEAD."""
    name = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return name or None


def ref_exists(cwd: Path, ref: str) -> bool:
    return bool(git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False))


def resolve_base(cwd: Path, base: str) -> str | None:
    """The ref to read the integration branch from: the local branch, else its remote-tracking copy."""
    for ref in (base, f"origin/{base}"):
        if ref_exists(cwd, ref):
            return ref
    return None


def read_at(cwd: Path, ref: str, path: Path) -> str | None:
    """A file's content at a ref, or None if it isn't there."""
    rel = path.resolve().relative_to(toplevel(cwd).resolve()).as_posix()
    proc = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=cwd, capture_output=True)
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def item_of_branch(branch: str | None) -> str | None:
    """The backlog ID a claim branch is for (`its/T-2.1-store` -> `T-2.1`)."""
    if not branch:
        return None
    m = BRANCH_RE.match(branch)
    return m.group(1) if m else None


def branch_name(ident: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return f"{BRANCH_PREFIX}{ident}-{slug}" if slug else f"{BRANCH_PREFIX}{ident}"


# --- the claims store ------------------------------------------------------------------------------------------

def _store_dir(cwd: Path) -> Path:
    common = Path(git(cwd, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (cwd / common).resolve()
    return common / "its"


@contextmanager
def claims_lock(cwd: Path):
    """An exclusive lock shared by every worktree of the repository."""
    store = _store_dir(cwd)
    store.mkdir(parents=True, exist_ok=True)
    lock = store / "claims.lock"
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink(missing_ok=True)  # left behind by a process that died
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise GitError(f"another `its` command holds {lock}; try again, or delete it if nothing is running") from None
            time.sleep(0.05)
    try:
        yield store
    finally:
        lock.unlink(missing_ok=True)


def _load_store(store: Path) -> dict[str, dict]:
    try:
        data = json.loads((store / "claims.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_store(store: Path, data: dict[str, dict]) -> None:
    tmp = store / "claims.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, store / "claims.json")


@dataclass
class Claim:
    id: str
    branch: str              # short name, e.g. its/T-2.1-store or origin/its/T-2.1-store
    remote: bool
    worktree: str | None = None
    agent: str | None = None
    started: str | None = None
    finished: bool = False   # the item is done (or dropped) on the integration branch, so the claim can go

    def to_dict(self) -> dict:
        return {
            "id": self.id, "branch": self.branch, "remote": self.remote, "worktree": self.worktree,
            "agent": self.agent, "started": self.started, "finished": self.finished,
        }


def list_claims(cwd: Path, closed: set[str] | frozenset[str] = frozenset()) -> list[Claim]:
    """Every claim branch, local and remote, with what the local store knows about it. `closed` are the items that
    are done or dropped on the integration branch: their claims are marked finished."""
    refs = git(cwd, "for-each-ref", "--format=%(refname)", "refs/heads/its/", "refs/remotes/", check=False)
    store = _load_store(_store_dir(cwd))
    local: dict[str, Claim] = {}
    remote: dict[str, Claim] = {}
    for ref in refs.splitlines():
        ident = item_of_branch(ref)
        if ident is None:
            continue
        is_remote = ref.startswith("refs/remotes/")
        short = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
        claim = Claim(ident, short, is_remote, finished=ident in closed)
        entry = store.get(ident) or {}
        if not is_remote and entry.get("branch") == short:
            claim.worktree, claim.agent, claim.started = entry.get("worktree"), entry.get("agent"), entry.get("started")
        (remote if is_remote else local).setdefault(ident, claim)
    out = dict(remote)
    out.update(local)  # a local branch says more (worktree, agent) than its pushed copy
    return sorted(out.values(), key=lambda c: c.id)


def active_claims(claims: list[Claim]) -> list[Claim]:
    """Claims still holding their item."""
    return [c for c in claims if not c.finished]


def create_claim(
    cwd: Path, ident: str, title: str, *, base_ref: str, worktree: Path | None, agent: str | None,
    closed: set[str] | frozenset[str] = frozenset(), switch: bool = True,
) -> Claim:
    """Atomically check no claim exists and create the branch (and worktree). Raises GitError if it is taken."""
    branch = branch_name(ident, title)
    with claims_lock(cwd) as store:
        taken = [c for c in active_claims(list_claims(cwd, closed)) if c.id == ident]
        if taken:
            raise ClaimTaken(taken[0])
        if worktree is not None:
            git(cwd, "worktree", "add", "-b", branch, str(worktree), base_ref)
        elif switch:
            git(cwd, "switch", "-c", branch, base_ref)
        else:
            git(cwd, "branch", branch, base_ref)
        data = _load_store(store)
        started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        data[ident] = {"branch": branch, "worktree": str(worktree) if worktree else None, "agent": agent,
                       "started": started}
        _save_store(store, data)
    return Claim(ident, branch, False, str(worktree) if worktree else None, agent, started)


class ClaimTaken(GitError):
    def __init__(self, claim: Claim):
        self.claim = claim
        where = f" in {claim.worktree}" if claim.worktree else ""
        super().__init__(f"{claim.id} is already claimed by branch {claim.branch}{where}")


def remote_claim_branches(cwd: Path, ident: str) -> list[str]:
    """Every remote-tracking claim branch for an item, e.g. `origin/its/T-2.1-store`."""
    refs = git(cwd, "for-each-ref", "--format=%(refname)", "refs/remotes/", check=False)
    return [r.removeprefix("refs/remotes/") for r in refs.splitlines()
            if r.startswith("refs/remotes/") and item_of_branch(r) == ident]


def _merged(cwd: Path, ref: str, base_ref: str | None) -> bool:
    if base_ref is None:
        return False
    return subprocess.run(["git", "merge-base", "--is-ancestor", ref, base_ref], cwd=cwd,
                          capture_output=True).returncode == 0


def release_claim(
    cwd: Path, ident: str, *, force: bool = False, remote: bool = False, base_ref: str | None = None,
) -> dict:
    """Remove a claim: its worktree (if it is clean, or with force) and its local branch. With `remote`, also delete
    its pushed branches; one that isn't merged into `base_ref` is only deleted with force."""
    with claims_lock(cwd) as store:
        data = _load_store(store)
        entry = data.get(ident) or {}
        local = [c for c in list_claims(cwd) if c.id == ident and not c.remote]
        pushed = remote_claim_branches(cwd, ident)
        if not local and not entry and not (remote and pushed):
            hint = f" (it has pushed branches: {', '.join(pushed)}; add --remote)" if pushed else ""
            raise GitError(f"{ident} has no local claim{hint}")
        if remote and not force:
            unmerged = [b for b in pushed if not _merged(cwd, f"refs/remotes/{b}", base_ref)]
            if unmerged:
                raise GitError(f"{', '.join(unmerged)} has work that isn't merged; add --force to delete it anyway")
        removed_worktree = None
        worktree = entry.get("worktree")
        if worktree and Path(worktree).exists():
            git(cwd, "worktree", "remove", *(["--force"] if force else []), worktree)
            removed_worktree = worktree
        deleted = []
        for claim in local:
            if current_branch(cwd) == claim.branch:
                raise GitError(f"{claim.branch} is checked out here; switch to another branch first")
            git(cwd, "branch", "-D" if force else "-d", claim.branch)
            deleted.append(claim.branch)
        deleted_remote = []
        if remote:
            for name in pushed:
                remote_name, branch = name.split("/", 1)
                git(cwd, "push", remote_name, "--delete", branch)
                deleted_remote.append(name)
        data.pop(ident, None)
        _save_store(store, data)
    return {"id": ident, "deleted_branches": deleted, "removed_worktree": removed_worktree,
            "deleted_remote_branches": deleted_remote, "remote_branches": remote_claim_branches(cwd, ident)}


# --- Touches overlap ------------------------------------------------------------------------------------------

GLOB_CHARS = re.compile(r"[*?\[]")


def _norm(path: str) -> str:
    path = path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def paths_overlap(a: str, b: str) -> bool:
    """Whether two Touches entries (paths or globs) could name the same file. Errs towards yes."""
    a, b = _norm(a), _norm(b)
    ga, gb = GLOB_CHARS.search(a), GLOB_CHARS.search(b)
    if not ga and not gb:
        a_dir, b_dir = a.rstrip("/") + "/", b.rstrip("/") + "/"
        return a.rstrip("/") == b.rstrip("/") or b.startswith(a_dir) or a.startswith(b_dir)
    pa = a[: ga.start()] if ga else a
    pb = b[: gb.start()] if gb else b
    return pa.startswith(pb) or pb.startswith(pa)


def touches_overlap(a: list[str], b: list[str]) -> bool:
    """Items overlap if any of their paths do. An item with no Touches could touch anything."""
    if not a or not b:
        return True
    return any(paths_overlap(x, y) for x in a for y in b)
