# Unified Memory Model

Every durable fact is stored once as **text + graph** together. Recall uses one ranked path, not separate text-only and graph-only searches.

## Storage

Each memory entry includes:

- `text` — canonical fact string
- `id` — stable memory id
- `graph_edges` — active edge ids tied to this memory
- `graph_nodes` — node ids touched by those edges
- `storage` — `unified`

The graph file also maintains `memory_links`: memory id → edge ids for fast expansion.

## Ingest

1. Append text to the daily JSON corpus and embedding index.
2. Parse relation rules from `MEMORY_GRAPH_RELATIONS.md` into edges.
3. If no rule matches, create a fallback `User|remembers|<fact>` edge.
4. Auto-link co-mentioned entities via `MEMORY_AUTO_RELATIONS.md`.
5. Sync `graph_edges` / `graph_nodes` on the memory row.

## Recall

`Brain.recall_unified()` merges:

- semantic similarity over the text index
- graph edge rank for the query
- one-hop expansion along active edges from matched nodes

Results are deduplicated by memory id and sorted by unified score.

## Callable Surface

- `Brain.recall_context()` — natural-language context string (unified)
- `Brain.profile_context()` — profile-style unified context
- `memory_recall` tool — structured unified hits with optional graph paths
