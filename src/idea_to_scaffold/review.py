"""`its review`: an evidence bundle for a second agent to check a change against its item.

An agent reviewing its own work tends to approve it. `its review <ID>` gathers what an independent reviewer needs
(the item, the text of every requirement it satisfies, the gate's findings, and the diff from the integration
branch) and wraps it in a prompt that asks for a verdict. `its` never launches the reviewer; it prints the commands.
"""
from __future__ import annotations

from . import gate
from .backlog import load_backlog
from .package import Package
from .work import SCHEMA, WorkError, _find, base_ref, item_here, show_payload

DIFF_LIMIT = 200_000  # characters of diff to include; the rest is cut and flagged


def _requirement(pkg: Package, ident: str) -> dict:
    try:
        shown = show_payload(pkg, ident)
    except WorkError:
        return {"id": ident, "text": None, "file": None}
    return {"id": ident, "text": shown["text"], "file": shown["file"]}


def _prompt(item: dict, requirements: list[dict], inspection: gate.Inspection, diff: str, truncated: bool) -> str:
    lines = [
        f"You are reviewing a change for backlog item {item['id']}: {item['title']}. You did not write it.",
        "Judge it only on the evidence below, and be specific: cite files and lines.",
        "",
        "Check that:",
        "1. Every requirement listed is met, including each acceptance criterion it states.",
        "2. The item's 'Done when' holds.",
        "3. The tests exercise the behaviour the requirements describe, not just that the code runs.",
        "4. No test was weakened, skipped or deleted to get a pass, and nothing changed outside the item's scope.",
        "",
        "Reply with one line `VERDICT: approve` or `VERDICT: changes-requested`, then one line per requirement",
        "(`FR-001: met | not met | can't tell`, with the evidence), then any other problem you found.",
        "",
        "## Item",
        f"- ID: {item['id']}",
        f"- Satisfies: {', '.join(item['satisfies']) or '(none)'}",
        f"- Do: {item.get('do') or '(not given)'}",
        f"- Done when: {item['done_when'] or '(not given)'}",
        f"- Verify: {item['verify'] or '(none)'}",
        f"- Touches: {', '.join(item['touches']) or '(not given)'}",
        "",
        "## Requirements",
    ]
    for req in requirements:
        lines += [f"### {req['id']}", req["text"] or "(not found in the planning docs)", ""]
    if not requirements:
        lines += ["(the item lists no requirements)", ""]
    lines.append("## Findings from its done's checks")
    lines += [f"- {f['kind']}: {f['message']}" for f in inspection.findings] or ["- none"]
    lines += ["", f"## Changed files (against {inspection.base or 'nothing: not a git repository'})"]
    lines += [f"- {c.status} {c.path}" + (f" (from {c.old_path})" if c.old_path else "") for c in inspection.changes]
    if not inspection.changes:
        lines.append("- none")
    lines += ["", "## Diff", "```diff", diff.rstrip("\n"), "```"]
    if truncated:
        lines.append(f"(the diff was cut at {DIFF_LIMIT} characters; read the remaining files in the repository)")
    return "\n".join(lines) + "\n"


def review(pkg: Package, ident: str | None = None, *, base: str | None = None) -> dict:
    items = load_backlog(pkg)
    ident = ident or item_here(pkg, items)
    if ident is None:
        raise WorkError("which_item", "name the item to review, e.g. `its review T-1.1`")
    item = _find(items, ident)
    ref = base or base_ref(pkg)
    inspection = gate.inspect(pkg, ident, ref)
    diff, truncated = gate.diff_text(pkg, inspection, DIFF_LIMIT)
    item_dict = {**item.to_dict(), "do": item.get("Do")}
    requirements = [_requirement(pkg, r) for r in item.satisfies]
    prompt = _prompt(item_dict, requirements, inspection, diff, truncated)
    return {
        "schema": SCHEMA, "ok": True, "id": ident, "item": item_dict, "requirements": requirements,
        **inspection.to_dict(), "diff": diff, "diff_truncated": truncated, "prompt": prompt,
        "commands": {"claude": f"its review {ident} | claude -p", "codex": f"its review {ident} | codex exec -"},
    }
