---
id: ADR-0001
title: Record architecture decisions
status: approved
version: 1.0
updated: YYYY-MM-DD
depends_on: []
---

# ADR-0001 — Record architecture decisions

**Status:** Accepted

## Context
Agents and humans need to know why the system is shaped the way it is, so they don't undo deliberate choices.

## Decision
Every decision that is expensive to reverse is recorded as an ADR in `docs/planning/adr/`, numbered sequentially and never renumbered.

## Consequences
Agents must check existing ADRs before proposing structural changes, and write a new ADR (status `Proposed`) when they recommend one.
