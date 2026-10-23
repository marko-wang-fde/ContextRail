---
name: context-rail
description: Maintain and follow project-local Markdown loading chains. Use when entering or resuming a documented project, deciding which project files to read, auditing document navigation, or creating, moving, renaming, splitting, or deleting managed Markdown files. Do not use for chat history, personal memory, scheduled reviews, hooks, or ordinary source-code edits.
---

# ContextRail

Navigate project documentation from explicit entrypoints and load only the files required by the current task. Maintain verifiable links when managed Markdown changes.

## Boundaries

- Treat reading and auditing requests as read-only unless the user asks for changes.
- Never inspect chat transcripts, Codex session files, personal memory, persona files, hooks, or scheduled tasks to infer a project.
- Do not summarize, archive, or update project status merely because a document was read.
- Stop and report an ambiguous project, missing target, or unclear relationship instead of inventing a pointer.
- Do not apply this skill to ordinary source-code edits unless managed Markdown also changes or the user explicitly requests a chain audit.

## Enter or resume a project

1. Read `.context-rail.json` from the repository root. If it is absent, use `workspace/projects` as the document root and `workspace/projects/_index.md` as the entrypoint.
2. Read only the relevant configured entrypoint. With multiple entrypoints, choose from the task's project, year, client, or domain; do not preload all of them.
3. Locate the project and read its `_overview.md` first.
4. Match the task against the reasons attached to `按需下游` pointers. Follow only matching pointers, one level at a time.
5. Stop when the loaded files answer the task. `同步对象` is a write-maintenance relation, not a default read path.

## Change managed Markdown

Before creating, moving, renaming, splitting, or deleting a managed file, read [references/schema.md](references/schema.md).

- Keep one authoritative file for each fact; use pointers elsewhere instead of copied content.
- Give every non-entrypoint document at least one explicit upstream.
- On creation, add the new document's upstream and the parent's matching downstream pointer in the same change.
- On move or rename, update all inbound, outbound, and synchronization pointers in the same change. Search for the old repository-relative path afterward.
- On deletion, remove incoming pointers or redirect them only when the correct replacement is explicit.
- Child documents contain incremental detail and do not repeat general rules from their overview or index.
- Preserve unrelated user edits and existing `AGENTS.md` instructions.

After a change, run:

```bash
python3 <skill-directory>/scripts/context_rail.py check --root <repository-root>
```

Use `--strict` for migration or release validation. The validator determines structural facts only; resolve semantic ambiguity through document evidence or user input.

## Audit or visualize

For an audit, run `check` and report errors separately from warnings. Do not repair anything unless requested.

For a compact map, run:

```bash
python3 <skill-directory>/scripts/context_rail.py graph --root <repository-root>
```

Use JSON output when another tool will consume the result and DOT output when the user requests a graph visualization.

## Initialize

Run `init` only when the user asks to adopt ContextRail in a repository. Initialization never overwrites existing files. If `AGENTS.md` already exists, present the emitted routing snippet for manual integration rather than appending it automatically.
