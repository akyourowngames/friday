---
type: memory-page
status: active
updated: 2026-05-28
---

# KING Project Memory

KING is a local-first assistant runtime with graph memory, semantic tool
routing, structured tool results, markdown-governed behavior, and verification
before shipping.

## Current Memory Surfaces

- Runtime memory implementation: `memory/brain.py`.
- Runtime model notes: [[Memory/Graph Memory]] and `memory/MEMORY_UNIFIED_MODEL.md`.
- Tool-facing memory operations: [[Memory/Memory Tools]] and `tools/TOOL_MANIFEST.md`.
- Visual memory workspace: [[Home|KING Memory Vault]].

## Project Rules

- Keep upgrades additive and backward compatible.
- Start tool-exposure fixes from markdown manifests and policy files.
- Use structured tool results for user-facing claims.
- Keep Obsidian as a graph-friendly management surface, not as a hidden
  replacement for runtime memory.

## Linked Workflows

- [[Workflows/Add Memory]]
- [[Workflows/Update Memory]]
- [[Workflows/Remove Memory]]
- [[Workflows/Lint Memory Vault]]
