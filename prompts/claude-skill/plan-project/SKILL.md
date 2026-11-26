---
name: plan-project
description: Turn a rough software idea into a complete planning package (vision, features, requirements, architecture, design, wireframes, project plan, AGENTS.md) that coding agents use as their source of truth. Use when the user wants to plan a new project before building it.
---

# Plan a project

You guide the user from a rough idea to a finished **planning package**, stage by stage. The package format is defined in `references/PACKAGE_SPEC.md`; blank templates are in `references/templates/`.

## Process

Work through the stages in `references/stages/` in order. Read each stage file when you reach it and follow it exactly.

| Stage | File | Produces |
| ----- | ---- | -------- |
| 0 | `00-primer.md` | the conventions and output format for the whole session |
| 1 | `01-ideate.md` | `planning/00-idea.md` |
| 2 | `02-vision-features.md` | `01-vision.md`, `02-features.md` |
| 3 | `03-requirements.md` | `03-requirements.md` |
| 4 | `04-architecture.md` | `04-architecture.md`, ADRs |
| 5 | `05-design.md` | `05-design.md`, wireframes |
| 6 | `06-project-plan.md` | `06-project-plan.md`, `07-backlog.md` |
| 7 | `07-agents.md` | `AGENTS.md`, `package.yaml`, extra playbooks |
| 8 | `08-export.md` | consistency report and full export |

For a small project, the primer offers a **lite** plan (idea, vision, requirements with features folded in, and backlog); its lighter templates are in `references/templates-lite/`. Follow the primer's per-stage notes for it.

Start by reading `00-primer.md` and adopting its rules (treat it as if the user had pasted it). Then ask for the idea and begin Stage 1. Don't advance to the next stage until the user approves the current stage's files. Tell the user which stage you are on and what comes next.

## Writing files

- If you can write files (Claude Code, or a claude.ai session with file creation), write each artifact to `<slug>-plan/<path>` as well as showing it, and copy the default playbooks from `references/templates/playbooks/` and ADR-0001 from `references/templates/planning/adr/` into the package.
- Otherwise, emit every file in the `<!-- file: path -->` … `<!-- end file -->` format from the primer so the user can import the chat export with `its import`.

## Quality bar

- Every ID is defined once and referenced correctly.
- Every Must requirement has testable acceptance criteria and is covered by a task.
- M1 is a walking skeleton, and every work item is in `07-backlog.md` and nowhere else.
- IDs are written bare; `its link` (run by `its import`) adds the cross-links. If you wrote files directly, tell the user to run `its link <slug>-plan/`.
- `AGENTS.md` has the eight required sections and real commands.
