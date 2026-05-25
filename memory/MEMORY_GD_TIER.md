# Memory GD Tier

GD tier means the memory subsystem is healthy enough for production recall:
indexed corpus, integrity checks passing, and bounded maintenance paths.

## Tier States

- `gd` - integrity checks pass, index coverage meets `KING_MEMORY_TIER_MIN_COVERAGE`, index state is `warm` or `empty`.
- `degraded` - failed integrity checks or coverage below the configured minimum.
- `developing` - transitional state while warming or rebuilding.

## Runtime APIs

- `Brain.verify_integrity()` - index coverage, signature, embedding shape, graph node integrity, duplicate scan.
- `Brain.tier_report()` - current tier with failed check count.
- `Brain.maintain(rebuild=False, backup=True)` - optional backup and rebuild when integrity fails.
- `Brain.system_assessment()` - full assessment including integrity and tier when `include_integrity=True`.

## Config Knobs

- `KING_MEMORY_QUERY_CACHE_SIZE` - bounded query embedding cache (default 32).
- `KING_MEMORY_REBUILD_BATCH_SIZE` - chunked embedding rebuild batch size (default 64).
- `KING_MEMORY_TIER_MIN_COVERAGE` - minimum index coverage for GD tier (default 1.0).

## Unified Model

Every stored fact is graph-backed (`storage: graph`) with a text projection for
embedding recall and display. See `memory/MEMORY_UNIFIED_MODEL.md`.
Recall merges semantic text rank, graph edge rank, and one-hop graph expansion in `Brain.recall_unified()`.

## Callable Tools

- `memory_assess` - tier and integrity report; optional maintain pass.
- `memory_recall` - unified ranked hits with optional graph paths.
- `memory_remember` - store a durable fact (text + graph + auto relations).
- `memory_forget` - remove matching memories.

## Verification

```bash
python -m unittest tests.test_memory tests.test_memory_gd -v
```
