# Planning Package Specification

**Spec version:** 0.1 (draft)

A *planning package* is a folder of Markdown files, plus one YAML manifest, that
captures everything decided about a project before code is written. It is the
canonical source of truth that coding agents (Codex, Claude Code, GitHub Copilot,
Cursor) read while they build the project.

The package is produced in Steps 1 and 2 of the workflow (ideation in Claude or
ChatGPT, exported as Markdown) and consumed by the scaffolder in Step 3, which
copies it into a new repository and generates the per-agent instruction files.

---

## 1. Design principles

1. **One source of truth, thin adapters.** Rules for agents live once, in
   `AGENTS.md`. Tool-specific files (`CLAUDE.md`, Copilot and Cursor rules,
   Claude skills) are generated stubs that point back to it. Nothing is
   written twice by hand.
2. **Plain Markdown, chat-friendly.** Every artifact can be pasted into or
   exported from a chat window without tooling. Diagrams use Mermaid in fenced
   code blocks so they render on GitHub and stay diffable.
3. **Stable IDs and traceability.** Features, requirements, milestones, tasks
   and decisions carry IDs so agents and humans can reference them precisely
   (`F-03`, `FR-012`, `M2`, `T-2.4`, `ADR-0003`).
4. **Status is explicit.** Each artifact declares whether it is `draft`,
   `review` or `approved`. Agents treat `approved` content as binding and
   `draft` content as guidance.
5. **Small, ordered files.** Numbered filenames give a reading order and keep
   each file focused enough to fit comfortably in an agent's context.

---

## 2. Package layout

```text
<slug>-plan/
├── package.yaml                 # manifest (required)
├── AGENTS.md                    # canonical agent instructions (required)
├── planning/
│   ├── 00-idea.md               # raw idea + ideation notes (required)
│   ├── 01-vision.md             # problem, users, goals, non-goals (required)
│   ├── 02-features.md           # feature catalog F-xx (required)
│   ├── 03-requirements.md       # FR-xxx / NFR-xxx with acceptance criteria (required)
│   ├── 04-architecture.md       # system context, components, data, Mermaid (required)
│   ├── 05-design.md             # UX, visual design, API & data contracts (required)
│   ├── 06-project-plan.md       # milestones M#, tasks T-#.#, sequencing (required)
│   ├── 07-glossary.md           # domain terms (optional)
│   ├── wireframes/              # one .md per screen, ASCII or Mermaid; images allowed
│   │   └── README.md
│   └── adr/                     # architecture decision records
│       └── ADR-0001-<title>.md
└── playbooks/                   # repeatable procedures for agents (optional)
    └── <name>.md                # becomes a Claude skill + is linked from AGENTS.md
```

When scaffolded, the package maps into the repository like this:

| Package path         | Repository path                      | Notes                              |
| -------------------- | ------------------------------------ | ---------------------------------- |
| `AGENTS.md`          | `AGENTS.md`                          | copied, project facts filled in    |
| `planning/**`        | `docs/planning/**`                   | copied verbatim                    |
| `playbooks/*.md`     | `docs/playbooks/*.md`                | copied verbatim                    |
| `playbooks/*.md`     | `.claude/skills/<name>/SKILL.md`     | generated wrapper                  |
| *(generated)*        | `CLAUDE.md`                          | imports `AGENTS.md`                |
| *(generated)*        | `.github/copilot-instructions.md`    | points to `AGENTS.md`              |
| *(generated)*        | `.cursor/rules/00-project.mdc`       | `alwaysApply: true`, points to it  |
| *(generated)*        | `HANDOFF.md`                         | first-session brief for any agent  |
| `package.yaml`       | `docs/planning/package.yaml`         | kept for re-generation             |

Codex reads `AGENTS.md` directly, so it needs no adapter.

---

## 3. The manifest: `package.yaml`

```yaml
spec_version: "0.1"

project:
  name: "Recipe Box"               # human name
  slug: "recipe-box"               # repo / folder name: lowercase, digits, hyphens
  summary: "Save, tag and search family recipes."
  license: "MIT"                   # optional

stack:                             # what the scaffolder sets up
  language: "python"               # python | typescript | ...
  runtime: "3.12"
  framework: "fastapi"             # optional
  package_manager: "uv"            # optional
  test: "pytest"                   # optional
  lint: "ruff"                     # optional

agents:                            # which adapter files to generate
  - codex
  - claude
  - copilot
  - cursor

commands:                          # copied into AGENTS.md so agents can verify work
  setup: "uv sync"
  test: "uv run pytest"
  lint: "uv run ruff check ."
  run: "uv run fastapi dev app/main.py"

artifacts:                         # status of each planning file
  planning/01-vision.md: approved
  planning/02-features.md: approved
  planning/03-requirements.md: review
  planning/04-architecture.md: draft
  planning/05-design.md: draft
  planning/06-project-plan.md: draft

first_task: "T-1.1"                # where the handoff tells agents to start
```

Only `spec_version`, `project.name`, `project.slug`, `project.summary` and
`stack.language` are required. Unknown keys are preserved and ignored.

---

## 4. Artifact conventions

### 4.1 Front matter

Every file under `planning/` and `playbooks/` begins with YAML front matter:

```yaml
---
id: requirements          # stable, matches filename stem without number
title: Requirements
status: draft             # draft | review | approved
version: 0.1
updated: 2026-11-26
depends_on: [vision, features]
---
```

`status` in front matter wins over `package.yaml` if they disagree; the
validator reports the mismatch.

### 4.2 ID formats

| Thing            | Format      | Defined in             | Example   |
| ---------------- | ----------- | ---------------------- | --------- |
| Goal             | `G-n`       | `01-vision.md`         | `G-2`     |
| Feature          | `F-nn`      | `02-features.md`       | `F-04`    |
| Functional req.  | `FR-nnn`    | `03-requirements.md`   | `FR-017`  |
| Non-functional   | `NFR-nnn`   | `03-requirements.md`   | `NFR-003` |
| Component        | `C-name`    | `04-architecture.md`   | `C-api`   |
| Screen           | `S-name`    | `wireframes/*.md`      | `S-login` |
| Milestone        | `Mn`        | `06-project-plan.md`   | `M2`      |
| Task             | `T-n.m`     | `06-project-plan.md`   | `T-2.4`   |
| Decision         | `ADR-nnnn`  | `adr/`                 | `ADR-0003`|

IDs are never reused or renumbered. Retired items are marked
`~~F-07 Bulk import~~ (dropped, see ADR-0004)` rather than deleted.

### 4.3 Traceability rules

- Every feature links to at least one goal (`Supports: G-1`).
- Every `FR` names the feature it belongs to (`Feature: F-02`) and has
  testable acceptance criteria written as Given/When/Then or a checklist.
- Every task in the project plan names the requirements it satisfies
  (`Satisfies: FR-004, FR-005`) and a definition of done.
- Architecture components list the requirements they serve.
- Any choice that would be expensive to reverse gets an ADR.

### 4.4 Diagrams and wireframes

- Use fenced ` ```mermaid ` blocks for architecture, sequence, ER and state
  diagrams.
- Wireframes are one file per screen in `planning/wireframes/`, using an ASCII
  box sketch or a Mermaid block, followed by a list of elements, states
  (empty, loading, error) and the requirements the screen covers. Image files
  (`.png`, `.svg`) may sit alongside and be linked.

---

## 5. `AGENTS.md` contract

`AGENTS.md` is the only file every agent is guaranteed to read. It must stay
short (aim for under ~200 lines) and point to the planning docs rather than
repeat them. Required sections, in order:

1. **Project** — one paragraph: what it is, who it is for.
2. **Source of truth** — the planning docs, their reading order, and the rule
   that `approved` docs are binding; agents propose changes to them rather than
   silently diverging.
3. **Stack & commands** — setup, test, lint, run (from `package.yaml`).
4. **Repository layout** — where code, tests and docs live.
5. **How to work** — pick the next task from `06-project-plan.md`, reference IDs
   in commits and PRs, keep tasks small, run tests before finishing.
6. **Conventions** — code style, naming, error handling, testing expectations.
7. **Boundaries** — what agents must not do without asking (new dependencies,
   schema changes, changing approved docs, touching secrets).
8. **Playbooks** — list of `docs/playbooks/*.md` with one line each.

Commit convention used across agents:

```text
<type>(<scope>): <summary> [T-2.4]

Refs: FR-012, FR-013
```

---

## 6. Generated adapter files

These are generated by the scaffolder and should not be edited by hand. Each
begins with a marker comment so regeneration can detect manual edits.

**`CLAUDE.md`**

```markdown
<!-- generated by idea-to-scaffold; edit AGENTS.md instead -->
@AGENTS.md
```

**`.github/copilot-instructions.md`**

```markdown
<!-- generated by idea-to-scaffold; edit AGENTS.md instead -->
Follow the instructions in [AGENTS.md](../AGENTS.md) at the repository root.
Planning docs in docs/planning/ are the source of truth.
```

**`.cursor/rules/00-project.mdc`**

```markdown
---
description: Project-wide rules
alwaysApply: true
---
<!-- generated by idea-to-scaffold; edit AGENTS.md instead -->
Follow @AGENTS.md. Planning docs in docs/planning/ are the source of truth.
```

**`.claude/skills/<name>/SKILL.md`** (one per playbook)

```markdown
---
name: <name>
description: <playbook front matter `description`>
---
<!-- generated by idea-to-scaffold; edit docs/playbooks/<name>.md instead -->
Follow the procedure in docs/playbooks/<name>.md.
```

---

## 7. Playbooks

A playbook is a repeatable procedure an agent can run, for example
`implement-task`, `update-plan`, `write-adr`. Front matter adds a
`description` field (one sentence, used as the Claude skill description). The
body is a numbered procedure with inputs, steps and a done-check. Playbooks are
plain Markdown so Codex, Copilot and Cursor can follow them via the link in
`AGENTS.md`, and Claude Code gets them as skills.

The template ships three default playbooks: `implement-task`, `update-plan`
and `write-adr`.

---

## 8. Validation

A package is **valid** when:

- `package.yaml` parses and has the required keys; `project.slug` matches
  `^[a-z0-9]+(-[a-z0-9]+)*$`.
- All required files exist and have front matter with `id`, `title`, `status`.
- Every ID referenced anywhere is defined exactly once.
- Every `F-` links to a `G-`; every `FR-` names an `F-`; every `T-` names at
  least one `FR-`/`NFR-`.
- `AGENTS.md` contains the eight required section headings.

A package is **ready to scaffold** when it is valid and `01-vision`,
`02-features` and `03-requirements` are `approved`. The scaffolder warns, but
does not block, if other artifacts are still `draft`.

---

## 9. Versioning

The spec uses `MAJOR.MINOR`. Minor versions only add optional fields or files.
The scaffolder refuses packages with a newer major version than it supports.

---

## 10. Chat export format

So a package can travel through a chat window, files are emitted inside marker
comments. Anything outside markers (conversation, questions) is ignored on import.

````text
<!-- file: planning/01-vision.md -->
```markdown
---
id: vision
...
```
<!-- end file -->
````

Rules for the importer:

- A block runs from `<!-- file: <path> -->` to the next `<!-- end file -->`.
- If the block's content is wrapped in a single outer code fence (three or more
  backticks, optional `markdown`/`md`/`yaml` tag), that fence is removed.
- If the same path appears more than once, the **last** occurrence wins, so a
  revised file later in the chat replaces the earlier draft.
- Paths must be relative, use `/`, and stay inside the package (no `..`).
