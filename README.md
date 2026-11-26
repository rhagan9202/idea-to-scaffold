# idea-to-scaffold

A reproducible workflow for starting agent-assisted software projects:

1. **Ideate.** Flesh out an idea with Claude or ChatGPT.
2. **Plan.** Expand it into features, requirements, architecture, design, wireframes and a project plan.
3. **Package.** Capture everything as a standard [planning package](spec/PACKAGE_SPEC.md) of Markdown files, `AGENTS.md` and playbooks.
4. **Scaffold.** Generate the repo, wire the package in for Codex, Claude Code, Copilot and Cursor, `git init`, make the first commit, and write a `HANDOFF.md` brief.

## Quick start

```bash
pip install -e .            # installs the `its` command (Python 3.10+)

# Steps 1 and 2: plan in a chat
#   Paste prompts/stages/00-primer.md, then 01 … 08, into Claude or ChatGPT.
#   Save the chat (stage 8 output) as my-idea.md.
#   Or use the Claude skill: python scripts/build_skill.py -> dist/plan-project.zip

# Step 3: import the export into a package and check it
its import my-idea.md --into my-idea-plan/
its validate my-idea-plan/

# Step 4: scaffold the repo (git init + first commit included)
its scaffold my-idea-plan/ --out ~/code
its scaffold my-idea-plan/ --out ~/code --github      # also create a private GitHub repo with gh and push
```

Then open `HANDOFF.md` in the new repo and give its kick-off prompt to any agent.

`link`, `validate` and `backlog` also work inside a scaffolded repo (they find `docs/planning/package.yaml`),
so you can add `its link --check .` to CI or a pre-commit hook to stop the docs drifting.

## How the docs stay connected

- **One backlog.** `planning/07-backlog.md` is the only list of work items; each has a Milestone, Status,
  what it Satisfies and what it Depends on. The project plan holds milestones only.
- **Define once, link everywhere.** Each ID (`FR-003`, `T-2.1`, `C-store`, …) is defined in one doc. `its link`
  turns every other mention into a link to that definition, and re-points links when headings change.
- **Nav blocks with backlinks.** Each doc starts with links to the index, its upstream docs, the next doc, and
  every doc that references it, so an agent changing a requirement knows exactly what else to revisit.
- **Generated index.** `planning/README.md` shows reading order with live statuses, a backlog summary and next item,
  and where every ID is defined. A status change without re-linking shows up in `its link --check`.

Try it on the bundled example:

```bash
its import examples/streak/chat-export.md --into /tmp/streak-plan
its scaffold /tmp/streak-plan --out /tmp
```

## What the scaffold contains

```text
<slug>/
├── AGENTS.md                        canonical rules (Codex reads this directly)
├── CLAUDE.md                        @AGENTS.md import
├── .github/copilot-instructions.md  points to AGENTS.md
├── .cursor/rules/00-project.mdc     alwaysApply rule pointing to AGENTS.md
├── .claude/skills/<playbook>/       one Claude skill per playbook
├── HANDOFF.md                       first task + kick-off prompt per agent
├── docs/planning/README.md          generated planning index: live statuses, ID map, next backlog item
├── docs/planning/07-backlog.md      the only list of work items
├── docs/planning/                   the planning package (source of truth), fully cross-linked
├── docs/playbooks/                  implement-task, update-plan, write-adr, …
└── stack skeleton                   e.g. pyproject.toml, src/, tests/, CI
```

## Commands

| Command | What it does |
| ------- | ------------ |
| `its new DIR` | Create a blank planning package to fill in by hand |
| `its import EXPORT.md --into DIR [--overwrite]` | Extract `<!-- file: … -->` blocks from a chat export, add default playbooks, cross-link, validate. Refuses to replace files you've edited unless `--overwrite` (originals kept as `.orig`) |
| `its link [DIR] [--check]` | Link every ID reference to its definition, refresh nav blocks (upstream, next, backlinks) and `planning/README.md`. `--check` exits 1 if anything drifted (for CI) |
| `its validate [DIR]` | Check required files, front matter, ID definitions, traceability, backlog rules, broken links and anchors, `AGENTS.md` sections, readiness |
| `its backlog [DIR] [--status S]` | List backlog items |
| `its scaffold DIR [--out PARENT] [--github [OWNER/NAME]] [--no-git] [--force [--yes]] [--allow-unready]` | Build the repo from a package or an existing scaffolded repo. `--force` asks before deleting the target. Stacks: `python`, plus `generic` (with a warning) for anything else |

## Repository layout

```text
spec/PACKAGE_SPEC.md                    the planning package contract
prompts/stages/                         copy-paste prompts for Claude or ChatGPT
prompts/claude-skill/plan-project/      the same flow as a Claude skill
src/idea_to_scaffold/                   the `its` CLI
src/idea_to_scaffold/templates/package/ blank planning package
src/idea_to_scaffold/templates/stacks/  per-language repo skeletons (python, generic)
examples/streak/                        a sample chat export
tests/                                  end-to-end tests
```

## Development

```bash
pip install -e '.[dev]'
ruff check src tests
pytest
```

CI runs lint and the tests on Python 3.10 to 3.13.

Adding a language: create `src/idea_to_scaffold/templates/stacks/<language>/` (see the README there).
