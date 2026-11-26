# Wireframes

One file per screen, named after its screen ID (`S-home.md`). Template:

````markdown
# S-home — Home

**Requirements:** FR-001, FR-002

```text
+------------------------------------------+
| Logo                     [Search.....]   |
+------------------------------------------+
| [ + New ]                                |
|                                          |
|  Card     Card     Card                  |
+------------------------------------------+
```

**Elements:** search box, new button, card grid.
**States:** empty (show onboarding hint), loading (skeleton cards), error (retry banner).
**Interactions:** click card → S-detail.
````

PNG or SVG exports may sit next to the `.md` file and be linked from it.
