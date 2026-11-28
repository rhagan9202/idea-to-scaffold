"""What each claim's branch tip says (`its claims`, and the claims in `its status`), conflicts between claims, and
`its release`."""
from __future__ import annotations

from . import claims as cl
from .backlog import (
    BACKLOG,
    blocked_by,
    parse_backlog,
)
from .contract import (
    SCHEMA,
    WorkError,
)
from .package import Package
from .view import View, closed_ids, view


def claims_payload(pkg: Package, base: str | None = None) -> dict:
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "claims need a git repository")
    v = view(pkg, base)
    claims = cl.list_claims(pkg.root, closed_ids(v.items), {i.id for i in v.items})
    out = [c.to_dict() for c in claims]
    if v.base is not None:
        _add_conflicts(pkg, v.base, claims, out)
        for c, d in zip(claims, out, strict=True):
            d.update(_tip(pkg, c, d, v))
    return {"schema": SCHEMA, "ok": True, "base": v.base, "claims": out}


def _tip(pkg: Package, claim: cl.Claim, entry: dict, v: View) -> dict:
    """What the claim branch's own tip says: its item's status there, its last commit, and whether `its land` would
    take it now (and if not, why). This is what the lead needs and can't see from the integration branch."""
    ref = cl.claim_ref(claim)
    text = cl.read_at(pkg.root, ref, pkg.path_of(BACKLOG))
    item = next((i for i in parse_backlog(text) if i.id == claim.id), None) if text else None
    status = item.status if item else None
    log = cl.git(pkg.root, "log", "-1", "--format=%h%x00%cI%x00%s", ref, check=False).split("\0")
    last = {"commit": log[0], "date": log[1], "subject": log[2]} if len(log) == 3 else None
    planned = next((i for i in v.items if i.id == claim.id), None)
    waiting = blocked_by(planned, v.items) if planned else []
    candidates = [_candidate_tip(pkg, c, v.base) for c in claim.candidates] if v.base else []
    if claim.orphaned:
        state = "orphaned"
    elif claim.finished:
        state = "finished"
    elif claim.candidates:  # adopted: land runs the done gate on the vendor's branches, so they are what counts
        ahead = [c for c in candidates if c["ahead"]]
        state = ("no_commits" if not ahead else "waiting" if waiting
                 else "conflicts" if all(c["conflicts_with_base"] for c in ahead) else "ready_to_land")
        if ahead and not entry.get("ahead"):
            last = ahead[0]["last_commit"]
    elif status == "blocked":
        state = "blocked"
    elif status != "done":
        state = "working"
    elif not entry.get("ahead"):
        state = "no_commits"
    elif entry.get("conflicts_with_base"):
        state = "conflicts"
    elif waiting:
        state = "waiting"
    else:
        state = "ready_to_land"
    return {"tip_status": status, "blocked_reason": (item.get("Blocked") or None) if item else None,
            "last_commit": last, "state": state, "waiting_on": waiting, "candidates": candidates}


def _candidate_tip(pkg: Package, cand: dict, base: str) -> dict:
    """An adopted candidate as `its status` shows it: its commits, its last one, and its conflicts with the base."""
    ref = cand["ref"]
    there = cl.ref_exists(pkg.root, ref)
    ahead = cl.ahead(pkg.root, ref, base) if there else 0
    log = cl.git(pkg.root, "log", "-1", "--format=%h%x00%cI%x00%s", ref, check=False).split("\0") if there else []
    return {**cand, "ahead": ahead, "exists": there,
            "last_commit": {"commit": log[0], "date": log[1], "subject": log[2]} if len(log) == 3 else None,
            "conflicts_with_base": (cl.conflicting_files(pkg.root, base, ref) or []) if ahead else []}


def _add_conflicts(pkg: Package, base: str, claims: list[cl.Claim], out: list[dict]) -> None:
    """Dry-run merges (nothing changes): which active claims would conflict with the integration branch, or with
    each other, if merged. Only claims with commits of their own can conflict."""
    pairs = list(zip(claims, out, strict=True))
    for c, d in pairs:
        d["ahead"] = cl.ahead(pkg.root, cl.claim_ref(c), base)
        d["conflicts_with_base"] = []
        d["conflicts_with"] = []
    active = [(c, d) for c, d in pairs if not c.finished and d["ahead"]]
    for c, d in active:
        d["conflicts_with_base"] = cl.conflicting_files(pkg.root, base, cl.claim_ref(c)) or []
    for i, (a, da) in enumerate(active):
        for b, db in active[i + 1:]:
            files = cl.conflicting_files(pkg.root, cl.claim_ref(a), cl.claim_ref(b))
            if files:
                da["conflicts_with"].append({"id": b.id, "files": files})
                db["conflicts_with"].append({"id": a.id, "files": files})


def release(pkg: Package, ident: str, *, force: bool = False, remote: bool = False) -> dict:
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "claims need a git repository")
    v = view(pkg)
    try:
        result = cl.release_claim(pkg.root, ident, force=force, remote=remote, base_ref=v.base,
                                  closed=closed_ids(v.items))
    except cl.GitError as exc:
        raise WorkError("git_error", str(exc), details={"id": ident}) from exc
    return {"schema": SCHEMA, "ok": True, **result}
