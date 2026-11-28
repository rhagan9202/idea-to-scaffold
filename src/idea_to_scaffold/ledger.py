"""The run ledger: one JSON line per attempt at an item, so `its stats` can say what works.

`start`, `adopt`, `done`, `block`, `land`, `gate`, `review --reply` and the Stop hook each append a record to
`runs/<ID>.jsonl` in the repository's state folder, outside the repository (`locking.state_dir`). That is on the
lead's machine, shared by every worktree, never committed and never part of a diff, so writing it can't trip the done
gate or conflict at land. An agent whose sandbox can't write there records nothing; the lead's
`&& its done --result` and `its land` still do.

A record is `{ts, item, event, outcome, ...}`. `done` records the checks and the gate's findings; with `--result`
it adds what the agent reported (`agent`, `cost_usd`, `turns`, `duration_ms`, `session_id`). A failed attempt
(`done`, `land` or `gate`) also records its number (`attempt`), the failing checks and tests and the findings'
messages: `its brief --retry` reads the last one back, and that is the only way anything here reaches an agent's
prompt. A failure that isn't the change's (`base_failing`: the integration branch fails on its own) is marked
`collateral`, and doesn't count as an attempt. Otherwise the ledger is for people deciding how to size and assign work.

Each `start` (and the `adopt` that claims an item) begins a run with a new `run` ID, which every later record for
the item carries until the next one: one ID links the start, the agent's result, `done`, the review and `land`.
`its stats` gives each rate with a 95% Wilson interval, and calls groups comparable only from `COMPARABLE` items
each: below that, the intervals overlap too much to rank agents or sizes, or to recalibrate anything.
"""
from __future__ import annotations

import json
import math
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .locking import state_dir
from .package import Package

EVENTS = ("start", "adopt", "brief", "done", "block", "land", "gate", "hook", "review")
FAILURES = ("check_failed", "gate_error", "no_fork_point", "test_changes", "dependency_changes", "outside_touches",
            "no_changes")  # the outcomes of `done`, `land` and `gate` that end an attempt at the item
KEPT_TESTS = 20
KEPT_MESSAGES = 10
COMPARABLE = 20  # items a group needs before its rate is compared with another's


def runs_dir(pkg: Package) -> Path | None:
    store = state_dir(pkg.root)
    return store / "runs" if store is not None else None


def new_run(pkg: Package, item: str) -> str | None:
    """Begin a run of `item` (at `start`, or the `adopt` that claims it): later records carry its ID."""
    folder = runs_dir(pkg)
    if folder is None:
        return None
    run = uuid.uuid4().hex[:12]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{item}.run").write_text(run + "\n", encoding="utf-8")
    except OSError:
        return None
    return run


def run_of(pkg: Package, item: str) -> str | None:
    """The ID of the item's current run, or None."""
    folder = runs_dir(pkg)
    if folder is None:
        return None
    try:
        return (folder / f"{item}.run").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def record(pkg: Package, item: str, event: str, outcome: str, **fields) -> None:
    """Append one record, with the item's current run ID. A ledger that can't be written never stops the command
    that is recording."""
    folder = runs_dir(pkg)
    if folder is None:
        return
    fields.setdefault("run", run_of(pkg, item))
    entry = {"ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "item": item, "event": event,
             "outcome": outcome, **{k: v for k, v in fields.items() if v is not None}}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{item}.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def failure_fields(checks: list[dict] | None, findings: list[dict] | None) -> dict:
    """What a failed attempt records for `its brief --retry`: the failing checks, the failing tests their test
    reports name, and the findings' messages (a few of each)."""
    failing = [c for c in checks or [] if not c.get("passed")]
    tests = sorted({t for c in failing for t in (c.get("results") or {}).get("failed_tests") or []})
    return {"failing_checks": [c["name"] for c in failing] or None, "failing_tests": tests[:KEPT_TESTS] or None,
            "messages": [str(f.get("message")) for f in findings or []][:KEPT_MESSAGES] or None}


def of(pkg: Package, item: str) -> list[dict]:
    """One item's records, oldest first."""
    folder = runs_dir(pkg)
    path = folder / f"{item}.jsonl" if folder is not None else None
    if path is None or not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def is_failure(entry: dict) -> bool:
    """Whether a record ends a failed attempt at its item: a refusal or failed check from `done` (not `--check`),
    `land` or `gate`, unless it was the integration branch's own failure."""
    return (entry.get("event") in ("done", "land", "gate") and entry.get("outcome") in FAILURES
            and not entry.get("check_only") and not entry.get("collateral"))


def failures(pkg: Package, item: str) -> list[dict]:
    """The failed attempts in the item's current run (since it was started or adopted), oldest first. One attempt is one agent
    run (its `session_id`, from `done --result`) or one commit (`head`): running `its done` again on the same work,
    or gating the same commit twice, is the same attempt, and only its latest record is kept."""
    entries = of(pkg, item)
    current = run_of(pkg, item)
    if current:
        scope = [e for e in entries if e.get("run") == current]
    else:  # a ledger from before run IDs: everything since the last start
        last = max((n for n, e in enumerate(entries) if e.get("event") == "start"
                    or (e.get("event") == "adopt" and e.get("outcome") == "claimed")), default=-1)
        scope = entries[last + 1:]
    seen: dict[str, dict] = {}
    for e in scope:
        if is_failure(e):
            key = str(e.get("session_id") or e.get("head") or e.get("ts"))
            seen.pop(key, None)
            seen[key] = e
    return list(seen.values())


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


def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    """The 95% Wilson score interval of a rate of `successes` in `n`, as [low, high], or None for n = 0."""
    if n <= 0:
        return None
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    scale = 1 + z * z / n
    return [round(max(0.0, (centre - margin) / scale), 2), round(min(1.0, (centre + margin) / scale), 2)]


def _attempt(e: dict) -> bool | None:
    """Whether a record is an attempt that passed (True) or failed (False), or None when it isn't an attempt: `done`
    (not `--check`), or `land` of an adopted candidate, which nobody ran `its done` on."""
    event, outcome = e.get("event"), e.get("outcome")
    if event == "done" and not e.get("check_only"):
        return outcome == "passed"
    if event == "land" and e.get("branch") and (outcome == "landed" or outcome in FAILURES) and not e.get("collateral"):
        return outcome == "landed"
    return None


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
    reviewed: Counter = Counter()
    by_size: dict[str, Counter] = defaultdict(Counter)
    by_agent: dict[str, Counter] = defaultdict(Counter)
    costs: list[float] = []
    for e in entries:
        it = items.setdefault(e["item"], {"id": e["item"], "attempts": 0, "passed": 0, "failed": 0, "blocked": 0,
                                         "landed": False, "cost_usd": 0.0, "first_try": None, "touches": None,
                                         "agents": [], "agent": None, "started_by": None, "runs": [],
                                         "review": None})
        if e.get("touches") is not None:
            it["touches"] = e["touches"]
        if e.get("run") and e["run"] not in it["runs"]:
            it["runs"].append(e["run"])
        agent = e.get("agent")
        if agent and agent not in it["agents"]:
            it["agents"].append(agent)
        event, outcome = e.get("event"), e.get("outcome")
        if agent and event in ("start", "adopt") and it["started_by"] is None:
            it["started_by"] = agent
        if isinstance(e.get("cost_usd"), (int, float)):
            it["cost_usd"] = round(it["cost_usd"] + float(e["cost_usd"]), 4)
            costs.append(float(e["cost_usd"]))
        passed = _attempt(e)
        if passed is not None:
            it["attempts"] += 1
            it["passed" if passed else "failed"] += 1
            if it["first_try"] is None:
                it["first_try"] = passed
                it["agent"] = agent  # the agent that made the first attempt is the item's agent
            for kind in e.get("findings", []) if not passed else []:
                refusals[kind] += 1
            if outcome == "check_failed":
                refusals["check_failed"] += 1
        if event == "block":
            it["blocked"] += 1
        elif event == "review":
            reviewed[outcome] += 1
            it["review"] = outcome
        elif event == "land":
            land[outcome] += 1
            if outcome == "landed":
                it["landed"] = True
    for it in items.values():
        # one identity per item: the agent that did the work (from its result or the vendor's branch), else whoever
        # started it, so a person's --agent at start and the model's name in its result aren't counted twice
        it["agent"] = it["agent"] or it["started_by"] or (it["agents"][0] if it["agents"] else None)
        if it["first_try"] is None:
            continue
        size = _bucket(it["touches"])
        by_size[size]["items"] += 1
        by_size[size]["first_try"] += bool(it["first_try"])
        by_agent[it["agent"] or "unknown"]["items"] += 1
        by_agent[it["agent"] or "unknown"]["first_try"] += bool(it["first_try"])

    def rates(groups: dict[str, Counter]) -> dict:
        return {k: {"items": v["items"], "first_try": v["first_try"],
                    "first_try_rate": round(v["first_try"] / v["items"], 2) if v["items"] else None,
                    "interval": wilson(v["first_try"], v["items"]), "comparable": v["items"] >= COMPARABLE}
                for k, v in sorted(groups.items())}

    tried = [i for i in items.values() if i["first_try"] is not None]
    passed_first = sum(1 for i in tried if i["first_try"])
    return {
        "items": sorted(items.values(), key=lambda i: i["id"]),
        "records": len(entries),
        "attempted": len(tried),
        "first_try": passed_first,
        "first_try_rate": round(passed_first / len(tried), 2) if tried else None,
        "first_try_interval": wilson(passed_first, len(tried)), "comparable_from": COMPARABLE,
        "refusals": dict(refusals.most_common()),
        "land": dict(land.most_common()),
        "reviews": dict(reviewed.most_common()),
        "cost_usd": round(sum(costs), 4), "cost_p90_usd": _p90(costs),
        "by_size": rates(by_size), "by_agent": rates(by_agent),
        "ledger": str(runs_dir(pkg)) if runs_dir(pkg) else None,
    }
