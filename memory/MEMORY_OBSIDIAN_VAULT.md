# Obsidian Memory Vault

KING has an Obsidian-facing memory vault at `obsidian/King`.

The vault is a markdown projection and management surface for memory. It helps
humans and agents browse, add, update, remove, and lint memory through Obsidian
Graph view. It does not replace the runtime graph memory in `memory/brain.py`.

## Authority Split

- Runtime memory authority: `memory_remember`, `memory_recall`,
  `memory_forget`, `memory_assess`, and the graph store behind
  `memory/brain.py`.
- Vault authority: markdown pages, wiki links, `index.md`, `log.md`, and
  workflow notes under `obsidian/King`.
- Proof rule: a markdown edit proves the vault changed. A runtime memory claim
  needs a structured memory tool result.

## Vault Entry Points

- `obsidian/King/Home.md`
- `obsidian/King/index.md`
- `obsidian/King/log.md`
- `obsidian/King/AGENTS.md`
- `obsidian/King/Health/Graph Health.md`

## Maintenance Rules

- Use wiki links as graph edges.
- Keep `index.md` updated for every maintained page.
- Append `log.md` for every ingest, query file, memory update, memory removal,
  and lint pass.
- Keep raw sources immutable under `obsidian/King/Raw`.
- Put processed source summaries under `obsidian/King/Sources`.
- Put retired claims under `obsidian/King/Removed` or mark them inactive with a
  dated history entry.
- Avoid regex, hardcoded memory routing, keyword lists, and phrase-match
  shortcuts.

## Integration With Runtime Graph Memory

When adding a durable fact, first decide whether it should affect assistant
recall. If yes, use the runtime remember tool and then mirror the result into
the vault. If no, keep it as a vault-only note and say so.

When removing a durable fact, use the runtime forget tool if the runtime memory
is in scope. Then retire the vault note and update links so Obsidian Graph view
does not keep a removed claim prominent.

When querying memory, read the vault index first, follow linked pages, and use
runtime recall when current assistant memory matters.

## Generated Runtime Graph

KING also writes the live runtime graph into
`obsidian/King/Generated Memory Graph` when graph memory is persisted. This
generated folder is owned by the runtime and should not be hand-edited.

Config:

- `KING_MEMORY_OBSIDIAN_SYNC_ENABLED`
- `KING_MEMORY_OBSIDIAN_VAULT_DIR`
- `KING_MEMORY_OBSIDIAN_GRAPH_DIR`

Generated pages:

- `Index.md` - generated graph entry point.
- `Nodes/` - one page per active graph node.
- `Edges/` - one page per active graph edge.
- `Memories/` - one page per active memory attached to active edges.
- `Removed Memory.md` - inactive or superseded graph facts as plain text, so
  removed memory stays auditable without staying active in Obsidian Graph view.
