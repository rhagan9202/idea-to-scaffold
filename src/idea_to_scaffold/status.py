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
from .package import Package
from .review import claim_review
from .work import claims_payload, integration_branch, next_payload, remote_of, shown, view

STATE_LABELS = {
    "ready_to_land": "ready to land",
    "blocked": "blocked",
    "working": "working",
    "changes_requested": "done, but its review requested changes",
    "not_reviewed": "done, waiting for an approving review (gate.require_review)",
    "conflicts": "done, but conflicts with the integration branch",
    "waiting": "done, waiting on dependencies",
    "no_commits": "done, but no commits yet",
    "finished": "finished: release it",
    "orphaned": "orphaned: not in the backlog, release it",
}


REVIEWED = ("ready_to_land", "waiting", "conflicts")  # done on the branch, so a review can count for it
ATTENTION = ("needs_you", "ready_to_land", "running", "stale", "done")  # the inbox's groups, most urgent first
NEEDS_YOU = ("blocked", "changes_requested", "not_reviewed", "conflicts")
SECTION_TITLES = {"needs_you": "needs you", "ready_to_land": "ready to land (its land)", "running": "running",
                  "stale": "stale (no commit for a while: check on it, or its release it)",
                  "done": "done (its release <ID>)"}


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
        from .drift import drift  # drift imports work, so import it late
        try:
            d = drift(pkg)
            out["drift"] = {"drifted": [x["id"] for x in d["drifted"]], "unknown": [x["id"] for x in d["unknown"]]}
        except WorkError:
            out["drift"] = None
    return out


def render(payload: dict) -> list[str]:
    counts = payload["counts"]
    order = ["done", "in-progress", "blocked", "todo", "proposed", "dropped"]
    summary = ", ".join(f"{counts[k]} {k}" for k in order + sorted(set(counts) - set(order)) if counts.get(k))
    where = f" on {payload['branch']}" if payload["branch"] else ""
    lines = [f"{payload['project']}{where}: {summary or 'no items'}"]
    if payload["source"] != "working tree":
        lines.append(f"  (backlog read from {payload['source']})")
    if payload.get("behind"):
        lines.append(f"  {payload['branch']} is {payload['behind']} commit(s) behind {payload['base']}: "
                     "pull before starting work here (git pull --ff-only)")
    unclaimed_blocked = [b for b in payload["blocked"] if "branch" not in b]
    for section in ATTENTION:
        group = [c for c in payload["claims"] if c.get("attention", "running") == section]
        extra = unclaimed_blocked if section == "needs_you" else []
        if not group and not extra:
            continue
        lines.append(f"{SECTION_TITLES[section]}:")
        for c in group:
            lines += _claim_lines(c)
        for b in extra:
            lines.append(f"  {b['id']:<8} blocked" + (f": {b['reason']}" if b["reason"] else ""))
    if payload["in_progress"]:
        lines.append(f"in progress here: {', '.join(payload['in_progress'])}")
    if payload["next"]:
        others = [i for i in payload["ready"] if i != payload["next"]]
        lines.append(f"next: its start {payload['next']} --worktree" + (f"  (also ready: {', '.join(others)})"
                                                                         if others else ""))
    elif payload["next_reason"] == "complete":
        lines.append("every item is done")
    if payload["to_release"]:
        lines.append(f"done, release: {', '.join(payload['to_release'])}  (its release <ID>)")
    if payload["proposals"]:
        lines.append(f"proposals: {', '.join(p['id'] for p in payload['proposals'])}  (its renumber)")
    for w in payload.get("warnings", []):
        lines.append(f"warning: {w['message']}")
        lines += [f"  {c}" for c in w.get("commits", [])]
    drift = payload["drift"]
    if drift and drift["drifted"]:
        lines.append(f"drifted: {', '.join(drift['drifted'])}  (its drift; its verify --drifted)")
    if drift and drift["unknown"]:
        lines.append(f"drift unknown: {', '.join(drift['unknown'])}")
    return lines


def _tail(c: dict) -> str:
    last = c.get("last_commit")
    return f"  {c['ahead']} commit(s), last {last['date'][:10]} \"{last['subject']}\"" if last and c.get("ahead") else ""


def _candidate_line(cand: dict) -> str:
    """One adopted candidate: its name and agent, its commits, conflicts and review."""
    who = f" ({cand['agent']})" if cand.get("agent") else ""
    if not cand.get("exists", True):
        return f"{cand['name']}{who}: not fetched here (its land --fetch)"
    if not cand.get("ahead"):
        return f"{cand['name']}{who}: no new commits"
    line = f"{cand['name']}{who}{_tail(cand)}"
    if cand.get("conflicts_with_base"):
        line += f"; conflicts in {', '.join(cand['conflicts_with_base'])}"
    review = cand.get("review")
    if review:
        line += f"; review: {review['label']}"
        why = [*review.get("unmet", []), *review.get("blocking", [])] if review.get("current") \
            and review.get("outcome") == "changes_requested" else []
        line += f" ({', '.join(why)})" if why else ""
    return line


def _claim_lines(c: dict) -> list[str]:
    label = STATE_LABELS.get(c["state"], c["state"])
    if c["state"] == "blocked" and c["blocked_reason"]:
        label += f": {c['blocked_reason']}"
    if c["state"] == "waiting" and c["waiting_on"]:
        label += f" ({', '.join(c['waiting_on'])})"
    if c["state"] == "conflicts" and c["conflicts_with_base"]:
        label += f" in {', '.join(c['conflicts_with_base'])}"
    if c.get("idle_days") is not None:
        label += f", no commit for {c['idle_days']} day(s)"
    review = c.get("review")
    if review and c["state"] not in ("changes_requested", "not_reviewed") and not c.get("candidates"):
        label += f"; review: {review['label']}"
    lines = [f"  {c['id']:<8} {label}"]
    if c["state"] == "changes_requested" and not c.get("candidates"):
        why = [*review["unmet"], *review["blocking"]]
        lines.append(f"           review: {', '.join(why) or 'changes requested'}")
    if c.get("candidates"):
        lines.append(f"           {c['branch']} (adopted; land tries these in turn)")
        lines += [f"           - {_candidate_line(x)}" for x in c["candidates"]]
    else:
        lines.append(f"           {c['branch']}{_tail(c)}" + (f"  ({c['worktree']})" if c["worktree"] else ""))
    for other in c.get("conflicts_with", []):
        lines.append(f"           would conflict with {other['id']} in {', '.join(other['files'])}")
    return lines
