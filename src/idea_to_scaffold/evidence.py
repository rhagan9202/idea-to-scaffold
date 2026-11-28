"""Runnable evidence for a review: a reviewer's claim that blocks a change has to reproduce.

A reviewer agent that reads the repository finds real problems, and also reports ones that aren't there. So the two
claims that can stop a change, a requirement marked `not_met` and a `blocker` finding, come with a `repro`: a
command, and any files to add first (a test that shows the problem), that fails because of the problem. `its
review --reply` runs each one in a throwaway worktree of exactly the content reviewed, after `commands.setup`, with
the gate's allow-listed environment and time limit. A repro that fails there stands. One that passes, can't run
(exit 126, 127 or 9009: no such command), runs out of time, or is missing, is downgraded: `not_met` to `cant_tell`
and `blocker` to `major`. No language model filters the findings; the command decides.

The commands are the reviewer's, which read the change, so they get no more than the branch's own tests already
get: the worktree is thrown away, the environment is allow-listed, and the process tree is killed at the time limit.
"""
from __future__ import annotations

import contextlib
import posixpath
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from . import claims as cl
from . import gate, gitx
from .checks import Check, run_check
from .package import Package

NOT_RUN = (126, 127, 9009)  # the shell couldn't run the command at all (9009: cmd.exe's "not recognized")
REPRO_SCHEMA = {
    "type": "object", "required": ["command"], "additionalProperties": False,
    "properties": {"command": {"type": "string"},
                   "files": {"type": "object", "additionalProperties": {"type": "string"}}},
}
OUTPUT_KEPT = 2000


def claims_to_check(reply: dict) -> list[tuple[str, dict]]:
    """The claims in a reply that need evidence: not_met requirements and blocker findings."""
    return ([("requirement", r) for r in reply.get("requirements", []) if r.get("status") == "not_met"]
            + [("finding", f) for f in reply.get("findings", []) if f.get("severity") == "blocker"])


def reproduce(pkg: Package, inspection: gate.Inspection, reply: dict, *, run: bool = True) -> list[dict]:
    """Run the repro of each claim that needs one, and downgrade those that don't reproduce, in `reply`. Returns
    what happened to each. Without `run` (or outside git) nothing runs, so every such claim is downgraded."""
    wanted = claims_to_check(reply)
    if not wanted:
        return []
    outcomes: dict[int, dict] = {}
    runnable = [e for _, e in wanted if isinstance(e.get("repro"), dict) and str(e["repro"].get("command", "")).strip()]
    if run and runnable and cl.in_git(pkg.root):
        try:
            with _tree(pkg, inspection) as (tree, here):
                setup = (inspection.policy.commands.get("setup") or "").strip()
                ready = run_check(Check("setup", setup), here, inspection.policy.run) \
                    if setup and setup != "true" else None
                for entry in runnable:
                    if ready is not None and not ready["passed"]:
                        outcomes[id(entry)] = {"reproduced": False, "why": f"setup ({setup}) failed in the worktree"}
                        continue
                    outcomes[id(entry)] = _try(tree, here, entry["repro"], inspection)
        except (gate.GateError, gitx.GitError, OSError) as exc:
            for entry in runnable:
                outcomes.setdefault(id(entry), {"reproduced": False, "why": f"couldn't make a worktree: {exc}"})
    report = []
    for kind, entry in wanted:
        outcome = outcomes.get(id(entry)) or {
            "reproduced": False, "why": "no repro given" if entry not in runnable else "not run"}
        entry["reproduced"] = outcome["reproduced"]
        if not outcome["reproduced"]:
            if kind == "requirement":
                entry.update(status="cant_tell", downgraded_from="not_met")
            else:
                entry.update(severity="major", downgraded_from="blocker")
        report.append({"kind": kind, "of": entry.get("id") or entry.get("file"),
                       "command": (entry.get("repro") or {}).get("command"), **outcome})
    return report


def _try(tree: Path, here: Path, repro: dict, inspection: gate.Inspection) -> dict:
    """Write the repro's files, run its command, and put the worktree back as it was."""
    try:
        for rel, text in (repro.get("files") or {}).items():
            norm = posixpath.normpath(str(rel).replace("\\", "/"))
            if norm.startswith(("../", "/")) or norm == ".." or ":" in norm.split("/", 1)[0]:
                return {"reproduced": False, "why": f"{rel} is outside the repository"}
            target = here / norm
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(text), encoding="utf-8", newline="\n")
        result = run_check(Check("repro", str(repro["command"])), here, inspection.policy.run)
    finally:
        gitx.run(tree, "checkout", "--quiet", "--", ".")
        gitx.run(tree, "clean", "-fdq")  # not -x: what setup installed (ignored files) stays for the next one
    out = {"exit_code": result["exit_code"], "output": (result.get("output") or "")[-OUTPUT_KEPT:]}
    if result.get("timed_out"):
        return {**out, "reproduced": False, "why": "it ran out of time"}
    if result["exit_code"] in NOT_RUN:
        return {**out, "reproduced": False, "why": "the command couldn't run"}
    if result["passed"]:
        return {**out, "reproduced": False, "why": "the command passed on the change"}
    return {**out, "reproduced": True, "why": f"the command failed on the change (exit {result['exit_code']})"}


@contextlib.contextmanager
def _tree(pkg: Package, inspection: gate.Inspection) -> Iterator[tuple[Path, Path]]:
    """A throwaway worktree holding exactly what the review looked at: the reviewed ref, or for a review of a
    checkout, its HEAD plus its uncommitted and untracked changes."""
    root = pkg.root
    top = cl.toplevel(root)
    prefix = gitx.out(root, "rev-parse", "--show-prefix", check=False).strip()
    at = inspection.head
    if at is None:  # the checkout itself: tracked changes as a stash commit, untracked files copied below
        at = gitx.out(top, "stash", "create", check=False).strip() or "HEAD"
    scratch = Path(tempfile.mkdtemp(prefix="its-repro-"))
    tree = scratch / "tree"
    try:
        gate.run_git(top, "worktree", "add", "--detach", "--quiet", str(tree), at)
        if inspection.head is None:
            for path in gitx.out(top, "ls-files", "--others", "--exclude-standard", "-z", check=False).split("\0"):
                if path and (top / path).is_file():
                    (tree / path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(top / path, tree / path)
            gitx.run(tree, "add", "-A")  # so resetting between repros keeps them
            gitx.run(tree, "-c", "user.name=its", "-c", "user.email=its@localhost", "commit", "-qm", "snapshot",
                     "--no-verify")
        yield tree, tree / prefix if prefix else tree
    finally:
        gitx.run(top, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(scratch, ignore_errors=True)
        gitx.run(top, "worktree", "prune")
