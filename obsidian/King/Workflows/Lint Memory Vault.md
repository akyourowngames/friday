---
type: workflow
status: active
updated: 2026-05-28
---

# Lint Memory Vault

Use this to keep the Obsidian graph useful as the vault grows.

## Checks

1. Every maintained page appears in [[index]].
2. Every maintained page has at least one outbound link.
3. Important pages have backlinks from [[Home]], [[index]], or a memory hub.
4. No active page depends on a removed claim without marking it inactive.
5. [[log]] has a dated entry for each ingest, filed query, update, removal, or
   lint pass.
6. Templates do not create unresolved placeholder links.

## Fixes

- Add meaningful links rather than tag piles.
- Move retired pages under [[Removed/README|Removed Memory]].
- Split oversized pages only when a new page has a clear graph role.
- Keep the lint result in [[Health/Graph Health]] and append [[log]].
