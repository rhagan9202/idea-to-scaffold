# Workflow Specification

**Spec version:** 0.1 (draft)

What `its` does with a planning package once it is in a repository: the JSON every
command prints and the exit codes it ends with, how parallel work is claimed, gated,
reviewed and landed, and how finished work is kept honest. The package itself (its
files, the manifest, the backlog, IDs and profiles) is the
[package spec](PACKAGE_SPEC.md). This spec continues that one's numbering, so its
sections are 16 to 18, and "section 17" means the same section wherever it is cited.

---

## 16. JSON output

`its backlog`, `next`, `show`, `start`, `done`, `claims`, `release`,
`renumber`, `land`, `propose`, `promote`, `block`, `unblock`, `drop`,
`set-status`, `waive`, `review`, `hooks`, `drift`, `verify`, `status`, `stats`, `gate-audit`, `adopt`, `brief`,
`gate`, `link` and `validate` take `--json`. Output is one
JSON object on stdout, so agents never parse text or stderr, even when `its` hits a bug (`internal_error`) or the
command line is wrong (`usage`, exit 2). The global `-C DIR` runs any command as if from `DIR`: it is put in front
of the command's relative folder arguments.

Output on a pipe or a file is UTF-8 whatever the locale (cp1252 on most Windows machines). Files `its` writes are
UTF-8 with LF line endings on every platform. A backlog field edit, such as a status change, and `its link` keep
the file's own line endings.

- Every object has `"schema": 1` and `"ok"`. A new field is not a schema
  change; removing or renaming one is.
- A backlog item is `{id, title, status, milestone, satisfies, depends_on,
  done_when, verify, touches, placeholder, ready, blocked_by, file, line}`.
- `next`: `{source, item, ready (with --all), in_progress, claimed, waiting:
  [{id, title, blocked_by}], cycles}`, plus `parallel` and `held_back: [{id,
  overlaps, touches_listed}]` with `--parallel`. `item` is the first ready
  item that can start now, so `start` never refuses it for overlap; it is
  `null` when none can, and then `reason` says why: `overlap` (items are ready
  but may touch running work's files), `waiting` (on dependencies),
  `in_progress` (everything left is running, claimed or blocked) or `complete`.
  `source` is where statuses were read: `working tree`, or the integration
  branch's ref (section 17). `blocked: [{id, title, reason}]` lists `blocked`
  items. `stale_remote` is `{remote, fetched_at}` when there is a remote that
  wasn't fetched in the last hour (`fetched_at` is `null` if never), since
  claims made on other machines may then be missing; `null` otherwise, and
  always with `--fetch`. `status` has it too. `warnings` holds `wip_limit`
  (`{kind, limit, items, message}`) once the finished items waiting for the lead
  (done on their claim branch, or an adopted claim with a candidate) reach
  `gate.max_unreviewed`; `start` returns the same `warnings`. Neither refuses.
- `backlog` adds `proposals`: the items in `planning/proposals/`.
- `show`: `{id, kind, file, anchor, line, text, referenced_by}`, plus `item`
  for a backlog ID. `referenced_by` lists the docs that mention the ID: the
  ones to revisit when it changes.
- `start`: `{id, previous_status, status, forced, claim, item}`, plus
  `commands: {claude, codex, cwd, argv}` for a claim (`claude` and `codex` are
  shell lines quoted for the platform, cmd.exe on Windows, with steps joined by
  `&&`; `argv` runs without a shell, and
  `headless: {claude, codex, scripts: {claude, codex: {sh, ps1, cmd}}, argv,
  cwd, result_file, settings, schema, budget_usd, env_unset, then, then_argv}`
  gives the unattended commands of section 17: `claude` and `codex` are the
  line that runs the agent's launcher script), and `claim_held_here: true` when
  the claim is already this checkout's, and `run`, the ID of the run the start
  began (section 17). A new worktree adds `setup`: the setup
  command's check result plus `lockfiles` (what it wrote that the gate lets
  through), or `null` with `--no-setup`.
  `pushed` and `remote` say whether the claim branch was pushed, and
  `push_error` why a push failed for a reason other than losing the race.
  `lost_to_others` lists the items `start --next` passed over because another
  machine claimed them first; a lost race for a named item is the `claimed`
  error, with the winner's `claim: {id, branch, remote}` when it is known. A claim is
  `{id, branch, remote_only, worktree, agent, started, finished, orphaned, candidates}`
  (`candidates` is the adopted branches, below);
  `remote_only` means the branch exists only as a remote-tracking branch (another machine's claim), and
  `orphaned` means its ID isn't in the integration branch's backlog.
- `claims`: `{base, claims}`, where each claim adds `ahead` (commits not on
  the integration branch), `conflicts_with_base` (files a merge would
  conflict in, or `null` if this git can't tell) and `conflicts_with: [{id,
  files}]` for other claims. Each also says what its branch tip says, read from
  the backlog at the tip: `tip_status`, `blocked_reason`, `last_commit: {commit,
  date, subject}` (or `null`), `waiting_on` (dependencies not done on the
  integration branch) and `state`: `working`, `blocked`, `ready_to_land`,
  `conflicts`, `waiting`, `no_commits` (marked done with nothing to land),
  `finished` or `orphaned`. In `status`, a done claim also has `review`
  (`null`, or `{label, required, current, passed, outcome, verdict, at, unmet,
  blocking}`), and `state` is `changes_requested` or `not_reviewed` when the
  review would keep `its land` from taking it. An adopted claim's
  `candidates` are `[{name, ref, remote, fetch, pr?, tip, agent, adopted,
  ahead, exists, last_commit, conflicts_with_base, review}]`; its `state`
  comes from them (`no_commits` until one has commits, `conflicts` when every
  one with commits conflicts), and its `review` is that of the first
  candidate its review doesn't hold back.
- `status`: `{project, source, branch, base, counts, next, next_reason, ready,
  blocked, in_progress, proposals: [{id, title, file}], claims,
  ready_to_land, to_release, drift: {drifted, unknown}, warnings, attention}`. `claims` are the
  active ones, with the fields above plus `attention` (their inbox group) and,
  for a stale one, `idle_days`, sorted most urgent first; `attention` is `{needs_you,
  ready_to_land, running, stale, done}`, each a list of IDs: `needs_you` is a
  claim that is blocked, whose review requested changes or is required, or that
  conflicts, plus items blocked without a claim; `stale` is a working claim with
  no commit for `gate.stale_days`; `blocked` adds a `branch` for an item
  blocked on its claim branch; `in_progress` lists items in progress without
  a claim; `drift` is `null` outside git or when it can't be computed.
  `warnings` is `[{kind, message, ...}]`: `main_moved` (with `commits`) when
  the integration branch has commits nothing vouches for, which `its land`
  would refuse (section 17), and `wip_limit`, as in `next`. `release`: `{id, released, deleted_branches,
  removed_worktree, deleted_remote_branches, remote_branches}`. `link`: `{check,
  changed}`; `link --check` with changes is the `link_outdated` error. `renumber`: `{dry_run, renumbered:
  [{from, to}], files, proposals_moved}`.
- `land`: `{base, dry_run, landed, skipped, released, pushed, proposals,
  base_trust}`. `base_trust` says why the integration branch was trusted
  (section 17): `trusted`, `first_use` or `accepted` (`--accept-base`).
  Each entry is `{id, branch, landed, ...}`: a landed one has `commit`,
  `checks`, `held_out` (`{folder, included, left_out, runs}`: whose held-out
  tests ran), `reverified` (other done items whose Verify ran again and whose
  marks were refreshed) and `review` (the approving verdict, or `null`); a
  skipped one has `reason` (`conflict` with `files`, `check_failed` with
  `checks` and, when held-out tests failed, `held_out_failed`, `base_failing`
  with `base_check` (the check fails on the integration branch itself; the
  items after it aren't tried), `test_changes`, `dependency_changes` or
  `outside_touches` with `findings`, `changes_requested` or `not_reviewed`
  with `review`, `gate_error`, `base_moved`, `base_untrusted` (the integration
  branch moved during the land), `checkout_blocked`, `git_error`, `waiting`
  with `blocked_by`, `no_claim`, `no_commits`, `not_done`, `not_chosen` (an
  adopted claim's other candidate, after one landed) or `dry_run`) and
  `message`. An adopted candidate's entry has `branch` (the candidate's
  name), `adopted: true`, `claim_branch` and `red_on_parent`.
- `adopt`: `{id, title, claim, created, candidate, added, candidates, pushed,
  push_error, remote, overlaps, run, commands: {land, review, release}}`;
  `created` says whether this adoption made the claim, `added` whether the
  candidate is new (adopting the same ref again changes nothing).
- `brief`: `{id, title, source, brief, retry, checks: [{name, command}],
  held_out, review_required, issue, commands}`. `brief` is the text; `retry` is
  `null`, or with `--retry` `{attempt, max_attempts, after, outcome, branch,
  head, failing_checks, failing_tests, held_out_failed, findings, messages}`
  (`failing_tests` leaves out held-out tests, which `held_out_failed` counts);
  `issue` is `null`, or with `--github` `{number, url, action: created | updated
  | commented}`.
- `gate`: `{ref, head, base, branch, id, found_by, passed, reason, message,
  findings, warnings, checks, held_out, red_on_parent, policy_source}`, plus
  `held_out_failed` when held-out tests failed. `found_by` is how the item was
  found: `--id`, `claim_branch`, `adopted`, `branch_name`, `backlog` or
  `commit_tags`; with none, `id` is `null` and `reason` is `no_item`. `reason`
  is otherwise `null` (passed), `check_failed`, or a gate refusal's code.
- `validate`: `{valid, ready, errors, warnings}`, plus `trace` with
  `--trace`: `{checked, criteria, requirements: [{id, title, file, criteria:
  [{ac, text, tests: [{file, line}], by_requirement}]}], uncovered: [{id, ac,
  text}], by_requirement}`.
- `drift`: `{drifted: [{id, title, satisfies, verified, verified_commit, files,
  commits, uncommitted}], requirements: [{id, items, files}], clean, unknown:
  [{id, reason, message}], no_touches, shallow}`. `verify`: the `done --check` object
  plus `verified` and `previous_verified`. `verify --drifted` and `verify --all`
  (every done item) return `{verified, failed}`: `verified` lists the objects for items that
  passed, `failed` is `[{id, code, message, ...}]`, and every item is tried.
- `review`: `{id, item, requirements: [{id, text, file}], base, merge_base,
  verify_source, touches_checked, findings, changed_files: [{status, path,
  old_path}], diff, diff_truncated, omitted, prompt, reply_schema, digest,
  reviewed_branch, commands}`; `digest` identifies the content the prompt
  shows, and `reviewed_branch` names the candidate reviewed (`null` for the
  claim's own branch or this checkout). `review ID --reply`: `{id, passed,
  outcome, verdict, min_severity, blocking, unmet, missing, findings,
  requirements, evidence, digest, current, recorded, reviewed_branch}`:
  `outcome` is `approved`, `changes_requested` or `inconclusive`; `missing`
  lists the item's requirements the reply left out (they count as unmet);
  `evidence` is `[{kind, of, command, reproduced, why, exit_code?, output?}]`,
  one per `not_met` requirement and `blocker` finding, and each of those has
  `reproduced` and, when it didn't, `downgraded_from`; `current` says whether
  the content is still what the prompt showed, and `recorded` whether the
  verdict was saved for `its land`. `stats` adds `reviews` (verdicts by
  outcome). `hooks`: `{command,
  written, unchanged}`.
- A gate finding is `{kind, message, ...}`, where `kind` is `verify_changed`,
  `test_deleted`, `assertions_removed` or `skip_added` (test changes), or
  `touches_changed` or `outside_touches` (scope changes, with `files`),
  `policy_changed`, `item_changed`, `other_item_changed` or
  `agent_config_changed` (changes to what the gate reads),
  `assertions_weakened`, `test_disabled`, `test_config_changed`,
  `acceptance_changed` or `tests_pass_on_parent` (phase 7), `test_removed`
  (phase 9, with `tests`), `no_test_report`, `failures_hidden`,
  `no_tests_ran` or `tests_skipped` (phase 9, from the test report, with
  `check`), `proposal_invalid` (with `file`: a proposal the branch changed isn't
  well formed, one finding per problem; it counts as a test change), `dependency_added` (with `file` and `added`: the
  entries the branch adds or changes in a manifest, empty for a lockfile or
  other whole file), or `no_changes` (the branch changes nothing outside the planning
  folder, so nothing shows the item was implemented; it is dropped when
  re-verifying a done item). `test_config_changed` has `hidden: true` when the
  file is ignored by git, so it isn't in the diff at all. A refusal's error carries the refusing findings
  in `findings` and the rest in `warnings`.
- `propose`: `{id, file, item}`. `promote`: `{id, status, renumbered_from}`,
  plus `committed` (the short commit) when the change was committed: by default
  for `promote` and `renumber`, and with `--commit` for `block`, `unblock`,
  `drop`, `set-status` and `verify`.
  `block`, `unblock`, `drop`, `set-status`: `{id, previous_status, status}`,
  plus `deleted_file` when a proposal was dropped. `waive`: `{id, waiver:
  {date, groups, reason}, committed, trusted}`; `trusted` says whether the
  commit was recorded as trusted (section 17).
- `done`: `{id, previous_status, status, checks: [{name, command, exit_code,
  passed, output, results?}], checks_skipped, manual_verify, verify_source, touches_checked,
  findings, warnings, red_on_parent, verified, now_ready, merge_needed}`; `verified` is the value
  written to the item's Verified field (`skipped (--no-verify)` when the checks were skipped, which
  `drift` then reports as `unknown` with reason `not_verified`). `verify_source` is where the Verify
  command was read (the integration branch's ref, or `working tree`);
  `findings` are the done gate's findings that were allowed. With `--check`:
  `{id, status, checks, ..., checked_only: true}`, and nothing is written. `attempt` is the
  attempt's number in the current run (section 17), and a failed `done`'s error
  has `retry: {attempt, max_attempts, retries_left, failing_checks,
  failing_tests, command}` (`command` is `its brief --retry ID` while a retry
  is left, else `null`). Checks are the
  item's `Verify` command, then `commands.test` and `commands.lint`, run from
  the repo root; they run only in a scaffolded repo, and `--no-verify` skips
  them. `Verify` is run only when its whole value is one backticked command;
  anything else (`manual: ...`, a test ID, prose that mentions a command) is
  never run, and is returned as `manual_verify` for a person to check. A check
  that runs pytest has `results`, the summary of its JUnit report: `{report:
  true, tests, passed, failed, errors, skipped, failed_tests, skipped_tests:
  [{id, file, reason}], files}`, or `{report: false, reason}` when it wrote
  none.
- An error is `{"schema": 1, "ok": false, "error": {"code", "message", ...}}`.
  An error about an item also has its `id`, and a gate refusal carries the
  refusing findings in `findings` and the rest in `warnings`. The codes, with
  the exit code each comes with, are in the table below.
- `done` returns `override: {waived, waiver: {date, groups, reason}}` when it
  let findings through on the lead's waiver (section 17), and writes the same to
  the item's Override field.

The error codes. This table and the next are generated from
`src/idea_to_scaffold/contract.py` (`python -m idea_to_scaffold.contract
spec/WORKFLOW_SPEC.md` rewrites them), so a new code is added there:

<!-- contract:errors -->
| Code | Exit | Meaning | Also has |
| ---- | ---- | ------- | -------- |
| `already_done` | 1 | `done`: the item is already done |  |
| `already_started` | 1 | `start`: the item is already in progress |  |
| `audit_missed` | 10 | `gate-audit`: the gate missed a seeded cheat, or only warned about one (unless `--allow-warn`) | `missed`, `warned` |
| `bad_agent` | 1 | `hooks`: no Stop hook is known for that agent |  |
| `bad_backlog` | 1 | the backlog can't be read or changed as asked |  |
| `bad_hooks_file` | 1 | `hooks`: the agent's settings file isn't a JSON object |  |
| `bad_name` | 1 | `propose`: a placeholder name is lowercase letters, digits and hyphens | `name` |
| `bad_proposal` | 1 | `promote`, `renumber`: a proposal file isn't well formed | `problems` |
| `bad_result` | 1 | `done --result`: the result file can't be read, isn't JSON or doesn't match the schema | `result_file` |
| `bad_review` | 1 | `review --reply`: the reply doesn't match the reply schema | `problems` |
| `bad_status` | 1 | the status isn't a backlog status, or the item's status doesn't allow the command | `status` |
| `bad_title` | 1 | `propose`: a proposal needs a title |  |
| `base_untrusted` | 7 | `land`: the integration branch has commits that `its` didn't record and that aren't on the remote; a person looks, then `land --accept-base` trusts them | `integration_branch`, `commits` |
| `behind_remote` | 1 | `start` on the integration branch: it is behind its remote copy, so its backlog is stale; pull first | `branch`, `base`, `behind` |
| `blocked` | 4 | `start`: a dependency isn't done | `blocked_by` |
| `check_failed` | 5 | `done`: a check failed | `checks` |
| `claim_branch` | 1 | `adopt`: the branch is an `its` claim branch already, which `its land` takes as it is | `branch` |
| `claimed` | 1 | `start`, `adopt`: another branch holds the item's claim | `claim` |
| `dependency_changes` | 12 | `done`, `gate`: the done gate refused the branch's dependency changes (`dependency_added` enforced); the lead waives them with `its waive <ID> --dependencies` | `findings`, `warnings` |
| `dirty` | 1 | `land`, `waive`: uncommitted or staged changes are in the way |  |
| `diverged` | 1 | `land --fetch`: the integration branch and its remote copy have diverged |  |
| `duplicate_id` | 1 | an ID is defined more than once in the backlog | `ids` |
| `exists` | 1 | `propose`: that proposal exists already |  |
| `fetch_failed` | 11 | `--fetch`: fetching the remote failed | `remote` |
| `gate_error` | 1 | `done`, `review`, `gate`: the gate couldn't compare the branch (git couldn't answer, or the branch is set up wrong), so nothing passed |  |
| `git_error` | 1 | a git command failed |  |
| `github_failed` | 1 | `brief --github`: the GitHub CLI (`gh`) isn't installed, isn't signed in, or failed |  |
| `import_failed` | 1 | `import`: the export can't be read, or conflicts with files in the package |  |
| `incomplete` | 1 | `promote`: the item has no Done when or Touches | `missing` |
| `internal_error` | 1 | a bug in `its`; the message says what to report | `version` |
| `launch_settings_changed` | 12 | `done --result`: the headless launch files were changed during the run, so it may have had no Stop hook or sandbox; nothing was marked | `files`, `result_file` |
| `link_outdated` | 8 | `link --check`: generated links, the index or the commands blocks are out of date | `changed` |
| `locked` | 11 | another `its` command holds the package lock |  |
| `no_base` | 1 | `start --claim`, `gate-audit`, `adopt`, `gate`: there is no integration branch (or no base to gate against) |  |
| `no_branch` | 1 | `adopt`, `gate`, `review --branch`: there is no such branch or pull request, here or on the remote | `branch` |
| `no_changes` | 12 | `done`: the branch changes nothing outside the planning folder, so nothing shows the item was implemented | `findings`, `warnings` |
| `no_fork_point` | 12 | `done` on the integration branch: no start was recorded for the item, so the gate can't tell which commits are its work; `its start <ID> --force` records one |  |
| `no_package` | 1 | no planning package was found |  |
| `no_reason` | 1 | `block`, `waive`: `--reason` is missing |  |
| `no_result` | 1 | `done --result`: the agent's run ended without a result; nothing was marked | `result_file` |
| `no_status` | 1 | `done`: the item has no Status field |  |
| `no_waiver_group` | 1 | `waive`: name what is waived (`--test-changes`, `--outside-touches`, `--dependencies`) |  |
| `not_empty` | 1 | `new`: the folder exists and isn't empty |  |
| `not_found` | 1 | no such ID, or a file `its` was given can't be read | `mentioned_in` |
| `not_git` | 1 | the command needs a git repository |  |
| `not_integration_branch` | 1 | `land`, `renumber`: run it on the integration branch | `branch`, `integration_branch` |
| `not_on_base` | 1 | `waive`: run it in the main checkout, on the integration branch | `branch`, `integration_branch` |
| `not_placeholder` | 1 | `renumber`: the ID is permanent already |  |
| `not_utf8` | 1 | `link`: a planning file isn't valid UTF-8 text |  |
| `not_verified` | 5 | `verify --drifted`, `verify --all`: some items failed their checks again; the rest were re-verified | `failed` |
| `nothing_ready` | 3, 9 | `start --next`: nothing can start, and `reason` says why; exit 9 when every item is done | `reason` |
| `nothing_to_adopt` | 1 | `adopt`: the branch has no commits the integration branch lacks | `branch` |
| `nothing_to_retry` | 1 | `brief --retry`: the item has no failed attempt since it was started or adopted |  |
| `nothing_to_review` | 1 | `review`: the branch only changes the backlog |  |
| `out_of_attempts` | 1 | `brief --retry`: the item has failed `gate.max_attempts` times; it needs a person (split it, clarify it, or block it) | `attempts`, `max_attempts` |
| `outside_touches` | 12 | `done`, `gate`: the done gate refused the branch's scope changes (files outside Touches, or Touches changed); the lead waives them with `its waive <ID> --outside-touches` | `findings`, `warnings` |
| `overlap` | 6 | `start`: the item may change the same files as one already running | `overlaps`, `touches_listed`, `overlapping_touches` |
| `placeholder` | 1 | the item has a placeholder ID; `its promote` gives it a permanent one |  |
| `push_failed` | 11 | `land --push`: the branches landed locally, but the push failed | `landed`, `skipped` |
| `result_mismatch` | 1 | `done --result`: the result is for another item | `result_id`, `result_file` |
| `scaffold_failed` | 1 | `scaffold`: the package can't be loaded or scaffolded (not ready, or the target is in the way) |  |
| `status_changed` | 1 | `done`: the item's status changed while its checks ran (a `block`, say), and that change is kept | `status`, `was` |
| `test_changes` | 12 | `done`, `gate`: the done gate refused the branch's test changes (tests, test config, or how the item is verified); the lead waives them with `its waive <ID> --test-changes` | `findings`, `warnings` |
| `usage` | 2 | the command line is wrong |  |
| `use_waive` | 1 | an `--allow-*` flag, which is gone: the lead waives with `its waive` | `flag` |
| `which_item` | 1 | `done`, `review`: name the item; none could be inferred. `gate`: the branch could be for several (`candidates`) | `in_progress`, `candidates` |
| `worktree_exists` | 1 | `start --claim`: the worktree's folder exists already | `worktree` |
<!-- /contract:errors -->

Exit codes, the same with or without `--json`:

<!-- contract:exit-codes -->
| Code | Meaning |
| ---- | ------- |
| 0 | ok |
| 1 | error |
| 2 | bad command-line usage |
| 3 | `next`, `start --next`, `land`: nothing is ready (with `--parallel`: nothing can start alongside the claims) |
| 4 | `start`: a dependency isn't done |
| 5 | `done`, `gate`, `verify`: a check failed (the change is in scope, but it doesn't work yet); the status is unchanged. `review --reply`: the review didn't pass |
| 6 | `start`: the item may change the same files as one already running |
| 7 | `land`: a branch could not land (it conflicted, failed the done gate or its checks, isn't done, has no commits, or the base moved), or the integration branch has commits nothing vouches for (`base_untrusted`); branches only waiting on dependencies don't count |
| 8 | `drift --check`, `link --check`: something is out of date (a done item's files changed after it was verified; generated links, the index or the commands blocks need `its link`) |
| 9 | `next`, `start --next`: every item is done |
| 10 | `gate-audit`: a seeded cheat was missed, or only warned about (unless `--allow-warn`) |
| 11 | nothing is wrong with the work, so the same command may succeed later: another `its` command holds the lock, or fetching or pushing failed |
| 12 | `done`, `gate`: the done gate refused the change itself (tests or how they run changed, files outside Touches, new dependencies, no change, or a headless run's launch files changed); trying again won't help, a person decides |
<!-- /contract:exit-codes -->

`its backlog --write` saves the `its backlog --json` object as
`planning/backlog.json` (`docs/planning/backlog.json` in a repo), and `start`
and `done` keep it current once it exists. It is a generated mirror for tools
that prefer a file: never edited by hand, gitignored, and not copied by
`its scaffold`. The Markdown backlog stays the only source of truth.

## 17. Parallel work

Several agents can work the backlog at once, each in its own branch and
worktree. `its` creates the branches and prints the command to start Claude Code
or Codex in each; it never launches an agent itself.

- **The branch is the claim.** `its start <ID> --worktree [PATH]` creates the
  branch `its/<ID>-<slug>` from the integration branch in a new worktree
  (default `../<repo>-<ID>`); `--branch` creates it and checks it out here. An
  item is claimed while any local or remote branch `its/<ID>` or
  `its/<ID>-...` exists and the item isn't `done` or `dropped` on the
  integration branch. `its next` never offers a claimed item, and
  `its start` refuses one. `its start --next` picks and claims the next ready
  item in one locked step. A new worktree then gets `commands.setup` run in
  it, as the lead and before any agent works there (`--no-setup` skips it).
  The lockfiles setup writes (`uv.lock` on a project without one) are recorded
  with a digest of their content in the state folder, and the done gate and
  `its land` on that machine let a lockfile through while it still holds that
  content: it is setup's output, not the agent's dependency change. Committing
  the lockfile on the integration branch (the template's T-1.1 asks for it)
  settles it for every machine.
- **Claims are pushed.** When the package has a remote (`remote` in
  `package.yaml`, default `origin`), `its start` pushes the new claim branch
  with a lease that requires the branch not to exist yet, so when two machines
  claim the same item the first push wins and the other's local claim is
  undone (`claimed`); `start --next` then tries the next ready item, up to five
  times (`lost_to_others`). `--no-push`, or `push_claims: false` in `package.yaml`,
  keeps claims local. `next`, `start` and `claims` take `--fetch` to read the
  remote first; when the local integration branch is strictly behind its
  remote-tracking branch, readiness is read from the remote one.
- **A lock makes claiming atomic on one machine.** While it checks
  readiness, claims and overlap and creates the branch, `its start` holds an
  OS file lock on `claims.lock` in `$(git rev-parse --git-common-dir)/its/`,
  the directory every worktree shares. Every backlog status write holds the
  same lock, so concurrent `start` and `done` never lose a change: `done`
  re-reads the item's status under the lock after its checks, and a status
  that changed meanwhile (a lead's `block`) is kept (`status_changed`). The OS frees
  the lock when its holder exits, even after a crash. Outside git the lock
  lives in the system temp folder.
- **`its` keeps its state outside the repository.** Claims (`claims.json`:
  each claim's worktree, agent and start time), fork points (`forks.json`),
  the run ledger (`runs/`), the headless launch files (`launch/`), the
  trusted commits (`trust.json`), the lockfiles `its start`'s setup wrote
  (`setup.json`) and review verdicts (`reviews.json`) live in a state folder
  per repository,
  shared by its worktrees: `$XDG_STATE_HOME/its/<repo>-<hash>` when
  `XDG_STATE_HOME` is set, otherwise under `~/.local/state/its/` on Linux,
  `~/Library/Application Support/its/` on macOS and `%LOCALAPPDATA%\its\` on
  Windows. A linked worktree can write the repository's git folder, so state
  kept there would be state an agent could rewrite (its own fork point, the
  next run's launch settings); a sandbox that limits writes to the working
  folders, such as Claude Code's, keeps the agent out of the state folder.
  State an older `its` kept in `<git common dir>/its/` is copied over once.
  The lock stays in the git folder, where an agent's own `its done` can take
  it; the worst an agent can do with it is hold it.
- **`in-progress` isn't committed on claim branches.** The branch already says
  so. The item's `done` is committed with the work (`its done` with no ID uses
  the item of the claim branch that is checked out).
- **Done means merged.** On any branch other than the integration branch
  (`integration_branch` in `package.yaml`, default `main`; `--base` overrides
  it), `next` and `start` read statuses from the integration branch's copy of
  the backlog, so a dependency counts only once it is merged there.
- **Only items confirmed apart run in parallel.** Running items are active
  claims and `in-progress` items. `its start` (with or without a branch)
  refuses an item whose `Touches` might overlap a running item's, with exit 6,
  unless `--allow-overlap`. `its next --parallel [N]` offers up to N ready
  items (default 3) that overlap neither each other nor a running item. Two
  entries overlap when they could name the same file: equal paths, a directory
  and a path inside it, or a glob and anything under its fixed prefix. An item
  without `Touches` could touch anything, so it never runs alongside another
  item; it can start only when nothing else is running. A claim branch for an
  ID that isn't in the integration branch's backlog is reported as orphaned
  and blocks nothing. Only permanent IDs can be claimed.
- **Releasing.** `its claims` lists claims and marks finished ones;
  `its release <ID>` removes the claim's worktree and deletes its local branch.
  It checks everything first, so a refusal changes nothing: without `--force`
  the worktree must have no uncommitted changes, and the branch must be merged
  into the integration branch or the item done there. Lockfiles the setup of
  `its start` wrote, still unchanged, aren't uncommitted work. It refuses to
  remove the folder you are in. `--remote` also deletes its pushed branches on
  every remote: each is fetched first and deleted with a lease, so commits
  someone else pushed are never deleted unseen. A pushed branch another machine
  already deleted (`its land --release` there) is fetched, found gone and
  forgotten, so it isn't reported as still on the remote.
- **Renumbering.** Branches propose items with placeholder IDs
  (`T-new-<name>`). After merging, run `its renumber` on the integration branch:
  each placeholder that isn't dropped gets `T-<n>.<next free>`, where `n` is its
  milestone (`M3` gives `T-3.x`) or, without one, the milestone of the last
  numbered item above it. Every mention in the planning docs is rewritten and
  the docs are re-linked. `--dry-run` shows the new IDs first. It refuses to
  run off the integration branch (unless `--force`) or when a placeholder is
  proposed twice, and never reuses a number still mentioned in any doc.
  A proposal skips the gate's item-by-item diff, so `promote`, `renumber` and
  `validate` check each proposal file first (`bad_proposal`, with
  `problems`): it holds exactly one item, whose placeholder ID is the file's
  name, with status `proposed` and none of the Verified, Override, Waiver or
  Blocked fields. The done gate reports a changed proposal that breaks these
  as `proposal_invalid`, a test change. So a proposal can't enter the backlog
  already done, or under a real item's ID.
- **The done gate.** An agent can edit the files its checks read, so before
  running any check `its done` compares the branch with what the item allows.
  The diff runs from the fork point to the working tree (uncommitted and
  untracked files included) and covers the whole repository, not only the
  package. On a claim branch the fork point is the merge base with the
  integration branch recorded when the claim was made. On the integration
  branch itself (a plain `its start`), it is the commit recorded at start,
  outside the working tree, so committed work is still checked.
  - The rules come from the integration branch: the item's Verify and
    Touches, its waiver, and the `commands`, `test_paths` and `gate` settings
    in `package.yaml`. A branch that edits `package.yaml` is refused
    (`policy_changed`). `its land` checks the integration branch itself
    before it trusts those rules (the trust boundary, below).
  - The plan is diffed item by item. A branch may change its own item's
    Status, Verified, Blocked and Override fields and add proposals. Changing
    its Verify (`verify_changed`) or Touches (`touches_changed`), any other
    field of its item (`item_changed`), any other item (`other_item_changed`)
    or another planning doc is refused; another planning doc counts as
    outside Touches.
  - A deleted test file, a test that lost assertions (`assertions_removed`),
    a test that is gone (`test_removed`), or a new skip or xfail
    (`skip_added`) is refused. Assertions are counted test by test, and only
    those that can fail count: a test that keeps fewer than it had is a loss
    whatever the branch adds elsewhere, so commenting out an assertion and
    adding two `assert True` lines is still a loss. A test that is gone,
    deleted or renamed so the runner no longer collects it (`test_add` to
    `check_add`), is `test_removed` unless a test of that name turns up in
    another test file the branch changed; a test that asserted nothing that
    can fail may go. Python test files are read with `ast`, so a comment or
    `pass  # assert later` doesn't count as an assertion, and
    `pytestmark = pytest.mark.skip`, `@mark.skip`, `from unittest import
    skip; @skip`, `pytest.param(..., marks=pytest.mark.skip)` and an empty
    `@pytest.mark.parametrize("x", [])` count as skips. Other languages are
    read line by line with comments stripped, a test running from the line
    that starts it (`it("adds", ...)`, `test('x', ...)`) to the next; skips
    there include `it.skip`, `xit`, `t.skip()`, `test(name, {skip: ...})`,
    `{todo: ...}`, `.skipIf` and `.runIf`, and `.only`, `fit` and
    `fdescribe`, which stop the other tests running. Test files match
    `test_paths`, or by default `tests/`, `test/`, `__tests__/`, `test_*`,
    `*_test.*`, `*_spec.*`, `*.test.*` and `*.spec.*`.
  - Found, and in warn mode in projects scaffolded before phase 9 (below):
    an assertion that can't fail, such as `assert True`, `assert x or True`,
    `assert 1 == 1`, `assert 1 + 1 == 2`, `assert [x == 1]` or
    `assert (x, "message")` (`assertions_weakened`); a test that returns at
    once or only passes (`test_disabled`); a change to which tests are
    collected or run, or to their results: a `conftest.py` with
    `collect_ignore`, `pytest_collection_modifyitems`, a result hook
    (`pytest_runtest_makereport`, `pytest_sessionfinish`,
    `pytest_report_teststatus`, a hook wrapper, `exitstatus`, ...),
    `os._exit` or `pytest_plugins`; a test file that adds `os._exit`, an exit
    at import or `pytest_plugins`; changed pytest settings in
    `pyproject.toml`, `setup.cfg`, `tox.ini` or `pytest.ini`; a `pytest11`
    plugin entry point; a `sitecustomize.py`, `usercustomize.py` or `.pth`
    file; or a changed Jest, Vitest, Mocha or test-script setting
    (`test_config_changed`). New test files are checked against nothing, so a
    weak test the branch added itself is found too. A file git ignores (by
    `.gitignore` or `.git/info/exclude`) is in no diff, so the gate also looks
    for ignored files with these names (`conftest.py`, `pytest.ini`,
    `sitecustomize.py`, `*.pth`, `jest.config.*`, ...) in the checkout, outside
    dot-folders and install folders such as `node_modules` and `venv`, and
    reports each as `test_config_changed` with `hidden: true`.
  - **The runner's report.** A check's exit code comes from the code under
    test, so it isn't trusted alone. When a check runs pytest (`pytest`,
    `py.test`, `python -m pytest`, `uv run pytest`, ...), it runs with
    `PYTEST_ADDOPTS` set to write a JUnit XML report to a fresh temporary
    folder (or the command's own `--junitxml` path wins), and the gate reads
    it after the checks pass. A run that wrote no
    report stopped before pytest finished, for example on `os._exit(0)` in
    the code under test (`no_test_report`, warn first). A report with failed
    or errored tests behind exit 0 means something rewrote the exit status
    (`failures_hidden`). A report with no tests, or with every test skipped,
    shows nothing (`no_tests_ran`). Skipped tests that the item's Verify ran,
    or that are in test files the branch changed, are `tests_skipped` (warn
    first). `its done`, `its verify` and `its land` all apply these. This
    makes faking a pass harder, not impossible: code running inside the test
    process can still write a report of its own. A report with a `DOCTYPE`
    or entities, or one over 64 MB, counts as no report.
  - **Dependencies.** `its land` runs `commands.setup` as the lead, and
    installing a package runs its build hooks. So a branch that adds a
    dependency, changes where one comes from, or rewrites a lockfile is
    reported (`dependency_added`, with `file` and `added`). Manifests are
    compared entry by entry, so only what the branch adds or changes counts:
    `pyproject.toml` (project, optional and group dependencies,
    `[build-system]`, Poetry, uv and PDM tables), `setup.cfg`,
    `requirements*.txt` and `constraints*.txt`, `Pipfile`, `package.json`
    (the dependency maps, `overrides`, `resolutions` and the install
    scripts), `Cargo.toml` (the dependency tables, `[patch]`, `[replace]`) and
    `go.mod`. Any change counts in a lockfile (`uv.lock`, `poetry.lock`,
    `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`,
    `go.sum`, ...), `setup.py`, and package-index settings (`uv.toml`,
    `pip.conf`, `.npmrc`, `.yarnrc.yml`). Removing a dependency isn't
    reported. It warns by default, and new scaffolds write `warn` for it too,
    since the first `uv sync` writes a lockfile; set
    `gate.checks.dependency_added: enforce` once the lockfile is committed.
    Enforced, the gate refuses with `dependency_changes`, and the lead's `its
    waive <ID> --dependencies` lets the branch through.
  - **How checks run.** A check runs code from the branch, and `its land`
    runs it as the lead, so a check gets only the caller's environment
    variables on an allow-list: the path, home and user, the locale (`LANG`,
    `LC_*`), temporary folders, `TERM`, `TZ`, `CI`, `NO_COLOR`, Python's
    UTF-8 settings, `VIRTUAL_ENV` and Conda's, `UV_*`, Java, Go and Rust
    homes, proxies and certificate bundles, the commit identity, and the
    variables Windows itself needs (`SYSTEMROOT`, `COMSPEC`, `PATHEXT`,
    `USERPROFILE`, `APPDATA`, ...). Names compare without case. A `LC_*` or
    `UV_*` name that looks like a secret (`TOKEN`, `SECRET`, `PASSWORD`,
    `API_KEY`, `AUTH`, ...) is left out. The integration branch's `gate.env`
    lists more names to pass. `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` are never
    passed, whatever `gate.env` says. So a token in the lead's shell, such as
    `GH_TOKEN`, never reaches the code under test. A check that runs longer
    than `gate.timeout` seconds (default 1200) is stopped together with
    everything it started (its process group on POSIX, its process tree on
    Windows); it fails with exit code 124 and `timed_out: true`. Its output
    goes to a file rather than a pipe, so a background process can't hold a
    check open after it exits. The allow-list keeps secrets out of the
    environment, not off the disk: the code under test can still read files
    the lead's account can read, such as `~/.config/gh` or `~/.ssh`.
  - **Red on parent.** After the checks pass, the branch's new and changed
    test files are copied onto the fork point in a throwaway worktree, and
    Verify (or `commands.test` when Verify isn't a command) runs there after
    `commands.setup`. If it passes, the tests don't show the change does
    anything (`tests_pass_on_parent`, with `files` and `command`). `done`
    returns what ran as `red_on_parent: {ran, passed_on_parent, command,
    files, exit_code}`, or `{ran: false, reason}`. It is skipped when
    re-verifying a done item, and for an item with `Kind: test-only`, whose
    new tests are meant to pass on the code as it is.
  - **Held-out acceptance tests.** Tests under `gate.acceptance_path`
    (default `tests/acceptance/`) are left out of every worktree `its start
    --worktree` creates, by a sparse checkout (`start` returns `held_out`),
    and a branch that changes them is refused (`acceptance_changed`). They are
    per item: the lead writes an item's tests in `<acceptance_path>/<ID>/`
    (`tests/acceptance/T-2.1/`); files directly in the folder are shared
    (fixtures, or tests that hold for the whole project). `its land` runs
    `commands.acceptance` on each merged result after the other checks, with
    a sparse checkout that leaves out the folders of items not done on that
    result, so two items landing in parallel never fail on each other's held-out
    tests, whose code isn't merged yet. A held-out failure names the item whose
    tests failed (`held_out_failed`). Before blaming the branch, land runs the
    failing check (test, lint or acceptance) on the integration branch itself:
    if it fails there too, the skip is `base_failing` (with `base_check`) and
    the remaining items aren't tried, since none could land on that base.
    Elsewhere (`its done` in a checkout that has the folder, as on the plain
    flow), a pytest check is given `--ignore` for the folders of items that
    aren't done. The folder is out of the agent's view, not secret: it is still
    in the repository's history.
  - **Warn mode.** The checks that can misfire on honest work
    (`assertions_weakened`, `test_disabled`, `test_config_changed`,
    `tests_pass_on_parent`, `no_test_report`, `tests_skipped`) start in warn
    mode: they are reported, with `mode: "warn"`, in `findings` and
    `warnings`, but don't refuse the item. `gate.checks.<kind>` in the
    integration branch's `package.yaml` sets `enforce`, `warn` or `off` for
    these and for `test_removed`, `failures_hidden` and `no_tests_ran`, which
    enforce by default. Every other check always enforces. When the
    package's `package.yaml` has no `gate` section, `its scaffold` writes one
    into the new repo that sets `enforce` for every warn-first check, so only
    projects scaffolded before phase 9 (or that set their own `gate`) warn
    first. Each finding has `mode`.
  - **Measuring the gate.** `its gate-audit [ID] [--no-run] [--allow-warn]`
    applies a fixed set of known cheats, one at a time, to a throwaway
    checkout of the integration branch and reports which the gate refused,
    which it only warned about and which it missed (`{ok, id, base,
    test_file, variants: [{name, description, expect, caught, found, mode,
    message}], caught, total, refused, warned, warned_kinds, missed,
    skipped}`). Most cheats need only the inspection. `os_exit` (`os._exit(0)`
    appended to the item's own Python source in Touches) needs the tests to
    run, so the audit runs `commands.setup` and the item's checks for it, and
    skips it when nothing shows the tests imported that source; `--no-run`
    leaves it out. `ok` is true only when every cheat was refused. It exits
    10 when any was missed or only warned about (`--allow-warn` accepts
    warnings), and prints the `gate.checks` settings that would refuse the
    warned ones. Nothing in the checkout changes.
  - Agent configuration (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.claude/`,
    `.codex/`, `.cursor/`, `.gemini/`, `.its/`, `.mcp.json`,
    `.github/workflows/`) is refused whatever the Touches say
    (`agent_config_changed`).
  - A changed file outside the item's Touches is refused, including a file
    outside the package in a monorepo (reported as `../path`). An item without
    Touches isn't scope-checked.

  Touches entries are paths relative to the package root: a plain path names
  that file or everything under that folder; a glob uses `*` (not crossing
  `/`), `**`, `?`, `[abc]` and `{a,b}`; every entry is also tried literally,
  so `app/[id]/page.tsx` works. The gate, `its land` and `its drift` share
  this one meaning.

  The gate fails closed: with no integration branch, a shallow clone, a fork
  point that left the history, or a git error, `its done` refuses with
  `gate_error` (exit 1) instead of passing. On the integration branch with no
  start recorded for the item, it refuses with `no_fork_point` (exit 12), since
  nothing says which commits are the item's; `its start <ID> --force`
  records one.

  Only the lead waives, on the integration branch: `its waive <ID>
  --test-changes` (test, plan and agent-config findings), `--outside-touches`
  (scope findings) or `--dependencies` (`dependency_added`), with `--reason`.
  It runs in the main checkout (`not_on_base` elsewhere) and commits
  `**Waiver:** <date>; <groups>; <reason>` on the item there, leaving the rest
  of the working tree alone. A branch can't write a waiver for itself: the
  gate reads the plan from the integration branch and refuses a branch that
  changes the item's other fields. On the plain flow, where the agent commits
  on the integration branch too, only the last waiver commit `its waive` made
  after the item's start counts. `done` and `land` let the waived groups'
  findings through, still reported, and `done` writes the waived kinds and the
  waiver to the item's `Override` field. Verify is always the integration
  branch's, even when test changes are waived. The old `--allow-*` flags are
  refused (`use_waive`), and a refusal never names `its waive`; an agent that
  can't finish honestly runs `its block <ID> --reason`. `its land` runs the
  same gate on each branch before merging it.

  `done --no-verify` still marks the item done, but writes `skipped
  (--no-verify)` as Verified, which `its drift` reports as `unknown`
  (`not_verified`) rather than as verified work. `land --no-verify` is the
  lead's own choice.
- **Review and hooks.** `its review <ID>` prints an evidence bundle for a second
  agent: the item, the text of each requirement it satisfies, the gate's
  findings and the diff, with instructions to review the change once (reading
  the repository and running its tests as it needs, and reporting only
  problems the change has or causes) and answer with one JSON object (`its
  review --schema`): `{verdict: approve | changes_requested, requirements:
  [{id, status: met | not_met | cant_tell, evidence, repro}], findings:
  [{severity: blocker | major | minor, file, line, message, repro}]}`. A
  `repro` is `{command, files}`: a command, and any files to add first (a
  test that shows the problem), that fails because of the problem. The author's own account is left out, since it sways a
  reviewer: the backlog's diff and notes files (`HANDOFF.md`, `NOTES.md`,
  `PROGRESS.md`, `SUMMARY.md`, `WORKLOG.md`, `CHANGES.md`, `.its/`) are listed
  but not shown (`omitted`). `its review ID --reply FILE` reads the answer
  (bare, fenced, or inside a `claude -p --output-format json` envelope) against
  the item (named, or this claim checkout's: `which_item` otherwise) and passes
  it when the verdict is approve, every requirement the item satisfies is in
  the reply and met (one left out is `missing`, and counts as unmet), and no
  finding is at or above `--min-severity` (default `major`); otherwise it exits
  5. A reply that doesn't fit the schema is `bad_review`. The answer's
  `outcome` is `approved`, `changes_requested` (a finding at or above the
  cut-off, or a requirement `not_met`) or `inconclusive` (nothing was shown
  wrong, but the reply doesn't approve: a requirement it can't tell, or a
  verdict of changes_requested with nothing behind it).
  - **Runnable evidence.** A reviewer that reads the repository finds real
    problems and also reports ones that aren't there, so the two claims that
    can stop a change, a `not_met` requirement and a `blocker` finding, have to
    reproduce. `--reply` runs each one's repro in a throwaway worktree of
    exactly the content reviewed (the reviewed ref, or for a checkout its
    HEAD plus its uncommitted and untracked changes), after `commands.setup`,
    with the gate's allow-listed environment and time limit: it writes the
    repro's files (a path outside the repository is refused), runs the
    command, and resets the worktree for the next. A repro that fails there
    stands (`reproduced: true`). One that passes, can't run (exit 126, 127 or
    9009), runs out of time, or is missing is downgraded, `not_met` to
    `cant_tell` and `blocker` to `major`, with `downgraded_from`; the answer's
    `evidence` says what happened to each. No language model filters the
    findings: the command decides. The commands are the reviewer's, so they
    get no more than the branch's own tests already get. `--no-evidence` runs
    none, so every such claim is downgraded.
  - **Recorded verdicts.** `its review ID` notes a digest of the change it put
    in the prompt (each changed file's git blob id, without the backlog and the
    author's notes, so the same content gives the same digest in a worktree,
    on a branch and in the commit land merges). `--reply` records the verdict
    against that digest in the state folder (`reviews.json`, the newest 20 per
    item) and the ledger. `its land` computes the digest of what it would
    merge: a verdict whose outcome is `changes_requested` on that content
    refuses it (`changes_requested`), and with `--require-review`,
    `gate.require_review: true` in the integration branch's `package.yaml`,
    or `Risk: high` on the item there, only content with an approving verdict
    lands (`not_reviewed`; an inconclusive one doesn't count); a landed merge commit says so (`Reviewed: approved DATE
    (DIGEST)`). Any later change to a reviewed file, or a new file, makes the
    verdict stale: it neither blocks nor approves. An adopted claim's review is
    of one candidate: `--branch NAME` names it (a candidate's name, or any
    branch here; `no_branch` otherwise), else the candidate checked out here,
    else the first with commits. Each candidate keeps its own verdict, and the
    payload says which was reviewed (`reviewed_branch`). `its status` shows
    each done claim's review, and each candidate's. The record is the lead's, like the claims: it isn't
    signed, so an agent that can write the state folder could forge one. The item and requirement text come from the
  integration branch (a requirement the branch changed is flagged
  `changed_on_branch`), and the diff sits in a fence longer than any run of
  backticks inside it, so the change can't rewrite what it is judged against
  or close the fence. `its hooks` adds an opt-in Stop hook for Claude
  Code and Codex that runs `its hook stop`: it re-runs the gate for the
  item of the session's checkout (the `cwd` in the hook's JSON, so a session
  in a worktree is checked there) and, if it fails or the item isn't marked
  done, answers `{"decision": "block", "reason": ...}`. The reason carries the
  first failing check's output once, not once per check. It never blocks
  twice in a row, or when no item is being worked on or the item is blocked.
  A hooks file whose `hooks` or `hooks.Stop` has the wrong shape is refused
  (`bad_hooks_file`) and left as it was.
- **Landing.** A clean merge of two branches can still break the build, so
  `its land` merges finished claims into the integration branch one at a
  time, in backlog order, and runs the item's Verify plus the test and lint
  commands on each merged result. It runs from the main checkout on a clean
  integration branch, but merges and checks each branch in a throwaway
  worktree (running `commands.setup` there first, since a fresh worktree has
  no installed dependencies). Only a result that passes moves the integration
  branch, by a fast-forward of the user's checkout taken under the package
  lock; the checkout is never merged into, reset or cleaned, so untracked
  files and statuses written while the checks run survive. If the
  integration branch moved during the checks the item is skipped
  (`base_moved`); if an untracked file is in the way it is skipped
  (`checkout_blocked`) and the file is left alone. Once the checks pass, land
  rewrites the landing item's `Verified` mark from the merged commit (`date,
  base X, tree HASH`, the files as they landed rather than the agent's working
  tree), and re-runs the Verify of every other done item whose Touches the
  merge changed: each that passes gets its mark refreshed in the same merge
  commit (`reverified`), so `its drift` stays quiet for files shared by design,
  and one that fails refuses the landing. A claim is finished when its item is `done` at the
  branch tip, and lands only once its dependencies are done on the
  integration branch (an item whose dependency lands earlier in the same run
  gets its turn). Before merging, `git merge-tree` checks for conflicts; a
  conflict in a generated file (`planning/README.md`, wireframe index,
  `backlog.json`) is resolved by regenerating it, any other conflict skips the
  branch. A branch whose checks fail on the merged tree is never applied, so
  the integration branch only moves to states that were tested whole. `--fetch`
  fast-forwards from the remote first, `--push` pushes the result, `--release`
  releases each landed claim, `--dry-run` reports what would land. `its claims`
  shows each claim's conflicts with the base and with other claims ahead of
  time. An adopted claim (*Vendor dispatchers*) has candidates instead of a
  finished branch: nobody ran `its done` on them, so land runs the whole done
  gate on each in turn, in the order they were adopted (the scope and tamper
  checks, the item's Verify and the checks on the merged tree, the item's own
  held-out tests, and red on parent), lands the first that passes with the
  item set done in the merge commit (`Adopted: NAME`), and leaves the others
  (`not_chosen`). So when several agents tried one item, the one that passes
  the tests it couldn't see is the one kept.
- **The lead's inbox.** `its status` groups claims by what they need from
  the lead, most urgent first (`attention`): `needs_you` (blocked, a review
  that requested changes or is required and missing, conflicts; unclaimed
  blocked items too), `ready_to_land`, `running`, `stale` (no commit for
  `gate.stale_days`, default 3; `idle_days`) and `done` (release them). An
  adopted claim lists its candidates, each with its review. Agents finish
  work faster than a person can review it, so `gate.max_unreviewed` on the
  integration branch is a work-in-progress limit: once that many finished
  items wait for the lead, `its next`, `its start` and `its status` warn
  (`wip_limit`, with the items). Nothing is refused.
- **The trust boundary.** An agent works in a linked worktree of the lead's
  repository, and `its land` runs as the lead, so the agent's work must not
  become the lead's actions. `its land`, and `its gate` in a pull request's
  CI, are where the rules are enforced, and the only gates that cover every
  surface an agent works on: `its done`, the Stop hook and a launcher's `done
  --result` run in the agent's checkout and are pre-checks it can skip, and a
  vendor's cloud agent runs none of them. A pull request merged without `its
  land` is gated only when its CI runs `its gate` and the merge requires that
  check to pass. The plain flow, where an agent
  works on the integration branch itself, isn't enforced: nothing stands
  between that agent and the branch.
  - **Git runs no repository code.** Every git command `its` runs sets
    `core.hooksPath` to an empty folder and `core.fsmonitor=false`, drops
    `GIT_*` variables other than the commit identity and the SSH and askpass
    settings, never waits for a credential prompt, and times out (300 s, or
    600 s for fetch and push). So a hook an agent planted in the shared git
    folder never runs in the lead's `its land`, and the project's own hooks
    don't run on `its`'s commits either. Other programs the repository's
    config can name (`filter.*`, `diff.external`) would still run if an agent
    could write that config; Claude Code's sandbox denies it.
  - **The integration branch is checked, not assumed.** A worktree shares the
    lead's refs, so an agent can move the integration branch (`git
    update-ref`), and that branch holds the gate's rules. A commit on it is
    trusted when it is on the remote's copy (`<remote>/<branch>`), or behind a
    tip `its` recorded itself: the result of a land, a waiver, or a planning
    commit (`promote`, `renumber`, `block --commit`, ...) made on trusted
    work. The record is `trust.json` in the state folder. With nothing
    recorded yet, the first land trusts the branch as it is. `its land`
    refuses (`base_untrusted`, exit 7, with `commits`) while the branch has
    any other commit, and skips a branch (`base_untrusted`) if the
    integration branch moved that way during the run. After a person has
    looked, `its land --accept-base` trusts them; the payload's `base_trust`
    says which case applied. `its status` warns (`main_moved`). A commit the
    lead made by hand counts as untrusted until it is pushed or accepted:
    nothing in git tells it apart from an agent's.
  - **What this doesn't cover.** `its land` runs the branch's code as the
    lead: `commands.setup`, the checks and the code under test. The
    allow-listed environment keeps tokens in the lead's shell away from that
    code, but not files: anything the lead's account can read
    (`~/.config/gh`, `~/.ssh`, cloud credentials) is readable to it. An agent
    without a sandbox can write anything its user can, the state folder and
    the repository's git config included. Running each agent in a separate
    clone, which shares no refs or git folder with the lead's, closes more of
    this; `its` doesn't set that up yet.

- **Unattended runs.** `its` never launches an agent; `its start` writes
  launcher scripts that do it safely, and prints the line that runs one
  (`commands.headless`). Each agent gets a script per shell in the state
  folder's `launch/`: `.sh` (`sh FILE`), `.ps1` (`powershell -NoProfile
  -ExecutionPolicy Bypass -File FILE`, UTF-8 with a BOM, so Windows
  PowerShell 5.1 reads it right) and a `.cmd` that runs the `.ps1`. A script
  checks the agent is installed, changes to the worktree, removes an old
  result file, runs the agent with push tokens unset, and then always runs
  `its done ID --result FILE` and exits with its code, even when the agent
  failed, so every run reaches the ledger. The worktree has had
  `commands.setup` run in it already (*The branch is the claim*).
  - `claude -p PROMPT --setting-sources user --settings FILE --output-format
    json --json-schema SCHEMA --max-budget-usd N --permission-mode acceptEdits
    --permission-prompts none`, where SCHEMA is the result schema as compact
    JSON with its spaces written `\u0020`: one word with no whitespace, which
    every shell passes whole. The project's settings aren't loaded, so the
    settings file carries the Stop hook (`its hook stop --headless`, which
    runs `done --check` and never asks the agent to mark the item) and turns
    the OS sandbox on. It is written to `launch/`, outside the repository, so
    a sandboxed agent can't edit it, and `its done --result` refuses a run
    whose launch files no longer match what `its` wrote
    (`launch_settings_changed`, exit 12). The budget is `launch.budget_usd`
    (default 5). The prompt tells the agent to check its work with `its done
    ID --check` and leave the status and the commit to `its done --result`.
  - `codex exec --sandbox workspace-write --output-schema FILE -o RESULT
    PROMPT`.
  - Push and registry tokens (`GH_TOKEN`, `GITHUB_TOKEN`, `NPM_TOKEN`, AWS
    keys, ...: `env_unset`) are unset for the agent: by `env -u` in the `.sh`
    script, and in the `.ps1` by clearing them for the agent's run and
    restoring them after, for `its done --result`. A program that runs `argv`
    itself drops `env_unset`. Nothing printed skips permissions or the
    sandbox.
  - Each script ends with `its done ID --result FILE`. The result is one
    object, `{id, status: done | blocked | needs_human, summary, reason,
    files_changed, tests_run, commit_message}` (`reason` is required unless
    `done`, other fields are refused), bare or as the `structured_output` of a
    `claude -p` envelope; a file PowerShell wrote as UTF-16 is read too. `done`
    runs the gate and checks as usual; `blocked` or `needs_human` runs `its
    block` with the reason. In the claim's own checkout the outcome is then
    committed (`commit_message`, else the summary, tagged `[ID]` with `Refs:`
    for its requirements; the planning folder only, for a block) and the
    claim branch pushed when it was pushed at start (`--no-push` keeps it
    local), so the lead sees it from main: `committed`, `pushed`, or
    `push_error`. On the plain flow nothing is committed for the person. A
    result for an item already done re-checks it and records the run
    (`already_done: true`), as when the agent ran `its done` itself. A wrong
    `id` is `result_mismatch` (with no ID given, the expected item is this
    claim checkout's), a file that doesn't fit is `bad_result`, and an
    envelope without `structured_output` (the run failed, ran out of budget or
    declined) is `no_result`; none of them change the item, and all are
    recorded. `done` returns `result` and `run: {agent, cost_usd, turns,
    duration_ms, session_id}`.
- **Vendor dispatchers.** Every major coding-agent vendor runs agents in
  parallel itself (Claude Code's background sessions and Projects, GitHub's
  issue assignment to Copilot, Claude or Codex, Codex's cloud queue), with
  dashboards, budgets and isolation `its` doesn't try to match. Those agents
  push branches under their own names (`claude/...`, `copilot/...`,
  `codex/...`) and usually never run `its`. `its` doesn't launch or watch
  them: it writes the task, takes in their branches and gates them.
  - **`its brief ID`** writes the task for an agent that never runs `its`:
    the item, the text of each requirement it satisfies, its Touches and
    Verify, the check commands, that held-out tests exist (not where they
    are, or what they test), whether a review is required, and the rules
    (work on a branch `<your-prefix>/ID-slug`, change only the Touches, tests
    included, tag each commit `[ID]`, leave the backlog alone, end with `its
    done ID --check` if `its` is installed, else say why it is blocked). The
    item and the rules come from the integration branch. A `done` or
    `dropped` item is `bad_status`. `--github` puts the brief on the
    repository's issue with `gh`: created as `[ID] title`, or updated when it
    exists (the number `its` recorded, else a search); `github_failed` without
    `gh` or when it fails. It returns `{id, title, brief, retry, checks,
    held_out, review_required, issue, commands}` and records a `brief` event.
  - **`its adopt REF ID`** claims the item as `its start --branch` does (a
    claim branch from the integration branch, pushed) and records REF as a
    candidate: a pull request (`pr:12`, `#12` or its URL, fetched into
    `refs/its/pull/12`), a branch, a remote branch or any ref (`no_branch`
    when it isn't there). Adopting more branches for the item adds candidates,
    so several agents can try one item; *Landing* keeps the first that
    passes.
  - **`its gate [REF] [DIR]`** runs the done gate on any branch or pull
    request, for CI, so a pull request shows red before anyone reviews it:
    the scope and tamper checks, the item's Verify, the test and lint
    commands, the item's held-out tests and red on parent, with the rules of
    the base (`--base`, else `GITHUB_BASE_REF`, else the integration branch,
    read from the remote when there is no local copy; `no_base`), in a
    throwaway worktree at REF (default `HEAD`), so the checkout is never
    changed. The item is `--id`, else the first of: a claim branch, a branch
    adopted here, a T-n.m in the branch name (`GITHUB_HEAD_REF` for `HEAD` in CI), the
    item whose status the branch changes, or `[T-n.m]` in its commit
    messages; several is `which_item` (with `candidates`). A branch that
    names no item passes with `reason: no_item`, unless `--require-item`. A
    failure exits 7. The Python stack's CI has a `gate` job that runs it on
    pull requests (`actions/checkout` with `fetch-depth: 0`).
  - **One targeted retry.** A failed `done` (not `--check`), `gate` or
    `land` of the item is an attempt: one per agent run (its `session_id`)
    or per commit, so running `its done` again on the same work, or landing
    the same commit twice, is the same attempt, and a held-out test failing
    on the integration branch itself (`base_failing`) isn't the item's. A
    failure's payload has `retry: {attempt, max_attempts, retries_left,
    failing_checks, failing_tests, command}`. `its brief --retry ID` writes
    a short brief of only what failed in the last attempt (the failing
    checks, the failing tests by name, held-out ones only counted as
    `held_out_failed`, and the first messages), not the whole brief again; with `--github`
    it is a comment on the issue. There is nothing to retry before a failure
    (`nothing_to_retry`), and after `gate.max_attempts` failed attempts
    (default 2) the item needs a person (`out_of_attempts`, with `attempts`
    and `max_attempts`).
  - **Locally**, the same flow is `its start ID --worktree`, then a
    background agent (`claude --bg`) in the worktree with the brief or the
    command `its start` prints; the Stop hook and `its done` run there too.
- **The run ledger.** `start`, `adopt`, `brief`, `done`, `block`, `gate`,
  `land`, `review --reply` and the Stop hook each append `{ts, item, event,
  outcome, run, ...}` to `runs/<ID>.jsonl` in the state folder: shared by every worktree on the
  machine, never committed and never in a diff, so recording can't trip the
  gate (an `.its/` folder in the checkout would be agent configuration) or
  conflict at land. `done` records its outcome (`passed`, `check_failed`,
  `test_changes`, `outside_touches`, `gate_error`, `no_result`), the finding
  kinds, the failed checks, the Touches count and what the agent reported; a
  failure also records the commit (`head`), its attempt number, the failing
  checks and tests (`failing_checks`, `failing_tests`, up to 20) and the
  first messages, which is what `its brief --retry` reads. `land` records the
  branch and commit it tried, and marks a `base_failing` refusal
  `collateral`. A run ID (`run`, also in `start`'s and `adopt`'s payloads)
  links the start or adopt of an item to its results, done and land; a new
  start or a claiming adopt begins a new run, and attempts count within it.
  `its stats` reads it: `{items: [{id, attempts, passed, failed, blocked,
  landed, cost_usd, first_try, touches, agents, agent, started_by, runs}],
  records, attempted, first_try, first_try_rate, first_try_interval,
  comparable_from, refusals, land, reviews, cost_usd, cost_p90_usd, by_size,
  by_agent, ledger}`. An item's `agent` is the one recorded with its first
  attempt, else the one that started or adopted it, so a lead's `its start` and a Claude session on the
  same item count once, as Claude's. Each rate in `by_size` and `by_agent`
  has a 95% Wilson `interval`, and `comparable` is false below 20 attempts
  (`comparable_from`): too few to rank agents or sizes by. A `done --check`
  (and so the Stop hook) is recorded but isn't an attempt. What goes back
  into an agent's prompt from the ledger is only a retry brief's failure
  details.
- **Sizing.** `its validate --sizing` adds `sizing: {checked, items,
  warnings: [{id, kind, message}], flagged}` for todo and proposed items:
  `too_many_touches` (more than 3), `too_many_requirements` (more than 2
  FRs), `too_many_criteria` (more than 5 Must acceptance criteria across its
  FRs), `no_verify` (no Verify command) and `vague` ("improve", "refactor",
  "clean up", ... in the title or Do, with no Done when). They are warnings:
  `validate` still passes. `promote` returns the item's as `sizing`.

## 18. Drift and traceability

A done item was checked against the code as it was then. Two commands say what
that guarantee still covers.

- **Drift.** `its done` and `its verify` write the item's `Verified` field,
  which ends with a hash of the files in its Touches as they were checked
  (`tree <hash>`). When the files on disk still match the hash, the item is
  clean, with no history needed; re-verifying changed code always writes a
  new value. When they don't, the history says which files changed: the commit that brought the current value into the branch (found on its
  first-parent history; for a landed item, the merge `its land` tested) is the
  item's verified commit. `its drift` lists each done item whose Touches differ
  between that commit and the working tree, with the commits that changed them,
  and the requirements those items satisfy. An item done before `Verified`
  existed uses the commit where it became done; one without Touches isn't
  tracked. `its verify <ID>` re-runs a done item's checks and refreshes its
  `Verified` value; commit it. `its land` refreshes it for what it lands: the
  landing item's mark is written from the merged commit, and every other done
  item whose files the merge changed has its Verify run again and its mark
  refreshed, so a land leaves `drift --check` clean. `verify --drifted` checks
  every drifted item before marking any, so one run settles them all. In a shallow clone, an item whose `Verified`
  has no hash is reported as unknown (`shallow`), never as clean. `--check` exits 8 on drift.
- **Traceability.** Acceptance criteria are numbered by position in a
  requirement's **Acceptance criteria** list (`AC1`, `AC2`, ...); a bullet
  that starts with a label (`AC3:`) keeps that number, and an unlabelled
  bullet whose position a label already took gets the next free number. The
  label may be a heading, a bold line or a field-list bullet (`-
  **Acceptance criteria:**`). Only bullets at the first criterion's
  indentation count, so a sub-bullet is part of the criterion above it; the
  list ends at the next heading or bold label, even before its first bullet,
  so a later list such as **Out of scope** is never read as the criteria. A
  test covers a criterion when a test file names both, in any usual spelling
  (`test_fr_001_ac2_...`, `FR-001/AC2`, `FR-001, AC2`, `FR-001 (AC2)`, a
  marker such as `@pytest.mark.req("FR-001 AC2")`); a reference must end at
  a word boundary, so `fr0012ab` is not FR-0012. Naming the requirement alone covers all
  of its criteria, reported as `by_requirement`. `its validate --trace` lists
  the Must criteria no test names and exits 1 if there are any; plain
  `its validate` never fails on coverage. A requirement with no criteria list
  counts as one criterion.
