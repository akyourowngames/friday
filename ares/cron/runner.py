"""Cron job runner that executes each run in a fresh Agent session."""
from __future__ import annotations

import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from ares.config import load_config
from ares.cron.schedule_utils import next_run_utc
from ares.cron.store import CronStore, utc_now

class CronRunner:
    def __init__(self, store: CronStore | None = None, config=None, on_complete=None):
        self.config = config or load_config()
        self.store = store or CronStore(Path(self.config.data_dir).expanduser().parent)
        self.on_complete = on_complete
    def latest_summary(self, job_id: str) -> str:
        logs=self.store.recent_logs(job_id, 1)
        if not logs: return ""
        text=logs[0].read_text(encoding='utf-8')
        marker='## Summary'
        if marker not in text: return ""
        part=text.split(marker,1)[1]
        for sep in ('\n## ', '\r\n## '):
            if sep in part: part=part.split(sep,1)[0]
        return part.strip()
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
            clean = re.sub(r'\[tool:[^\]]*\]', '', output).strip()
            summary_text = (clean.split('\n\n')[0] if clean else ('Run failed.' if status == 'failed' else 'No output.'))
            self.on_complete(job['name'], summary_text, status, duration)
        return log
    def _write_log(self, job: dict, started: str, status: str, duration: float, output: str, err: str='') -> Path:
        path=self.store.log_dir(job['id']) / (started.replace(':','-') + '.md')
        clean = re.sub(r'\[tool:[^\]]*\]', '', output).strip()
        summary=(clean.split('\n\n')[0] if clean else ('Run failed.' if status=='failed' else 'No output.'))
        path.write_text(f"# Cron Run: {job['name']}\n**Job:** {job['id']}\n**Run:** {started}\n**Status:** {status}\n**Duration:** {duration:.1f}s\n\n## Prompt\n{job['prompt']}\n\n## Agent Output\n{output}\n\n## Summary\n{summary}\n\n## Run Metadata\n- Model: {getattr(self.config,'model','')}\n", encoding='utf-8')
        return path
