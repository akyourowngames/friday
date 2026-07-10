"""Async cron scheduler with one tracked task per claimed job."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from ares.cron.runner import CronRunner
from ares.cron.store import CronAlreadyRunningError, CronStore


class CronScheduler:
    def __init__(
        self,
        store: CronStore,
        runner: CronRunner | None = None,
        tick_seconds: int = 60,
        max_concurrent: int = 3,
        on_complete=None,
    ):
        self.store = store
        self.on_complete = on_complete
        self.runner = runner or CronRunner(store=store, on_complete=on_complete)
        self.tick_seconds = max(1, int(tick_seconds))
        self.sem = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="ares-cron-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        # Stop scheduling new work, but let accepted work reach the runner's
        # durable terminal transition rather than abandoning a live lease.
        running = list(self._running.values())
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._running.clear()

    def _observe_task(self, job_id: str, task: asyncio.Task) -> None:
        def done(completed: asyncio.Task) -> None:
            self._running.pop(job_id, None)
            # Retrieve the exception so no background task warning hides a
            # lifecycle failure.  CronRunner has already persisted/logged its
            # own terminal failure when it held the lease.
            with suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(done)

    async def tick(self) -> None:
        for job in self.store.get_due_jobs():
            job_id = job["id"]
            existing = self._running.get(job_id)
            if existing and not existing.done():
                continue
            task = asyncio.create_task(self._run(job_id), name=f"ares-cron-{job_id}")
            self._running[job_id] = task
            self._observe_task(job_id, task)

    async def _run(self, job_id: str) -> None:
        async with self.sem:
            try:
                await self.runner.run_job(job_id)
            except CronAlreadyRunningError:
                # A manual run won the atomic claim race.  It is already
                # tracked by its caller, so this scheduler tick has nothing to
                # report or retry.
                return

    async def _loop(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self.tick_seconds)
