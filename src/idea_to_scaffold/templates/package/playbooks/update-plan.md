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

Backlog status changes and new `proposed` backlog items don't need this playbook. Everything else in `docs/planning/` does.
First decide which kind of change it is.

## Clarification: goes in with the work

A clarification makes an approved doc say more precisely what it already meant: a typo, a missing detail, an
acceptance criterion worded more exactly. It adds, removes and renames no ID, and changes no scope, priority or
architecture.

1. Edit the doc in the same branch and commit as the work that needed it. Keep its `status`; bump `version`.
2. Add `Plan: <doc> — <what changed>` to the commit body, so a reviewer sees it.
3. Run `its link .` and `its validate .` if available.

If you are not sure it is only a clarification, treat it as a plan change.

## Plan change: needs review

Anything that adds, removes or changes the meaning of an ID, or changes scope, priority or architecture.

1. State the problem in one or two sentences, citing the IDs involved (`FR-012 conflicts with ADR-0002`).
2. Edit the affected planning docs on a branch. Never renumber IDs. Mark removed items with strikethrough and a reason.
3. Set the edited docs' `status` to `review` and bump `version`. Update the docs that mention each changed ID:
   `its show <ID>` lists them (without `its`, the nav block's **Referenced by** line lists every doc that refers to this one).
4. Run `its link .` and `its validate .` if available, so links, backlinks and the planning index match.
5. If the change is structural, add an ADR with `write-adr`.
6. Open a PR (or present the diff) titled `plan: <summary>` and wait for a human to approve before building on it.

**Done check:** every changed doc is in `review`, IDs are intact, the reason is written down.
