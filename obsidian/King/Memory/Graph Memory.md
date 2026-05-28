---
type: memory-model
status: active
updated: 2026-05-28
---

# Graph Memory Model

KING stores durable runtime memory as graph-backed facts. The Obsidian vault is
a readable projection for humans and agents, using wiki links as visible graph
edges.

## Runtime Shape

- A memory has a text projection for display and embedding recall.
- Relation rules create graph edges from stable facts.
- Auto-relations connect co-mentioned entities when the graph already contains
  enough evidence.
- Recall blends semantic rank, graph rank, and bounded graph expansion.

## Vault Shape

- Pages are nodes.
- Wiki links are visible edges in Obsidian Graph view.
- [[index]] is the content catalog.
- [[log]] is the chronological audit trail.
- [[Removed/README|Removed Memory]] keeps retired claims visible without making
  them active.

## Contradictions

Do not silently overwrite active facts. For runtime memory, use
[[Memory/Memory Tools]] and require the returned fields to prove the change. For
vault pages, keep a dated history entry and link to the retired claim.

## Related Pages

- [[Memory/User]]
- [[Memory/KING]]
- [[Workflows/Update Memory]]
- [[Health/Graph Health]]
