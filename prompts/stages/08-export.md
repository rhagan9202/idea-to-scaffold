# Stage 8 — Final check and export

1. Run a consistency check across all files and report, as a short list:
   - IDs referenced but never defined, or defined twice
   - features without a goal, FRs without a feature, backlog items that satisfy nothing
   - Must FRs not covered by any backlog item
   - task lists anywhere other than `07-backlog.md`
   - files still in `draft` or `review`
2. Fix anything I agree to fix.
3. Then output **every file in the package**, in this order, each wrapped in the `<!-- file: ... -->` / `<!-- end file -->` format from the primer: `package.yaml`, `AGENTS.md`, `planning/00-idea.md` … `planning/08-glossary.md`, every wireframe, every ADR, every extra playbook. Output nothing between files.
4. If you run out of space, stop at a file boundary and say "Type **continue** for the rest."

I will save this output as one Markdown file and import it with `its import`.
