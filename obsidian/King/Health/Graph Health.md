---
type: health-page
status: active
updated: 2026-05-28
---

# Graph Health

This page tracks whether the Obsidian vault is still easy to browse and safe to
use as a memory management surface.

## Current Baseline

- Graph plugin is enabled in `.obsidian/core-plugins.json`.
- Workspace opens on Obsidian Graph view.
- [[Home]] links to all primary hubs and workflows.
- [[index]] lists all shipped vault pages.
- [[log]] records setup and future maintenance events.
- [[Generated Memory Graph/Index|Generated Runtime Memory Graph]] is regenerated
  from KING runtime memory.

## Health Checklist

- No unresolved links in active pages.
- No isolated maintained pages.
- Removed claims are not active graph hubs.
- Memory pages link to relevant workflows.
- Workflow pages link to the runtime proof rules in [[Memory/Memory Tools]].

## Last Lint

- 2026-05-28: Vault scaffold created for graph-ready memory management.
- 2026-05-28: Obsidian JSON parsed, 21 markdown files had 125 wiki links with
  0 unresolved links, and the repo-owned verification pipeline reported
  `Status: success` with `Ship decision: ship`.
- 2026-05-28: Runtime memory graph sync generated 24 active nodes, 40 active
  edges, 19 active memories, and 90 generated files. Vault link check found 111
  markdown files, 709 wiki links, and 0 unresolved links. The repo-owned
  verification pipeline reported `Status: success` with `Ship decision: ship`.
