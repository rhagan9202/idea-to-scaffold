# Stage 7 — Agent instructions and manifest

Using the approved package:

1. Write `AGENTS.md` (under ~200 lines) with exactly these sections, in order: **Project**, **Source of truth**, **Stack & commands**, **Repository layout**, **How to work**, **Conventions**, **Boundaries**, **Playbooks**. Point to the planning docs rather than repeating them. Fill in real commands for the chosen stack. Commit convention: `<type>(<scope>): <summary> [T-n.m]` with `Refs: <IDs>` in the body.
2. Write `package.yaml`:
   ```yaml
   spec_version: "0.1"
   project: {name, slug, summary, license}
   stack: {language, runtime, framework, package_manager, test, lint}
   agents: [codex, claude, copilot, cursor]
   commands: {setup, test, lint, run}
   artifacts: {<path>: <status>, ...}
   first_task: "T-1.1"
   ```
   `slug` must be lowercase letters, digits and hyphens.
3. If this project has repeatable procedures beyond the defaults (implement-task, update-plan, write-adr), write extra playbooks at `playbooks/<name>.md` with front matter including a one-sentence `description`, and a numbered procedure ending in a done-check.
4. Revise until I approve.
