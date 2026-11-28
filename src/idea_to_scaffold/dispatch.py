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

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone

from . import claims as cl
from . import gate, gitx, ledger
from .checks import checks_for
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
    ledger.record(pkg, ident, "adopt", "claimed" if created else "candidate_added", branch=cand["name"],
                  agent=cand["agent"], pr=cand.get("pr"))
    claim_dict = {**claim.to_dict(), "candidates": candidates}
    return {
        "schema": SCHEMA, "ok": True, "id": ident, "title": item.title, "claim": claim_dict, "created": created,
        "candidate": cand, "added": added, "candidates": candidates, "pushed": pushed, "push_error": push_error,
        "remote": remote if pushed else None, "overlaps": overlaps,
        "commands": {"land": f"its land {ident}", "review": f"its review {ident} --branch {cand['name']}",
                     "release": f"its release {ident}" + (" --remote" if pushed else "")},
    }


# --- its brief ---------------------------------------------------------------------------------------------------
# A vendor's agent starts from a prompt, usually an issue, and sees none of `its`. The brief is everything it needs
# in one text: the item and the requirements it satisfies, as the integration branch has them, the commands that
# must pass, and the rules the gate holds the branch to, so the agent doesn't learn them by being refused.

def brief(pkg: Package, ident: str, *, retry: bool = False, github: bool = False, base: str | None = None) -> dict:
    """`its brief ID`: a self-contained task for an agent that may never run `its`. `retry` writes the brief for one
    more attempt after a failed one: only what failed (checks, tests, findings), up to `gate.max_attempts`. `github`
    puts it on a GitHub issue (created, updated, or commented on for a retry) that can be assigned to an agent."""
    from .review import _requirement, needs_review  # review imports work; dispatch is imported by the CLI only

    v = view(pkg, base)
    item = _find(v.items, ident)
    if item.status in ("done", "dropped"):
        raise WorkError("bad_status", f"{ident} is {item.status} on {v.source}; there is nothing to brief",
                        details={"id": ident, "status": item.status})
    ref = v.base
    policy = gate.Policy.of((gate.manifest_at(pkg, ref) if ref else pkg.manifest) or {}, ref or "working tree")
    again = _retry(pkg, ident, policy) if retry else None
    checks = [{"name": c.name, "command": c.command} for c in checks_for(pkg, item, item.verify_command, policy.commands)]
    held = bool(ref) and bool(gitx.out(pkg.root, "ls-tree", "-d", "--name-only", ref, "--",
                                       (_prefix(pkg) + policy.acceptance).rstrip("/"), check=False).strip())
    review_needed = needs_review(gate.Inspection(base=ref, head=None, policy=policy, planned=item))
    rules = _rules(item, policy, checks, held, review_needed)
    if again:
        text = _retry_text(ident, item, again, rules)
    else:
        reqs = [_requirement(pkg, r, ref) for r in item.satisfies]
        text = _brief_text(ident, item, reqs, rules)
    issue = _github(pkg, ident, item.title, text, retry=bool(again)) if github else None
    ledger.record(pkg, ident, "brief", "retry" if again else "written", attempt=again["attempt"] if again else None,
                  issue=issue["number"] if issue else None)
    return {"schema": SCHEMA, "ok": True, "id": ident, "title": item.title, "source": v.source, "brief": text,
            "retry": again, "checks": checks, "held_out": held, "review_required": review_needed, "issue": issue,
            "commands": {"claude": f"its brief {ident}{' --retry' if again else ''} | claude -p",
                         "codex": f"its brief {ident}{' --retry' if again else ''} | codex exec -",
                         "adopt": f"its adopt <branch or pr:N> {ident}", "gate": f"its gate <branch> --id {ident}"}}


def _prefix(pkg: Package) -> str:
    return gitx.out(pkg.root, "rev-parse", "--show-prefix", check=False).strip() if cl.in_git(pkg.root) else ""


def _retry(pkg: Package, ident: str, policy: gate.Policy) -> dict:
    """The last failed attempt at `ident`, for a retry brief, or why there can't be one."""
    failed = ledger.failures(pkg, ident)
    if not failed:
        last = next((e for e in reversed(ledger.of(pkg, ident)) if e.get("collateral")), None)
        why = " (its last failure was the integration branch's own: fix that first)" if last else ""
        raise WorkError("nothing_to_retry", f"{ident} has no failed attempt to retry since it was started{why}",
                        details={"id": ident})
    if len(failed) >= policy.max_attempts:
        raise WorkError("out_of_attempts", f"{ident} has failed {len(failed)} time(s) and gate.max_attempts is "
                        f"{policy.max_attempts}: it needs a person. Split or clarify it, or its block {ident}",
                        details={"id": ident, "attempts": len(failed), "max_attempts": policy.max_attempts})
    last = failed[-1]
    acceptance = "/" + policy.acceptance.strip("/") + "/"
    tests = last.get("failing_tests") or []
    hidden = [t for t in tests if acceptance in "/" + t.split("::", 1)[0]]  # held-out tests stay hidden
    return {"attempt": len(failed) + 1, "max_attempts": policy.max_attempts, "after": last.get("event"),
            "outcome": last.get("outcome"), "branch": last.get("branch"), "head": last.get("head"),
            "failing_checks": last.get("failing_checks") or last.get("checks") or [],
            "failing_tests": [t for t in tests if t not in hidden], "held_out_failed": len(hidden),
            "findings": last.get("findings") or [], "messages": last.get("messages") or []}


def _rules(item, policy: gate.Policy, checks: list[dict], held: bool, review: bool) -> list[str]:
    touches = ", ".join(f"`{t}`" for t in item.touches)
    rules = [f"Change only the files the item touches, tests included: {touches}. A change anywhere else is "
             "refused." if touches else
             "The item lists no files: keep the change to what it needs, and say which files you changed.",
             "Don't delete, skip, disable or weaken a test, and don't change the test configuration, package.yaml, "
             "the item's Verify, other backlog items, or agent instructions and settings (AGENTS.md, CLAUDE.md, "
             ".claude/, .codex/). The gate refuses a branch that does."]
    if policy.mode("tests_pass_on_parent") != "off":
        rules.append("Each test you add or change has to fail on the code you started from: a test that already "
                     "passes there shows nothing" + (" (the gate refuses it)." if policy.mode("tests_pass_on_parent")
                                                      == "enforce" else " (the gate flags it)."))
    if policy.mode("dependency_added") != "off":
        rules.append("Don't add a dependency unless the item asks for one" +
                     (": the gate refuses it." if policy.mode("dependency_added") == "enforce" else
                      ": the gate flags it for the lead."))
    if checks:
        rules.append("These commands must pass: " + "; ".join(f"{c['name']} `{c['command']}`" for c in checks) + ".")
    if held:
        rules.append("Held-out acceptance tests that you can't see run when the branch lands; they check the "
                     "requirements.")
    if review:
        rules.append("A reviewer checks the change against the requirements before it lands.")
    return rules


def _finish(ident: str, item) -> list[str]:
    slug = cl.branch_name(ident, item.title).split("/", 1)[-1]
    return [
        "## When you finish",
        f"- Commit on a branch named with the item's ID, such as `<your-prefix>/{slug}`, and put `[{ident}]` in the "
        "commit message and the pull request title: that is how its finds the item.",
        f"- If its is installed, end with `its done {ident} --check`. It runs the same gate and commands now (not the "
        "held-out tests), so you can fix what it finds before you push.",
        f"- If you can't finish, say why in the pull request (or run `its block {ident} --reason \"...\"`).",
    ]


def _brief_text(ident: str, item, reqs: list[dict], rules: list[str]) -> str:
    lines = [f"# {ident}: {item.title}", "",
             f"Implement backlog item {ident}, and only it. Everything you need is below; the repository's AGENTS.md "
             "has the project's conventions.", "", "## The item",
             f"- Do: {item.get('Do') or '(see the requirements)'}",
             f"- Done when: {item.done_when or '(the requirements are met and the commands pass)'}",
             f"- Verify: {item.verify or '(the test command)'}",
             f"- Touches: {', '.join(item.touches) or '(not listed)'}"]
    if item.depends_on:
        lines.append(f"- Builds on: {', '.join(item.depends_on)} (done already)")
    lines += ["", "## Requirements it satisfies"]
    for req in reqs:
        lines += [req["text"] or f"{req['id']} (not found in the planning docs)", ""]
    if not reqs:
        lines += ["(none listed)", ""]
    lines += ["## How the branch is checked", "It lands only through its gate, with these rules from the integration "
              "branch (your branch can't change them):"]
    lines += [f"{n}. {rule}" for n, rule in enumerate(rules, 1)]
    return "\n".join([*lines, "", *_finish(ident, item)]) + "\n"


def _retry_text(ident: str, item, again: dict, rules: list[str]) -> str:
    where = f" on {again['branch']}" if again.get("branch") else ""
    lines = [f"# Retry {ident}: {item.title} (attempt {again['attempt']} of {again['max_attempts']})", "",
             f"An earlier attempt at {ident}{where} failed. Fix what is listed here and keep what already works; the "
             f"item hasn't changed (`its brief {ident}` has the whole brief).", "", "## What failed"]
    failed = [f"- check failed: {name}" for name in again["failing_checks"]]
    if again["failing_tests"]:
        failed += ["- failing tests:", *[f"  - {t}" for t in again["failing_tests"]]]
    if again["held_out_failed"]:
        failed.append(f"- {again['held_out_failed']} held-out acceptance test(s) failed. You can't see them: they "
                      "check the item's requirements, so re-read those (`its brief` lists them) rather than guessing.")
    failed += [f"- {m}" for m in again["messages"]]
    lines += failed or [f"- {again['outcome']}"]
    lines += ["", "## The rules, as before"]
    lines += [f"{n}. {rule}" for n, rule in enumerate(rules, 1)]
    return "\n".join([*lines, "", *_finish(ident, item)]) + "\n"


def _github(pkg: Package, ident: str, title: str, text: str, *, retry: bool) -> dict:
    """Create the item's issue, or update it (comment on it, for a retry), with the GitHub CLI."""
    gh = shutil.which("gh")
    if gh is None:
        raise WorkError("github_failed", "--github needs the GitHub CLI, gh, installed and signed in (gh auth login)",
                        details={"id": ident})
    if not cl.in_git(pkg.root):
        raise WorkError("not_git", "--github needs a git repository with a GitHub remote")

    def run(*args: str, body: str | None = None) -> str:
        try:
            proc = subprocess.run([gh, *args], cwd=pkg.root, input=body, capture_output=True, text=True,
                                  encoding="utf-8", timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkError("github_failed", f"gh {args[0]} {args[1]} failed: {exc}", details={"id": ident}) from exc
        if proc.returncode != 0:
            raise WorkError("github_failed", f"gh {args[0]} {args[1]} failed: {(proc.stderr or proc.stdout).strip()}",
                            details={"id": ident})
        return proc.stdout

    heading = f"[{ident}] {title}"
    number = cl.brief_of(pkg.root, ident).get("issue")
    if number is None:
        found = json.loads(run("issue", "list", "--state", "open", "--search", f"{ident} in:title", "--json",
                               "number,title,url", "--limit", "50") or "[]")
        number = next((i["number"] for i in found if str(i.get("title", "")).startswith(f"[{ident}]")), None)
    if number is not None and retry:
        url = run("issue", "comment", str(number), "--body-file", "-", body=text).strip()
        action = "commented"
    elif number is not None:
        run("issue", "edit", str(number), "--title", heading, "--body-file", "-", body=text)
        url, action = None, "updated"
    else:
        url = run("issue", "create", "--title", heading, "--body-file", "-", body=text).strip().splitlines()[-1]
        match = re.search(r"/issues/(\d+)", url)
        if match is None:
            raise WorkError("github_failed", f"gh issue create didn't say which issue it made: {url}",
                            details={"id": ident})
        number, action = int(match.group(1)), "created"
    try:
        cl.record_brief(pkg.root, ident, {"issue": number, "at": _now()})
    except (OSError, cl.GitError):
        pass
    return {"number": number, "url": url, "action": action}
