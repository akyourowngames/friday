# KING Scheduler

This markdown is the control surface for the KING scheduler system. Edit this
file to set runtime defaults, expose new safe actions to scheduling, and
configure how scheduled items integrate with notes, memory, and the folder
watcher.

The scheduler stores items in `KING_SCHEDULER_STORE_PATH` and writes audit
entries to `KING_SCHEDULER_LOG_PATH`. Every scheduled action runs through the
KING tool registry so behavior matches an interactive call.

## Runtime

- check_interval_seconds: 30
- max_items_per_run: 50
- default_timezone: local

## Action Whitelist

- note_save
- note_update
- memory_remember
- daily_maintenance
- reminder_fire
- project_brief_fire

## Memory Linkage

- remember_on_create: true
- remember_on_complete: true
- importance_default: 0.6

## Notes Linkage

- note_on_complete: true
- note_title_prefix: "Scheduled: "
- note_tags: scheduled, scheduler

## Folder Watcher Linkage

- emit_event_on_complete: false

## Reschedule Policy

- failed_item_retry_minutes: 30
- failed_item_max_retries: 3

## Verification

- `python -m unittest tests.test_scheduler`
- `python -m maintenance.daily --dry-run`
- `python -m scheduler.cli list`
