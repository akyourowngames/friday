# KING Daily Maintenance

This markdown is the control surface for KING's nightly self-maintenance routine.
Edit this file to change the cutoff time, enable or disable steps, set retention,
and configure scheduler-system links.

The runtime entrypoint is `python -m maintenance.daily`. The folder watcher and
telegram watcher services also read this file when their in-process schedulers
fire.

The same routine is safe to run on demand: it is idempotent for the current day
because the engine writes a "last run" stamp into
`KING_MAINTENANCE_STATE_PATH`.

## Runtime

- cutoff_time: 03:30
- timezone: local
- min_run_interval_minutes: 60
- log_max_runs: 90
- enabled: true

## Steps

- memory_daily: enabled=true label=daily
- folder_scan: enabled=true include_summarize_pending=false
- telegram_summary: enabled=true include_stats=true
- scheduler_due: enabled=true horizon_minutes=1440

## Step Notes

- `memory_daily` calls `Brain.daily_maintenance(label)` which runs backup,
  rebuild, reflect, and Obsidian sync without touching agent core.
- `folder_scan` calls `IngestPipeline.daily_maintenance()` which scans the
  watched root, reconciles deleted paths, refreshes the audio playlist, and
  records the latest stats.
- `telegram_summary` posts a single status line to authorized chat ids when
  the telegram watcher service is reachable. It does nothing when no chat is
  authorized.
- `scheduler_due` asks the scheduler engine for due items inside the horizon and
  runs only items whose action is in `Action Whitelist` below. New scheduler
  entries default to inert until their action is whitelisted.

## Action Whitelist

- note_save
- note_update
- memory_remember
- daily_maintenance

## Retention

- memory_backup_keep_count: 14
- maintenance_log_keep_runs: 90
- folder_watcher_event_keep_days: 30

## Verification

- `python -m unittest tests.test_daily_maintenance`
- `python -m maintenance.daily --dry-run`
- `python -m maintenance.daily --status`
