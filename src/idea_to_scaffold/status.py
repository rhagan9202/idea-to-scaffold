"""`its status`: the lead's view of the whole project from one checkout, as an attention inbox.

Everything the lead would otherwise visit worktrees for: each claim with what its branch tip says (working,
blocked and why, ready to land, conflicting or waiting), plain in-progress items, what can start next, claims to
release, proposals waiting for `its renumber`, and drifted items. It also warns (`main_moved`) when the integration
branch holds commits `its` doesn't trust (see `trust.py`), which `its land` would refuse. It changes nothing.

Claims are grouped by what they need from the lead, most urgent first (`attention`): `needs_you` (blocked, a
review requested changes or is required, conflicts), `ready_to_land`, `running`, `stale` (no commit for
`gate.stale_days`) and `done` (release them). An adopted claim lists its candidates, each with its own review.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from . import claims as cl
from . import gate, gitx, trust
from .backlog import load_proposals
from .contract import SCHEMA, WorkError
from .drift import drift
from .package import Package
from .review import claim_review
from .tips import claims_payload
from .view import integration_branch, next_payload, remote_of, shown, view

REVIEWED = ("ready_to_land", "waiting", "conflicts")  # done on the branch, so a review can count for it
ATTENTION = ("needs_you", "ready_to_land", "running", "stale", "done")  # the inbox's groups, most urgent first
NEEDS_YOU = ("blocked", "changes_requested", "not_reviewed", "conflicts")


def _attention(claim: dict, stale_days: int, now: datetime) -> str:
    """Which inbox group a claim goes in."""
    state = claim["state"]
    if state in NEEDS_YOU:
        return "needs_you"
    if state == "ready_to_land":
        return "ready_to_land"
    if state in ("finished", "orphaned"):
        return "done"
    if state in ("working", "no_commits"):
        last = (claim.get("last_commit") or {}).get("date") if claim.get("ahead") or claim.get("candidates") else None
        when = _parse_time(last or claim.get("started"))
        if when is not None and (now - when).total_seconds() > stale_days * 86400:
            claim["idle_days"] = int((now - when).total_seconds() // 86400)
            return "stale"
    return "running"


def _parse_time(value: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def status_payload(pkg: Package, base: str | None = None) -> dict:
    v = view(pkg, base)
    counts = Counter(i.status or "missing" for i in v.items)
    nxt = next_payload(pkg, base=base)
    out: dict = {
        "schema": SCHEMA, "ok": True, "project": pkg.name or pkg.slug or pkg.root.name, "source": v.source,
        "branch": v.branch, "base": v.base, "behind": v.behind or None, "counts": dict(sorted(counts.items())),
        "next": nxt["item"]["id"] if nxt["item"] else None, "next_reason": nxt["reason"],
        "ready": [i["id"] for i in nxt["ready"]],
        "blocked": nxt["blocked"],
        "in_progress": [i.id for i in v.items if i.status == "in-progress" and i.id not in v.claimed],
        "proposals": [{"id": i.id, "title": i.title, "file": shown(pkg, pkg.rel(p))} for p, i in load_proposals(pkg)],
        "claims": [], "ready_to_land": [], "to_release": [], "drift": None, "warnings": list(nxt.get("warnings", [])),
        "attention": {k: [] for k in ATTENTION},
    }
    if cl.in_git(pkg.root):
        claims = claims_payload(pkg, base)["claims"]
        for c in claims:  # what its land would say about the review of what it would merge
            c["review"] = claim_review(pkg, c, v.base) if v.base and c["state"] in REVIEWED else None
            if c["state"] == "ready_to_land" and c["review"] and c["review"]["current"] \
                    and c["review"]["outcome"] == "changes_requested":
                c["state"] = "changes_requested"
            elif c["state"] == "ready_to_land" and c["review"] and c["review"]["required"] and not c["review"]["current"]:
                c["state"] = "not_reviewed"
        stale_days = gate.Policy.of(gate.manifest_at(pkg, v.base) or {}, v.base).stale_days if v.base else 3
        now = datetime.now(timezone.utc)
        for c in claims:
            c["attention"] = _attention(c, stale_days, now)
            out["attention"][c["attention"]].append(c["id"])
        rank = {k: n for n, k in enumerate(ATTENTION)}
        out["claims"] = sorted((c for c in claims if c["state"] not in ("finished", "orphaned")),
                               key=lambda c: rank[c["attention"]])
        out["ready_to_land"] = [c["id"] for c in claims if c["state"] == "ready_to_land"]
        out["to_release"] = [c["id"] for c in claims if c["state"] in ("finished", "orphaned")]
        out["blocked"] += [{"id": c["id"], "title": None, "reason": c["blocked_reason"], "branch": c["branch"]}
                           for c in claims if c["state"] == "blocked"]
        out["attention"]["needs_you"] += [b["id"] for b in nxt["blocked"] if b["id"] not in v.claimed]
        name = integration_branch(pkg)
        try:
            left = trust.untrusted(pkg.root, name, name, remote_of(pkg)) if cl.ref_exists(pkg.root, name) else None
        except gitx.GitError:
            left = None
        if left:
            out["warnings"].append({
                "kind": "main_moved", "commits": left,
                "message": f"{name} has {len(left)} commit(s) that its didn't make and that aren't pushed; its land "
                           "refuses until you look at them and run its land --accept-base"})
        try:
            d = drift(pkg)
            out["drift"] = {"drifted": [x["id"] for x in d["drifted"]], "unknown": [x["id"] for x in d["unknown"]]}
        except WorkError:
            out["drift"] = None
    return out
