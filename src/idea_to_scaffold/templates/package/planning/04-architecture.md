---
id: architecture
title: Architecture
status: draft
version: 0.1
updated: YYYY-MM-DD
depends_on: [requirements]
---

# Architecture

## System context
```mermaid
flowchart LR
  user([User]) --> app[<Project Name>]
  app --> ext[(External service)]
```

## Components
| ID      | Responsibility | Tech | Serves |
| ------- | -------------- | ---- | ------ |
| C-api   |                |      | FR-001 |

```mermaid
flowchart TB
  subgraph App
    ui[C-ui] --> api[C-api]
    api --> db[(C-db)]
  end
```

## Data model
```mermaid
erDiagram
  ENTITY {
    string id PK
  }
```

## Key flows
```mermaid
sequenceDiagram
  actor U as User
  participant A as C-api
  U->>A: request
  A-->>U: response
```

## Cross-cutting concerns
- **AuthN/AuthZ:** 
- **Error handling & logging:** 
- **Configuration & secrets:** 
- **Testing strategy:** 
- **Deployment:** 

## Decisions
<!-- Link ADRs: ADR-0001 ... -->
