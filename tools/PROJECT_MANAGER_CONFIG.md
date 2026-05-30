# KING Project Manager

This markdown is the control surface for KING's autonomous project manager. Edit
this file to tune trigger thresholds, scoring weights, and which triggers are
live. Nothing here is a keyword table or phrase shortcut: intent parsing is done
by the language model, and triggers fire from computed project state compared
against the thresholds below.

Projects are stored as JSON in `KING_PROJECT_STORE_PATH`. Each project carries
its tasks, updates, blockers, decisions, a rolling health score, momentum, and
the alerts the trigger engine has raised. The night audit recomputes all of this
and is wired through the daily maintenance routine (`project_audit` step).

## Runtime

- history_keep_points: 60
- updates_keep: 200
- alerts_keep: 40
- archive_keep: 50

## Scoring

- momentum_window_days: 7
- velocity_window_days: 14
- health_blocker_penalty: 12
- health_overdue_penalty: 25
- health_stall_penalty: 20
- health_sentiment_penalty: 18
- health_scope_penalty: 10
- momentum_close_weight: 0.6
- momentum_update_weight: 0.4
- momentum_expected_per_week: 4

## Triggers

- inactivity_enabled: true
- inactivity_days: 4
- velocity_collapse_enabled: true
- velocity_collapse_ratio: 0.35
- deadline_proximity_enabled: true
- deadline_warn_days: 10
- blocker_age_enabled: true
- blocker_age_days: 3
- health_drop_enabled: true
- health_drop_points: 15
- health_drop_window_hours: 48
- scope_expansion_enabled: true
- scope_growth_ratio: 1.25
- cross_project_conflict_enabled: true
- conflict_window_days: 3
- sentiment_deterioration_enabled: true
- sentiment_streak: 3
- ghost_detection_enabled: true
- ghost_days: 7
- ghost_max_updates: 1

## Status Thresholds

- stalling_health_below: 60
- ghost_health_below: 35

## Intake

- infer_tasks_min: 3
- infer_tasks_max: 5
- intake_max_tokens: 700
- intake_retries: 1

## Brief

- worry_health_below: 55
- focus_top_n: 3
- push_desktop_notification: true

## Integrations

- github_correlation_enabled: false
- gmail_correlation_enabled: false

## Obsidian Export

- enabled: true
- subfolder: Projects
- include_archived: true
- context_brief: true

## Verification

- `python -m unittest tests.test_project_manager`
- `python -m maintenance.daily --dry-run`
