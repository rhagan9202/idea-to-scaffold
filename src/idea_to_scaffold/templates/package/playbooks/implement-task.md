---
id: implement-task
title: Implement a task
description: Take one backlog item (T-n.m) to a tested, committed change that traces back to its requirements.
status: approved
version: 0.1
updated: YYYY-MM-DD
depends_on: []
---

# Playbook: implement a task

**Input:** a backlog item ID (for example `T-2.3`). If none is given, run `its next` (or, without `its`, use the first `todo` item in `docs/planning/07-backlog.md` whose dependencies are `done`).

1. Open the item in `docs/planning/07-backlog.md`. Read its `Satisfies`, `Depends on`, `Done when` and `Verify`. Run `its start <ID>` to set **Status** to `in-progress` (it refuses if a dependency isn't done), or set it by hand. If you are on a claim branch (`its/<ID>-...`, made by `its start --worktree`), skip this: the branch is the claim.
2. Follow each requirement link to its acceptance criteria in `03-requirements.md`; from there, follow the nav block's backlinks to the components and screens that serve it in `04-architecture.md` and `05-design.md`.
3. Check `docs/planning/adr/` (listed in `docs/planning/README.md`) for decisions that constrain the work.
4. Write or update tests that express the acceptance criteria, named after the criterion they prove (`test_fr_001_ac2_...` for the second criterion of FR-001). Confirm they fail.
5. Implement the smallest change that makes them pass, following `AGENTS.md` conventions.
6. Run the test and lint commands from `AGENTS.md`. Fix until both pass.
7. Run `its done <ID>`. It runs the item's **Verify** command (read from the integration branch) and the test and lint commands, and sets **Status** to `done` only if they all pass and the change keeps every test, adds no skips, leaves the item's Verify alone and stays inside its **Touches**. Without `its`, run them yourself and set the status by hand. If you discovered follow-up work, run `its propose "Title"` for each item (without `its`, add a backlog item with a placeholder ID, `T-new-<short-name>`, and `Status: proposed`). If you can't finish, run `its block <ID> --reason "..."` and stop; never weaken a test to pass. Run `its link .` if available.
8. Commit: `<type>(<scope>): <summary> [T-n.m]` with `Refs: <requirement IDs>`. If you clarified a planning doc on the way (see `update-plan`), add a `Plan: <doc> — <what changed>` line.

**Done check:** tests and lint pass, every listed requirement has a test, the backlog status is `done`, the commit references the item.

If a requirement is ambiguous or conflicts with the architecture, use `update-plan` instead of guessing. A clarification that changes no ID and no scope can go in with this change; anything bigger waits for review.
