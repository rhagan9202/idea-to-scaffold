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

Every command except `new`, `import` and `scaffold` also works inside a scaffolded repo (it finds
`docs/planning/package.yaml`), so you can add `its link --check .` to CI or a pre-commit hook to stop the docs
drifting.

For a small project, `its new DIR --profile lite` starts a lighter plan: idea, vision, requirements (with features
folded in) and backlog. Architecture, design and a project plan are optional, and only the requirements need
approval before scaffolding.

## Working the backlog

Agents (or you) drive the backlog with four commands instead of reading and editing Markdown by hand:

```bash
its next                 # the next ready item: the first todo whose dependencies are done (exit 3 if none)
its start T-2.1          # set it in-progress (exit 4 if a dependency isn't done; --force to override)
its show FR-003          # any ID: its definition, and the docs that mention it
its done T-2.1           # run its Verify command plus test and lint; set it done only if they pass (exit 5)
```

To run several agents at once, give each item its own branch and worktree:

```bash
its next --parallel      # ready items whose Touches don't overlap each other or active claims
its start T-2.1 --worktree   # branch its/T-2.1-<slug> in ../<repo>-T-2.1; prints the claude / codex command to run there
its claims               # who has what; its release T-2.1 [--remote] removes a finished claim
its renumber             # after merging, give proposed T-new-... items permanent IDs
```

The branch is the claim, so a pushed branch is visible to every machine and cloud agent, and `its next` reads
statuses from `main`, so a dependency counts as done only once it is merged. Items run side by side only when
their Touches confirm they change different files: `its start` refuses an item that might overlap one already
running (exit 6), and an item with no Touches never runs alongside another
([spec section 17](spec/PACKAGE_SPEC.md#17-parallel-work)).

Add `--json` to any of them, or to `its backlog`, for a structured answer in a versioned format
([spec section 16](spec/PACKAGE_SPEC.md#16-json-output)). `its backlog --write` writes `backlog.json` next to the
backlog for tools that want a file; it is generated and gitignored, never committed.

## How the docs stay connected

- **One backlog.** `planning/07-backlog.md` is the only list of work items; each has a Milestone, Status,
  what it Satisfies and what it Depends on, plus optional Verify (how to check it is done) and Touches (the files
  it changes, so parallel items stay apart). Agents propose new items with a placeholder ID (`T-new-<name>`), so
  parallel branches never mint the same number. The project plan holds milestones only.
- **Define once, link everywhere.** Each ID (`FR-003`, `T-2.1`, `C-store`, …) is defined in one doc. `its link`
  turns every other mention into a link to that definition, and re-points links when headings change.
- **Nav blocks with backlinks.** Each doc starts with links to the index, its upstream docs, the next doc, and
  every doc that references it, so an agent changing a requirement knows exactly what else to revisit.
- **Generated index.** `planning/README.md` shows the reading order, each doc's status and where every ID is
  defined. It carries no backlog status, so starting or finishing an item never touches it and parallel branches
  don't conflict on it; `its next` and `its backlog` answer that live.

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
├── HANDOFF.md                       how to pick work (`its next`) + kick-off prompt per agent
├── docs/planning/README.md          generated planning index: reading order, doc statuses, ID map
├── docs/planning/07-backlog.md      the only list of work items
├── docs/planning/                   the planning package (source of truth), fully cross-linked
├── docs/playbooks/                  implement-task, update-plan, write-adr, …
└── stack skeleton                   e.g. pyproject.toml, src/, tests/, CI
```

## Commands

| Command | What it does |
| ------- | ------------ |
| `its new DIR [--profile lite]` | Create a blank planning package to fill in by hand |
| `its import EXPORT.md --into DIR [--overwrite]` | Extract `<!-- file: … -->` blocks from a chat export, add default playbooks, cross-link, validate. Refuses to replace files you've edited unless `--overwrite` (originals kept as `.orig`) |
| `its link [DIR] [--check]` | Link every ID reference to its definition, refresh nav blocks (upstream, next, backlinks) and `planning/README.md`. `--check` exits 1 if anything drifted (for CI) |
| `its validate [DIR]` | Check required files, front matter, ID definitions, traceability, backlog rules, broken links and anchors, `AGENTS.md` sections, readiness |
| `its backlog [DIR] [--status S] [--ready] [--json] [--write]` | List backlog items; `--write` writes the gitignored `backlog.json` mirror |
| `its next [DIR] [--all] [--parallel [N]] [--base BRANCH] [--json]` | The next ready, unclaimed item; `--all` for every one, `--parallel` for up to N that don't overlap. Exit 3 if nothing is ready |
| `its show ID [DIR] [--json]` | Where any ID is defined, its text, and the docs that mention it |
| `its start ID [DIR] [--force] [--branch \| --worktree [PATH]] [--agent NAME] [--allow-overlap] [--json]` | Set a ready `todo` item `in-progress`, or with `--branch`/`--worktree` claim it with a branch instead. Exit 4 if a dependency isn't done, 6 if it may overlap running work |
| `its claims [DIR] [--json]` | List claim branches, local and remote |
| `its release ID [DIR] [--remote] [--force] [--json]` | Remove a claim's worktree and local branch; `--remote` also deletes its pushed branches (fetched first, deleted with a lease). Refuses, changing nothing, if work would be lost, unless `--force` |
| `its renumber [ID ...] [--dir DIR] [--dry-run] [--force] [--json]` | Give placeholder items permanent `T-n.m` IDs and update every mention. Runs on the integration branch only, unless `--force` |
| `its done [ID] [DIR] [--no-verify] [--json]` | Run the item's Verify command (only when Verify is a single backticked command) and the test and lint commands (in a repo), then set it `done`. Exit 5 if a check fails. With no ID, the item of this claim branch or worktree, else the only in-progress item |
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
