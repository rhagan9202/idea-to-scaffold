"""The run ledger: one JSON line per attempt at an item, so `its stats` can say what works.

`start`, `done`, `block`, `land` and the Stop hook each append a record to `runs/<ID>.jsonl` in the repository's
shared git folder (`$(git rev-parse --git-common-dir)/its/runs/`). That is on the lead's machine, shared by every
worktree, never committed and never part of a diff, so writing it can't trip the done gate or conflict at land.

A record is `{ts, item, event, outcome, ...}`. `done` records the checks and the gate's findings; with `--result`
it adds what the agent reported (`agent`, `cost_usd`, `turns`, `duration_ms`, `session_id`). Nothing here is ever
read back into an agent's prompt: the ledger is for people deciding how to size and assign work.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .locking import git_store_dir
from .package import Package

EVENTS = ("start", "done", "block", "land", "hook")


def runs_dir(pkg: Package) -> Path | None:
    store = git_store_dir(pkg.root)
    return store / "runs" if store is not None else None


def record(pkg: Package, item: str, event: str, outcome: str, **fields) -> None:
    """Append one record. A ledger that can't be written never stops the command that is recording."""
    folder = runs_dir(pkg)
    if folder is None:
        return
    entry = {"ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "item": item, "event": event,
             "outcome": outcome, **{k: v for k, v in fields.items() if v is not None}}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{item}.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def read(pkg: Package) -> list[dict]:
    folder = runs_dir(pkg)
    if folder is None or not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("item"):
                out.append(entry)
    return sorted(out, key=lambda e: e.get("ts", ""))


def _bucket(n: int | None) -> str:
    if n is None:
        return "unknown"
    return "1 file" if n <= 1 else "2-3 files" if n <= 3 else "4+ files"


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 4)
    return round(statistics.quantiles(values, n=10, method="inclusive")[-1], 4)


def stats(pkg: Package) -> dict:
    """What the ledger says: per item and overall, how often `done` passed first time, what the gate refused, how
    land went, and what it cost, grouped by size (Touches entries) and by agent."""
    entries = read(pkg)
    items: dict[str, dict] = {}
    refusals: Counter = Counter()
    land: Counter = Counter()
    by_size: dict[str, Counter] = defaultdict(Counter)
    by_agent: dict[str, Counter] = defaultdict(Counter)
    costs: list[float] = []
    for e in entries:
        it = items.setdefault(e["item"], {"id": e["item"], "attempts": 0, "passed": 0, "failed": 0, "blocked": 0,
                                         "landed": False, "cost_usd": 0.0, "first_try": None, "touches": None,
                                         "agents": []})
        if e.get("touches") is not None:
            it["touches"] = e["touches"]
        agent = e.get("agent")
        if agent and agent not in it["agents"]:
            it["agents"].append(agent)
        if isinstance(e.get("cost_usd"), (int, float)):
            it["cost_usd"] = round(it["cost_usd"] + float(e["cost_usd"]), 4)
            costs.append(float(e["cost_usd"]))
        event, outcome = e.get("event"), e.get("outcome")
        if event == "done" and not e.get("check_only"):
            it["attempts"] += 1
            passed = outcome == "passed"
            it["passed" if passed else "failed"] += 1
            if it["first_try"] is None:
                it["first_try"] = passed
            for kind in e.get("findings", []) if not passed else []:
                refusals[kind] += 1
            if outcome == "check_failed":
                refusals["check_failed"] += 1
        elif event == "block":
            it["blocked"] += 1
        elif event == "land":
            land[outcome] += 1
            if outcome == "landed":
                it["landed"] = True
    for it in items.values():
        if it["first_try"] is None:
            continue
        size = _bucket(it["touches"])
        by_size[size]["items"] += 1
        by_size[size]["first_try"] += bool(it["first_try"])
        for agent in it["agents"] or ["unknown"]:
            by_agent[agent]["items"] += 1
            by_agent[agent]["first_try"] += bool(it["first_try"])

    def rates(groups: dict[str, Counter]) -> dict:
        return {k: {"items": v["items"], "first_try": v["first_try"],
                    "first_try_rate": round(v["first_try"] / v["items"], 2) if v["items"] else None}
                for k, v in sorted(groups.items())}

    tried = [i for i in items.values() if i["first_try"] is not None]
    return {
        "items": sorted(items.values(), key=lambda i: i["id"]),
        "records": len(entries),
        "attempted": len(tried),
        "first_try": sum(1 for i in tried if i["first_try"]),
        "first_try_rate": round(sum(1 for i in tried if i["first_try"]) / len(tried), 2) if tried else None,
        "refusals": dict(refusals.most_common()),
        "land": dict(land.most_common()),
        "cost_usd": round(sum(costs), 4), "cost_p90_usd": _p90(costs),
        "by_size": rates(by_size), "by_agent": rates(by_agent),
        "ledger": str(runs_dir(pkg)) if runs_dir(pkg) else None,
    }
