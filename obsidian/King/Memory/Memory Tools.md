---
type: tool-surface
status: active
updated: 2026-05-28
---

# Memory Tools

This page explains how the Obsidian vault should coordinate with KING runtime
memory. It does not create new routing behavior by itself.

## Callable Runtime Surface

- `memory_remember` stores a durable fact in runtime graph memory.
- `memory_recall` returns ranked graph-backed hits.
- `memory_forget` removes matching memories.
- `memory_assess` reports memory tier, integrity, and maintenance status.

## Proof Rules

- A vault edit proves only that the markdown projection changed.
- A runtime add, recall, forget, or maintenance claim needs a structured tool
  result.
- A sent command or assistant sentence is not proof of memory state.
- If the tool is unavailable, state the missing registry or schema evidence
  instead of claiming KING cannot remember.

## Vault Sync

- After `memory_remember`, update the relevant page under [[Memory/User]] or
  [[Memory/KING]] and append [[log]].
- After `memory_forget`, remove or retire the vault claim through
  [[Workflows/Remove Memory]].
- After `memory_assess`, file only durable findings in [[Health/Graph Health]].
