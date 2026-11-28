# idea-to-scaffold

A reproducible workflow for starting agent-assisted software projects:

1. **Ideate.** Flesh out an idea with Claude or ChatGPT.
2. **Plan.** Expand it into features, requirements, architecture, design, wireframes and a project plan.
3. **Package.** Capture everything as a standard [planning package](spec/PACKAGE_SPEC.md) of Markdown files, `AGENTS.md` and playbooks.
4. **Scaffold.** Generate the repo, wire the package in for Codex, Claude Code, Copilot and Cursor, `git init`, make the first commit, and write a `HANDOFF.md` brief.

## Quick start

```bash
pip install -e .            # installs the `its` command (Python 3.10+)

# Steps 1 and 2, ideate and plan: in a chat
#   Paste prompts/stages/00-primer.md, then 01 … 08, into Claude or ChatGPT.
#   Save the chat (stage 8 output) as my-idea.md.
#   Or use the Claude skill: python scripts/build_skill.py -> dist/plan-project.zip

# Step 3, package: import the chat export into a planning package and check it
its import my-idea.md --into my-idea-plan/
its validate my-idea-plan/

# Step 4, scaffold: generate the repo (git init + first commit included)
its scaffold my-idea-plan/ --out ~/code
its scaffold my-idea-plan/ --out ~/code --github      # also create a private GitHub repo with gh and push
```

Then open `HANDOFF.md` in the new repo and give its kick-off prompt to any agent.

Every command except `new`, `import` and `scaffold` also works inside a scaffolded repo (it finds
`docs/planning/package.yaml`), so you can add `its link --check .` to CI or a pre-commit hook to stop the docs
drifting. Like git, `its -C DIR <command>` runs a command as if it were started in `DIR`.

For a small project, `its new DIR --profile lite` starts a lighter plan: idea, vision, requirements (with features
folded in) and backlog. Architecture, design and a project plan are optional, and only the requirements need
approval before scaffolding.

## Working the backlog

Agents (or you) drive the backlog with `its` instead of reading and editing Markdown by hand. One item at a time
needs only four commands:

```bash
its next                 # the next ready item: the first todo whose dependencies are done (exit 3 if none, 9 if all done)
its start T-2.1          # set it in-progress (exit 4 if a dependency isn't done; --force to override)
its show FR-003          # any ID: its definition, and the docs that mention it
its done T-2.1           # run its Verify command plus test and lint; set it done only if they pass (exit 5)
```

To run several agents at once, give each item its own branch and worktree:

```bash
its next --parallel      # ready items whose Touches don't overlap each other or active claims
its start T-2.1 --worktree   # branch its/T-2.1-<slug> in ../<repo>-T-2.1; prints the claude / codex command to run there
its start --next --worktree  # pick and claim the next item in one step; the claim is pushed if there is a remote
its status               # on main: every claim's state (working, blocked and why, ready to land), what's next
its claims               # who has what, and which claim branches would conflict if merged
its land --push --release    # on main: merge finished branches one at a time, testing each merged result
its propose "Export to CSV"  # a new item, in its own file, so parallel branches never conflict
its promote T-new-export-to-csv  # on main: give it a permanent ID, schedule it and commit
```

The branch is the claim, so a pushed branch is visible to every machine and cloud agent, and `its next` reads
statuses from `main`, so a dependency counts as done only once it is merged. Items run side by side only when
their Touches confirm they change different files: `its start` refuses an item that might overlap one already
running (exit 6), and an item with no Touches never runs alongside another
([spec section 17](spec/PACKAGE_SPEC.md#17-parallel-work)).

Add `--json` to any of them, or to `its backlog`, for a structured answer in a versioned format
([spec section 16](spec/PACKAGE_SPEC.md#16-json-output), which also lists the exit codes). A usage error with
`--json` is JSON too. `its backlog --write` writes `backlog.json` next to the
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
| `its link [DIR] [--check] [--json]` | Link every ID reference to its definition, refresh nav blocks (upstream, next, backlinks) and `planning/README.md`, and rewrite the commands blocks from `package.yaml`: the lists between `<!-- its:commands -->` markers in `AGENTS.md` and `HANDOFF.md`, and the steps between `# its:commands` markers in the CI workflow. `package.yaml` is the one place to change a command. An unmarked command list gets the markers when every line in it is a `package.yaml` command. In a claim's checkout the commands blocks are left to the lead. `--check` exits 1 if anything drifted (for CI; `link_outdated` with `--json`) |
| `its validate [DIR] [--trace] [--sizing] [--json]` | Check required files, front matter, ID definitions, traceability, backlog rules, broken links and anchors, `AGENTS.md` sections, readiness. `--trace` also lists Must acceptance criteria that no test names (`test_fr_001_ac2_...`), and exits 1 if there are any. `--sizing` warns about todo and proposed items too big or vague for one agent run (more than 3 Touches, more than 2 requirements or 5 Must criteria, no Verify command, "improve X" with no Done when); it never fails |
| `its backlog [DIR] [--status S] [--ready] [--json] [--write]` | List backlog items, then proposals waiting in `planning/proposals/`; `--write` writes the gitignored `backlog.json` mirror |
| `its next [DIR] [--all] [--parallel [N]] [--base BRANCH] [--fetch] [--json]` | The next ready, unclaimed item; `--all` for every one, `--parallel` for up to N that don't overlap. `--fetch` reads the remote first; without it, a note says when the remote hasn't been fetched for an hour, since other machines' claims may be missing. Lists blocked items. Exit 3 if nothing is ready, 9 if every item is done |
| `its status [DIR] [--base BRANCH] [--fetch] [--json]` | The lead's view from one checkout: item counts, each claim with what its branch tip says (working, blocked and why, ready to land, conflicting, waiting on dependencies, and the review of what would land: approved, changes requested, of an earlier version, or missing when `gate.require_review` is on), what can start next, claims to release, proposals waiting for `renumber`, and drifted items. Changes nothing |
| `its show ID [DIR] [--json]` | Where any ID is defined, its text, and the docs that mention it |
| `its start [ID \| --next] [DIR] [--force] [--branch \| --worktree [PATH]] [--agent NAME] [--allow-overlap] [--no-push] [--no-setup] [--fetch] [--json]` | Set a ready `todo` item `in-progress`, or with `--branch`/`--worktree` claim it with a branch instead. `--next` picks and claims the next ready item in one locked step. A claim is pushed when there's a remote (unless `--no-push`), and the first push wins: losing the race undoes the local claim, and `--next` then takes the next ready item (`lost_to_others`). A new worktree gets the setup command (`commands.setup`) run in it as the lead, before any agent (`--no-setup` skips it); the lockfiles it writes are recorded, so the gate doesn't count them as the agent's change. It prints the commands that start an agent on the item, and for unattended runs a launcher script per agent and shell (`.sh`, `.ps1`, and a `.cmd` that runs the `.ps1`) in the state folder: it unsets push tokens, runs `claude -p` with a settings file `its` keeps outside the worktree (the Stop hook and the sandbox), the result schema and a budget (`launch.budget_usd`, default 5), or `codex exec --sandbox workspace-write --output-schema`, and always ends in `its done ID --result FILE`, even when the agent fails, so every run reaches the ledger. Run it with the printed line (`sh FILE`, or `powershell -File FILE` on Windows). `its` never launches an agent itself. Exit 3 if nothing is ready, 4 if a dependency isn't done, 6 if it may overlap running work, 9 (with `--next`) if every item is done |
| `its claims [DIR] [--fetch] [--json]` | List claim branches, local and remote, with commits ahead and files that would conflict with the integration branch or with each other |
| `its release ID [DIR] [--remote] [--force] [--json]` | Remove a claim's worktree and local branch; `--remote` also deletes its pushed branches (fetched first, deleted with a lease). A pushed branch another machine already deleted is forgotten. Lockfiles `its start`'s setup wrote, unchanged, aren't work. Refuses, changing nothing, if work would be lost, unless `--force` |
| `its renumber [ID ...] [--dir DIR] [--dry-run] [--force] [--no-commit] [--json]` | Give placeholder items permanent `T-n.m` IDs and update every mention. Also folds files in `planning/proposals/` into the backlog. Commits the planning folder, so a worktree started next sees the new IDs, unless `--no-commit`. Runs on the integration branch only, unless `--force` |
| `its done [ID] [DIR] [--no-verify] [--check] [--result FILE] [--no-push] [--json]` | Run the item's Verify command and the test and lint commands (all read from the integration branch; Verify runs only when it is a single backticked command), then set it `done`. First it compares the branch with the integration branch and refuses deleted tests, tests that lost assertions (counted test by test; comments and assertions that can't fail don't count), tests that are gone or renamed out of collection, new skips (including `pytestmark`, an empty parametrize, JS `{skip: ...}` and `.only`), edits to `package.yaml`, agent config, held-out acceptance tests or any other backlog item, and changes outside Touches. When a check runs pytest it also reads pytest's JUnit report, and refuses a run whose report shows failures behind exit 0 or no test that passed. It also finds assertions that can't fail, tests that return at once, changes to which tests run or how their results are reported (a `conftest.py` that deselects or rewrites results, pytest or Jest settings, a `sitecustomize.py` or pytest plugin), a run that stopped before pytest wrote its report (`os._exit(0)` in the code), skipped tests the item should have run, and new tests that pass without the change (red on parent): a project scaffolded now refuses these too; one scaffolded before phase 9 only warns, until `gate: {checks: {<kind>: enforce}}` in `package.yaml` makes them refuse. `Kind: test-only` on an item that only adds tests skips red on parent. Only the lead can waive a refusal, with `its waive` on the integration branch. A branch that adds or changes dependencies or lockfiles is reported too (`dependency_added`). `--no-verify` skips the checks and records that as the item's Verified value. `--check` runs everything but leaves the status alone. Exit 5 if anything fails. With no ID, the item of this claim branch or worktree, else the only in-progress item. `--result FILE` acts on what a headless agent handed back (`{id, status: done \| blocked \| needs_human, summary, reason, ...}`, bare or inside the `claude -p --output-format json` envelope): `done` runs everything above as usual, `blocked` or `needs_human` blocks the item with the agent's reason, and a run that ended without a result changes nothing (`no_result`). In a claim's checkout the outcome is committed (the result's `commit_message`, else its summary, tagged `[ID]`) and the claim branch pushed when it was pushed before (`--no-push` keeps it local), so the lead sees it from main. A result for an item that is already done re-checks it and records the run (`already_done`); a result for another item is refused (`result_mismatch`). The run's cost and turns go to the run ledger, failed runs included |
| `its drift [DIR] [--check] [--json]` | Done items whose Touches changed after they were verified, with the commits and the requirements affected. `--check` exits 8 on drift |
| `its verify [ID \| --drifted \| --all] [DIR] [--commit] [--json]` | Re-run a done item's checks on the current code and refresh its `Verified` mark, which clears the drift once committed (`--commit` does that). `--drifted` re-verifies every drifted item and `--all` every done item; both keep going past a failure and exit 5 if any failed |
| `its stats [DIR] [--json]` | What the run ledger says: how often `done` passed first time, overall and by size (Touches entries) and agent; what the gate refused; how land went; review verdicts; and what agent runs cost (total and p90). `start`, `done`, `block`, `land`, `review --reply` and the Stop hook record to it, in the repository's state folder, never in a commit |
| `its gate-audit [ID] [DIR] [--no-run] [--allow-warn] [--json]` | Measure the done gate: apply known cheats one at a time (delete a test, comment out an assertion, `assert True`, `assert [x]`, a skip, an empty parametrize, a renamed test, a deselecting or result-rewriting `conftest.py`, a weak new test, an edited Verify, `os._exit(0)` in the item's source, and more) to a throwaway checkout of the integration branch and report which the gate refuses, which it only warns about, and which it misses. `os._exit(0)` needs the item's checks to run; `--no-run` leaves it out. Exit 10 unless every cheat is refused (`--allow-warn`: unless every cheat is at least warned about) |
| `its review [ID] [DIR] [--base REF] [--json]` / `its review [ID] --reply FILE [--min-severity S]` / `its review --schema` | Print a prompt for a second agent: the item, the text of the requirements it satisfies, the gate's findings and the diff, without the author's own notes. The reviewer answers in JSON with a severity per finding; `--reply` checks that answer against the item and exits 5 unless it approves with every requirement the item satisfies listed and met (one left out is `missing`) and nothing at or above `--min-severity` (default `major`). The verdict is recorded for exactly the content the prompt showed: `its land` refuses that content after changes requested, and a later change makes the verdict stale. `its review T-2.1 \| claude -p > review.json && its review T-2.1 --reply review.json`. Run from `main`, it reviews the item's claim branch. Refuses (`nothing_to_review`) when the branch changes only the backlog |
| `its hooks [DIR] [--agent claude\|codex] [--json]` | Add an opt-in Stop hook (`.claude/settings.json`, `.codex/hooks.json`) that runs `its hook stop`: when an agent tries to finish, the done gate runs and a failure sends it back to work, once per turn. The hook finds the package from the session's `cwd`, so it works in a worktree. A blocked item lets it stop. The headless launch settings use `its hook stop --headless`, which runs `done --check` and never asks the agent to set the status, since `its done --result` does that |
| `its land [ID ...] [--dir DIR] [--no-verify] [--fetch] [--push] [--release] [--dry-run] [--accept-base] [--require-review] [--json]` | Merge finished claim branches into the integration branch one at a time, in backlog order, running Verify plus test and lint on each merged result in a throwaway worktree. Your checkout only fast-forwards to results that passed, so nothing in it is reset or deleted. A branch that fails the done gate, conflicts or fails a check isn't landed; the rest still land. Conflicts only in generated files (index, `backlog.json`) are regenerated. When `commands.acceptance` is set, it also runs the held-out acceptance tests, which `its start --worktree` leaves out of agents' worktrees. Each item's held-out tests go in `tests/acceptance/<ID>/` (files directly in `tests/acceptance/` hold for every item): a merged result runs those of the items done on it, so two items landing in parallel never fail on each other's tests. A held-out test that fails on the integration branch itself, before the merge, is `base_failing`, and the remaining items aren't tried. After the checks pass, land rewrites the landing item's `Verified` mark from the merged commit, and re-runs the Verify of every other done item whose files the merge changed, refreshing their marks too, so `its drift` stays quiet; a merge that breaks another item's Verify doesn't land. A changes-requested review of what would merge refuses it (`changes_requested`); `--require-review`, or `gate.require_review: true` in the integration branch's `package.yaml`, lands only what has an approving review of exactly that content (`not_reviewed`), and the merge commit records it (`Reviewed:`). `--release` removes each landed claim's worktree and branches, including its pushed branches on the remote (as `its release --remote` does); a worktree on another machine stays until `its release` runs there. It refuses while the integration branch has commits that `its` didn't record and that aren't on the remote, since an agent's worktree shares that branch and it holds the gate's rules; `--accept-base` trusts them once you have looked. Checks run with an allow-listed environment, so tokens in your shell don't reach the code under test, and are killed with everything they started after `gate.timeout` seconds. Exit 3 if nothing is ready, 7 if anything couldn't land |
| `its propose TITLE [--dir DIR] [--name N] [--satisfies IDS] [--depends-on IDS] [--milestone M] [--do ..] [--done-when ..] [--verify ..] [--touches ..] [--json]` | Write a new item to its own file in `planning/proposals/`, so two branches proposing items never conflict. `renumber` folds it in later |
| `its promote ID [DIR] [--force] [--allow-incomplete] [--no-commit] [--json]` | Accept a proposed item: renumber it if it's a placeholder, set it `todo` and commit the planning folder (unless `--no-commit`). Refuses an item with no Done when or Touches, since it can't be checked or run in parallel, unless `--allow-incomplete`. Shows the sizing warnings for the item it schedules |
| `its waive ID [DIR] [--test-changes] [--outside-touches] [--dependencies] --reason TEXT [--json]` | The lead lets one item through part of the gate: run it in the main checkout on the integration branch, where it commits a Waiver field on the item. `its done` and `its land` honour only a waiver there, so an agent's branch can't waive for itself; the waived findings are still reported, and the item's Override field records what was let through |
| `its block ID [DIR] --reason TEXT [--commit] [--json]` / `its unblock ID` | Set an item `blocked` with a reason, or back to `todo`. `--commit` commits the change (on a claim branch, that is how `its status` on `main` sees the block); `drop` and `set-status` take it too |
| `its drop ID [DIR] [--reason TEXT] [--json]` | Set an item `dropped`; a proposal's file is deleted instead |
| `its set-status ID STATUS [DIR] [--reason TEXT] [--json]` | Set any status directly |
| `its scaffold DIR [--out PARENT] [--github [OWNER/NAME]] [--no-git] [--force [--yes]] [--allow-unready] [--hooks]` | Build the repo from a package or an existing scaffolded repo. Its CI also runs `its validate`, `its link --check` and `its drift --check` (with full history), plus `its validate --trace` as a non-blocking step, using `its` pinned to the commit that scaffolded it (`its_source` in `package.yaml` overrides that); `--hooks` adds the Stop hooks. `--force` asks before deleting the target. Stacks: `python`, plus `generic` (with a warning) for anything else |

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

CI runs lint, and the tests on Linux (Python 3.10 to 3.13), macOS (3.12) and Windows (3.10 and 3.12). `its` supports all three.

Error codes and exit codes live in `src/idea_to_scaffold/contract.py`. Add a new code there, then regenerate the
spec's tables with `python -m idea_to_scaffold.contract spec/PACKAGE_SPEC.md`; a test fails until the spec and the
registry match.

Adding a language: create `src/idea_to_scaffold/templates/stacks/<language>/` (see the README there).
