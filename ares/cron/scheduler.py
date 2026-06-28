"""Async cron scheduler tick loop."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from ares.cron.runner import CronRunner
from ares.cron.store import CronStore

class CronScheduler:
    def __init__(self, store: CronStore, runner: CronRunner | None = None, tick_seconds: int = 60, max_concurrent: int = 3):
        self.store=store; self.runner=runner or CronRunner(store=store); self.tick_seconds=tick_seconds; self.sem=asyncio.Semaphore(max_concurrent); self._task=None; self._running={}
    async def start(self):
        if self._task is None or self._task.done(): self._task=asyncio.create_task(self._loop())
    async def stop(self):
        if self._task: self._task.cancel();
        if self._task:
            with suppress(asyncio.CancelledError): await self._task
        if self._running: await asyncio.gather(*self._running.values(), return_exceptions=True)
    async def tick(self):
        for job in self.store.get_due_jobs():
            jid=job['id']
            if jid not in self._running or self._running[jid].done():
                self._running[jid]=asyncio.create_task(self._run(jid))
    async def _run(self, jid):
        async with self.sem: await self.runner.run_job(jid)
    async def _loop(self):
        while True:
            await self.tick(); await asyncio.sleep(self.tick_seconds)
