---
id: backlog
title: Backlog
status: draft
version: 0.1
updated: YYYY-MM-DD
depends_on: [project-plan, requirements]
---

# Backlog

The backlog is the **only** list of work items for this project. Items are in priority order: the first `todo`
item whose dependencies are `done` is next.

**Rules**
- One `### T-n.m — Title` heading per item. IDs are never reused or renumbered.
- `Status` is one of: `proposed` (suggested, not agreed), `todo`, `in-progress`, `blocked`, `done`, `dropped`.
- `Milestone` is a milestone from the [project plan](06-project-plan.md), or `—` if unscheduled.
- `Satisfies` lists the requirements the item delivers. Agents may add new items with `Status: proposed`
  and may change the `Status` of the item they are working on; anything else needs a human.
- Update the item's status in the same commit as the work.

## Items

### T-1.1 — Project setup verified
- **Milestone:** M1
- **Status:** todo
- **Satisfies:** NFR-001
- **Depends on:** —
- **Do:** Install dependencies; confirm the test and lint commands pass on the fresh scaffold; add CI.
- **Done when:** CI runs on push and passes.

### T-1.2 — <title>
- **Milestone:** M2
- **Status:** todo
- **Satisfies:** FR-001
- **Depends on:** T-1.1
- **Do:** 
- **Done when:** 
