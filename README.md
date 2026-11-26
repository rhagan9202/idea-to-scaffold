# idea-to-scaffold

A reproducible workflow for starting agent-assisted software projects:

1. **Ideate** — flesh out an idea with Claude or ChatGPT.
2. **Plan** — expand it into features, requirements, architecture, design, wireframes and a project plan.
3. **Package** — capture everything as a standard [planning package](spec/PACKAGE_SPEC.md) of Markdown files, `AGENTS.md` and playbooks.
4. **Scaffold** — generate the repository, wire the package in for Codex, Claude Code, Copilot and Cursor, `git init`, make the first commit, and write a handoff brief.

## Status

| Step | What | State |
| ---- | ---- | ----- |
| 1 | Planning package spec + templates (`spec/`, `templates/package/`) | draft |
| 2 | Ideation-to-plan prompt pack + Claude skill (`prompts/`) | not started |
| 3 | Python scaffolder CLI (`src/`) | not started |
| 4 | End-to-end example (`examples/`) | not started |

## Layout

```text
spec/PACKAGE_SPEC.md     the planning package contract
templates/package/       blank package to copy or fill via prompts
```
