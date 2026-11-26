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
├── docs/planning/                   the planning package (source of truth)
├── docs/playbooks/                  implement-task, update-plan, write-adr, …
└── stack skeleton                   e.g. pyproject.toml, src/, tests/, CI
```

## Commands

| Command | What it does |
| ------- | ------------ |
| `its new DIR` | Create a blank planning package to fill in by hand |
| `its import EXPORT.md --into DIR` | Extract `<!-- file: … -->` blocks from a chat export, add default playbooks, validate |
| `its validate DIR` | Check required files, front matter, ID definitions, traceability, `AGENTS.md` sections, readiness |
| `its scaffold DIR [--out PARENT] [--github [OWNER/NAME]] [--no-git] [--force] [--allow-unready]` | Build the repo |

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
pytest
```

Adding a language: create `src/idea_to_scaffold/templates/stacks/<language>/` (see the README there).
