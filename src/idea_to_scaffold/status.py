"""`its status`: the lead's view of the whole project from one checkout.

Everything the lead would otherwise visit worktrees for: each claim with what its branch tip says (working,
blocked and why, ready to land, conflicting or waiting), plain in-progress items, what can start next, claims to
release, proposals waiting for `its renumber`, and drifted items. It changes nothing.
"""
from __future__ import annotations

from collections import Counter

from . import claims as cl
from .backlog import load_proposals
from .package import Package
from .work import SCHEMA, WorkError, claims_payload, next_payload, shown, view

STATE_LABELS = {
    "ready_to_land": "ready to land",
    "blocked": "blocked",
    "working": "working",
    "conflicts": "done, but conflicts with the integration branch",
    "waiting": "done, waiting on dependencies",
    "no_commits": "done, but no commits yet",
    "finished": "finished: release it",
    "orphaned": "orphaned: not in the backlog, release it",
}


def status_payload(pkg: Package, base: str | None = None) -> dict:
    v = view(pkg, base)
    counts = Counter(i.status or "missing" for i in v.items)
    nxt = next_payload(pkg, base=base)
    out: dict = {
        "schema": SCHEMA, "ok": True, "project": pkg.name or pkg.slug or pkg.root.name, "source": v.source,
        "branch": v.branch, "base": v.base, "counts": dict(sorted(counts.items())),
        "next": nxt["item"]["id"] if nxt["item"] else None, "next_reason": nxt["reason"],
        "ready": [i["id"] for i in nxt["ready"]],
        "blocked": nxt["blocked"],
        "in_progress": [i.id for i in v.items if i.status == "in-progress" and i.id not in v.claimed],
        "proposals": [{"id": i.id, "title": i.title, "file": shown(pkg, pkg.rel(p))} for p, i in load_proposals(pkg)],
        "claims": [], "ready_to_land": [], "to_release": [], "drift": None,
    }
    if cl.in_git(pkg.root):
        claims = claims_payload(pkg, base)["claims"]
        out["claims"] = [c for c in claims if c["state"] not in ("finished", "orphaned")]
        out["ready_to_land"] = [c["id"] for c in claims if c["state"] == "ready_to_land"]
        out["to_release"] = [c["id"] for c in claims if c["state"] in ("finished", "orphaned")]
        out["blocked"] += [{"id": c["id"], "title": None, "reason": c["blocked_reason"], "branch": c["branch"]}
                           for c in claims if c["state"] == "blocked"]
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
    if payload["claims"]:
        lines.append("claims:")
        for c in payload["claims"]:
            label = STATE_LABELS.get(c["state"], c["state"])
            if c["state"] == "blocked" and c["blocked_reason"]:
                label += f": {c['blocked_reason']}"
            if c["state"] == "waiting" and c["waiting_on"]:
                label += f" ({', '.join(c['waiting_on'])})"
            if c["state"] == "conflicts":
                label += f" in {', '.join(c['conflicts_with_base'])}"
            last = c["last_commit"]
            tail = f"  {c['ahead']} commit(s), last {last['date'][:10]} \"{last['subject']}\"" if last and c["ahead"] else ""
            lines.append(f"  {c['id']:<8} {label}")
            lines.append(f"           {c['branch']}{tail}" + (f"  ({c['worktree']})" if c["worktree"] else ""))
            for other in c.get("conflicts_with", []):
                lines.append(f"           would conflict with {other['id']} in {', '.join(other['files'])}")
    if payload["in_progress"]:
        lines.append(f"in progress here: {', '.join(payload['in_progress'])}")
    blocked = [b for b in payload["blocked"] if "branch" not in b]
    for b in blocked:
        lines.append(f"blocked: {b['id']}" + (f": {b['reason']}" if b["reason"] else ""))
    if payload["ready_to_land"]:
        lines.append(f"ready to land: {', '.join(payload['ready_to_land'])}  (its land)")
    if payload["next"]:
        others = [i for i in payload["ready"] if i != payload["next"]]
        lines.append(f"next: its start {payload['next']} --worktree" + (f"  (also ready: {', '.join(others)})"
                                                                         if others else ""))
    elif payload["next_reason"] == "complete":
        lines.append("every item is done")
    if payload["to_release"]:
        lines.append(f"release: {', '.join(payload['to_release'])}  (its release <ID>)")
    if payload["proposals"]:
        lines.append(f"proposals: {', '.join(p['id'] for p in payload['proposals'])}  (its renumber)")
    drift = payload["drift"]
    if drift and drift["drifted"]:
        lines.append(f"drifted: {', '.join(drift['drifted'])}  (its drift; its verify --drifted)")
    if drift and drift["unknown"]:
        lines.append(f"drift unknown: {', '.join(drift['unknown'])}")
    return lines
