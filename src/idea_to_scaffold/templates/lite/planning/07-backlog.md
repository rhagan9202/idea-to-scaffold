---
id: backlog
title: Backlog
status: draft
version: 0.1
updated: YYYY-MM-DD
depends_on: [requirements]
---

# Backlog

The backlog is the **only** list of work items for this project. Items are in priority order: the first `todo`
item whose dependencies are `done` is next.

**Rules**
- One `### T-n.m — Title` heading per item. IDs are never reused or renumbered.
- `Status` is one of: `proposed` (suggested, not agreed), `todo`, `in-progress`, `blocked`, `done`, `dropped`.
- `Milestone` is optional in a lite plan. Leave it `—`, or add `06-project-plan.md` to define milestones.
- `Satisfies` lists the requirements the item delivers. Agents may add new items with `Status: proposed`
  and may change the `Status` of the item they are working on; anything else needs a human.
- New items proposed by an agent get a placeholder ID, `T-new-<short-name>` (lowercase letters, digits and hyphens),
  so agents on parallel branches never pick the same number. A human gives it a permanent `T-n.m` ID when it
  is scheduled.
- `Verify` (optional) is how to check the item is done: a command in backticks, a test ID, or `manual: ...`.
- `Touches` (optional) lists the paths or globs the item is expected to change, so items that would edit the
  same files aren't run in parallel.
- Update the item's status in the same commit as the work. On a claim branch (`its/<ID>-...`) the branch
  itself marks the item as taken, so only `done` (or `blocked`) is committed.

## Items

### T-1.1 — Project setup verified
- **Milestone:** —
- **Status:** todo
- **Satisfies:** NFR-001
- **Depends on:** —
- **Do:** Install dependencies; confirm the test and lint commands pass on the fresh scaffold; add CI.
- **Done when:** CI runs on push and passes.
- **Verify:** `<test command>`
- **Touches:** `.github/workflows/`

### T-1.2 — <title>
- **Milestone:** —
- **Status:** todo
- **Satisfies:** FR-001
- **Depends on:** T-1.1
- **Do:** 
- **Done when:** 
- **Verify:** 
- **Touches:** 
