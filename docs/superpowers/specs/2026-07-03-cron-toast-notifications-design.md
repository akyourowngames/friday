# Cron Toast Notifications — Non-Intrusive Job Completion UX

**Date:** 2026-07-03
**Status:** Draft
**Author:** Claude (brainstorming session)

---

## Overview

When a cron job finishes while Ares is running, the result currently sits silently in `~/.ares/cron/logs/`. The user has to manually ask to see logs. This adds a non-intrusive toast notification that appears above the `❯` prompt whenever a cron job completes — staying until dismissed with Esc, never interrupting the user's input flow.

**Use case:** User is chatting with Ares while a background cron job (e.g. "check stock prices every 2 min") finishes. A small toast line appears above the prompt showing the result, without breaking the current input or conversation flow.

## Current State

- **Cron scheduler** runs as an asyncio background task, ticks every 60s
- **CronRunner.run_job()** executes jobs and writes markdown logs to `~/.ares/cron/logs/{job_id}/`
- **CLI** uses `prompt_toolkit.PromptSession.prompt_async()` for input
- **CLI does NOT use `patch_stdout()`** — so background output during prompt would corrupt user input
- **CLI has no callback mechanism** from cron completion to terminal display
- **DesktopNotifier** exists for OS-level notifications (plyer), but nothing for in-terminal

## Design Goals

1. **Non-intrusive** — appears above prompt, never breaks input or clears screen
2. **Stacking** — multiple jobs finishing shows a queue, most recent first
3. **Minimal footprint** — small single-line toast, not a full panel
4. **Graceful degradation** — if terminal doesn't support it, falls back silently (desktop notification still works)

**Note on dismiss:** Once `console.print()` outputs a line, it becomes part of terminal scrollback and cannot be removed. Toasts print once and stay visible as a history record. This is standard CLI behavior (like git hook output). The user can scroll up to review past toasts. This is intentional — the toast is a lightweight notification, not a blocking modal.

---

## Architecture

### Core Mechanism: `patch_stdout()` + Async Queue

The key insight from prompt_toolkit is `patch_stdout()` — a context manager that intercepts all `print()` calls and redirects them above the active input line. This is exactly what we need: background cron jobs can "print" their results, and prompt_toolkit handles placing them above the prompt automatically.

```
┌──────────────────────────────────────────────────┐
│  CLI Main Loop (asyncio event loop)              │
│                                                  │
│  ┌──────────────┐    ┌────────────────────────┐  │
│  │ CronScheduler│    │  CronToastManager      │  │
│  │ (tick loop)  │    │  - asyncio.Queue       │  │
│  │              │───▶│  - on_complete callback │  │
│  └──────────────┘    │  - toast rendering      │  │
│                      └──────────┬─────────────┘  │
│                                 │                 │
│  ┌──────────────┐               │                 │
│  │PromptSession │    ┌──────────▼─────────────┐  │
│  │prompt_async()│◀───│  patch_stdout()        │  │
│  │              │    │  intercepts print()    │  │
│  │  ❯ input...  │    │  places toast above    │  │
│  └──────────────┘    └────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

### Data Flow

```
CronRunner.run_job() completes
    │
    ▼
CronRunner calls on_complete callback
    │
    ▼
CronToastManager.put(job_name, summary, status)
    │
    ▼
ToastManager renders toast via Rich Text above prompt
    │
    ▼
patch_stdout() places it cleanly above ❯ without breaking input
    │
    ▼
User sees toast, presses Esc to dismiss
```

---

## Components

### 1. CronToastManager

**File:** `ares/cron/toast.py`

A simple callable class that renders a single-line toast via Rich. No queue needed — `patch_stdout()` ensures `console.print()` appears above the active prompt.

```python
from rich.console import Console
from rich.text import Text


class CronToastManager:
    """Renders non-intrusive toast notifications for cron job completions.
    
    Designed to be called as a callback from CronRunner. Uses Rich's
    console.print() which, under patch_stdout(), renders above the
    active prompt without corrupting user input.
    """

    def __init__(self, console: Console):
        self.console = console

    def __call__(self, job_name: str, summary: str, status: str, duration: float):
        """Render a toast line. Called by CronRunner on job completion."""
        icon = "✅" if status == "completed" else "❌"
        text = Text()
        text.append(f"  {icon} Cron: ", style="dim")
        text.append(job_name, style="bold cyan")
        text.append(f" — {summary[:60]}", style="dim")
        text.append(f" ({duration:.1f}s)", style="dim white")
        self.console.print(text)
```

No `ToastEvent` dataclass needed — the callback is a simple function signature `(job_name, summary, status, duration) -> None`.

### 2. CronRunner Callback

**File:** `ares/cron/runner.py` (modify existing)

Add an optional `on_complete` callback parameter to `CronRunner`. Called synchronously after writing the log — it just invokes the callable (which does `console.print()` under `patch_stdout()`).

```python
class CronRunner:
    def __init__(self, store, config=None, on_complete=None):
        self.on_complete = on_complete  # Callable[[str, str, str, float], None]
        ...

    async def run_job(self, job_id):
        ...
        # After writing log and updating state:
        if self.on_complete:
            self.on_complete(job['name'], summary, status, duration)
        ...
```

### 3. CronScheduler Callback Passthrough

**File:** `ares/cron/scheduler.py` (modify existing)

Pass the callback through from CLI to scheduler to runner:

```python
class CronScheduler:
    def __init__(self, store, runner=None, tick_seconds=60, max_concurrent=3, on_complete=None):
        self.on_complete = on_complete
        ...

    async def _run(self, jid):
        async with self.sem:
            runner = CronRunner(store=self.store, on_complete=self.on_complete)
            await runner.run_job(jid)
```

### 4. CLI Integration

**File:** `ares/cli.py` (modify existing)

Two changes:

**a) Wrap prompt in `patch_stdout()`:**

```python
from prompt_toolkit.patch_stdout import patch_stdout

async def run(self):
    ...
    with patch_stdout():
        while True:
            user_input = await self._prompt()
            ...
```

**b) Create and wire up the toast manager:**

```python
from ares.cron.toast import CronToastManager

class AresCLI:
    def __init__(self):
        ...
        self.toast_manager = CronToastManager(self.console)
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            tick_seconds=self.config.cron_tick_seconds,
            max_concurrent=self.config.cron_max_concurrent,
            on_complete=self.toast_manager,
        ) if self.config.cron_enabled else None

    async def run(self):
        ...
        with patch_stdout():
            while True:
                ...
```

### 5. Esc Key Binding

**File:** `ares/cli.py` (modify existing)

Add a key binding to dismiss toasts:

```python
from prompt_toolkit.key_binding import KeyBindings

def _create_prompt_session(self):
    kb = KeyBindings()

    @kb.add("escape")
    def dismiss_toasts(event):
        self.toast_manager.dismiss_all()

    return PromptSession(
        ...
        key_bindings=kb,
    )
```

**Note:** This is a no-op currently — once `console.print()` outputs a line, it's permanent terminal scrollback. The binding is included for future enhancement (e.g., using prompt_toolkit's display buffer to show/hide dynamic content above the prompt). For now, toasts print once and stay visible as history.

---

## Toast Visual Design

### Single job completion:

```
  ✅ Cron: stock-monitor — AAPL up 1.2%, GOOGL flat... (3.4s)
❯ _
```

### Multiple jobs (stacked, most recent first):

```
  ✅ Cron: weather-alert — Rain expected tomorrow (1.2s)
  ❌ Cron: stock-monitor — API rate limited (5.0s)
❯ _
```

### Failed job:

```
  ❌ Cron: research-daily — Agent error: timeout (10.0s)
❯ _
```

### Formatting rules:
- **No border, no panel** — just a styled text line
- **Dim by default** — `style="dim"` on most text so it doesn't scream
- **Job name highlighted** — `bold cyan` to draw the eye
- **Status icon** — ✅ for completed, ❌ for failed
- **Duration** — always shown, dim white
- **Summary** — truncated to 60 chars, dim
- **Position** — always directly above the `❯` prompt line

---

## Error Handling

1. **Terminal doesn't support `patch_stdout`** — Toast still renders via `console.print()`, just might interleave with input. Degrades gracefully.
2. **Queue overflow** — Unlikely (cron jobs are infrequent), but cap `active_toasts` at 5, drop oldest.
3. **Toast manager crashes** — Caught in CLI cleanup, doesn't affect main loop.
4. **Callback fails** — Wrapped in try/except in `CronRunner`, logged but not fatal.

---

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `ares/cron/toast.py` | **Create** | `CronToastManager`, `ToastEvent` |
| `ares/cron/runner.py` | **Modify** | Add `on_complete` callback parameter |
| `ares/cron/scheduler.py` | **Modify** | Pass `on_complete` through to runner |
| `ares/cli.py` | **Modify** | Add `patch_stdout()`, toast manager init, Esc binding |

---

## Testing

1. **Manual test:** Create a cron job that fires in 1-2 minutes, verify toast appears above prompt
2. **Esc test:** Press Esc while toast is visible, verify it disappears
3. **Input test:** Start typing while toast is visible, verify input isn't corrupted
4. **Multiple jobs:** Create two jobs that fire close together, verify stacking
5. **Failure test:** Create a job with an invalid prompt, verify ❌ toast appears
6. **Regression:** Verify normal chat flow still works identically

---

## Out of Scope

- Auto-fade / timeout dismiss (user chose manual Esc only)
- Toast in Electron desktop app (separate feature)
- Desktop notification integration with toasts (existing `DesktopNotifier` still works independently)
- Sound on toast
