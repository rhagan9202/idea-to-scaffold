# Stage 7 — Agent instructions and manifest

Using the approved package:

1. Write `AGENTS.md` (under ~200 lines) with exactly these sections, in order: **Project**, **Source of truth**, **Stack & commands**, **Repository layout**, **How to work**, **Conventions**, **Boundaries**, **Playbooks**. Point to the planning docs rather than repeating them. Fill in real commands for the chosen stack. Start from the AGENTS.md template conventions: agents begin at `docs/planning/README.md` (the generated index), take work only from `07-backlog.md` (`its next`), set an item's Status with `its start` and `its done` (or by hand, in the same commit as the work), add discoveries as `proposed` items, and follow ID links rather than copying text. Commit convention: `<type>(<scope>): <summary> [T-n.m]` with `Refs: <IDs>` in the body.
2. Write `package.yaml`:
   ```yaml
   spec_version: "0.1"
   profile: full    # or lite, if we chose a lite plan
   project: {name, slug, summary, license}
   stack: {language, runtime, framework, package_manager, test, lint}
   agents: [codex, claude, copilot, cursor]
   commands: {setup, test, lint, run}
   artifacts: {<path>: <status>, ...}
   first_task: ""   # optional; HANDOFF names it. Otherwise agents run `its next`
   ```
   `slug` must be lowercase letters, digits and hyphens.
3. If this project has repeatable procedures beyond the defaults (implement-task, update-plan, write-adr), write extra playbooks at `playbooks/<name>.md` with front matter including a one-sentence `description`, and a numbered procedure ending in a done-check.
4. Revise until I approve.
