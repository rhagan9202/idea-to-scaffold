# AGENTS.md — <Project Name>

> Canonical instructions for every coding agent (Codex, Claude Code, Copilot, Cursor).
> Tool-specific files point here. Edit this file, not them.

## Project
<!-- One paragraph: what it is, who it's for, the core value. -->

## Source of truth
Planning docs live in `docs/planning/`. **Start at `docs/planning/README.md`**, the generated index: it lists
every doc with its status and where every ID is defined. Every doc has a nav block (upstream docs, next doc,
backlinks) and every ID reference is a link to its definition, so follow links rather than searching.

1. `01-vision.md` — goals (G-n) and non-goals
2. `02-features.md` — features (F-nn)
3. `03-requirements.md` — requirements (FR-nnn, NFR-nnn) and acceptance criteria
4. `04-architecture.md` — components (C-name), data model, flows
5. `05-design.md` and `wireframes/` — screens (S-name), API contracts
6. `06-project-plan.md` — milestones (Mn) and sequencing
7. `07-backlog.md` — **the only list of work items** (T-n.m), with status
8. `adr/` — decisions already made

Docs with `status: approved` are binding. If the code needs to diverge from an
approved doc, stop and propose the change (see `docs/playbooks/update-plan.md`)
instead of diverging silently. A clarification that changes no ID and no scope
can go in with your work; the playbook says how. `draft` docs are guidance.

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
1. Pick the item: `its next` prints the next ready item (`--json` for a structured answer). Then `its start <ID>` sets it `in-progress`. Without `its`, take the first `todo` item in `07-backlog.md` whose dependencies are `done` and set its **Status** to `in-progress` by hand.
2. Follow its links to the requirements it satisfies and their acceptance criteria, and on to the components and screens that serve them. `its show <ID>` prints any ID's definition and the docs that mention it.
3. Implement in small steps with tests that prove each acceptance criterion.
4. Run `its done <ID>`: it runs the item's **Verify** command and the test and lint commands, and sets the item `done` only if they pass. Commit in the same change, using `<type>(<scope>): <summary> [T-n.m]` with `Refs: FR-xxx` in the body.
5. Found more work? Add it to the end of the backlog as a new item with a placeholder ID (`### T-new-<short-name> — Title`) and `Status: proposed`. Don't pick the next number yourself, and don't keep TODO lists anywhere else.
6. If you edited anything in `docs/planning/`, refresh the links (see *Keeping docs in sync*).

### Keeping docs in sync
- Each ID is defined in one place; elsewhere refer to it by ID only. Don't copy requirement text into code comments or other docs: link or cite the ID.
- If `its` (idea-to-scaffold) is installed, run `its link .` after editing planning docs and `its validate .` before committing. CI may run `its link --check .`.
- Without `its`, keep links relative; the index has no live status to update.

## Conventions
<!-- Style guide, naming, error handling, logging, test style. Keep it to bullets. -->
- 

## Boundaries
Ask a human before you:
- add a new runtime dependency or service
- change the data schema or a public API contract
- add, remove or change the meaning of an ID, or change scope, in an `approved` planning doc (clarifications follow the light path in `update-plan.md`)
- touch secrets, credentials, CI deploy steps or licensing

Never commit secrets. Never disable or skip tests to make a build pass.

## Playbooks
- `docs/playbooks/implement-task.md` — take one task from the plan to a reviewed commit
- `docs/playbooks/update-plan.md` — propose a change to the planning docs
- `docs/playbooks/write-adr.md` — record an architecture decision
