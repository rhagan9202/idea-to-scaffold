"""The text output: one renderer for each command's payload, and the extra lines some errors get.

A renderer takes the service's `Outcome` and the parsed arguments and prints. It reads only the payload, the
outcome's `extra` facts and the flags that change the text; it decides nothing. `--json` output is `emit`.
"""
from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from pathlib import Path

from ..contract import WorkError
from ..service import REFUSALS, Outcome, failure_report

# --- the envelope ---------------------------------------------------------------------------------------------------


def emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def fail(args: Namespace, err: WorkError) -> int:
    """Report an error: the JSON envelope with --json, else one line on stderr. The exit is the error's."""
    if getattr(args, "json", False):
        emit(err.to_dict())
    else:
        print(f"error: {err.message}", file=sys.stderr)
    return err.exit_code


def item_line(item: dict) -> str:
    milestone = f" ({item['milestone']})" if item.get("milestone") and item["milestone"] not in ("—", "-") else ""
    return f"{item['id']} — {item['title']}{milestone}"


def stale_note(stale: dict | None) -> str | None:
    if not stale:
        return None
    when = f"last fetched {stale['fetched_at'][:16].replace('T', ' ')} UTC" if stale["fetched_at"] else "never fetched"
    return f"note: {stale['remote']} {when}; claims made on other machines may be missing (use --fetch)"


# --- planning -------------------------------------------------------------------------------------------------------


def new(out: Outcome, args: Namespace) -> None:
    print(f"created blank {out.payload['profile']} planning package in {Path(out.payload['path'])}")
    print("next: fill it in with the prompts in prompts/stages/, or run the plan-project skill")


def import_(out: Outcome, args: Namespace) -> None:
    p = out.payload
    print(f"imported {len(p['written'])} new or changed file(s) into {args.into}" + (":" if p["written"] else ""))
    for rel in p["written"]:
        print(f"  {rel}")
    if p["seeded"]:
        print(f"added {len(p['seeded'])} default file(s): {', '.join(p['seeded'])}")
    if p["link_warning"] is None:
        print(f"cross-linked {p['linked']} file(s) and wrote planning/README.md")
    else:
        print(f"warning: could not cross-link: {p['link_warning']}")
    if p["validation"] is None:
        print(f"error: {p['error']['message']}", file=sys.stderr)
        return
    validate(Outcome(p["validation"], out.code, out.extra), args)


def validate(out: Outcome, args: Namespace) -> None:
    p = out.payload
    for e in p["errors"]:
        print(f"ERROR   {e}")
    for w in p["warnings"]:
        print(f"warning {w}")
    traced, sized = p.get("trace"), p.get("sizing")
    if traced is not None:
        _trace(traced)
    if sized is not None:
        for w in sized["warnings"]:
            print(f"sizing  {w['kind']}: {w['message']}")
        print(f"sizing: {len(sized['flagged'])} of {sized['items']} todo or proposed item(s) may be too big or vague"
              if sized["checked"] else f"sizing: skipped ({sized['reason']})")
    if p["valid"] and out.extra.get("repo_layout"):
        print("planning docs are valid")
    elif p["ready"]:
        print("package is valid and ready to scaffold")
    elif p["valid"]:
        print("package is valid, but not ready to scaffold yet")
    else:
        print(f"package has {len(p['errors'])} error(s)")


def _trace(traced: dict) -> None:
    if not traced["checked"]:
        print(f"trace: skipped ({traced['reason']})")
        return
    for c in traced["uncovered"]:
        print(f"UNTESTED {c['id']} AC{c['ac']}: {c['text']}")
    for c in traced["by_requirement"]:
        print(f"coarse   {c['id']} AC{c['ac']}: only tests naming {c['id']} as a whole")
    covered = traced["criteria"] - len(traced["uncovered"])
    print(f"trace: {covered} of {traced['criteria']} Must acceptance criteria are named by a test")


def link(out: Outcome, args: Namespace) -> None:
    changed = out.payload["changed"]
    if args.check:
        print("cross-links and commands are up to date")
        return
    print(f"updated {len(changed)} file(s)" if changed else "already up to date")
    for rel in changed:
        print(f"  {rel}")


def link_error(err: WorkError, args: Namespace) -> bool:
    if err.code != "link_outdated":
        return False
    print("generated links or commands are out of date in:")
    for rel in err.details["changed"]:
        print(f"  {rel}")
    print("run `its link` and commit the result")
    return True


def show(out: Outcome, args: Namespace) -> None:
    p = out.payload
    where = f"{p['file']}#{p['anchor']}" if p["anchor"] else p["file"]
    print(f"{p['id']} ({p['kind']}), defined in {where}, line {p['line']}")
    print()
    print(p["text"])
    print()
    refs = p["referenced_by"]
    print("mentioned in: " + (", ".join(refs) if refs else "no other doc"))


def propose(out: Outcome, args: Namespace) -> None:
    print(f"proposed {out.payload['id']} in {out.payload['file']}")
    print("  it gets a permanent ID when someone runs its promote or its renumber on the integration branch")


def edited(out: Outcome, args: Namespace) -> None:
    """promote, block, unblock, drop and set-status."""
    p = out.payload
    was = p.get("renumbered_from")
    if was:
        print(f"{was} is now {p['id']}")
    if p.get("deleted_file"):
        print(f"{p['id']} dropped: deleted {p['deleted_file']}")
    else:
        print(f"{p['id']} is now {p['status']} (was {p['previous_status']})")
    if p.get("reason"):
        print(f"  reason: {p['reason']}")
    for w in p.get("sizing") or []:
        print(f"  sizing: {w['message']}")
    if p.get("committed"):
        print(f"  committed {p['committed']}")


def renumber(out: Outcome, args: Namespace) -> None:
    p = out.payload
    if not p["renumbered"]:
        print("no placeholder items to renumber")
    for r in p["renumbered"]:
        print(f"{r['from']} -> {r['to']}" + (" (dry run)" if args.dry_run else ""))
    for f in p["files"]:
        print(f"  updated {f}")
    if p.get("committed"):
        print(f"  committed {p['committed']}")


def scaffold(out: Outcome, args: Namespace) -> None:
    p = out.payload
    if p["aborted"]:
        print("aborted: nothing was changed", file=sys.stderr)
        return
    print(f"scaffolded {Path(p['path'])} ({p['files']} files)")
    for w in p["warnings"]:
        print(f"warning {w}")
    if p["committed"]:
        print("git: initialised on main with the initial commit")
    if p["remote"]:
        print(f"github: pushed to {p['remote']}")
    print(f"next: open {Path(p['path']) / 'HANDOFF.md'} and give its kick-off prompt to your agent")


# --- work -----------------------------------------------------------------------------------------------------------


def backlog(out: Outcome, args: Namespace) -> None:
    if out.extra.get("written"):
        print(f"wrote {out.extra['written']} (generated and gitignored; don't edit it)")
        return
    p = out.payload
    items = p["items"]
    proposals = [] if args.ready or args.status not in (None, "proposed") else p["proposals"]
    if not items and not proposals:
        what = " ready" if args.ready else (f" with status {args.status}" if args.status else "")
        print(f"no backlog items{what}")
        return
    rows = items + proposals
    id_width = max(8, *(len(i["id"]) + 1 for i in rows))
    print(f"{'ID':<{id_width}} {'STATUS':<12} {'MILESTONE':<10} TITLE")
    for i in rows:
        print(f"{i['id']:<{id_width}} {i['status']:<12} {i['milestone'] or '—':<10} {i['title']}")
    if proposals:
        print(f"{len(proposals)} proposal(s) in {p['proposals'][0]['file'].rsplit('/', 1)[0]}/ wait for "
              "its promote or its renumber")


def next_notes(out: Outcome, args: Namespace) -> None:
    """What `next` says on stderr, with --json or not: only text gets it."""
    p = out.payload
    note = stale_note(p["stale_remote"])
    if note:
        print(note, file=sys.stderr)
    if p["behind"]:
        print(f"note: {p['source']} is ahead of this checkout by {p['behind']} commit(s); the backlog is "
              f"read from there. Pull before starting work here: git pull --ff-only", file=sys.stderr)


def next_(out: Outcome, args: Namespace) -> None:
    p = out.payload
    next_notes(out, args)
    for w in p.get("warnings", []):
        print(f"warning: {w['message']}", file=sys.stderr)
    if args.parallel is not None:
        for item in p["parallel"]:
            print(item_line(item))
            print(f"  touches: {', '.join(item['touches']) or '(not listed)'}")
            print(f"  start:   its start {item['id']} --worktree")
        for held in p["held_back"]:
            why = "" if held["touches_listed"] else " (it lists no Touches)"
            print(f"held back: {held['id']}{why} may touch the same files as {', '.join(held['overlaps'])}")
        if not p["parallel"]:
            print("nothing can start in parallel right now")
        return
    for claim in p["claimed"]:
        print(f"claimed: {claim['id']} on {claim['branch']}" + (f" ({claim['worktree']})" if claim["worktree"] else ""))
    if p["item"] is None:
        print({
            "overlap": "nothing can start now: every ready item may touch the same files as running work "
                       "(its next --parallel shows which)",
            "waiting": "nothing is ready",
            "in_progress": "nothing is ready: everything left is in progress, claimed or blocked",
            "complete": "nothing is left: every item is done",
        }.get(p["reason"], "nothing is ready"))
        if p["in_progress"]:
            print(f"  in progress: {', '.join(p['in_progress'])}")
        for w in p["waiting"]:
            print(f"  {w['id']} waits on {', '.join(w['blocked_by'])}")
        for cycle in p["cycles"]:
            print(f"  dependency cycle: {' -> '.join(cycle)}")
        return
    for item in p["ready"] if args.all else [p["item"]]:
        print(item_line(item))
        if item["satisfies"]:
            print(f"  satisfies: {', '.join(item['satisfies'])}")
        if item["verify"]:
            print(f"  verify:    {item['verify']}")
    if not args.all:
        print(f"next: its start {p['item']['id']}")


def start(out: Outcome, args: Namespace) -> None:
    p = out.payload
    item, claim = p["item"], p["claim"]
    if claim:
        where = f" in the new worktree {claim['worktree']}" if claim["worktree"] else ", checked out here"
        print(f"{item_line(item)} is claimed by branch {claim['branch']}{where}")
        if p.get("pushed"):
            print(f"  pushed to {p['remote']}, so other machines see the claim")
        elif p.get("push_error"):
            print(f"  warning: the claim is only local; pushing it failed: {p['push_error']}")
        setup = p.get("setup")
        if setup and setup["passed"]:
            wrote = f"; it wrote {', '.join(setup['lockfiles'])}, which done lets through" if setup["lockfiles"] else ""
            print(f"  ran setup in the worktree: {setup['command']}{wrote}")
        elif setup:
            print(f"  warning: setup failed in the worktree (exit {setup['exit_code']}): {setup['command']}")
            for line in (setup.get("output") or "").splitlines()[-5:]:
                print(f"    {line}")
        for w in p.get("warnings", []):
            print(f"  warning: {w['message']}")
        print("start an agent on it:")
        print(f"  {p['commands']['claude']}")
        print(f"  {p['commands']['codex']}")
        headless = p["commands"].get("headless")
        if headless:
            print(f"or unattended (sandboxed, budget ${headless['budget_usd']:g}, push tokens unset; the script ends "
                  "with `its done --result`, which commits the work and pushes the branch):")
            print(f"  {headless['claude']}")
            print(f"  {headless['codex']}")
            if os.name == "nt":
                print(f"  (from cmd you can also run {headless['scripts']['claude']['cmd']})")
    else:
        print(f"{item_line(item)} is now in-progress (was {p['previous_status']})")
    if item["verify"]:
        print(f"  verify: {item['verify']}")
    print(f"when it's finished: its done {item['id']}")


def start_error(err: WorkError, args: Namespace) -> bool:
    if err.code == "blocked":
        print(f"error: {err.message} (use --force to start it anyway)", file=sys.stderr)
        return True
    if err.code == "overlap":
        print(f"error: {err.message}", file=sys.stderr)
        print("see what can run now: its next --parallel", file=sys.stderr)
        return True
    return False


def done(out: Outcome, args: Namespace) -> None:
    p = out.payload
    if p.get("status") == "blocked" and args.result:  # the headless run ended blocked
        print(f"{p['id']} is now blocked (was {p['previous_status']})")
        print(f"  reason: {p['reason']}")
        run_commit(p)
        return
    if p["checks_skipped"]:
        print(f"checks skipped: {p['checks_skipped']}")
    for finding in p["findings"]:
        if finding.get("mode") == "warn":
            print(f"  warning: {finding['kind']}: {finding['message']} (warn mode: not refused yet)")
        else:
            print(f"  allowed: {finding['kind']}: {finding['message']}")
    if p.get("checked_only"):
        print(f"{p['id']} passes its checks (status unchanged: {p['status']})")
        return
    if p.get("already_done"):
        print(f"{p['id']} was already done; its checks pass again and its Verified mark is {p['verified']}")
        run_commit(p)
        return
    print(f"{p['id']} is now done (was {p['previous_status']})")
    if p["manual_verify"]:
        print(f"  check by hand: {p['manual_verify']}")
    if p["now_ready"]:
        when = "ready once this branch is merged" if p["merge_needed"] else "now ready"
        print(f"  {when}: {', '.join(p['now_ready'])}")
    elif p["merge_needed"] and "committed" not in p:
        print("  commit, then merge this branch; dependent items become ready once it is merged")
    run_commit(p)


def done_error(err: WorkError, args: Namespace) -> bool:
    if err.code in REFUSALS and len(err.details["findings"]) > 1:
        for finding in err.details["findings"]:
            print(f"  {finding['kind']}: {finding['message']}")
    if err.code == "check_failed":
        print("\n" + failure_report(err.details["checks"]))
    again = err.details.get("retry")
    if again and args.result:  # a headless run ended: say what's next
        print(f"attempt {again['attempt']} of {again['max_attempts']}: "
              + (f"retry with {again['command']}" if again["command"] else "no retries left; it needs a person"),
              file=sys.stderr)
    return False


def run_commit(payload: dict) -> None:
    """What `done --result` did with the run's work: the commit and the push."""
    if "committed" not in payload:
        return
    if payload.get("commit_error"):
        print(f"  warning: the work isn't committed: {payload['commit_error']}")
    elif payload.get("commit_skipped"):
        print(f"  {payload['commit_skipped']}")
    elif payload["committed"]:
        print(f"  committed {payload['committed']}")
    if payload.get("pushed"):
        print(f"  pushed {payload['branch']} to {payload['remote']}; land it from the integration branch: its land --fetch")
    elif payload.get("push_error"):
        print(f"  warning: the branch isn't pushed: {payload['push_error']}")
    elif payload.get("push_skipped"):
        print(f"  not pushed: {payload['push_skipped']}")


# --- lead -----------------------------------------------------------------------------------------------------------

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
SECTION_TITLES = {"needs_you": "needs you", "ready_to_land": "ready to land (its land)", "running": "running",
                  "stale": "stale (no commit for a while: check on it, or its release it)",
                  "done": "done (its release <ID>)"}


def status(out: Outcome, args: Namespace) -> None:
    print("\n".join(status_lines(out.payload)))
    note = stale_note(out.payload["stale_remote"])
    if note:
        print(note)


def status_lines(payload: dict) -> list[str]:
    """`its status` as text: the attention inbox, most urgent group first."""
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
    for section in payload["attention"]:  # in the payload's order: most urgent first
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


def claims(out: Outcome, args: Namespace) -> None:
    p = out.payload
    if not p["claims"]:
        print("no claims")
    for c in p["claims"]:
        state = STATE_LABELS.get(c.get("state", ""), "active")
        if c.get("state") == "blocked" and c.get("blocked_reason"):
            state += f": {c['blocked_reason']}"
        extra = f"  worktree {c['worktree']}" if c["worktree"] else ""
        print(f"{c['id']:<10} {c['branch']}  [{state}]{extra}")
        if c.get("conflicts_with_base"):
            print(f"           would conflict with {p['base']} in {', '.join(c['conflicts_with_base'])}")
        for other in c.get("conflicts_with", []):
            print(f"           would conflict with {other['id']} in {', '.join(other['files'])}")


def release(out: Outcome, args: Namespace) -> None:
    p = out.payload
    if not p["released"]:
        print(f"{p['id']} has no claim to release")
    if p["removed_worktree"]:
        print(f"removed worktree {p['removed_worktree']}")
    for b in p["deleted_branches"]:
        print(f"deleted branch {b}")
    for b in p["deleted_remote_branches"]:
        print(f"deleted pushed branch {b}")
    for b in p["remote_branches"]:
        print(f"still on the remote: {b} (add --remote to delete it)")


def adopt(out: Outcome, args: Namespace) -> None:
    p = out.payload
    cand = p["candidate"]
    if p["created"]:
        where = f", pushed to {p['remote']}" if p["pushed"] else ""
        print(f"claimed {p['id']} with {p['claim']['branch']}{where}")
    if p["push_error"]:
        print(f"warning: the claim wasn't pushed: {p['push_error']}", file=sys.stderr)
    verb = "adopted" if p["added"] else "already adopted"
    print(f"{verb} {cand['name']} ({cand['tip'][:10]}) for {p['id']}: {p['title']}")
    if len(p["candidates"]) > 1:
        print(f"candidates, tried in this order: {', '.join(c['name'] for c in p['candidates'])}")
    for other in p["overlaps"]:
        print(f"note: {p['id']} may change the same files as {other}, which is running")
    print(f"next: {p['commands']['land']}  (runs the done gate and the held-out tests on each candidate)")


def brief(out: Outcome, args: Namespace) -> None:
    p = out.payload
    issue = p["issue"]
    if issue is None:
        sys.stdout.write(p["brief"])
        return
    where = f": {issue['url']}" if issue.get("url") else ""
    print(f"{issue['action']} issue #{issue['number']} for {p['id']}{where}")
    if issue["action"] == "created":
        print("assign it to an agent on GitHub (Copilot, Claude or Codex), then: "
              f"its adopt <its branch or pr:N> {p['id']}")


def gate(out: Outcome, args: Namespace) -> None:
    p = out.payload
    what = f"{p['id']} ({p['found_by'].replace('_', ' ')})" if p["id"] else "nothing"
    print(f"gate {what} on {p['branch'] or p['ref']} against {p['base']}: " + ("passed" if p["passed"] else "refused"))
    print(f"  {p['message']}")
    for finding in p["findings"]:
        print(f"  {finding['kind']}: {finding['message']}")
    for check in p["checks"]:
        print(f"  {'ok' if check['passed'] else 'failed'}: {check['name']}: {check['command']}")
        if not check["passed"] and check.get("output"):
            print(check["output"])
    for w in p["warnings"]:
        print(f"  warning: {w['kind']}: {w['message']}")


def land(out: Outcome, args: Namespace) -> None:
    p = out.payload
    for landed in p["landed"]:
        print(f"landed {landed['id']} ({landed['branch']})")
    for skip in p["skipped"]:
        if skip["reason"] == "dry_run":
            print(f"would land {skip['id']} ({skip['branch']})")
            continue
        print(f"not landed: {skip['id']}: {skip['message']}")
        for check in skip.get("checks", []):
            if not check["passed"]:
                print(f"  {check['name']} failed (exit {check['exit_code']}): {check['command']}")
                if check["output"]:
                    print(check["output"])
    if not p["landed"] and not p["skipped"]:
        print("nothing to land: no claim branch has a done item that isn't merged yet")
    if p["pushed"]:
        print(f"pushed {p['base']}")
    for ident in p["released"]:
        print(f"released the claim on {ident}")
    if p["proposals"]:
        print(f"{len(p['proposals'])} proposal(s) are waiting: its promote <ID>, or its renumber to number them")


def land_error(err: WorkError, args: Namespace) -> bool:
    if err.code == "base_untrusted":
        for line in err.details["commits"]:
            print(f"  {line}", file=sys.stderr)
    return False


def waive(out: Outcome, args: Namespace) -> None:
    p = out.payload
    w = p["waiver"]
    print(f"{p['id']}: waived {', '.join(w['groups'])} ({w['reason']})")
    print(f"  committed {p['committed']}; its done and its land honour it from now on")


def review_prompt(out: Outcome, args: Namespace) -> None:
    sys.stdout.write(out.payload["prompt"])


def review_reply(out: Outcome, args: Namespace) -> None:
    p = out.payload
    print(f"review of {p['id']}: {p['outcome'].replace('_', ' ')}"
          + (f" (the reviewer said {p['verdict'].replace('_', ' ')})" if p["verdict"] != "approve"
             and p["outcome"] != "changes_requested" else ""))
    for e in p["evidence"]:
        print(f"  {'reproduced' if e['reproduced'] else 'not reproduced, downgraded'}: {e['kind']} {e['of']}: "
              f"{e['why']}" + (f" ({e['command']})" if e.get("command") else ""))
    for ident in p["unmet"]:
        print(f"  {ident}: " + ("not in the reply" if ident in p["missing"] else "not met"))
    for f in p["findings"]:
        mark = "!" if f in p["blocking"] else " "
        print(f" {mark}{f['severity']:<8} {f['file']}{':' + str(f['line']) if f.get('line') else ''}  {f['message']}")
    if not p["current"]:
        print(f"{p['id']} changed since the review prompt was made: this verdict counts only for what the reviewer saw")
    elif p["recorded"]:
        print("recorded: its land " + ("will take it" if p["passed"] else
                                       "refuses this version until it changes and is reviewed again"
                                       if p["outcome"] == "changes_requested" else
                                       "takes it, unless it needs an approving review"))


# --- maintain -------------------------------------------------------------------------------------------------------


def drift(out: Outcome, args: Namespace) -> None:
    p = out.payload
    for entry in p["drifted"]:
        print(f"{entry['id']} {entry['title']}: changed since it was verified ({entry['verified'] or 'when done'})")
        for f in entry["files"]:
            print(f"  {f}")
        for c in entry["commits"]:
            print(f"    {c}")
    for req in p["requirements"]:
        print(f"{req['id']} may no longer hold: re-verify {', '.join(req['items'])} (its verify <ID>)")
    for entry in p["unknown"]:
        print(f"{entry['id']}: can't tell ({entry['message']})")
    if p["no_touches"]:
        print(f"not tracked (no Touches): {', '.join(p['no_touches'])}")
    if not p["drifted"]:
        print(f"no drift: {len(p['clean'])} done item(s) unchanged since they were verified")


def verify(out: Outcome, args: Namespace) -> None:
    p = out.payload
    if "ids" not in out.extra:  # one item
        print(f"{p['id']} verified again: {p['verified']}"
              + (f" (committed {p['committed']})" if p.get("committed") else " (commit the backlog to record it)"))
        return
    verified, failed = p["verified"], p["failed"]
    if not out.extra["ids"]:
        print("nothing to verify" + (": no item has drifted" if args.drifted else ""))
        return
    for f in failed:
        print(f"  not verified: {f['id']}: {f['message']}")
    print(f"verified again: {', '.join(v['id'] for v in verified) or 'none'}"
          + (f"; failed: {', '.join(f['id'] for f in failed)}" if failed else "")
          + ((f" (committed {p['committed']})" if p.get("committed") else
              " (commit the backlog to record it)") if verified else ""))


def verify_error(err: WorkError, args: Namespace) -> bool:
    if err.code == "check_failed":
        print("\n" + failure_report(err.details["checks"]))
    return False


def stats(out: Outcome, args: Namespace) -> None:
    p = out.payload
    if not p["records"]:
        print("the run ledger is empty: start, done, block and land record to it" if p["ledger"] else
              "the run ledger needs a git repository")
        return

    def rate(value: float | None, interval: list[float] | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.0%}" + (f", 95% interval {interval[0]:.0%}-{interval[1]:.0%}" if interval else "")

    print(f"{p['attempted']} item(s) attempted, {p['first_try']} passed first time "
          f"({rate(p['first_try_rate'], p['first_try_interval'])})")
    few = False
    for name, label in (("by_size", "size"), ("by_agent", "agent")):
        for key, g in p[name].items():
            few = few or not g["comparable"]
            print(f"  {label} {key}: {g['first_try']} of {g['items']} first time "
                  f"({rate(g['first_try_rate'], g['interval'])})")
    if few:
        print(f"  (groups under {p['comparable_from']} items are too few to compare: their intervals overlap)")
    if p["refusals"]:
        print("refused for: " + ", ".join(f"{k} {v}" for k, v in p["refusals"].items()))
    if p["land"]:
        print("land: " + ", ".join(f"{k} {v}" for k, v in p["land"].items()))
    if p["reviews"]:
        print("reviews: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in p["reviews"].items()))
    if p["cost_usd"]:
        p90 = f", p90 per run ${p['cost_p90_usd']:.2f}" if p["cost_p90_usd"] is not None else ""
        print(f"agent cost ${p['cost_usd']:.2f}{p90}")
    print(f"ledger: {p['ledger']}")


def gate_audit(out: Outcome, args: Namespace) -> None:
    p = out.payload
    print(f"gate-audit for {p['id']} against {p['base']}" + (f", using {p['test_file']}" if p["test_file"] else ""))
    for v in p["variants"]:
        if v["caught"] is None:
            mark, note = "skip   ", v["skipped"]
        elif v["caught"]:
            mark, note = ("refused" if v["mode"] == "enforce" else "warns  "), ", ".join(v["found"])
        else:
            mark, note = "MISSED ", "found " + (", ".join(v["found"]) or "nothing")
        print(f"  {mark}  {v['name']:<24} {v['description']}  ({note})")
    print(f"refused {p['refused']} of {p['total']}"
          + (f"; only warned: {', '.join(p['warned'])}" if p["warned"] else "")
          + (f"; missed: {', '.join(p['missed'])}" if p["missed"] else ""))
    if p["warned_kinds"]:
        checks = "".join(f"\n    {kind}: enforce" for kind in p["warned_kinds"])
        print(f"to refuse the warned ones, set this in package.yaml on the integration branch:\n  gate:\n  "
              f"  checks:{checks.replace(chr(10), chr(10) + '  ')}")


def hooks(out: Outcome, args: Namespace) -> None:
    p = out.payload
    for rel in p["written"]:
        print(f"added a Stop hook ({p['command']}) to {rel}")
    for rel in p["unchanged"]:
        print(f"{rel} already has the Stop hook")
    print("commit them to share; Codex runs project hooks only once you trust them (/hooks)")


def hook(out: Outcome, args: Namespace) -> None:
    if out.payload:
        print(json.dumps(out.payload))
