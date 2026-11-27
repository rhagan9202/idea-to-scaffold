"""The sizing lint: `its validate --sizing` warns about todo and proposed items too big or too vague for one agent run.

An item an agent can finish in one go touches a few files, serves one or two requirements, and says how to check it.
Each rule below is a warning, never an error: it helps a person split work before it is assigned, and `validate`
still passes. `promote` shows the same warnings for the item it schedules.
"""
from __future__ import annotations

import re

from .backlog import NO_VALUE, BacklogError, BacklogItem, load_backlog, load_proposals
from .package import Package
from .trace import must_requirements

MAX_TOUCHES = 3
MAX_REQUIREMENTS = 2
MAX_MUST_CRITERIA = 5
VAGUE_RE = re.compile(r"\b(improve|improvements?|refactor|clean ?up|cleanup|polish|various|misc|tidy|optimi[sz]e|"
                      r"enhance|update stuff|fix (?:things|issues|bugs))\b", re.IGNORECASE)


def _empty(value: str) -> bool:
    return value.strip().lower() in NO_VALUE


def item_warnings(item: BacklogItem, criteria_by_fr: dict[str, int]) -> list[dict]:
    """The sizing warnings for one item: `{id, kind, message}` each."""
    out: list[dict] = []

    def warn(kind: str, message: str) -> None:
        out.append({"id": item.id, "kind": kind, "message": message})

    if len(item.touches) > MAX_TOUCHES:
        warn("too_many_touches", f"{item.id} touches {len(item.touches)} paths (more than {MAX_TOUCHES}); "
                                 "split it so each part changes a few files")
    frs = [s for s in item.satisfies if s.startswith("FR-")]
    if len(frs) > MAX_REQUIREMENTS:
        warn("too_many_requirements", f"{item.id} satisfies {len(frs)} requirements (more than {MAX_REQUIREMENTS}); "
                                      "split it by requirement")
    must = sum(criteria_by_fr.get(fr, 0) for fr in frs)
    if must > MAX_MUST_CRITERIA:
        warn("too_many_criteria", f"{item.id} has to meet {must} Must acceptance criteria (more than "
                                  f"{MAX_MUST_CRITERIA}); split it so each part meets a few")
    if item.verify_command is None:
        warn("no_verify", f"{item.id} has no Verify command, so `its done` can't check it; give it one "
                          "(a test command in backticks)")
    do = item.get("Do") if "do" in item.fields else ""
    vague = VAGUE_RE.search(item.title) or VAGUE_RE.search(do or "")
    if vague and _empty(item.done_when):
        warn("vague", f"{item.id} says \"{vague.group(0)}\" with no Done when; say what is true when it is finished")
    return out


def sizing(pkg: Package) -> dict:
    """Sizing warnings for every todo item in the backlog and every proposal."""
    try:
        items = [i for i in load_backlog(pkg) if i.status in ("todo", "proposed")]
        items += [i for _, i in load_proposals(pkg)]
    except BacklogError as exc:
        return {"checked": False, "reason": str(exc), "items": 0, "warnings": []}
    criteria_by_fr = {r["id"]: len(r["criteria"]) for r in must_requirements(pkg)}
    warnings = [w for item in items for w in item_warnings(item, criteria_by_fr)]
    return {"checked": True, "items": len(items), "warnings": warnings,
            "flagged": sorted({w["id"] for w in warnings})}


def warnings_for(pkg: Package, item: BacklogItem) -> list[dict]:
    try:
        criteria_by_fr = {r["id"]: len(r["criteria"]) for r in must_requirements(pkg)}
    except OSError:
        criteria_by_fr = {}
    return item_warnings(item, criteria_by_fr)
