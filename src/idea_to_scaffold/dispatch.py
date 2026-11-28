"""Working with other tools' dispatchers: `its adopt` takes in a branch a vendor's agent made, and `its brief` writes
the task for one.

Every major coding-agent vendor now runs agents in parallel itself (Claude Code's agent view and Projects, GitHub's
issue assignment to Copilot, Claude or Codex, Codex's queue, Cursor's worktrees), with dashboards, budgets and cloud
isolation `its` doesn't try to match. What `its` owns is the backlog, the claims, the gate and landing. Those agents
push branches under their own names (``claude/...``, ``copilot/...``, ``codex/...``) and usually never run `its`,
and the Stop hook doesn't run on their cloud surfaces, so `its land` is where their work meets the gate.

`its adopt REF ID` claims the item the way `its start --branch` does (a claim branch from the integration branch,
pushed so other machines see it) and records REF, a vendor branch or pull request, as a *candidate* for it. Adopting
more branches for the same item adds candidates: several agents can try one item. `its land` then runs the whole
done gate on each candidate in turn, the held-out tests included, lands the first that passes, sets the item done
in the merge commit, and leaves the rest (`not_chosen`). The vendor's branches themselves are never changed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import claims as cl
from .contract import SCHEMA, WorkError
from .package import Package
from .work import _closed, _find, _locked, integration_branch, overlaps_with_running, remote_of, view

# Vendor branch prefixes, read as the agent that made the branch (the agent identity in the run ledger).
VENDORS = {"claude": "claude", "copilot": "copilot", "codex": "codex", "cursor": "cursor", "gemini": "gemini",
           "jules": "jules", "devin": "devin", "openhands": "openhands", "amp": "amp"}
PR_RE = re.compile(r"^(?:pr:|#)(\d+)$|/pull/(\d+)(?:/|$)", re.I)


def vendor_of(name: str | None) -> str | None:
    """The agent a branch name says made it: `claude/fix-store` -> `claude`."""
    head = (name or "").split("/", 1)[0].lower()
    return VENDORS.get(head) if "/" in (name or "") else None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve(pkg: Package, ref: str, remote: str | None, *, fetch: bool = False) -> dict:
    """A candidate for `ref`: a pull request (`pr:12`, `#12` or its URL), a local branch, a remote branch
    (`claude/x` or `origin/claude/x`), or any ref. Remote ones are fetched, into `refs/its/pull/N` for a pull
    request and the usual remote-tracking ref for a branch. Raises `no_branch` when it isn't there."""
    root = pkg.root
    m = PR_RE.search(ref.strip())
    if m:
        number = int(m.group(1) or m.group(2))
        if remote is None:
            raise WorkError("no_branch", f"{ref} is a pull request, but this repository has no remote to fetch it from",
                            details={"branch": ref})
        cand = {"name": f"pr:{number}", "ref": f"{cl.ADOPTED_REFS}pull/{number}", "remote": remote,
                "fetch": f"refs/pull/{number}/head", "pr": number}
    elif ref.startswith("refs/"):
        cand = {"name": ref, "ref": ref, "remote": None, "fetch": None}
    elif not fetch and cl.ref_exists(root, f"refs/heads/{ref}"):
        cand = {"name": ref, "ref": f"refs/heads/{ref}", "remote": None, "fetch": None}
    else:
        name = ref
        if remote and name.startswith(remote + "/"):
            name = name[len(remote) + 1:]
        if remote is None:
            raise WorkError("no_branch", f"there is no branch {ref} here, and no remote to look for it on",
                            details={"branch": ref})
        cand = {"name": name, "ref": f"refs/remotes/{remote}/{name}", "remote": remote, "fetch": f"refs/heads/{name}"}
    if cl.item_of_branch(cand["name"]):
        raise WorkError("claim_branch", f"{cand['name']} is an its claim branch already; its land takes it as it is",
                        details={"branch": cand["name"]})
    try:
        tip = cl.fetch_candidate(root, cand)
    except cl.GitError as exc:
        raise WorkError("fetch_failed", str(exc), details={"remote": remote}) from exc
    if tip is None:
        where = f" on {remote}" if cand["remote"] else ""
        raise WorkError("no_branch", f"there is no branch or pull request {ref}{where}", details={"branch": ref})
    cand["tip"] = tip
    return cand


def adopt(pkg: Package, ref: str, ident: str, *, push: bool | None = None, fetch: bool = False,
          agent: str | None = None) -> dict:
    """`its adopt REF ID`: claim `ident` for a branch a vendor's agent made, so `its land` can gate and merge it.
    The first adoption makes the claim (pushed, like `its start --branch`, unless `push` is off); each one records
    REF as a candidate. Refuses an item that is done, claimed by another branch, or a REF with nothing new."""
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "adopting a branch needs a git repository")
    base = integration_branch(pkg)
    remote = remote_of(pkg)
    if fetch and remote:
        try:
            cl.fetch(pkg.root, remote, base)
        except cl.GitError as exc:
            raise WorkError("fetch_failed", str(exc), details={"remote": remote}) from exc
    cand = resolve(pkg, ref, remote, fetch=fetch)
    cand.update(agent=agent or vendor_of(cand["name"]), adopted=_now())
    created = False
    with _locked(pkg):
        v = view(pkg)
        if v.base is None:
            raise WorkError("no_base", f"can't adopt for {ident}: there is no integration branch ({base})",
                            details={"id": ident})
        item = _find(v.items, ident)
        if item.placeholder:
            raise WorkError("placeholder", f"{ident} has a placeholder ID; give it a permanent T-n.m ID first",
                            details={"id": ident})
        if item.status not in ("todo", "in-progress", "blocked"):
            raise WorkError("bad_status", f"{ident} is {item.status or 'missing a status'} on {v.source}; only work "
                            "still to do can be adopted", details={"id": ident, "status": item.status})
        if not cl.ahead(pkg.root, cand["ref"], v.base):
            raise WorkError("nothing_to_adopt", f"{cand['name']} has no commits that {v.base} doesn't have",
                            details={"id": ident, "branch": cand["name"]})
        claim = next((c for c in v.claims if c.id == ident), None)
        if claim is not None and not (not claim.remote and cl.claim_entry(pkg.root, ident).get("adopted")):
            where = f" in {claim.worktree}" if claim.worktree else ""
            raise WorkError("claimed", f"{ident} is already claimed by branch {claim.branch}{where}; release that "
                            "claim first to land a vendor's branch instead", details={"id": ident,
                                                                                    "claim": claim.to_dict()})
        overlaps = overlaps_with_running(v, item) if claim is None else []
        if claim is None:
            try:
                claim = cl.create_claim(pkg.root, ident, item.title, base_ref=v.base, worktree=None,
                                        agent=cand["agent"], closed=_closed(v.items), switch=False,
                                        integration=base)
            except cl.ClaimTaken as exc:
                raise WorkError("claimed", str(exc), details={"id": ident, "claim": exc.claim.to_dict()}) from exc
            except cl.GitError as exc:
                raise WorkError("git_error", str(exc), details={"id": ident}) from exc
            created = True
        try:
            candidates, added = cl.add_candidate(pkg.root, ident, cand)
        except cl.GitError as exc:
            raise WorkError("git_error", str(exc), details={"id": ident}) from exc
    pushed, push_error = False, None
    if push is None:
        push = pkg.get("push_claims") is not False
    if created and push and remote:
        try:  # first push wins, as for its start: another machine may have claimed the item meanwhile
            cl.push_claim(pkg.root, remote, ident, claim.branch)
            pushed = True
        except cl.PushRejected as exc:
            cl.release_claim(pkg.root, ident, force=True)
            theirs = {"id": ident, "branch": exc.branch, "remote": remote} if exc.branch else None
            raise WorkError("claimed", f"{exc}; your adoption was undone",
                            details={"id": ident, "remote": remote, "claim": theirs}) from exc
        except cl.GitError as exc:
            push_error = str(exc)
    claim_dict = {**claim.to_dict(), "candidates": candidates}
    return {
        "schema": SCHEMA, "ok": True, "id": ident, "title": item.title, "claim": claim_dict, "created": created,
        "candidate": cand, "added": added, "candidates": candidates, "pushed": pushed, "push_error": push_error,
        "remote": remote if pushed else None, "overlaps": overlaps,
        "commands": {"land": f"its land {ident}", "review": f"its review {ident} --branch {cand['name']}",
                     "release": f"its release {ident}" + (" --remote" if pushed else "")},
    }
