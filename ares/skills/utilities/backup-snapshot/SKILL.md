---
name: backup-snapshot
description: Snapshot and verify important files or directories — copy with timestamp, checksum each file, verify integrity, report results. Use for "backup this folder", "snapshot these files", "backup with verification".
category: utilities
version: 1.0.0
---

# Backup Snapshot

## Procedure

1. **Identify targets** — Clarify what to back up. If a directory, use `list_directory` or `file_tree` to understand scope.

2. **Create backup dir** — Use `run_command` to create a timestamped backup folder:
   - `mkdir -p "backups/<name>-<YYYY-MM-DD-HHMMss>/"`

3. **Copy files** — For each target:
   - Use `copy_file` or `run_command cp -r` for directories
   - For single files, use `copy_file` directly

4. **Checksum verify** — For every copied file, run `checksum` on both source and destination and compare:
   - `checksum(path=source, algorithm="sha256")`
   - `checksum(path=dest, algorithm="sha256")`
   - Confirm they match. If any mismatch, flag it immediately.

5. **Report** — Use `write_file` to save a backup manifest alongside the backup:
   - ## Backup Manifest
   - Source: path
   - Timestamp
   - Files backed up (list with sizes)
   - Checksums verified (all match ✓)
   - Total size

6. **Tell the user** — Location of backup, number of files, total size, verification status.

## Rules
- ALWAYS checksum-verify every copied file — this is non-negotiable.
- If any checksum mismatches, report it as a failure, don't hide it.
- Skip `.git/`, `node_modules/`, `__pycache__/`, `.venv/`, `target/`, `dist/`, `build/`.
