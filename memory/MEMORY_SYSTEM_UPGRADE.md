# Memory System Upgrade Project

## Scope

This project upgrades the repository's software memory subsystem in `memory/brain.py`.
The original request described a hardware RAM refresh project; this document translates that structure into the equivalent software-memory upgrade plan for the current codebase.

## System Assessment

### Baseline Findings

Before the upgrade, the memory subsystem had these bottlenecks and operational gaps:

- `recall()` re-embedded the full memory corpus on every query, making read latency scale linearly with total stored memories.
- Persistence rewrote daily JSON memory files without backup or rollback safeguards.
- There was no persistent embedding index, so startup and recall behavior could be inconsistent after memory growth.
- Capacity management was undefined, so long-running usage could grow storage without archival policy.
- Validation covered filtering and contradiction handling, but not index migration, archival behavior, or benchmark reporting.

### Compatibility Constraints

- Existing memory data is stored in daily files under `storage/memories/memory_YYYY-MM-DD.json`.
- Existing callers expect `Brain.commit()` and `Brain.recall()` to preserve their public behavior.
- The system must continue to operate even when only legacy JSON files are present.

## Implemented Upgrade

### Core Changes

- Added a persistent embedding index stored beside the daily memory files.
- Added automatic migration from legacy JSON-only storage to indexed storage.
- Added backup creation before index migration or rebuild.
- Added atomic file writes for JSON and NumPy index files to reduce corruption risk.
- Added capacity controls with archival of evicted memories.
- Added system assessment and benchmark methods for validation and documentation.

### Storage Layout

The upgraded memory system now uses:

- Daily source files: `storage/memories/memory_YYYY-MM-DD.json`
- Index metadata: `storage/memories/memory_index.json`
- Embedding matrix: `storage/memories/memory_embeddings.npy`
- Archived evictions/removals: `storage/memories/memory_archive.jsonl`
- Backup snapshots: `storage/memory_backups/`

### Configuration Controls

New settings in `config.py`:

- `KING_MEMORY_BACKUP_DIR`
- `KING_MEMORY_INDEX_FILE`
- `KING_MEMORY_EMBEDDINGS_FILE`
- `KING_MEMORY_ARCHIVE_FILE`
- `KING_MEMORY_MAX_ENTRIES`

## Migration Plan

### Pre-Migration Backup

The upgrade creates a backup snapshot before rebuilding or migrating the index when legacy memory files exist.

Backup contents include:

- all daily memory JSON files
- index metadata if present
- embedding matrix if present
- archive file if present
- backup manifest with timestamp and copied file count

### Downtime Approach

This migration is low-downtime:

- legacy memory files remain the source of truth
- migration only adds index artifacts
- public APIs remain stable during the upgrade

### Rollback Protocol

If the index is corrupted or incompatible:

1. Stop the application.
2. Restore the latest backup snapshot from `storage/memory_backups/`.
3. Remove the current `memory_index.json` and `memory_embeddings.npy` if needed.
4. Restart the application and let the system rebuild from restored daily memory files.

## Installation Equivalent

For a software subsystem, the equivalent of physical installation is safe activation of the new index and persistence layer:

- load legacy daily memories
- verify signature coverage against the current corpus
- attach or rebuild the embedding index
- persist index metadata and embedding matrix atomically

## Validation Plan

### Required Checks

Post-upgrade validation includes:

- index file generation verification
- index coverage verification against total memory count
- successful legacy migration from daily JSON files
- contradiction removal and duplicate protection checks
- archive generation when capacity trimming occurs
- recall benchmark timing report

### Automated Evidence

Targeted test coverage was added in `tests/test_grounding.py` for:

- persistent index creation
- assessment coverage reporting
- capacity trimming with archive logging
- benchmark reporting

### Suggested Runtime Validation

Run these checks after deployment:

```bash
python -m unittest tests.test_grounding
```

Optional runtime inspection from Python:

```python
from memory.brain import Brain

brain = Brain()
print(brain.system_assessment())
print(brain.benchmark_recall("where does the user live", runs=25))
```

## Documentation Of Changes

### Old vs New

Old system:

- daily JSON persistence only
- no persistent embedding index
- full-corpus embedding on every recall
- no backup-aware migration
- no archival policy

New system:

- daily JSON persistence plus persistent index
- incremental embedding append on commit
- automatic index rebuild when state drifts
- backup snapshots before migration/rebuild
- archival log for trimmed or contradicted memories
- assessment and benchmark reporting

## Success Criteria Mapping

The original request's hardware success criteria have been translated into software-memory equivalents:

- 100% memory recognition:
  index coverage ratio is `1.0` and indexed count matches stored memory count
- 24-hour zero-error stress testing:
  targeted automated tests pass and extended runtime monitoring can run without index corruption or recall failures
- 30% improvement in memory-bound workloads:
  recall latency should improve materially because the system no longer re-embeds the full corpus on every query
- 7-day stability:
  monitor backup creation, archive growth, recall results, and index rebuild frequency during normal use
- complete repository documentation:
  this document records the assessment, migration plan, rollback strategy, validation steps, and storage changes

## Monitoring Recommendations

For the 7-day monitoring period, track:

- unexpected index rebuilds
- backup snapshot creation frequency
- archive growth due to capacity trimming
- recall latency drift
- mismatch between `entry_count` and `indexed_count`

## Central Repository Location

Project documentation is stored in:

- `memory/MEMORY_SYSTEM_UPGRADE.md`
