# Stage 6 — Project plan and backlog

Using everything approved so far:

1. Define milestones in `planning/06-project-plan.md`. **M1 is always a walking skeleton**: the app builds, tests and lint run, CI is green, and one trivial path works end to end. Each milestone gets a demoable outcome, the features it delivers, and exit criteria. Add a **Sequencing** section (order, what can run in parallel) and a **Risks** table (risk, likelihood, impact, mitigation). **Do not list tasks in this file.**
2. Write `planning/07-backlog.md`, the only list of work items, in priority order under a `## Items` heading. Each item:
   ```markdown
   ### T-1.1 — Short title
   - **Milestone:** M1
   - **Status:** todo
   - **Satisfies:** FR-001, NFR-002
   - **Depends on:** —
   - **Do:** what to build
   - **Done when:** how a test or reviewer can tell
   - **Verify:** `command that proves it` (or a test ID, or `manual: ...`)
   - **Touches:** `src/store/`, `tests/test_store.py`
   ```
   - Number items `T-<milestone>.<n>`. Keep each small enough for one coding-agent session (about half a day of human work or less).
   - Use `Status: todo` for agreed work. Put ideas that aren't scheduled at the end with `Milestone: —` and `Status: proposed`.
   - `Verify` and `Touches` are optional but strongly recommended: `Verify` lets a tool check the item, and `Touches` lets independent items run in parallel without editing the same files. Only list a dependency in `Depends on` when the item really needs the other one first; false dependencies stop parallel work.
3. Check traceability and report: any Must FR not covered by a backlog item, any item that satisfies nothing, any dependency on an item that doesn't exist.
4. Revise until I approve both files.
