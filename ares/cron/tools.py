"""Tool handlers for managing cron jobs without detached work."""
from __future__ import annotations

import asyncio
import json
import traceback
from contextlib import suppress
from typing import Any, Callable

from ares.cron.schedule_utils import simulate_next_runs
from ares.cron.store import CronAlreadyRunningError, CronStore, utc_now


class CronToolHandlers:
    def __init__(self, store: CronStore, runner_factory: Callable[..., Any] | None = None):
        self.store = store
        self._runner_factory = runner_factory
        self._run_tasks: dict[str, asyncio.Task] = {}

    def _runner(self):
        if self._runner_factory is not None:
            return self._runner_factory(store=self.store)
        from ares.cron.runner import CronRunner

        return CronRunner(store=self.store)

    def create_cron_job(self, args: dict) -> str:
        job = self.store.create_job(
            args["name"],
            args["prompt"],
            args["cron"],
            args.get("timezone", "UTC"),
            args.get("enabled", True),
            args.get("max_iterations"),
            expected_store_revision=args.get("expected_store_revision"),
        )
        return f"Created cron job: {job}"

    def list_cron_jobs(self, args: dict) -> str:
        jobs = self.store.list_jobs(bool(args.get("include_disabled", True)))
        if not jobs:
            return "No cron jobs."
        return "\n".join(
            f"- {job['id']}: {job['name']} [{job['state']}] next={job.get('next_run_at')} "
            f"enabled={job.get('enabled')} revision={job.get('revision')}"
            for job in jobs
        )

    def get_cron_job(self, args: dict) -> str:
        job = self.store.get_job(args["job_id"])
        if not job:
            return f"Cron job {args['job_id']} not found"
        job["schedule_simulation"] = simulate_next_runs(
            job["cron"],
            job.get("timezone", "UTC"),
            last_run_at=job.get("last_run_at"),
        )
        return json.dumps(job, indent=2, ensure_ascii=False)

    def update_cron_job(self, args: dict) -> str:
        payload = dict(args)
        job_id = payload.pop("job_id")
        expected_revision = payload.pop("expected_revision", None)
        expected_store_revision = payload.pop("expected_store_revision", None)
        job = self.store.update_job(
            job_id,
            expected_revision=expected_revision,
            expected_store_revision=expected_store_revision,
            **payload,
        )
        return f"Updated cron job: {job}"

    def delete_cron_job(self, args: dict) -> str:
        self.store.delete_job(args["job_id"], expected_revision=args.get("expected_revision"))
        return f"Deleted cron job {args['job_id']} (logs retained)."

    def _setup_failure(self, job_id: str, lease_id: str, error: BaseException) -> None:
        path = self.store.log_dir(job_id) / f"{utc_now().replace(':', '-')}-setup-failed.md"
        path.write_text(f"# Cron Run Setup Failure\n\n{traceback.format_exc()}\n", encoding="utf-8")
        with suppress(Exception):
            self.store.complete_job(job_id, lease_id, status="failed", log_path=path)

    def _observe_task(self, job_id: str, task: asyncio.Task) -> None:
        def done(completed: asyncio.Task) -> None:
            if self._run_tasks.get(job_id) is completed:
                self._run_tasks.pop(job_id, None)
            with suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(done)

    def run_cron_job_now(self, args: dict) -> str:
        """Claim synchronously, then run synchronously or as a tracked task."""
        job_id = args["job_id"]
        # Make invalid IDs observable before creating any coroutine/task.
        if self.store.get_job(job_id) is None:
            raise ValueError(f"Cron job '{job_id}' not found")
        try:
            claimed = self.store.claim_job(job_id)
        except CronAlreadyRunningError:
            return f"Cron job {job_id} is already running."
        lease_id = str(claimed["lease_id"])
        runner = self._runner()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                path = asyncio.run(runner.run_job(job_id, lease_id=lease_id))
            except Exception as exc:
                # run_job normally records its own terminal state.  This path
                # protects the narrow window before its lifecycle starts.
                self._setup_failure(job_id, lease_id, exc)
                raise
            return f"Ran cron job {job_id}; log: {path}"

        try:
            task = loop.create_task(runner.run_job(job_id, lease_id=lease_id), name=f"ares-cron-manual-{job_id}")
        except Exception as exc:
            self._setup_failure(job_id, lease_id, exc)
            raise
        self._run_tasks[job_id] = task
        self._observe_task(job_id, task)
        return f"Started cron job {job_id} in the background (tracked task)."

    def get_cron_logs(self, args: dict) -> str:
        logs = self.store.recent_logs(args["job_id"], int(args.get("limit", 5)))
        if not logs:
            return "No logs found."
        return "\n\n".join(f"# {path.name}\n{path.read_text(encoding='utf-8')[:4000]}" for path in logs)
