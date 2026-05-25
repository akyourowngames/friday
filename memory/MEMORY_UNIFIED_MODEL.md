# Unified Memory Model

Every durable fact is stored as **graph memory**. The text field remains as a
compatibility projection for embeddings, display, and rollback, but the graph
edge set is the authoritative memory shape.

## Storage

Each memory entry includes:

- `text` - canonical fact string projection
- `id` - stable memory id
- `graph_edges` - active edge ids tied to this memory
- `graph_nodes` - node ids touched by those edges
- `storage` - `graph`

The graph file also maintains `memory_links`: memory id to edge ids for fast
expansion. Startup repair backfills older text-only rows into graph edges and
writes the daily memory projection back with `storage: graph`.

## Ingest

1. Append the canonical text projection to the daily JSON corpus and embedding index.
2. Parse relation rules from `MEMORY_GRAPH_RELATIONS.md` into edges.
3. If no rule matches, create a fallback `User|remembers|<fact>` edge.
4. Auto-link co-mentioned entities via `MEMORY_AUTO_RELATIONS.md`.
5. Sync `graph_edges` / `graph_nodes` on the memory row.

## Recall

`Brain.recall_unified()` ranks graph-backed memories by merging:

- semantic similarity over the text projection index
- graph edge rank for the query
- one-hop expansion along active edges from matched nodes

Results are deduplicated by memory id and sorted by unified score.

## Callable Surface

- `Brain.recall_context()` - natural-language context string from graph memory
- `Brain.profile_context()` - profile-style graph context
- `memory_recall` tool - structured graph-backed hits with optional graph paths
