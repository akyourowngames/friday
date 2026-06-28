# Cron Jobs — Scheduled Recurring Task Execution

## Overview

Ares gets cron jobs — the ability to run agent tasks on a recurring schedule. The agent creates, manages, and monitors its own scheduled jobs via natural language. Each run is a fresh agent session that reads the previous run's log for continuity, then produces a new markdown log.

**Use cases:** Monitor stock prices daily, track Instagram followers, run periodic research, generate daily reports, watch for changes — anything Ares's existing tools can do, but on autopilot.

## Architecture

### Storage

**Jobs:** JSON file at `~/.ares/cron/jobs.json`. Atomic writes via write-to-temp-then-rename to prevent corruption on crash.

```json
{
  "jobs": {
    "job_stock_monitor": {
      "id": "job_stock_monitor",
      "name": "Stock Price Monitor",
      "prompt": "Check current stock prices for AAPL, GOOGL, MSFT and prepare a summary report as markdown. Save the report to ~/reports/stocks/. Compare with the previous run's prices and note any significant changes.",
      "cron": "0 9 * * 1-5",
      "timezone": "Asia/Kolkata",
      "created_at": "2026-06-28T14:00:00Z",
      "enabled": true,
      "state": "scheduled",
      "next_run_at": "2026-06-30T03:30:00Z",
      "last_run_at": null,
      "run_count": 0,
      "last_status": null,
      "max_iterations": 10,
      "output_dir": "~/.ares/cron/logs/job_stock_monitor"
    }
  }
}
```

**Logs:** Per-run markdown at `~/.ares/cron/logs/{job_id}/{timestamp}.md`

```markdown
# Cron Run: Stock Price Monitor
**Job:** job_stock_monitor
**Run:** 2026-06-28T09:00:00Z
**Status:** completed
**Duration:** 45s
**Tokens:** 2,340

## Previous Run Summary
Last run completed at 2026-06-27T09:00:00Z. AAPL was $198.50, GOOGL was $175.20, MSFT was $420.10. Overall market was up 0.5%.

## Prompt
Check current stock prices...

## Agent Output
### Current Prices
- **AAPL**: $199.20 (+0.35%)
- **GOOGL**: $176.80 (+0.91%)
...

## Summary
Market up 1.2% day-over-day. AAPL continues 3-day rally...

## Tools Used
- web_search (2 calls)
- write_file (1 call)

## Run Metadata
- Model: mimo-v2.5-free
- Iterations: 3/20
```

### Execution Engine

**Scheduler:** Asyncio background task, ticks every 60 seconds. Starts when Ares CLI boots, stops on shutdown.

**Execution flow per job:**
1. Lock the job (`state: "running"`) with atomic JSON write
2. Read previous run log — extract "Previous Run Summary" section and prepend to prompt
3. Create fresh `Agent` instance — no conversation history, only the job's prompt
4. Run `agent.run_stream(prompt)` with iteration limit (default 10, configurable per job)
5. Write full markdown log with status, duration, tokens, summary
6. Update jobs.json — `state: "scheduled"`, `next_run_at`, `last_run_at`, `run_count`, `last_status`

**Concurrency:** Multiple jobs run in parallel via separate asyncio tasks. `max_concurrent` (default 3) limits resource use.

**Overlap:** If a job is still running when the next tick fires, it queues behind the current run. When the running job finishes, it checks if `next_run_at` has passed and runs immediately if so.

**Startup recovery:** On boot, any jobs with `next_run_at` in the past run immediately, ordered by how overdue they are.

**Failure handling:** On error — log the error with traceback, set `last_status: "failed"`, send desktop notification. No retry. Next scheduled run happens normally.

**Previous log extraction:** The runner reads the most recent log file for the job (sorted by filename timestamp). It extracts the `## Summary` section (everything between `## Summary` and the next `##` heading or end of file) and prepends it as the `## Previous Run Summary` section in the new run's prompt. If no previous log exists, this section is omitted.

### Anti-Recursion

Cron-run agent sessions have cron tools **disabled**. The agent can use web_search, run_code, write_file, and all other tools — but cannot create, edit, or delete cron jobs. Prevents runaway scheduling loops.

**Implementation:** The `Agent` class gets an `is_cron_session: bool = False` parameter. When `True`, the `ToolExecutor` filters cron tools (`create_cron_job`, `list_cron_jobs`, `get_cron_job`, `update_cron_job`, `delete_cron_job`, `run_cron_job_now`, `get_cron_logs`) from the tool definitions before sending to the LLM. The agent simply never sees these tools.

## Agent Tools

7 new tools, available in interactive sessions only (disabled in cron sessions):

| Tool | Description | Key Params |
|------|-------------|------------|
| `create_cron_job` | Create a scheduled job | name, prompt, cron, timezone |
| `list_cron_jobs` | List all jobs with status | include_disabled |
| `get_cron_job` | One job detail + recent runs | job_id |
| `update_cron_job` | Modify job fields | job_id + any field |
| `delete_cron_job` | Remove a job (logs kept) | job_id |
| `run_cron_job_now` | Trigger immediate execution | job_id |
| `get_cron_logs` | Read recent run logs | job_id, limit |

### Natural Language → Cron Mapping

The agent converts natural language to cron expressions:

- "every day at 9am" → `0 9 * * *`
- "every weekday at 9:30am" → `30 9 * * 1-5`
- "every hour" → `0 * * * *`
- "every 5 minutes" → `*/5 * * * *`
- "every monday at 10am" → `0 10 * * 1`
- "every 1st of the month" → `0 9 1 * *`

If the expression is ambiguous, the agent asks for clarification.

## Module Structure

```
ares/
├── cron/
│   ├── __init__.py          # Exports CronScheduler, CronStore
│   ├── store.py             # CronStore — JSON read/write, job CRUD
│   ├── scheduler.py         # CronScheduler — asyncio tick loop
│   ├── runner.py            # CronRunner — fresh Agent, run, save log
│   └── schedule_utils.py    # parse_natural_schedule(), cron validation
├── tools/
│   ├── cron_tools.py        # 7 cron tool handlers
│   └── definitions.py       # +7 new tool definitions
├── prompts.py               # +Scheduled Jobs system prompt section
├── agent.py                 # is_cron_session flag
└── cli.py                   # Start/stop scheduler
```

## Disk Layout

```
~/.ares/
├── config.json
├── data/
│   ├── soul.md
│   └── profile.md
└── cron/
    ├── jobs.json
    └── logs/
        ├── job-stock-monitor/
        │   ├── 2026-06-28T09-00-00Z.md
        │   └── 2026-06-29T09-00-00Z.md
        └── job-insta-monitor/
            └── 2026-06-28T10-00-00Z.md
```

## Config Additions

New fields in `AppConfig`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cron_enabled` | bool | `True` | Enable/disable cron system |
| `cron_tick_seconds` | int | `60` | Scheduler tick interval |
| `cron_max_concurrent` | int | `3` | Max parallel job runs |
| `cron_max_iterations` | int | `10` | Default agent iterations per run |
| `cron_log_retention_days` | int | `90` | Auto-cleanup old logs |

## System Prompt Addition

New section in system prompt under "Scheduled Jobs":

- How to create/manage cron jobs via the tools
- How to read previous run logs and summarize them
- Anti-recursion rule: cron sessions cannot create/edit/delete cron jobs
- How to construct self-contained prompts for cron jobs

## Scope

**In scope:**
- Recurring jobs with cron expressions
- Natural language → cron conversion
- Fresh agent sessions per run
- Per-run markdown logs with previous run summary
- Agent self-management of jobs (create, list, update, delete, run now)
- Desktop notifications on failure
- Log retention / cleanup
- Anti-recursion guards

**Out of scope (future):**
- One-shot / delayed jobs
- Polling/watch jobs
- Webhook triggers
- Distributed execution
- Web UI for job management
