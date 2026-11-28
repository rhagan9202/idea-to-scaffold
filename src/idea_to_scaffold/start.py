"""`its start`: claiming an item, on the integration branch or with a branch and worktree of its own, running
setup in a new worktree, and the commands that start an agent there."""
from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
from pathlib import Path

from . import claims as cl
from . import deps, gate, gitx, ledger, results
from .backlog import (
    BacklogError,
    blocked_by,
    check_unique,
    find_item,
    load_backlog,
    set_status,
)
from .checks import (
    Check,
    run_check,
)
from .contract import (
    EXIT_BLOCKED,
    EXIT_COMPLETE,
    EXIT_NOTHING_READY,
    EXIT_OVERLAP,
    SCHEMA,
    WorkError,
)
from .locking import holding_lock
from .mirror import refresh_mirror
from .package import Package, PackageError
from .view import _running, closed_ids, integration_branch, item_payload, next_payload, overlaps_with_running, remote_of, view, wip_warning


def _quote(arg: str) -> str:
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


def launch_commands(ident: str, worktree: str | None) -> dict:
    """Commands that start an agent on the item. its prints them; it never launches an agent itself.

    `claude` and `codex` are shell lines quoted for this platform (cmd.exe on Windows); `argv` and `cwd` give the same
    command unquoted, for a program to run without a shell."""
    prompt = f"Read AGENTS.md, then follow docs/playbooks/implement-task.md for {ident}."
    cd = f"{results.cd_command(worktree)} && " if worktree else ""
    return {
        "claude": f"{cd}claude {_quote(prompt)}", "codex": f"{cd}codex {_quote(prompt)}",
        "cwd": worktree, "argv": {"claude": ["claude", prompt], "codex": ["codex", prompt]},
    }


def _commands(pkg: Package, ident: str, worktree: str | None) -> dict:
    """The interactive launch commands, plus the headless ones (`results.headless_commands`) inside git."""
    commands = launch_commands(ident, worktree)
    headless = results.headless_commands(pkg, ident, worktree)
    if headless is not None:
        commands["headless"] = headless
    return commands


def start(pkg: Package, ident: str | None, *, setup: bool = True, **kwargs) -> dict:
    """`_begin`, recorded in the run ledger. A new worktree then gets the setup command (`commands.setup`), as the
    lead and before any agent runs in it, unless `setup` is False (`--no-setup`); a failed setup is reported, and
    the claim is kept."""
    payload = _begin(pkg, ident, **kwargs)
    claim = payload.get("claim") or {}
    if claim.get("worktree") and not payload.get("claim_held_here"):
        payload["setup"] = run_setup(pkg, payload["id"], Path(claim["worktree"])) if setup else None
    elif not claim and cl.in_git(pkg.root):
        record_setup_output(pkg, payload["id"], cl.toplevel(pkg.root))  # the plain flow: setup ran here already
    if not payload.get("claim_held_here"):
        payload["run"] = ledger.new_run(pkg, payload["id"])
    ledger.record(pkg, payload["id"], "start", "claimed" if claim else "in-progress", agent=kwargs.get("agent"),
                  branch=claim.get("branch"), worktree=claim.get("worktree"),
                  touches=len(payload["item"]["touches"]) if payload.get("item", {}).get("touches") else None)
    try:
        wip = wip_warning(pkg, view(pkg, kwargs.get("base")))
    except WorkError:
        wip = None
    payload["warnings"] = [*payload.get("warnings", []), wip] if wip else payload.get("warnings", [])
    return payload


def run_setup(pkg: Package, ident: str, worktree: Path) -> dict | None:
    """Run the integration branch's setup command in a new claim worktree, then record the lockfiles it wrote
    (`claims.record_setup`), so the gate doesn't take them for the agent's dependency changes. None when there is
    nothing to run (a planning package, or no setup command)."""
    if not pkg.repo_layout:
        return None
    rel = pkg.root.resolve().relative_to(cl.toplevel(pkg.root).resolve())
    try:
        here = Package.find(worktree / rel)
    except PackageError:
        return None
    command = (here.commands.get("setup") or "").strip()
    if not command or command == "true":
        return None
    settings = gate.Policy.of(here.manifest, "worktree").run
    result = run_check(Check("setup", command), here.root, settings)
    result["lockfiles"] = sorted(record_setup_output(pkg, ident, worktree))
    return result


def record_setup_output(pkg: Package, ident: str, checkout: Path) -> dict[str, str]:
    """Record the lockfiles that differ from HEAD in `checkout` (new or changed) as setup's output for `ident`."""
    out = gitx.run(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    files = {}
    if out.returncode == 0:
        for entry in out.stdout.split("\0"):
            path = entry[3:]
            if len(entry) > 3 and deps.kind_of(path) == "lock" and entry[:2] != " D":
                try:
                    text = (checkout / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                files[path] = gate.content_digest(text)
    try:
        cl.record_setup(pkg.root, ident, files)
    except (OSError, cl.GitError):
        return {}
    return files


def _begin(
    pkg: Package,
    ident: str | None,
    *,
    force: bool = False,
    branch: bool = False,
    worktree: str | None = None,
    agent: str | None = None,
    base: str | None = None,
    allow_overlap: bool = False,
    push: bool | None = None,
) -> dict:
    """Start an item. With no `ident`, start the next item that can start now (`start --next`): picking and claiming
    happen under one lock, so two agents asking at the same moment get different items.

    Without `branch` or `worktree`, set a ready `todo` item to `in-progress` in the backlog. With either, claim it
    instead: create the claim branch `its/<ID>-<slug>` from the integration branch (checked out here, or in a new
    worktree), and leave the backlog alone, since the branch is the claim. `force` also starts an item whose
    dependencies aren't done, or a `blocked` one.

    Items only run in parallel when their Touches confirm they change different files: starting an item that might
    overlap an active claim or an in-progress item is refused unless `allow_overlap`.

    A claim is pushed to the remote (`origin`, or `remote` in package.yaml) when there is one, so other machines see
    it; the push only succeeds if nobody claimed the item there first. `push=False` (`--no-push`), or
    `push_claims: false` in package.yaml, keeps it local.
    """
    previous_branch = cl.current_branch(pkg.root) if cl.in_git(pkg.root) else None
    picking, lost = ident is None, []  # `start --next` tries the next item when another machine won the race
    while True:
        with holding_lock(pkg):
            if picking:
                ident = _pick_next(pkg, base, skip=set(lost))
            payload = _start(pkg, ident, force=force, branch=branch, worktree=worktree, agent=agent, base=base,
                             allow_overlap=allow_overlap)
        if lost:
            payload["lost_to_others"] = list(lost)
        claim = payload.get("claim")
        if push is None:
            push = pkg.get("push_claims") is not False
        payload["pushed"] = False
        if not claim or payload.get("claim_held_here") or not push:
            return payload
        remote = remote_of(pkg)
        if remote is None:
            return payload
        try:  # outside the lock: a slow network never holds up other agents on this machine
            cl.push_claim(pkg.root, remote, ident, claim["branch"])
        except cl.PushRejected as exc:
            with contextlib.suppress(cl.GitError):
                if claim["worktree"] is None and previous_branch:  # --branch checked it out here: go back first
                    cl.git(pkg.root, "switch", "--quiet", previous_branch)
                cl.release_claim(pkg.root, ident, force=True)
            if picking and len(lost) < PICK_RETRIES:
                lost.append(ident)
                continue
            theirs = {"id": ident, "branch": exc.branch, "remote": remote} if exc.branch else None
            raise WorkError("claimed", f"{exc}; your local claim was undone",
                            details={"id": ident, "remote": remote, "claim": theirs, "lost_to_others": lost or None}) \
                from exc
        except cl.GitError as exc:
            payload["push_error"] = str(exc)
            return payload
        payload["pushed"] = True
        payload["remote"] = remote
        return payload


PICK_RETRIES = 5  # items `start --next` gives up to other machines before it stops trying


def _pick_next(pkg: Package, base: str | None, skip: set[str] | frozenset[str] = frozenset()) -> str:
    """The item `its next` would offer, for `start --next`, passing over `skip` (lost to another machine)."""
    out = next_payload(pkg, base=base, skip=skip)
    if out["item"] is None:
        raise WorkError("nothing_ready", f"nothing can start now ({out['reason']})",
                        EXIT_COMPLETE if out["reason"] == "complete" else EXIT_NOTHING_READY,
                        {"reason": out["reason"]})
    return out["item"]["id"]


def _start(
    pkg: Package, ident: str, *, force: bool, branch: bool, worktree: str | None, agent: str | None,
    base: str | None, allow_overlap: bool,
) -> dict:
    """`start`, run under the lock: readiness, claims and overlap are checked and the claim made in one step."""
    claiming = branch or worktree is not None
    v = view(pkg, base)
    # Readiness and overlap always come from the integration branch (v.items). A plain start writes the working
    # tree's backlog, so the item's own status is read from there.
    items = v.items if claiming else load_backlog(pkg)
    item = find_item(items, ident)
    if not claiming:
        check_unique(items)
    if item.placeholder:
        raise WorkError("placeholder", f"{ident} has a placeholder ID; give it a permanent T-n.m ID before starting it",
                        details={"id": ident})
    claim = next((c for c in v.claims if c.id == ident), None)
    if claim is not None:
        here = v.branch == claim.branch or bool(
            claim.worktree and cl.toplevel(pkg.root).resolve() == Path(claim.worktree).resolve())
        if here:
            return {
                "schema": SCHEMA, "ok": True, "id": ident, "previous_status": item.status, "status": item.status,
                "forced": False, "claim": claim.to_dict(), "claim_held_here": True,
                "item": item_payload(pkg, item, items), "commands": _commands(pkg, ident, claim.worktree),
            }
        where = f" in {claim.worktree}" if claim.worktree else ""
        raise WorkError("claimed", f"{ident} is already claimed by branch {claim.branch}{where}",
                        details={"id": ident, "claim": claim.to_dict()})
    if item.status == "in-progress" and (claiming or not force):  # --force here records the start again
        raise WorkError("already_started", f"{ident} is already in-progress", details={"id": ident})
    if item.status not in ("todo", "in-progress") and not (force and item.status == "blocked"):
        raise WorkError("bad_status", f"{ident} is {item.status or 'missing a status'}; only todo items can be started",
                        details={"id": ident, "status": item.status})
    base_item = next((i for i in v.items if i.id == ident), item)
    waiting = blocked_by(base_item, v.items)
    if waiting and not force:
        where = f" on {v.source}" if v.source != "working tree" else ""
        raise WorkError(
            "blocked",
            f"{ident} depends on {', '.join(waiting)}, which {'is' if len(waiting) == 1 else 'are'} not done{where}",
            EXIT_BLOCKED, {"id": ident, "blocked_by": waiting},
        )
    running = dict(_running(v))
    clash = overlaps_with_running(v, item)
    if clash and not allow_overlap:
        others = "; ".join(f"{o} (touches {', '.join(running.get(o, [])) or 'anything: no Touches listed'})"
                           for o in clash)
        why = ("it lists no Touches, so it could change any file" if not item.touches
               else f"its Touches ({', '.join(item.touches)}) may change the same files")
        raise WorkError(
            "overlap",
            f"{ident} can't run alongside {others}: {why}. Finish those first, "
            "list non-overlapping Touches on both items, or pass --allow-overlap",
            EXIT_OVERLAP, {"id": ident, "overlaps": clash, "touches_listed": bool(item.touches),
                           "overlapping_touches": {other: running.get(other, []) for other in clash}},
        )
    forced = bool(waiting) or item.status != "todo" or bool(clash)
    if not claiming:
        if v.behind:
            raise WorkError("behind_remote", f"{v.branch} is {v.behind} commit{'s' if v.behind != 1 else ''} behind "
                            f"{v.base}, so this backlog is stale; pull first (git pull --ff-only), or claim the item "
                            f"from {v.base} with its start {ident} --worktree",
                            details={"id": ident, "branch": v.branch, "base": v.base, "behind": v.behind})
        try:
            previous = set_status(pkg, ident, "in-progress")
        except BacklogError as exc:
            raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
        if cl.in_git(pkg.root):  # the fork point the gate diffs from, kept where the agent can't edit it
            head = cl.git(pkg.root, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
            if head:
                cl.record_fork(pkg.root, ident, head)
        refresh_mirror(pkg)
        items = load_backlog(pkg)
        return {
            "schema": SCHEMA, "ok": True, "id": ident, "previous_status": previous, "status": "in-progress",
            "forced": forced, "claim": None, "item": item_payload(pkg, find_item(items, ident), items),
        }
    if v.base is None:
        raise WorkError("no_base", f"can't claim {ident}: this is not a git repository with an integration branch "
                        f"({integration_branch(pkg) if base is None else base})", details={"id": ident})
    path = None
    if worktree is not None:
        top = cl.toplevel(pkg.root)
        path = Path(worktree) if worktree else top.parent / f"{top.name}-{ident}"
        path = path if path.is_absolute() else (Path.cwd() / path)
        if path.exists():
            raise WorkError("worktree_exists", f"{path} already exists; pass another path to --worktree",
                            details={"id": ident, "worktree": str(path)})
    try:
        claim = cl.create_claim(pkg.root, ident, item.title, base_ref=v.base, worktree=path, agent=agent,
                                closed=closed_ids(items), integration=base or integration_branch(pkg))
    except cl.ClaimTaken as exc:
        raise WorkError("claimed", str(exc), details={"id": ident, "claim": exc.claim.to_dict()}) from exc
    except cl.GitError as exc:
        raise WorkError("git_error", str(exc), details={"id": ident}) from exc
    held_out = hide_acceptance(pkg, path, v.base) if path is not None else None
    return {
        "schema": SCHEMA, "ok": True, "id": ident, "previous_status": item.status, "status": item.status,
        "forced": forced, "claim": claim.to_dict(), "item": item_payload(pkg, item, items), "held_out": held_out,
        "commands": _commands(pkg, ident, claim.worktree),
    }


def hide_acceptance(pkg: Package, worktree: Path, base: str) -> str | None:
    """Leave the held-out acceptance tests out of an agent's worktree with a sparse checkout, so the agent can't
    read them and write code to the test. Returns the hidden folder, or None when the integration branch has none.

    This keeps the tests out of view, not secret: they are still in the repository's history. `its land` runs them
    on each merged result (`commands.acceptance`), and the gate refuses a branch that changes them."""
    manifest = gate.manifest_at(pkg, base)
    policy = gate.Policy.of(manifest or {}, base)
    prefix = gate.run_git(pkg.root, "rev-parse", "--show-prefix", check=False).strip()
    folder = prefix + policy.acceptance
    if not gate.run_git(pkg.root, "ls-tree", "-d", "--name-only", base, "--", folder.rstrip("/"), check=False).strip():
        return None
    proc = gitx.run(worktree, "sparse-checkout", "set", "--no-cone", "/*", f"!/{folder}")
    if proc.returncode != 0:
        raise WorkError("git_error", f"couldn't hide {folder} from the worktree: {proc.stderr.strip()}",
                        details={"worktree": str(worktree)})
    return folder
