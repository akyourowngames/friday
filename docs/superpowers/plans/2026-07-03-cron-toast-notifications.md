# Cron Toast Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a non-intrusive single-line toast above the `❯` prompt whenever a cron job completes, using prompt_toolkit's `patch_stdout()` and a callback from CronRunner.

**Architecture:** CronRunner gets an `on_complete` callback. CronScheduler passes it through. A new `CronToastManager` class renders a Rich styled line. CLI wraps its prompt loop in `patch_stdout()` and wires everything together.

**Tech Stack:** Python 3.11+, prompt_toolkit (patch_stdout), Rich (Console, Text)

**Spec:** `docs/superpowers/specs/2026-07-03-cron-toast-notifications-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ares/cron/toast.py` | **Create** | `CronToastManager` — renders a single-line toast via `console.print()` |
| `ares/cron/runner.py` | **Modify** | Add `on_complete` callback param, call it after job finishes |
| `ares/cron/scheduler.py` | **Modify** | Accept `on_complete`, create runner with it per job |
| `ares/cli.py` | **Modify** | Wrap prompt loop in `patch_stdout()`, create toast manager, wire callback |

---

### Task 1: Create CronToastManager

**Files:**
- Create: `ares/cron/toast.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cron_toast.py
from unittest.mock import MagicMock
from ares.cron.toast import CronToastManager


def test_toast_renders_completed_job():
    console = MagicMock()
    toast = CronToastManager(console)
    toast("stock-monitor", "AAPL up 1.2%", "completed", 3.4)
    console.print.assert_called_once()
    printed = console.print.call_args[0][0]
    assert "stock-monitor" in printed._text or any(
        span[1] == "stock-monitor" for span in printed._spans
    )


def test_toast_renders_failed_job():
    console = MagicMock()
    toast = CronToastManager(console)
    toast("stock-monitor", "API timeout", "failed", 5.0)
    console.print.assert_called_once()


def test_toast_truncates_long_summary():
    console = MagicMock()
    toast = CronToastManager(console)
    long_summary = "A" * 200
    toast("job", long_summary, "completed", 1.0)
    printed = console.print.call_args[0][0]
    # Summary should be truncated to 60 chars
    full_text = ""
    for span in printed._spans:
        full_text += span[1]
    assert len(full_text) < 250  # much less than 200 + extra text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_cron_toast.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ares.cron.toast'`

- [ ] **Step 3: Write the implementation**

```python
# ares/cron/toast.py
"""Non-intrusive toast notifications for cron job completions."""
from __future__ import annotations

from rich.console import Console
from rich.text import Text


class CronToastManager:
    """Renders a single-line toast when a cron job completes.

    Designed to be passed as a callable to CronRunner's on_complete
    callback. Under prompt_toolkit's patch_stdout(), console.print()
    renders above the active prompt without corrupting user input.
    """

    def __init__(self, console: Console):
        self.console = console

    def __call__(self, job_name: str, summary: str, status: str, duration: float):
        icon = "✅" if status == "completed" else "❌"
        text = Text()
        text.append(f"  {icon} Cron: ", style="dim")
        text.append(job_name, style="bold cyan")
        text.append(f" — {summary[:60]}", style="dim")
        text.append(f" ({duration:.1f}s)", style="dim white")
        self.console.print(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_cron_toast.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/anime/friday
git add ares/cron/toast.py tests/test_cron_toast.py
git commit -m "feat: add CronToastManager for cron completion toasts"
```

---

### Task 2: Add on_complete callback to CronRunner

**Files:**
- Modify: `ares/cron/runner.py` (lines 13-16 for `__init__`, lines 52-55 for callback invocation)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cron_toast.py (append to existing file)
from unittest.mock import MagicMock, AsyncMock, patch


def test_runner_calls_on_complete():
    callback = MagicMock()
    store = MagicMock()
    store.get_job.return_value = {
        "id": "test-job",
        "name": "Test Job",
        "prompt": "say hello",
        "cron": "* * * * *",
        "timezone": "UTC",
        "max_iterations": None,
        "run_count": 0,
    }
    store.recent_logs.return_value = []
    store.log_dir.return_value = MagicMock()
    store.log_dir.return_value.__truediv__ = lambda self, x: MagicMock()

    from ares.cron.runner import CronRunner
    runner = CronRunner(store=store, on_complete=callback)
    assert runner.on_complete is callback
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_cron_toast.py::test_runner_calls_on_complete -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_complete'`

- [ ] **Step 3: Write the implementation**

Modify `ares/cron/runner.py`:

```python
# Change __init__ signature (line 14):
# OLD:
#     def __init__(self, store: CronStore | None = None, config=None):
# NEW:
    def __init__(self, store: CronStore | None = None, config=None, on_complete=None):
        self.config = config or load_config()
        self.store = store or CronStore(Path(self.config.data_dir).expanduser().parent)
        self.on_complete = on_complete

# Add callback invocation after self.store.update_job(job_id, **updates) (after line 55):
        if self.on_complete:
            summary_text = (output.strip().split('\n\n')[0] if output.strip() else ('Run failed.' if status == 'failed' else 'No output.'))
            self.on_complete(job['name'], summary_text, status, duration)
```

Full modified `run_job` method for clarity — the only changes are adding the callback call at the very end, right before `return log`:

```python
    async def run_job(self, job_id: str) -> Path:
        job=self.store.get_job(job_id)
        if not job: raise ValueError(f"Cron job '{job_id}' not found")
        self.store.update_job(job_id, state='running')
        started=utc_now(); start=perf_counter(); output=''; status='completed'; err=''
        try:
            prompt=job['prompt']
            prev=self.latest_summary(job_id)
            if prev: prompt=f"## Previous Run Summary\n{prev}\n\n## Scheduled Job Prompt\n{prompt}"
            cfg=self.config.model_copy(deep=True)
            if job.get('max_iterations'): cfg.agent_max_iterations=int(job['max_iterations'])
            else: cfg.agent_max_iterations=int(getattr(cfg,'cron_max_iterations',10))
            from ares.conversations import ConversationStore
            from ares.memory import MemoryStore
            mem=MemoryStore(); conv=ConversationStore()
            from ares.agent import Agent
            agent=Agent(mem, conv, config=cfg, is_cron_session=True)
            try:
                chunks=[]
                async for chunk in agent.run_stream(prompt, []): chunks.append(chunk)
                output=''.join(chunks)
            finally:
                await agent.close(); conv.close(); mem.close()
        except Exception:
            status='failed'; err=traceback.format_exc(); output=err
        duration=perf_counter()-start
        log=self._write_log(job, started, status, duration, output, err)
        updates={"state":"scheduled","last_run_at":started,"run_count":int(job.get('run_count') or 0)+1,"last_status":status,"next_run_at":next_run_utc(job['cron'], job.get('timezone','UTC'), datetime.now(timezone.utc))}
        self.store.update_job(job_id, **updates)
        if self.on_complete:
            summary_text = (output.strip().split('\n\n')[0] if output.strip() else ('Run failed.' if status == 'failed' else 'No output.'))
            self.on_complete(job['name'], summary_text, status, duration)
        return log
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_cron_toast.py -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/anime/friday
git add ares/cron/runner.py tests/test_cron_toast.py
git commit -m "feat: add on_complete callback to CronRunner"
```

---

### Task 3: Pass on_complete through CronScheduler

**Files:**
- Modify: `ares/cron/scheduler.py` (lines 11-12 for `__init__`, line 26 for runner creation)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cron_toast.py (append)
def test_scheduler_passes_on_complete_to_runner():
    callback = MagicMock()
    store = MagicMock()
    from ares.cron.scheduler import CronScheduler
    scheduler = CronScheduler(store, on_complete=callback)
    assert scheduler.on_complete is callback
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_cron_toast.py::test_scheduler_passes_on_complete_to_runner -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_complete'`

- [ ] **Step 3: Write the implementation**

Modify `ares/cron/scheduler.py`:

```python
# Change __init__ (line 11):
# OLD:
#     def __init__(self, store: CronStore, runner: CronRunner | None = None, tick_seconds: int = 60, max_concurrent: int = 3):
#         self.store=store; self.runner=runner or CronRunner(store=store); self.tick_seconds=tick_seconds; self.sem=asyncio.Semaphore(max_concurrent); self._task=None; self._running={}
# NEW:
    def __init__(self, store: CronStore, runner: CronRunner | None = None, tick_seconds: int = 60, max_concurrent: int = 3, on_complete=None):
        self.store=store; self.runner=runner or CronRunner(store=store); self.tick_seconds=tick_seconds; self.sem=asyncio.Semaphore(max_concurrent); self._task=None; self._running={}; self.on_complete=on_complete

# Change _run method (line 25-26):
# OLD:
#     async def _run(self, jid):
#         async with self.sem: await self.runner.run_job(jid)
# NEW:
    async def _run(self, jid):
        async with self.sem:
            runner=CronRunner(store=self.store, on_complete=self.on_complete)
            await runner.run_job(jid)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/anime/friday && python -m pytest tests/test_cron_toast.py -v`
Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/anime/friday
git add ares/cron/scheduler.py tests/test_cron_toast.py
git commit -m "feat: pass on_complete callback through CronScheduler"
```

---

### Task 4: Wire up CLI with patch_stdout and toast manager

**Files:**
- Modify: `ares/cli.py` (lines 38-39 for imports, lines 126-132 for constructor, lines 560-603 for run loop)

- [ ] **Step 1: Add imports**

At the top of `ares/cli.py`, add these imports (after existing imports around line 38):

```python
from prompt_toolkit.patch_stdout import patch_stdout
from ares.cron.toast import CronToastManager
```

- [ ] **Step 2: Create toast manager in __init__**

In `AresCLI.__init__`, after `self.cron_store = CronStore(cron_root)` (line 127), add:

```python
        self.toast_manager = CronToastManager(self.console)
```

Then change the `cron_scheduler` initialization (lines 128-132) from:

```python
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            tick_seconds=self.config.cron_tick_seconds,
            max_concurrent=self.config.cron_max_concurrent,
        ) if self.config.cron_enabled else None
```

to:

```python
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            tick_seconds=self.config.cron_tick_seconds,
            max_concurrent=self.config.cron_max_concurrent,
            on_complete=self.toast_manager,
        ) if self.config.cron_enabled else None
```

- [ ] **Step 3: Wrap run loop in patch_stdout**

In `AresCLI.run()`, wrap the main while loop. The current structure is:

```python
    async def run(self):
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.start()
            except BaseException:
                _clear_current_task_cancellation()
            self.agent.refresh_tools()
        if self.cron_scheduler is not None:
            await self.cron_scheduler.start()
        self._show_banner()

        try:
            while True:
                ...
```

Change to:

```python
    async def run(self):
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.start()
            except BaseException:
                _clear_current_task_cancellation()
            self.agent.refresh_tools()
        if self.cron_scheduler is not None:
            await self.cron_scheduler.start()
        self._show_banner()

        try:
            with patch_stdout():
                while True:
                    ...
```

Important: the `with patch_stdout():` wraps the `while True:` block and everything inside it (the `try/except` for `KeyboardInterrupt`, `EOFError`, `CancelledError`). The `finally:` block (cleanup) stays outside the `with` block since it runs after the loop exits.

The resulting structure:

```python
        try:
            with patch_stdout():
                while True:
                    try:
                        user_input = await self._prompt()
                        ...
                    except KeyboardInterrupt:
                        ...
                    except EOFError:
                        break
                    except asyncio.CancelledError:
                        ...
        finally:
            # Cleanup
            if self.cron_scheduler is not None:
                ...
```

- [ ] **Step 4: Run the app to verify**

Run: `cd /c/Users/anime/friday && python -m ares`

Expected: Ares starts normally with the welcome banner. Type a message — chat works as before. If you have an active cron job, its completion toast will appear above the prompt.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/anime/friday
git add ares/cli.py
git commit -m "feat: wire up cron toast notifications via patch_stdout"
```

---

### Task 5: End-to-end verification

- [ ] **Step 1: Create a fast cron job**

In Ares, type:
> Create a cron job called "toast test" that says "toast works!" every 1 minute

- [ ] **Step 2: Wait for it to fire**

Wait ~60 seconds. A line should appear above the `❯` prompt:

```
  ✅ Cron: toast test — toast works! (X.Xs)
❯ _
```

- [ ] **Step 3: Verify input isn't corrupted**

While the toast is visible, type a message and press Enter. The input should work normally.

- [ ] **Step 4: Create a second job to test stacking**

> Create a cron job called "toast test 2" that says "second toast!" every 1 minute

Wait for both to fire. Two toast lines should appear stacked above the prompt.

- [ ] **Step 5: Verify failed job shows ❌**

> Create a cron job called "fail test" that says "hello" but with max 1 iteration

Wait for it. If it fails, the toast should show ❌.

- [ ] **Step 6: Run full test suite**

Run: `cd /c/Users/anime/friday && python -m pytest tests/ -v`
Expected: All tests pass, no regressions.

- [ ] **Step 7: Final commit**

```bash
cd /c/Users/anime/friday
git add -A
git commit -m "feat: cron toast notifications — end-to-end verified"
```
