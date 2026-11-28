"""Claims for parallel work: the branch is the claim, plus a lock so two agents on one machine can't race.

An item is claimed when a local or remote branch named ``its/<ID>`` or ``its/<ID>-<slug>`` exists. Branches already
travel to every place agents run (other worktrees, other machines once pushed, cloud agents), so they need no extra
state store, and an abandoned claim is just a branch to delete.

On one machine, ``its start`` holds an OS file lock (see ``locking.py``) while it checks readiness, claims and overlap
and creates the branch, so two agents starting at the same moment can't both win. The repository's state folder,
outside the repository (``locking.state_dir``), keeps ``claims.json``: who started each claim, where its worktree
is, and when, and which integration branch it lands on.

Readiness comes from the integration branch (``main`` unless ``integration_branch`` in ``package.yaml`` says
otherwise): an item's dependency counts as done when it is done there, that is, merged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import gitx, locking

BRANCH_PREFIX = "its/"
# Only permanent IDs can be claimed: placeholders (T-new-...) are renumbered before anyone starts them, and a
# placeholder pattern would also swallow the slug (`its/T-new-foo-store` would read as the ID `T-new-foo-store`).
BRANCH_RE = re.compile(r"^(?:refs/heads/|refs/remotes/[^/]+/)?its/(T-\d+\.\d+)(?:-[a-z0-9-]*)?$")


GitError = gitx.GitError


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return gitx.out(cwd, *args, check=check)


def in_git(cwd: Path) -> bool:
    proc = gitx.run(cwd, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def toplevel(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel"))


def current_branch(cwd: Path) -> str | None:
    """The checked-out branch, or None on a detached HEAD."""
    name = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return name or None


def ref_exists(cwd: Path, ref: str) -> bool:
    return bool(git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False))


def resolve_base(cwd: Path, base: str, remote: str | None = "origin") -> str | None:
    """The ref to read the integration branch from: the local branch, else its remote-tracking copy. When the local
    branch is only behind the remote copy (others merged and you fetched, but haven't pulled), the remote copy is
    newer and is used, so merged work counts as soon as it is fetched."""
    local = base if ref_exists(cwd, base) else None
    tracking = f"{remote}/{base}" if remote and ref_exists(cwd, f"{remote}/{base}") else None
    if local and tracking:
        a, b = git(cwd, "rev-parse", local), git(cwd, "rev-parse", tracking)
        if a != b and gitx.ok(cwd, "merge-base", "--is-ancestor", a, b):
            return tracking
    return local or tracking


def remotes(cwd: Path) -> list[str]:
    return git(cwd, "remote", check=False).split()


def default_remote(cwd: Path, configured: str | None = None) -> str | None:
    """The remote claims are shared through: `remote` in package.yaml, else `origin` if it exists."""
    names = remotes(cwd)
    if configured:
        return configured if configured in names else None
    return "origin" if "origin" in names else None


def fetch(cwd: Path, remote: str, base: str) -> None:
    """Fetch the integration branch and every claim branch, pruning claim branches deleted on the remote."""
    proc = gitx.run(cwd, "fetch", "--quiet", "--prune", remote, f"+refs/heads/{base}:refs/remotes/{remote}/{base}",
                    f"+refs/heads/its/*:refs/remotes/{remote}/its/*")
    if proc.returncode != 0:
        raise GitError(f"couldn't fetch from {remote}: {proc.stderr.strip()}")


class PushRejected(GitError):
    """The claim branch already exists on the remote: someone else claimed the item first (`branch`, when known)."""

    def __init__(self, message: str, branch: str | None = None):
        super().__init__(message)
        self.branch = branch


def push_claim(cwd: Path, remote: str, ident: str, branch: str) -> None:
    """Publish a claim. It only succeeds if no claim branch for the item exists on the remote yet, so the remote
    settles a race between machines: the first push wins."""
    proc = gitx.run(cwd, "ls-remote", "--heads", remote, f"refs/heads/its/{ident}", f"refs/heads/its/{ident}-*")
    if proc.returncode != 0:
        raise GitError(f"couldn't reach {remote}: {proc.stderr.strip()}")
    taken = [line.split("refs/heads/", 1)[1] for line in proc.stdout.splitlines() if "refs/heads/" in line]
    taken = [b for b in taken if item_of_branch(b) == ident]
    if taken:
        raise PushRejected(f"{ident} is already claimed on {remote} by branch {taken[0]}", taken[0])
    proc = gitx.run(cwd, "push", "--quiet", "--set-upstream", f"--force-with-lease=refs/heads/{branch}:", remote,
                    f"refs/heads/{branch}:refs/heads/{branch}")
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "stale info" in err or "rejected" in err or "already exists" in err:
            raise PushRejected(f"{ident} was claimed on {remote} first ({err.splitlines()[-1] if err else 'rejected'})",
                               branch)
        raise GitError(f"couldn't push {branch} to {remote}: {err}")


def commit_all(cwd: Path, message: str, paths: list[str] | None = None) -> str | None:
    """Stage every change in the checkout (only those under `paths`, if given), new files included, and commit them.
    Returns the new commit, or None when there was nothing to commit. Ignored files stay out, as in `git add -A`."""
    spec = ["--", *paths] if paths else []
    proc = gitx.run(cwd, "add", "--all", *spec)
    if proc.returncode != 0:
        raise GitError(f"couldn't stage the changes: {proc.stderr.strip()}")
    if gitx.run(cwd, "diff", "--cached", "--quiet", *spec).returncode == 0:
        return None
    proc = gitx.run(cwd, "commit", "--quiet", "-m", message, *spec)
    if proc.returncode != 0:
        raise GitError(f"couldn't commit: {proc.stderr.strip() or proc.stdout.strip()}")
    return git(cwd, "rev-parse", "HEAD")


def push_branch(cwd: Path, remote: str, branch: str) -> None:
    """Push a branch's new commits to the remote. Never forced: a branch someone else moved there is refused."""
    proc = gitx.run(cwd, "push", "--quiet", remote, f"refs/heads/{branch}:refs/heads/{branch}")
    if proc.returncode != 0:
        raise GitError(f"couldn't push {branch} to {remote}: {proc.stderr.strip()}")


def conflicting_files(cwd: Path, ours: str, theirs: str) -> list[str] | None:
    """Files that would conflict if `theirs` were merged into `ours` (a dry run with `git merge-tree`, which
    changes nothing). [] means it merges cleanly; None means git can't tell (git older than 2.38)."""
    proc = gitx.run(cwd, "merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs)
    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        return sorted(set(proc.stdout.splitlines()[1:]) - {""})
    return None


def ahead(cwd: Path, ref: str, base: str) -> int:
    """Commits on `ref` that aren't on `base`."""
    out = git(cwd, "rev-list", "--count", f"{base}..{ref}", check=False)
    return int(out) if out.isdigit() else 0


def read_at(cwd: Path, ref: str, path: Path) -> str | None:
    """A file's content at a ref, or None if it isn't there (or isn't text).

    The path is given to git relative to the file's own folder (`ref:./name`), so no path arithmetic is needed and
    symlinks and Windows short names or long-path prefixes can't make it fail."""
    if not path.parent.is_dir():
        return None
    data = gitx.show(path.parent, f"{ref}:./{path.name}")
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


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
    store = locking.state_dir(cwd)
    if store is None:
        raise GitError(f"{cwd} is not in a git repository")
    return store


@contextmanager
def claims_lock(cwd: Path, wait: float | None = None):
    """The repository's lock (shared by every worktree), yielding the claims store folder."""
    store = _store_dir(cwd)
    try:
        with locking.locked(locking.lock_path(cwd), wait):
            yield store
    except locking.LockTimeout as exc:
        raise GitError(str(exc)) from None


def main_worktree(cwd: Path) -> Path:
    """The repository's main worktree: a safe place to run git from when removing another worktree."""
    out = git(cwd, "worktree", "list", "--porcelain", check=False)
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree "))
    return toplevel(cwd)


def claim_of_worktree(cwd: Path) -> str | None:
    """The item whose claim worktree `cwd` is in, from the claims store (works on a detached HEAD too)."""
    try:
        top = toplevel(cwd).resolve()
        data = _load_store(_store_dir(cwd))
    except GitError:
        return None
    for ident, entry in data.items():
        worktree = entry.get("worktree") if isinstance(entry, dict) else None
        if worktree and Path(worktree).resolve() == top:
            return ident
    return None


def _load_store(store: Path) -> dict[str, dict]:
    try:
        data = json.loads((store / "claims.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_store(store: Path, data: dict[str, dict]) -> None:
    store.mkdir(parents=True, exist_ok=True)
    tmp = store / "claims.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
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
    orphaned: bool = False   # the item isn't in the integration branch's backlog (deleted, renumbered or a typo)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "branch": self.branch, "remote_only": self.remote, "worktree": self.worktree,
            "agent": self.agent, "started": self.started, "finished": self.finished, "orphaned": self.orphaned,
        }


def list_claims(
    cwd: Path, closed: set[str] | frozenset[str] = frozenset(), known: set[str] | frozenset[str] | None = None,
) -> list[Claim]:
    """Every claim branch, local and remote, with what the local store knows about it. `closed` are the items that
    are done or dropped on the integration branch: their claims are marked finished. With `known` (every ID in that
    backlog), a claim for any other ID is marked orphaned."""
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
        claim = Claim(ident, short, is_remote, finished=ident in closed,
                      orphaned=known is not None and ident not in known)
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
    closed: set[str] | frozenset[str] = frozenset(), switch: bool = True, integration: str | None = None,
) -> Claim:
    """Atomically check no claim exists and create the branch (and worktree). Raises GitError if it is taken."""
    branch = branch_name(ident, title)
    with claims_lock(cwd) as store:
        taken = [c for c in active_claims(list_claims(cwd, closed)) if c.id == ident]
        if taken:
            raise ClaimTaken(taken[0])
        if worktree is not None:
            git(cwd, "worktree", "add", "--no-track", "-b", branch, str(worktree), base_ref)
        elif switch:
            git(cwd, "switch", "--no-track", "-c", branch, base_ref)
        else:
            git(cwd, "branch", "--no-track", branch, base_ref)
        data = _load_store(store)
        started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        data[ident] = {"branch": branch, "worktree": str(worktree) if worktree else None, "agent": agent,
                       "started": started, "base": base_ref,
                       "integration_branch": integration}
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
    return gitx.ok(cwd, "merge-base", "--is-ancestor", ref, base_ref)


def _refresh_remote(cwd: Path, name: str) -> str | None:
    """Fetch one pushed claim branch (`origin/its/T-2.1-store`) so we judge what is really on the remote. Returns its
    tip, or None if it is gone from the remote (its stale tracking ref is removed)."""
    remote_name, branch = name.split("/", 1)
    proc = gitx.run(cwd, "fetch", "--quiet", remote_name, f"+refs/heads/{branch}:refs/remotes/{name}")
    if proc.returncode != 0:
        if "couldn't find remote ref" in proc.stderr.lower():
            git(cwd, "update-ref", "-d", f"refs/remotes/{name}", check=False)
            return None
        raise GitError(f"couldn't fetch {name} to check it before deleting it: {proc.stderr.strip()}")
    return git(cwd, "rev-parse", f"refs/remotes/{name}")


def release_claim(
    cwd: Path, ident: str, *, force: bool = False, remote: bool = False, base_ref: str | None = None,
    closed: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Remove a claim: its worktree and its local branch. With `remote`, also delete its pushed branches.

    Everything is checked before anything changes, so a refusal leaves the claim as it was. Without `force`, a claim
    is only released if losing it loses no work: its worktree has no uncommitted changes, and its branches are merged
    into `base_ref` or its item is done there (`closed`, which also covers squash merges). Pushed branches are fetched
    first and deleted with a lease, so commits someone pushed since are never deleted unseen.
    """
    run_dir = main_worktree(cwd)
    deletions: list[tuple[str, str]] = []
    with claims_lock(run_dir) as store:
        data = _load_store(store)
        entry = data.get(ident) or {}
        local = [c for c in list_claims(run_dir) if c.id == ident and not c.remote]
        pushed = remote_claim_branches(run_dir, ident)
        if remote:
            tips = {name: _refresh_remote(run_dir, name) for name in pushed}
            pushed = [name for name, tip in tips.items() if tip is not None]
        if not local and not entry and not (remote and pushed):
            if force:
                return {"id": ident, "deleted_branches": [], "removed_worktree": None, "deleted_remote_branches": [],
                        "remote_branches": pushed, "released": False}
            hint = f" (it has pushed branches: {', '.join(pushed)}; add --remote)" if pushed else ""
            raise GitError(f"{ident} has no local claim{hint}")

        worktree = entry.get("worktree")
        wt = Path(worktree) if worktree and Path(worktree).exists() else None
        if wt is not None:
            here = Path.cwd().resolve()
            if here == wt.resolve() or wt.resolve() in here.parents:
                raise GitError(f"you are inside {wt}, the worktree this would remove; run it from another folder")
        checked_out = current_branch(run_dir)
        for claim in local:
            if claim.branch == checked_out:
                raise GitError(f"{claim.branch} is checked out in {run_dir}; switch to another branch first")
        setup_only = False  # the worktree holds nothing but what its's setup wrote: removing it loses no work
        if not force:
            if wt is not None and uncommitted(wt, ident, store):
                raise GitError(f"{wt} has uncommitted changes; commit them, or add --force to discard them")
            setup_only = wt is not None and bool(git(wt, "status", "--porcelain", check=False))
            if ident not in closed:
                unmerged = [c.branch for c in local if not _merged(run_dir, f"refs/heads/{c.branch}", base_ref)]
                if remote:
                    unmerged += [b for b in pushed if not _merged(run_dir, f"refs/remotes/{b}", base_ref)]
                if unmerged:
                    raise GitError(f"{', '.join(unmerged)} has work that isn't merged; "
                                   "add --force to delete it anyway")

        removed_worktree = None
        if wt is not None:
            git(run_dir, "worktree", "remove", *(["--force"] if force or setup_only else []), str(wt))
            removed_worktree = str(wt)
        elif worktree:
            git(run_dir, "worktree", "prune", check=False)  # deleted by hand: forget it
        deleted = []
        for claim in local:
            git(run_dir, "branch", "-D", claim.branch)  # merged-ness was checked above, against base_ref
            deleted.append(claim.branch)
        if remote:
            deletions = [(name, git(run_dir, "rev-parse", f"refs/remotes/{name}")) for name in pushed]
        data.pop(ident, None)
        _save_store(store, data)

    # Network pushes happen outside the lock, so a slow remote never holds up other agents.
    deleted_remote = []
    for name, tip in deletions:
        remote_name, branch = name.split("/", 1)
        git(run_dir, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{tip}", remote_name,
            f":refs/heads/{branch}")
        deleted_remote.append(name)
    return {"id": ident, "deleted_branches": deleted, "removed_worktree": removed_worktree,
            "deleted_remote_branches": deleted_remote, "remote_branches": still_pushed(run_dir, ident),
            "released": True}


def still_pushed(cwd: Path, ident: str) -> list[str]:
    """The item's pushed claim branches that are really still on the remote. A remote-tracking ref for a branch
    someone deleted there (`land --release` on another machine) is stale: it is removed, not reported."""
    out = []
    for name in remote_claim_branches(cwd, ident):
        try:
            if _refresh_remote(cwd, name) is None:
                continue
        except GitError:
            pass  # the remote can't be reached: report what we last saw
        out.append(name)
    return out


def claim_entry(cwd: Path, ident: str) -> dict:
    """What the local claims store recorded about an item's claim (its branch, worktree and base), or {}."""
    try:
        entry = _load_store(_store_dir(cwd)).get(ident)
    except GitError:
        return {}
    return entry if isinstance(entry, dict) else {}


# --- fork points for items started on the integration branch ---------------------------------------------------
# A plain `its start` (no claim branch) works on the integration branch itself, where a merge base can't say what
# the item changed. So start records the commit it began from in the state folder, outside the repository, and the
# gate diffs from there.

def _load_forks(store: Path) -> dict[str, str]:
    try:
        data = json.loads((store / "forks.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_forks(store: Path, data: dict[str, str]) -> None:
    store.mkdir(parents=True, exist_ok=True)
    tmp = store / "forks.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, store / "forks.json")


def record_fork(cwd: Path, ident: str, commit: str) -> None:
    store = _store_dir(cwd)
    data = _load_forks(store)
    data[ident] = commit
    _save_forks(store, data)


def fork_of(cwd: Path, ident: str) -> str | None:
    try:
        return _load_forks(_store_dir(cwd)).get(ident)
    except GitError:
        return None


def clear_fork(cwd: Path, ident: str) -> None:
    """Forget an item's fork point. Best effort: an agent's sandbox may not let it write the state folder."""
    try:
        store = _store_dir(cwd)
    except GitError:
        return
    data = _load_forks(store)
    if data.pop(ident, None) is not None:
        try:
            _save_forks(store, data)
        except OSError:
            pass


# --- Touches overlap ------------------------------------------------------------------------------------------

GLOB_CHARS = re.compile(r"[*?\[{]")


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


# --- what `its`'s own setup wrote -----------------------------------------------------------------------------
# `its start` runs the setup command in a new worktree, as the lead and before any agent runs. A lockfile that setup
# writes (`uv.lock` on a project without one) isn't the agent's dependency change, so the gate lets it through when
# its content is still what setup wrote. The record lives in the state folder, outside every checkout.

def record_setup(cwd: Path, ident: str, files: dict[str, str]) -> None:
    """Remember the lockfiles `its`'s setup wrote for an item: repository path to content digest."""
    store = _store_dir(cwd)
    data = _load_json(store / "setup.json")
    if files:
        data[ident] = files
    else:
        data.pop(ident, None)
    _save_json(store, "setup.json", data)


def content_digest(text: str | None) -> str:
    """A digest of a file's text as the gate reads it, for the setup record."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text is not None else ""


def uncommitted(checkout: Path, ident: str | None = None, store: Path | None = None) -> list[str]:
    """The paths with uncommitted changes in `checkout`, without the lockfiles `its`'s setup wrote there for `ident`
    that still hold what it wrote: those aren't anyone's work, so they don't keep a claim from being released."""
    proc = gitx.run(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if proc.returncode != 0:
        raise GitError(f"couldn't read the status of {checkout}: {proc.stderr.strip()}")
    written = {}
    if ident:
        entry = _load_json((store or _store_dir(checkout)) / "setup.json").get(ident)
        written = entry if isinstance(entry, dict) else {}
    entries, out = iter(proc.stdout.split("\0")), []
    for entry in entries:
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            next(entries, None)  # the rename's old path
        path = entry[3:]
        if path in written and entry[:2] in ("??", " M"):
            try:
                text = (checkout / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = None
            if text is not None and written[path] == content_digest(text):
                continue
        out.append(path)
    return out


def setup_of(cwd: Path, ident: str) -> dict[str, str]:
    try:
        entry = _load_json(_store_dir(cwd) / "setup.json").get(ident)
    except GitError:
        return {}
    return {str(k): str(v) for k, v in entry.items()} if isinstance(entry, dict) else {}


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# --- review verdicts ---------------------------------------------------------------------------------------------
# `its review` notes what it put in front of the reviewer, and `its review --reply` the verdict, per item, so that
# `its land` and `its status` can act on it (review.py). Like the setup record, it is the lead's record, not proof.

def record_review(cwd: Path, ident: str, *, prompt: dict | None = None, verdict: dict | None = None) -> None:
    """Record the review prompt `its review` made for an item, or the verdict on it."""
    store = _store_dir(cwd)
    data = _load_json(store / "reviews.json")
    entry = data.get(ident) if isinstance(data.get(ident), dict) else {}
    if prompt is not None:
        entry["prompt"] = prompt
    if verdict is not None:
        entry["verdict"] = verdict
    data[ident] = entry
    _save_json(store, "reviews.json", data)


def reviews_of(cwd: Path, ident: str) -> dict:
    entry = _load_json(_store_dir(cwd) / "reviews.json").get(ident)
    return entry if isinstance(entry, dict) else {}


def _save_json(store: Path, name: str, data: dict) -> None:
    store.mkdir(parents=True, exist_ok=True)
    tmp = store / f"{name}.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, store / name)
