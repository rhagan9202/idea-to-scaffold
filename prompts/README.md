# Prompt pack

Two ways to run the planning stages.

## Copy-paste (Claude or ChatGPT)

1. Open a new chat. Paste `stages/00-primer.md`.
2. Paste `stages/01-ideate.md` with your idea filled in. Answer the questions.
3. Continue through `02` … `08`, approving each stage's files ("approve") before moving on.
4. At stage 8 the model outputs every file in `<!-- file: path -->` blocks. Save the chat output (or ChatGPT/Claude's "export" Markdown) as one `.md` file.
5. Import it: `its import my-chat.md --into my-project-plan/` (Step 3 CLI).

Tip: one chat per project keeps context. If a chat gets long, start a new one, paste the primer and the approved files, and carry on from the next stage.

## Claude skill

`claude-skill/plan-project/SKILL.md` runs the same stages automatically. Build the uploadable bundle with:

```bash
python scripts/build_skill.py   # -> dist/plan-project.zip
```

Upload the zip in Claude (Settings → Capabilities → Skills), or copy `dist/plan-project/` to `~/.claude/skills/` for Claude Code. Then say "plan a new project".
