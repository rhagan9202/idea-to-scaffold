# AGENTS.md — <Project Name>

> Canonical instructions for every coding agent (Codex, Claude Code, Copilot, Cursor).
> Tool-specific files point here. Edit this file, not them.

## Project
<!-- One paragraph: what it is, who it's for, the core value. -->

## Source of truth
Planning docs live in `docs/planning/`. Read them in this order before starting work:

1. `01-vision.md` — goals (G-n) and non-goals
2. `02-features.md` — features (F-nn)
3. `03-requirements.md` — requirements (FR-nnn, NFR-nnn) and acceptance criteria
4. `04-architecture.md` — components (C-name), data model, flows
5. `05-design.md` and `wireframes/` — screens (S-name), API contracts
6. `06-project-plan.md` — milestones (Mn) and tasks (T-n.m)
7. `adr/` — decisions already made

Docs with `status: approved` are binding. If the code needs to diverge from an
approved doc, stop and propose the change (see `docs/playbooks/update-plan.md`)
instead of diverging silently. `draft` docs are guidance.

## Stack & commands
- **Language / runtime:** <language> <runtime>
- **Framework:** <framework>
- **Setup:** `<setup>`
- **Test:** `<test>`
- **Lint:** `<lint>`
- **Run:** `<run>`

Run test and lint before declaring any task done.

## Repository layout
```text
src/            application code
tests/          automated tests, mirroring src/
docs/planning/  planning package (source of truth)
docs/playbooks/ repeatable procedures for agents
```

## How to work
1. Pick the next unblocked task from `06-project-plan.md` (start at the task named in `HANDOFF.md`).
2. Read the requirements it satisfies and their acceptance criteria.
3. Implement in small steps with tests that prove each acceptance criterion.
4. Commit using `<type>(<scope>): <summary> [T-n.m]` with `Refs: FR-xxx` in the body.
5. Tick the task's `- [ ] Done` box in `06-project-plan.md` in the same change.

## Conventions
<!-- Style guide, naming, error handling, logging, test style. Keep it to bullets. -->
- 

## Boundaries
Ask a human before you:
- add a new runtime dependency or service
- change the data schema or a public API contract
- edit any `approved` planning doc
- touch secrets, credentials, CI deploy steps or licensing

Never commit secrets. Never disable or skip tests to make a build pass.

## Playbooks
- `docs/playbooks/implement-task.md` — take one task from the plan to a reviewed commit
- `docs/playbooks/update-plan.md` — propose a change to the planning docs
- `docs/playbooks/write-adr.md` — record an architecture decision
