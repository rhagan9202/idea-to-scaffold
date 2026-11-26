---
id: implement-task
title: Implement a task
description: Take one task (T-n.m) from the project plan to a tested, committed change that traces back to its requirements.
status: approved
version: 0.1
updated: YYYY-MM-DD
depends_on: []
---

# Playbook: implement a task

**Input:** a task ID (for example `T-2.3`). If none is given, use the first unchecked task whose dependencies are done.

1. Open `docs/planning/06-project-plan.md` and read the task, its `Satisfies` list and `Done when`.
2. For each requirement listed, read its acceptance criteria in `03-requirements.md`, and the components and screens that serve it in `04-architecture.md` and `05-design.md`.
3. Check `docs/planning/adr/` for decisions that constrain the work.
4. Write or update tests that express the acceptance criteria. Confirm they fail.
5. Implement the smallest change that makes them pass, following `AGENTS.md` conventions.
6. Run the test and lint commands from `AGENTS.md`. Fix until both pass.
7. Tick the task's `- [ ] Done` box in the plan.
8. Commit: `<type>(<scope>): <summary> [T-n.m]` with `Refs: <requirement IDs>`.

**Done check:** tests and lint pass, every listed requirement has a test, the plan is updated, the commit references the task.

If a requirement is ambiguous or conflicts with the architecture, stop and use `update-plan` instead of guessing.
