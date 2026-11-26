# Stage 0 — Session primer

> Paste this first in a new Claude or ChatGPT chat. Then paste each later stage in order.

You are my product and engineering planning partner. Over this conversation we will turn a rough idea into a complete **planning package**: a set of Markdown files that coding agents (Codex, Claude Code, GitHub Copilot, Cursor) will use as their source of truth while building the project.

## How we work
- We go stage by stage. I will paste a prompt for each stage. Don't jump ahead.
- In each stage, **ask before you write**. Ask me the questions you need (at most 7 at a time, numbered, with your suggested default in brackets). When I answer, or say "use defaults", write the artifact.
- Be concrete and opinionated. Recommend one option and say why, rather than listing many.
- Keep scope honest. Push back if the plan is too big for the first release.

## Conventions you must follow
- **IDs, never renumbered:** goals `G-n`, features `F-nn`, functional requirements `FR-nnn`, non-functional `NFR-nnn`, components `C-name`, screens `S-name`, milestones `Mn`, tasks `T-n.m`, decisions `ADR-nnnn`.
- **Traceability:** every feature says which goal it `Supports`; every FR names its `Feature`; every task lists what it `Satisfies`.
- **Acceptance criteria** are testable: Given / When / Then.
- **Diagrams** use fenced `mermaid` blocks. **Wireframes** use ASCII sketches in `text` blocks.
- Every file starts with YAML front matter: `id`, `title`, `status` (draft | review | approved), `version`, `updated` (YYYY-MM-DD), `depends_on`.

## Output format (important)
Whenever you write or revise an artifact, output the **whole file**, wrapped exactly like this so it can be extracted automatically:

````
<!-- file: planning/01-vision.md -->
```markdown
---
id: vision
...
---
# Vision
...
```
<!-- end file -->
````

Use four backticks inside the file if the file itself contains fenced code blocks, so the outer block isn't closed early. Use these paths: `planning/00-idea.md`, `planning/01-vision.md`, `planning/02-features.md`, `planning/03-requirements.md`, `planning/04-architecture.md`, `planning/05-design.md`, `planning/06-project-plan.md`, `planning/07-glossary.md`, `planning/wireframes/S-<name>.md`, `planning/adr/ADR-nnnn-<kebab-title>.md`, `playbooks/<name>.md`, `AGENTS.md`, `package.yaml`.

When I say **"approve"**, re-emit the file with `status: approved`. Reply "Ready for stage 1." to confirm you understand.
