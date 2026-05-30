# KING Memory Consolidation

This markdown documents KING's nightly memory consolidation worker — the process
that makes memory smarter over time instead of just larger. It runs in the
nightly maintenance pass (`memory_consolidate` step), never on an interactive
turn, so it never adds latency to a conversation.

Consolidation has three jobs, each independently toggleable and tuned by the
settings below (read from environment via `config.py`; this file is the
human-facing contract). Nothing here is a keyword table: duplicate detection is
embedding-similarity + one LLM judgement, and insight distillation reads
clustered facts and returns a higher-order fact. No regex, no phrase matching.

## Jobs

- dedup: find near-duplicate memories by embedding similarity, ask the model
  whether a candidate pair is truly the same fact, and merge the pair into the
  single best-phrased memory (keeping the higher importance and earliest date).
- insights: cluster related memories (shared graph nodes) and distill each
  sizable cluster into one higher-order insight memory, stored at insight
  importance so it surfaces in recall and profile context.
- decay: gently lower the importance of stale, low-value memories that have not
  been reinforced, so recall stays sharp and capacity trimming removes the right
  things. Decay never touches high-importance or graph-backed identity facts.

## Settings

These map to `KING_MEMORY_CONSOLIDATION_*` environment variables (defaults shown):

- enabled: true
- dedup_enabled: true
- dedup_similarity: 0.86
- dedup_max_pairs: 20
- insights_enabled: true
- insight_min_cluster: 3
- insight_max: 5
- insight_importance: 0.75
- decay_enabled: true
- decay_after_days: 45
- decay_rate: 0.05
- decay_floor: 0.1
- max_tokens: 600

## Safety

- Consolidation only ever runs against the live brain in the nightly pass and is
  skipped entirely when the vault path is a temp directory (test guard) or no API
  key is configured for the LLM-backed steps.
- Merges and insight writes go through the brain's normal commit/remove paths, so
  the graph, embeddings, vector store, and Obsidian vault stay consistent.
- Decay only reduces importance; it never deletes. Capacity trimming (existing
  behavior) is what eventually removes the lowest-value memories.

## Verification

- `python -m unittest tests.test_memory_consolidation`
- `python -m maintenance.daily --dry-run`
