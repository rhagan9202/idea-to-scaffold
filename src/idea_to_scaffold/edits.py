"""Changing the backlog from the CLI: `its propose`, `promote`, `block`, `unblock`, `drop`, `set-status` and
`waive`.

Agents and people change items through these commands instead of editing Markdown, so every change is one small,
locked, predictable edit. Each returns a dict in the versioned JSON contract (spec section 16).
"""
from __future__ import annotations

import re
import tempfile
from datetime import date
from pathlib import Path

from . import claims as cl
from . import gitx, ledger, results, sizing, trust
from .backlog import (
    NO_VALUE,
    BacklogError,
    BacklogItem,
    check_proposal,
    load_backlog,
    load_proposals,
    remove_field,
    set_field,
    set_status,
)
from .gate import WAIVER_FIELD, WAIVER_GROUPS, Waiver
from .ids import is_placeholder
from .locking import LockTimeout, package_lock
from .package import BACKLOG_STATUSES, Package
from .work import (
    BACKLOG,
    EXIT_CHECK_FAILED,
    SCHEMA,
    WorkError,
    _find,
    _locked,
    _on_base_checkout,
    _refresh_mirror,
    integration_branch,
    item_payload,
    remote_of,
    renumber,
    shown,
)

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _slug(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    out = ""
    for word in words:
        if len(out) + len(word) + 1 > 40:
            break
        out = f"{out}-{word}" if out else word
    return out


def _clean(value: str | None) -> str:
    return (value or "").replace("\n", " ").strip()


def propose(
    pkg: Package,
    title: str,
    *,
    name: str | None = None,
    satisfies: str | None = None,
    depends_on: str | None = None,
    milestone: str | None = None,
    do: str | None = None,
    done_when: str | None = None,
    verify: str | None = None,
    touches: str | None = None,
) -> dict:
    """Write a new proposed item to `planning/proposals/T-new-<name>.md`.

    Each proposal is its own file, so branches that propose items at the same time never conflict when merged.
    `its renumber` (on the integration branch) gives it a permanent ID and moves it into the backlog."""
    title = _clean(title)
    if not title:
        raise WorkError("bad_title", "a proposal needs a title")
    name = (name or _slug(title)).lower()
    if not NAME_RE.fullmatch(name):
        raise WorkError("bad_name", f"{name!r} can't be a placeholder name: use lowercase letters, digits and hyphens",
                        details={"name": name})
    ident = f"T-new-{name}"
    path = pkg.proposals_dir / f"{ident}.md"
    taken = {i.id for i in load_backlog(pkg)} | {i.id for _, i in load_proposals(pkg)}
    if ident in taken or path.exists():
        raise WorkError("exists", f"{ident} is already proposed; pass --name to pick another name",
                        details={"id": ident})
    fields = [
        ("Milestone", _clean(milestone) or "—"),
        ("Status", "proposed"),
        ("Satisfies", _clean(satisfies) or "—"),
        ("Depends on", _clean(depends_on) or "—"),
        ("Do", _clean(do)),
        ("Done when", _clean(done_when)),
        ("Verify", _clean(verify)),
        ("Touches", _clean(touches)),
    ]
    body = f"### {ident} — {title}\n" + "".join(f"- **{k}:** {v}".rstrip() + "\n" for k, v in fields)
    try:
        with package_lock(pkg.root):
            if path.exists():
                raise WorkError("exists", f"{ident} is already proposed", details={"id": ident})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8", newline="\n")
    except LockTimeout as exc:
        raise WorkError("locked", str(exc)) from None
    item = next(i for p, i in load_proposals(pkg) if p == path)
    return {"schema": SCHEMA, "ok": True, "id": ident, "file": shown(pkg, pkg.rel(path)), "item": item.to_dict()}


def _locate(pkg: Package, ident: str) -> tuple[BacklogItem, Path | None, list[BacklogItem]]:
    """An item and the proposal file holding it (None when it is in the backlog)."""
    items = load_backlog(pkg)
    if any(i.id == ident for i in items):
        return _find(items, ident), None, items
    for path, item in load_proposals(pkg):
        if item.id == ident:
            return item, path, items
    raise WorkError("not_found", f"{ident} is not in the backlog or the proposals", details={"id": ident})


def _result(pkg: Package, ident: str, previous: str, status: str, **extra) -> dict:
    _refresh_mirror(pkg)
    items = load_backlog(pkg)
    item = next((i for i in items if i.id == ident), None)
    return {"schema": SCHEMA, "ok": True, "id": ident, "previous_status": previous, "status": status,
            "item": item_payload(pkg, item, items) if item else None, **extra}


def set_item_status(pkg: Package, ident: str, status: str, *, reason: str | None = None) -> dict:
    """Set any status by hand: the escape hatch behind promote, block and drop."""
    status = status.strip().lower()
    if status not in BACKLOG_STATUSES:
        raise WorkError("bad_status", f"status {status!r} must be one of {', '.join(BACKLOG_STATUSES)}",
                        details={"id": ident, "status": status})
    item, path, _ = _locate(pkg, ident)
    if is_placeholder(ident) and status not in ("proposed", "dropped"):
        raise WorkError("placeholder", f"{ident} has a placeholder ID; `its promote {ident}` gives it a permanent ID "
                        "and schedules it", details={"id": ident})
    try:
        previous = set_status(pkg, ident, status, path=path)
        if status == "blocked" and reason:
            set_field(pkg, ident, "Blocked", _clean(reason), add=True, path=path)
        elif status != "blocked":
            remove_field(pkg, ident, "Blocked", path=path)
        if status == "dropped" and reason:
            set_field(pkg, ident, "Dropped", _clean(reason), add=True, path=path)
    except BacklogError as exc:
        raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
    return _result(pkg, ident, previous, status, reason=_clean(reason) or None)


def block(pkg: Package, ident: str, reason: str, *, run: dict | None = None) -> dict:
    """Mark an item blocked and say why. This is also how an agent stops honestly when it can't finish. `run` is
    what a headless run reported (see `apply_result`), recorded in the ledger."""
    if not _clean(reason):
        raise WorkError("no_reason", "say why it is blocked: its block <ID> --reason '...'", details={"id": ident})
    item, _, _ = _locate(pkg, ident)
    if item.status in ("done", "dropped"):
        raise WorkError("bad_status", f"{ident} is {item.status}; it can't be blocked",
                        details={"id": ident, "status": item.status})
    payload = set_item_status(pkg, ident, "blocked", reason=reason)
    ledger.record(pkg, ident, "block", "blocked", reason=reason, **(run or {}))
    return payload


def waive(pkg: Package, ident: str, groups: list[str], reason: str) -> dict:
    """`its waive`: let one item through the parts of the gate named by `groups` (WAIVER_GROUPS), for `reason`.

    The waiver is written as the item's Waiver field and committed on the integration branch, in the repository's
    main checkout: done and land read waivers only from there, where an agent's worktree can't write. That makes a
    waiver the lead's alone, where the old `--allow-*` flags and their terminal check were advisory. A new waiver
    replaces the item's old one, so name every group it needs."""
    reason = _clean(reason)
    if not reason:
        raise WorkError("no_reason", "say why: its waive <ID> --test-changes --reason '...' (it is recorded)",
                        details={"id": ident})
    chosen = tuple(g for g in WAIVER_GROUPS if g in groups)
    if not chosen:
        raise WorkError("no_waiver_group", "name what to waive: --test-changes, --outside-touches or --dependencies",
                        details={"id": ident})
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "a waiver is committed on the integration branch, so it needs a git repository",
                        details={"id": ident})
    base = integration_branch(pkg)
    if not _on_base_checkout(pkg):
        raise WorkError(
            "not_on_base", f"run its waive on {base}, in the repository's main checkout: done and land honour a waiver "
            "only on the integration branch, where an agent's worktree can't write it",
            details={"id": ident, "branch": cl.current_branch(pkg.root), "integration_branch": base})
    item = _find(load_backlog(pkg), ident)
    if item.status in ("done", "dropped"):
        raise WorkError("bad_status", f"{ident} is {item.status}; there is nothing left to waive",
                        details={"id": ident, "status": item.status})
    backlog = pkg.path_of(BACKLOG)
    if gitx.out(pkg.root, "diff", "--cached", "--name-only", "--", str(backlog), check=False):
        raise WorkError("dirty", f"{shown(pkg, pkg.rel(backlog))} has staged changes; commit or unstage them first",
                        details={"id": ident})
    waiver = Waiver(date.today().isoformat(), chosen, reason)
    before = gitx.out(pkg.root, "rev-parse", "HEAD", check=False)
    try:
        with _locked(pkg):
            commit = _commit_waiver(pkg, ident, waiver, f"Waive {', '.join(chosen)} for {ident}\n\n{reason}")
            set_field(pkg, ident, WAIVER_FIELD, waiver.render(), add=True, apart=True)  # the working tree too
            gitx.run(pkg.root, "reset", "--quiet", "HEAD", "--", str(backlog))  # and the index
    except BacklogError as exc:
        raise WorkError("bad_backlog", str(exc), details={"id": ident}) from exc
    except cl.GitError as exc:
        raise WorkError("git_error", f"the waiver couldn't be committed: {exc}", details={"id": ident}) from exc
    trust.note_waiver(pkg.root, base, commit)
    trusted = trust.record(pkg.root, base, commit, after=before or None, remote=remote_of(pkg))
    _refresh_mirror(pkg)
    return {"schema": SCHEMA, "ok": True, "id": ident, "waiver": waiver.to_dict(),
            "committed": commit[:7], "trusted": trusted}


def _commit_waiver(pkg: Package, ident: str, waiver: Waiver, message: str) -> str:
    """Commit HEAD's backlog with the waiver added, and nothing else: uncommitted work in the checkout (on the plain
    flow, the agent's) stays out of the commit `its` vouches for. Built in a temporary index, then the branch is
    moved to it only if it hasn't moved meanwhile. Returns the new commit."""
    root = pkg.root
    top = cl.toplevel(root)
    rel = Path(pkg.path_of(BACKLOG)).resolve().relative_to(top.resolve()).as_posix()
    head = cl.git(root, "rev-parse", "HEAD")
    branch = cl.git(root, "symbolic-ref", "HEAD")
    old = gitx.show(top, f"HEAD:{rel}")
    if old is None:
        raise cl.GitError(f"{rel} isn't committed on {branch}")
    mode = (cl.git(top, "ls-tree", "HEAD", "--", rel).split() or ["100644"])[0]
    with tempfile.TemporaryDirectory(prefix="its-waive-") as scratch:
        copy = Path(scratch) / "backlog.md"
        copy.write_bytes(old)
        set_field(pkg, ident, WAIVER_FIELD, waiver.render(), add=True, apart=True, path=copy)
        index = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        blob = _plumb(top, "hash-object", "-w", "--stdin", input=copy.read_bytes())
        _plumb(top, "read-tree", "HEAD", env=index)
        _plumb(top, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}", env=index)
        tree = _plumb(top, "write-tree", env=index)
    commit = _plumb(top, "commit-tree", tree, "-p", head, "-m", message)
    _plumb(top, "update-ref", "-m", "its waive", branch, commit, head)
    return commit


def _plumb(cwd: Path, *args: str, input: bytes | None = None, env: dict[str, str] | None = None) -> str:
    proc = gitx.run(cwd, *args, input=input, binary=True, extra_env=env)
    if proc.returncode != 0:
        raise cl.GitError(proc.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return proc.stdout.decode("utf-8", "replace").strip()


def apply_result(pkg: Package, ident: str | None, result_file: str, **done_options) -> dict:
    """`its done --result FILE`: act on what a headless agent handed back. `done` runs the gate and checks as usual;
    `blocked` or `needs_human` blocks the item with the agent's reason. Either way the run's cost and turns go to the
    ledger. A run that ended without a result changes nothing (code `no_result`)."""
    try:
        text = Path(result_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkError("bad_result", f"can't read the result file {result_file}: {exc}",
                        details={"id": ident, "result_file": result_file}) from exc
    try:
        result, run = results.parse_result(text)
    except results.ResultError as exc:
        if ident:
            ledger.record(pkg, ident, "done", exc.code, **exc.run)
        raise WorkError(exc.code, str(exc), details={"id": ident, "result_file": result_file, **exc.run}) from exc
    if ident and result["id"] != ident:
        raise WorkError("result_mismatch", f"the result is for {result['id']}, not {ident}; nothing was marked",
                        details={"id": ident, "result_id": result["id"], "result_file": result_file})
    ident = result["id"]
    tampered = results.launch_changes(pkg)
    if tampered:
        ledger.record(pkg, ident, "done", "launch_settings_changed", **run)
        raise WorkError("launch_settings_changed",
                        f"the headless launch files were changed ({', '.join(tampered)}), so this run may have had "
                        "no Stop hook or no sandbox; nothing was marked. Check the run's work by hand; its start "
                        "writes the files again", EXIT_CHECK_FAILED,
                        details={"id": ident, "files": tampered, "result_file": result_file})
    if result["status"] == "done":
        from .work import done
        payload = done(pkg, ident, run=run, **done_options)
    else:
        reason = f"{result['status'].replace('_', ' ')}: {result['reason'].strip()}"
        payload = block(pkg, ident, reason, run=run)
    return {**payload, "result": result, "run": run}


def unblock(pkg: Package, ident: str) -> dict:
    item, _, _ = _locate(pkg, ident)
    if item.status != "blocked":
        raise WorkError("bad_status", f"{ident} is {item.status}, not blocked", details={"id": ident})
    return set_item_status(pkg, ident, "todo")


def drop(pkg: Package, ident: str, reason: str | None = None) -> dict:
    """Drop an item. A dropped proposal is simply deleted: it was never agreed."""
    item, path, _ = _locate(pkg, ident)
    if item.status == "done":
        raise WorkError("bad_status", f"{ident} is done; it can't be dropped", details={"id": ident})
    if path is not None:
        try:
            with package_lock(pkg.root):
                path.unlink()
        except LockTimeout as exc:
            raise WorkError("locked", str(exc)) from None
        return {"schema": SCHEMA, "ok": True, "id": ident, "previous_status": item.status, "status": "dropped",
                "item": None, "deleted_file": shown(pkg, pkg.rel(path)), "reason": _clean(reason) or None}
    return set_item_status(pkg, ident, "dropped", reason=reason)


def promote(pkg: Package, ident: str, *, force: bool = False, allow_incomplete: bool = False) -> dict:
    """Schedule a proposed item: set it `todo`. A placeholder is renumbered first (on the integration branch).

    An item with no `Done when` or no `Touches` is refused unless `allow_incomplete`: an agent would have nothing to
    finish against, and an item without Touches can't run alongside anything."""
    item, path, _ = _locate(pkg, ident)
    if path is not None:
        problems = check_proposal(pkg.rel(path), path.stem, path.read_text(encoding="utf-8"))
        if problems:
            raise WorkError("bad_proposal", "; ".join(problems), details={"id": ident, "problems": problems})
    if item.status != "proposed":
        raise WorkError("bad_status", f"{ident} is {item.status}, not proposed", details={"id": ident})
    missing = [name for name, empty in (("Done when", item.done_when.strip().lower() in NO_VALUE),
                                        ("Touches", not item.touches)) if empty]
    if missing and not allow_incomplete:
        raise WorkError("incomplete", f"{ident} has no {' or '.join(missing)}; fill "
                        f"{'them' if len(missing) > 1 else 'it'} in before scheduling it, or pass --allow-incomplete",
                        details={"id": ident, "missing": missing})
    renumbered = None
    if item.placeholder:
        out = renumber(pkg, [ident], force=force)
        renumbered = out["renumbered"][0]["to"]
        ident = renumbered
    result = set_item_status(pkg, ident, "todo")
    result["renumbered_from"] = item.id if renumbered else None
    result["sizing"] = sizing.warnings_for(pkg, item)
    return result
