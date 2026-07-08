"""Tool handlers for managing cron jobs."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ares.cron.store import CronStore
from ares.cron.schedule_utils import simulate_next_runs

class CronToolHandlers:
    def __init__(self, store: CronStore): self.store=store
    def create_cron_job(self,args): return f"Created cron job: {self.store.create_job(args['name'], args['prompt'], args['cron'], args.get('timezone','UTC'), args.get('enabled', True), args.get('max_iterations'))}"
    def list_cron_jobs(self,args):
        jobs=self.store.list_jobs(bool(args.get('include_disabled', True)))
        return "No cron jobs." if not jobs else "\n".join(f"- {j['id']}: {j['name']} [{j['state']}] next={j.get('next_run_at')} enabled={j.get('enabled')}" for j in jobs)
    def get_cron_job(self,args):
        job=self.store.get_job(args['job_id'])
        if not job:
            return f"Cron job {args['job_id']} not found"
        job["schedule_simulation"] = simulate_next_runs(
            job["cron"],
            job.get("timezone", "UTC"),
            last_run_at=job.get("last_run_at"),
        )
        return json.dumps(job, indent=2)
    def update_cron_job(self,args):
        jid=args.pop('job_id'); return f"Updated cron job: {self.store.update_job(jid, **args)}"
    def delete_cron_job(self,args): self.store.delete_job(args['job_id']); return f"Deleted cron job {args['job_id']} (logs retained)."
    def run_cron_job_now(self,args):
        from ares.cron.runner import CronRunner
        runner=CronRunner(store=self.store)
        try:
            loop=asyncio.get_running_loop()
        except RuntimeError:
            path=asyncio.run(runner.run_job(args['job_id']))
            return f"Ran cron job {args['job_id']}; log: {path}"
        loop.create_task(runner.run_job(args['job_id']))
        return f"Started cron job {args['job_id']} in the background."
    def get_cron_logs(self,args):
        logs=self.store.recent_logs(args['job_id'], int(args.get('limit', 5)))
        return "No logs found." if not logs else "\n\n".join(f"# {p.name}\n{p.read_text(encoding='utf-8')[:4000]}" for p in logs)
