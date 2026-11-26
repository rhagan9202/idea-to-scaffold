---
id: update-plan
title: Update the plan
description: Propose a change to the planning docs when implementation reveals a gap, conflict or better approach.
status: approved
version: 0.1
updated: YYYY-MM-DD
depends_on: []
---

# Playbook: update the plan

1. State the problem in one or two sentences, citing the IDs involved (`FR-012 conflicts with ADR-0002`).
2. Edit the affected planning docs on a branch. Never renumber IDs. Mark removed items with strikethrough and a reason.
3. Set the edited docs' `status` to `review` and bump `version`.
4. If the change is structural, add an ADR with `write-adr`.
5. Open a PR (or present the diff) titled `plan: <summary>` and wait for a human to approve before building on it.

**Done check:** every changed doc is in `review`, IDs are intact, the reason is written down.
