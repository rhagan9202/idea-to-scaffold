---
id: write-adr
title: Write an ADR
description: Record an architecture decision that would be expensive to reverse, with context, options and consequences.
status: approved
version: 0.1
updated: YYYY-MM-DD
depends_on: []
---

# Playbook: write an ADR

1. Find the next number in `docs/planning/adr/` (`ADR-0004`).
2. Create `ADR-nnnn-<kebab-title>.md` with front matter (`status: draft`) and these sections:
   - **Status:** Proposed
   - **Context:** forces at play, requirements involved (IDs)
   - **Options considered:** at least two, with trade-offs
   - **Decision:** the chosen option, stated plainly
   - **Consequences:** what becomes easier, harder, or must change
3. Link the ADR from the `Decisions` section of `04-architecture.md`.
4. A human changes the status to Accepted. Until then, treat it as a proposal.
