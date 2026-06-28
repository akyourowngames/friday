"""Persistent JSON store for scheduled cron jobs."""
from __future__ import annotations

import json, os, re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ares.cron.schedule_utils import next_run_utc, parse_natural_schedule, validate_cron


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

class CronStore:
    def __init__(self, data_dir: str | Path | None = None):
        root = Path(data_dir or "~/.ares").expanduser()
        if root.name == "data": root = root.parent
        self.root = root
        self.cron_dir = root / "cron"
        self.logs_root = self.cron_dir / "logs"
        self.cron_dir.mkdir(parents=True, exist_ok=True); self.logs_root.mkdir(parents=True, exist_ok=True)

    def _jobs_path(self) -> Path: return self.cron_dir / "jobs.json"
    def _read(self) -> dict[str, Any]:
        path = self._jobs_path()
        if not path.exists(): return {"jobs": {}}
        return json.loads(path.read_text(encoding="utf-8") or '{"jobs":{}}')
    def _write(self, data: dict[str, Any]) -> None:
        path=self._jobs_path(); path.parent.mkdir(parents=True, exist_ok=True)
        tmp=path.with_suffix('.json.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    def _slug(self, name: str) -> str:
        slug=re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-') or 'job'
        return slug
    def _copy(self, job): return deepcopy(job) if job is not None else None
    def create_job(self, name: str, prompt: str, cron: str, timezone: str = "UTC", enabled: bool = True, max_iterations: int | None = None) -> dict:
        data=self._read(); jobs=data.setdefault('jobs', {})
        jid=self._slug(name); base=jid; n=2
        while jid in jobs:
            if jobs[jid].get('name','').lower()==name.lower(): raise ValueError(f"Cron job '{name}' already exists")
            jid=f"{base}-{n}"; n+=1
        expr=validate_cron(parse_natural_schedule(cron))
        job={"id":jid,"name":name,"prompt":prompt,"cron":expr,"timezone":timezone or "UTC","created_at":utc_now(),"enabled":bool(enabled),"state":"scheduled","next_run_at":next_run_utc(expr, timezone or "UTC"),"last_run_at":None,"run_count":0,"last_status":None,"max_iterations":max_iterations,"output_dir":str(self.log_dir(jid))}
        jobs[jid]=job; self._write(data); return self._copy(job)
    def list_jobs(self, include_disabled: bool = True) -> list[dict]:
        jobs=list(self._read().get('jobs',{}).values())
        if not include_disabled: jobs=[j for j in jobs if j.get('enabled', True)]
        return [self._copy(j) for j in sorted(jobs, key=lambda x:x.get('name',''))]
    def get_job(self, job_id: str) -> dict | None: return self._copy(self._read().get('jobs',{}).get(job_id))
    def update_job(self, job_id: str, **updates) -> dict:
        data=self._read(); jobs=data.setdefault('jobs', {})
        if job_id not in jobs: raise ValueError(f"Cron job '{job_id}' not found")
        job=jobs[job_id]
        for k,v in updates.items():
            if v is not None: job[k]=v
        if 'cron' in updates: job['cron']=validate_cron(parse_natural_schedule(job['cron']))
        if 'cron' in updates or 'timezone' in updates:
            job['next_run_at']=next_run_utc(job['cron'], job.get('timezone','UTC'))
        self._write(data); return self._copy(job)
    def delete_job(self, job_id: str) -> None:
        data=self._read();
        if job_id not in data.get('jobs',{}): raise ValueError(f"Cron job '{job_id}' not found")
        del data['jobs'][job_id]; self._write(data)
    def get_due_jobs(self, now: str | None = None) -> list[dict]:
        now_dt=_parse_iso(now or utc_now()); due=[]
        for job in self._read().get('jobs',{}).values():
            if not job.get('enabled', True) or job.get('state') == 'running' or not job.get('next_run_at'): continue
            if _parse_iso(job['next_run_at']) <= now_dt: due.append(job)
        return [self._copy(j) for j in sorted(due, key=lambda x:x.get('next_run_at',''))]
    def log_dir(self, job_id: str) -> Path:
        p=self.logs_root/job_id; p.mkdir(parents=True, exist_ok=True); return p
    def recent_logs(self, job_id: str, limit: int = 5) -> list[Path]:
        return sorted(self.log_dir(job_id).glob('*.md'), reverse=True)[:limit]

def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z','+00:00'))
