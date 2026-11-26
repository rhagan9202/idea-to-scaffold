# Stage 4 — Architecture

Using the approved requirements:

1. Ask me about stack preferences, hosting, budget, and existing systems to integrate with. Recommend a stack that fits the requirements and a solo or small team. Say why.
2. Write `planning/04-architecture.md` with:
   - System context diagram (mermaid `flowchart`)
   - Components table (`C-name`, responsibility, tech, which FR/NFR it serves) and a component diagram
   - Data model (mermaid `erDiagram`)
   - Sequence diagrams (mermaid `sequenceDiagram`) for the 2–3 most important flows
   - Cross-cutting concerns: auth, error handling & logging, config & secrets, testing strategy, deployment
   - A Decisions section linking ADRs
3. For each significant choice (language/framework, data store, hosting, auth approach, anything hard to reverse) write an ADR at `planning/adr/ADR-nnnn-<title>.md` with Status, Context, Options considered (at least two), Decision, Consequences. Start numbering at ADR-0002 (ADR-0001 is "record architecture decisions").
4. Check that every Must FR is served by at least one component. List any gaps. Revise until I approve.
