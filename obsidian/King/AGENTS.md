---
type: vault-schema
status: active
updated: 2026-05-28
---

# Vault Rules

These rules govern the Obsidian memory vault at `obsidian/King`.

## Authority

- Raw sources live under [[Raw/README|Raw Sources]] and are immutable.
- LLM-maintained summaries live under [[Sources/README|Source Summaries]].
- Durable memory pages live under [[Memory/User|User Memory]],
  [[Memory/KING|KING Project Memory]], and related memory hubs.
- Runtime recall is still owned by KING memory tools. Use [[Memory/Memory Tools]]
  before claiming a memory was added, recalled, or removed in the assistant.
- Runtime memory graph pages are generated under
  [[Generated Memory Graph/Index|Generated Runtime Memory Graph]]. Do not
  hand-edit that folder; change memory through runtime tools.

## Operating Rules

- Do not use regex.
- Do not hardcode entities, routes, or brittle phrase shortcuts.
- Do not add keyword routing or keyword lists.
- Store only stable memory that improves future responses.
- Prefer links between real pages over loose tags.
- Every maintained page should link to [[index]] or [[Home]] and at least one
  relevant memory hub.
- Never overwrite contradictions silently. Preserve old values in a history
  section or a [[Removed/README|Removed Memory]] note.
- Do not claim a runtime memory action succeeded unless a structured tool result
  proves it.

## Add Memory

Use [[Workflows/Add Memory]] when the user gives a durable fact, preference,
project constraint, or relationship. Add the runtime memory through
[[Memory/Memory Tools]] when the fact should affect assistant recall, then mirror
the fact into the vault with links. The generated runtime graph should update
automatically after the graph is persisted.

## Remove Memory

Use [[Workflows/Remove Memory]] when the user asks to forget, delete, retire, or
correct memory. If runtime memory is in scope, call the forget tool and cite the
returned result. Move vault-only claims to [[Removed/README|Removed Memory]] or
mark them inactive with a dated note. Runtime removed and superseded graph facts
appear in generated removed-memory output without active graph links.

## Query Memory

Use [[Workflows/Query Memory]] for questions about remembered facts. Read
[[index]] first, then the linked pages. When current runtime recall matters, use
[[Memory/Memory Tools]] and compose the answer from returned fields.

## Lint

Use [[Workflows/Lint Memory Vault]] periodically. Check unresolved links, orphan
pages, stale summaries, contradictions, and pages missing an index entry. Append
results to [[log]].
