"""Durable, revisioned storage for scheduled cron jobs.

The scheduler, CLI, desktop server, and manual tool calls can all touch the
same JSON document.  Atomic ``os.replace`` alone is not enough here: without a
lock, two read/modify/write cycles can still lose a valid update.  This module
therefore makes every mutation pass through one cross-process critical section
and records revisions/leases in the persisted document.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ares.cron.schedule_utils import next_run_utc, parse_natural_schedule, validate_cron


class CronConflictError(RuntimeError):
    """A mutation was based on stale cron state."""


class CronAlreadyRunningError(CronConflictError):
    """A second caller attempted to run a job with a live lease."""


class CronLeaseLostError(CronConflictError):
    """A runner tried to finish after its lease was superseded."""


_THREAD_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCK_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _exclusive_file_lock(path: Path, timeout: float = 15.0) -> Iterator[None]:
    """Take a lock that works across both processes and CronStore instances."""
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _lock_for(path)
    with thread_lock:
        with path.open("a+b") as handle:
            # msvcrt locks byte ranges, so make sure byte zero exists.
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + timeout
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Timed out waiting for cron store lock: {path}")
                        time.sleep(0.025)
                try:
                    yield
                finally:
                    handle.seek(0)
                    with suppress(OSError):
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class CronStore:
    """A small, transactional JSON store with leases for active jobs."""

    _ALLOWED_STATES = {"scheduled", "running", "completed", "failed"}
    _USER_UPDATABLE = {"name", "prompt", "cron", "timezone", "enabled", "max_iterations"}
    _SYSTEM_UPDATABLE = {
        "state", "next_run_at", "last_run_at", "run_count", "last_status",
        "last_log_path", "lease_id", "lease_expires_at", "last_heartbeat_at",
        "run_started_at", "output_dir",
    }

    def __init__(self, data_dir: str | Path | None = None):
        root = Path(data_dir or "~/.ares").expanduser()
        if root.name == "data":
            root = root.parent
        self.root = root
        self.cron_dir = root / "cron"
        self.logs_root = self.cron_dir / "logs"
        self.cron_dir.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        # A crashed process must not leave a job permanently invisible to the
        # scheduler.  Recovery is safe to attempt on every process startup.
        with suppress(Exception):
            self.recover_expired_leases()

    def _jobs_path(self) -> Path:
        return self.cron_dir / "jobs.json"

    def _lock_path(self) -> Path:
        return self.cron_dir / "jobs.lock"

    @staticmethod
    def _copy(value: Any) -> Any:
        return deepcopy(value)

    def _read_unlocked(self) -> dict[str, Any]:
        path = self._jobs_path()
        if not path.exists():
            return {"revision": 0, "jobs": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8") or '{"jobs":{}}')
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Cron store is not valid JSON: {path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("jobs", {}), dict):
            raise RuntimeError(f"Cron store has an invalid document shape: {path}")
        data.setdefault("revision", 0)
        if not isinstance(data["revision"], int) or data["revision"] < 0:
            raise RuntimeError("Cron store has an invalid revision")
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        path = self._jobs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            # Flush the parent metadata on platforms where this is supported.
            with suppress(OSError):
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                tmp.unlink()

    def _validate_timezone(self, value: str) -> str:
        name = str(value or "UTC")
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid IANA timezone: {name}") from exc
        return name

    def _validate_job(self, job: dict[str, Any]) -> None:
        if not isinstance(job, dict):
            raise ValueError("Cron job must be an object")
        for field in ("id", "name", "prompt", "cron", "timezone", "created_at"):
            if not isinstance(job.get(field), str) or not job[field].strip():
                raise ValueError(f"Cron job field '{field}' must be a non-empty string")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", job["id"]):
            raise ValueError("Cron job id contains invalid characters")
        job["cron"] = validate_cron(parse_natural_schedule(job["cron"]))
        job["timezone"] = self._validate_timezone(job["timezone"])
        if not isinstance(job.get("enabled"), bool):
            raise ValueError("Cron job field 'enabled' must be boolean")
        if job.get("state") not in self._ALLOWED_STATES:
            raise ValueError("Cron job state is invalid")
        if not isinstance(job.get("revision"), int) or job["revision"] < 1:
            raise ValueError("Cron job revision is invalid")
        if not isinstance(job.get("run_count"), int) or job["run_count"] < 0:
            raise ValueError("Cron job run_count is invalid")
        maximum = job.get("max_iterations")
        if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1):
            raise ValueError("Cron job max_iterations must be a positive integer or null")
        for key in ("next_run_at", "last_run_at", "lease_expires_at", "last_heartbeat_at", "run_started_at"):
            value = job.get(key)
            if value is not None:
                _parse_iso(value)
        if job["state"] == "running":
            if not isinstance(job.get("lease_id"), str) or not job["lease_id"]:
                raise ValueError("Running cron job is missing its lease id")
            if not job.get("lease_expires_at"):
                raise ValueError("Running cron job is missing its lease expiration")

    def _validate_document(self, data: dict[str, Any]) -> None:
        if not isinstance(data.get("jobs"), dict):
            raise ValueError("Cron store jobs must be an object")
        seen_names: set[str] = set()
        for job_id, job in data["jobs"].items():
            if not isinstance(job, dict) or job.get("id") != job_id:
                raise ValueError("Cron store contains a mismatched job id")
            self._validate_job(job)
            name = job["name"].casefold()
            if name in seen_names:
                raise ValueError("Cron store contains duplicate job names")
            seen_names.add(name)

    def _transaction(
        self,
        mutation: Callable[[dict[str, Any]], Any],
        *,
        expected_store_revision: int | None = None,
    ) -> Any:
        with _exclusive_file_lock(self._lock_path()):
            data = self._read_unlocked()
            if expected_store_revision is not None and data["revision"] != int(expected_store_revision):
                raise CronConflictError(
                    f"Stale cron store revision {expected_store_revision}; current revision is {data['revision']}"
                )
            result = mutation(data)
            self._validate_document(data)
            data["revision"] += 1
            self._write_unlocked(data)
            return self._copy(result)

    def _read(self) -> dict[str, Any]:
        with _exclusive_file_lock(self._lock_path()):
            return self._copy(self._read_unlocked())

    @staticmethod
    def _slug(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "job"

    def _new_job(
        self,
        job_id: str,
        name: str,
        prompt: str,
        cron: str,
        timezone_name: str,
        enabled: bool,
        max_iterations: int | None,
    ) -> dict[str, Any]:
        now = utc_now()
        job = {
            "id": job_id,
            "name": name,
            "prompt": prompt,
            "cron": validate_cron(parse_natural_schedule(cron)),
            "timezone": self._validate_timezone(timezone_name),
            "created_at": now,
            "enabled": bool(enabled),
            "state": "scheduled",
            "next_run_at": None,
            "last_run_at": None,
            "run_count": 0,
            "last_status": None,
            "last_log_path": None,
            "max_iterations": max_iterations,
            "output_dir": str(self.log_dir(job_id)),
            "revision": 1,
            "lease_id": None,
            "lease_expires_at": None,
            "last_heartbeat_at": None,
            "run_started_at": None,
        }
        job["next_run_at"] = next_run_utc(job["cron"], job["timezone"])
        self._validate_job(job)
        return job

    def create_job(
        self,
        name: str,
        prompt: str,
        cron: str,
        timezone: str = "UTC",
        enabled: bool = True,
        max_iterations: int | None = None,
        *,
        expected_store_revision: int | None = None,
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        prompt = str(prompt or "")
        if not name:
            raise ValueError("Cron job name is required")
        if not prompt.strip():
            raise ValueError("Cron job prompt is required")
        if max_iterations is not None:
            max_iterations = int(max_iterations)

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            jobs = data.setdefault("jobs", {})
            if any(existing.get("name", "").casefold() == name.casefold() for existing in jobs.values()):
                raise ValueError(f"Cron job '{name}' already exists")
            base = self._slug(name)
            job_id = base
            suffix = 2
            while job_id in jobs:
                job_id = f"{base}-{suffix}"
                suffix += 1
            job = self._new_job(job_id, name, prompt, cron, timezone or "UTC", bool(enabled), max_iterations)
            jobs[job_id] = job
            return job

        return self._transaction(mutate, expected_store_revision=expected_store_revision)

    def list_jobs(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        data = self._read()
        jobs = list(data["jobs"].values())
        if not include_disabled:
            jobs = [job for job in jobs if job.get("enabled", True)]
        return [self._copy(job) for job in sorted(jobs, key=lambda job: job.get("name", "").casefold())]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.recover_expired_leases()
        return self._copy(self._read().get("jobs", {}).get(job_id))

    def update_job(
        self,
        job_id: str,
        *,
        expected_revision: int | None = None,
        expected_store_revision: int | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        unknown = set(updates).difference(self._USER_UPDATABLE | self._SYSTEM_UPDATABLE)
        if unknown:
            raise ValueError(f"Unknown or immutable cron job field(s): {', '.join(sorted(unknown))}")

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            jobs = data.setdefault("jobs", {})
            if job_id not in jobs:
                raise ValueError(f"Cron job '{job_id}' not found")
            job = jobs[job_id]
            if expected_revision is not None and job.get("revision") != int(expected_revision):
                raise CronConflictError(
                    f"Stale revision for cron job '{job_id}': expected {expected_revision}, current {job.get('revision')}"
                )
            candidate = self._copy(job)
            for key, value in updates.items():
                if value is not None:
                    candidate[key] = value
            if "name" in updates and candidate["name"].casefold() != job["name"].casefold():
                if any(jid != job_id and other.get("name", "").casefold() == candidate["name"].casefold() for jid, other in jobs.items()):
                    raise ValueError(f"Cron job '{candidate['name']}' already exists")
            if "cron" in updates:
                candidate["cron"] = validate_cron(parse_natural_schedule(str(candidate["cron"])))
            if "timezone" in updates:
                candidate["timezone"] = self._validate_timezone(str(candidate["timezone"]))
            if "cron" in updates or "timezone" in updates:
                candidate["next_run_at"] = next_run_utc(candidate["cron"], candidate["timezone"])
            # Preserve the legacy internal state-update API while retaining
            # the invariant that a running job always has a recoverable lease.
            # Public execution uses claim_job instead.
            if candidate.get("state") == "running" and not candidate.get("lease_id"):
                now = datetime.now(timezone.utc)
                candidate["lease_id"] = f"legacy-{uuid.uuid4().hex}"
                candidate["lease_expires_at"] = (now + timedelta(seconds=900)).isoformat().replace("+00:00", "Z")
                candidate["last_heartbeat_at"] = now.isoformat().replace("+00:00", "Z")
                candidate["run_started_at"] = candidate.get("run_started_at") or candidate["last_heartbeat_at"]
            candidate["revision"] = int(job.get("revision", 0)) + 1
            self._validate_job(candidate)
            jobs[job_id] = candidate
            return candidate

        return self._transaction(mutate, expected_store_revision=expected_store_revision)

    def delete_job(self, job_id: str, *, expected_revision: int | None = None) -> None:
        def mutate(data: dict[str, Any]) -> None:
            jobs = data.setdefault("jobs", {})
            job = jobs.get(job_id)
            if job is None:
                raise ValueError(f"Cron job '{job_id}' not found")
            if expected_revision is not None and job.get("revision") != int(expected_revision):
                raise CronConflictError(f"Stale revision for cron job '{job_id}'")
            if job.get("state") == "running" and not self._lease_expired(job):
                raise CronAlreadyRunningError(f"Cron job '{job_id}' is running and cannot be deleted")
            del jobs[job_id]

        self._transaction(mutate)

    @staticmethod
    def _lease_expired(job: dict[str, Any], now: datetime | None = None) -> bool:
        expiry = job.get("lease_expires_at")
        if not expiry:
            return True
        try:
            return _parse_iso(expiry) <= (now or datetime.now(timezone.utc))
        except (TypeError, ValueError):
            return True

    def _recovery_log(self, job: dict[str, Any], when: str) -> str:
        path = self.log_dir(job["id"]) / f"{when.replace(':', '-')}-lease-recovered.md"
        path.write_text(
            f"# Cron Run Recovery: {job['name']}\n\n"
            f"The previous run lease expired at {job.get('lease_expires_at')}; "
            "Ares marked the run failed so the schedule can recover.\n",
            encoding="utf-8",
        )
        return str(path)

    def recover_expired_leases(self, now: str | datetime | None = None) -> list[dict[str, Any]]:
        now_dt = _parse_iso(now) if isinstance(now, str) else (now or datetime.now(timezone.utc))
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        recovered: list[dict[str, Any]] = []

        def mutate(data: dict[str, Any]) -> list[dict[str, Any]]:
            for job in data.setdefault("jobs", {}).values():
                if job.get("state") == "running" and self._lease_expired(job, now_dt):
                    when = now_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    job["state"] = "failed"
                    job["last_status"] = "failed"
                    job["last_run_at"] = job.get("run_started_at") or when
                    job["last_log_path"] = self._recovery_log(job, when)
                    job["lease_id"] = None
                    job["lease_expires_at"] = None
                    job["last_heartbeat_at"] = when
                    job["revision"] = int(job.get("revision", 0)) + 1
                    recovered.append(self._copy(job))
            return recovered

        # Avoid rewriting the store during ordinary read paths.
        snapshot = self._read()
        if not any(job.get("state") == "running" and self._lease_expired(job, now_dt) for job in snapshot["jobs"].values()):
            return []
        return self._transaction(mutate)

    def get_due_jobs(self, now: str | None = None) -> list[dict[str, Any]]:
        now_dt = _parse_iso(now or utc_now())
        self.recover_expired_leases(now_dt)
        data = self._read()
        due = []
        for job in data.get("jobs", {}).values():
            if not job.get("enabled", True) or job.get("state") == "running" or not job.get("next_run_at"):
                continue
            if _parse_iso(job["next_run_at"]) <= now_dt:
                due.append(job)
        return [self._copy(job) for job in sorted(due, key=lambda job: job.get("next_run_at", ""))]

    def claim_job(self, job_id: str, *, lease_seconds: int = 900) -> dict[str, Any]:
        duration = max(1, int(lease_seconds))

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            job = data.setdefault("jobs", {}).get(job_id)
            if job is None:
                raise ValueError(f"Cron job '{job_id}' not found")
            now = datetime.now(timezone.utc)
            if job.get("state") == "running" and not self._lease_expired(job, now):
                raise CronAlreadyRunningError(f"Cron job '{job_id}' is already running")
            # An expired lease is considered a completed failed attempt before
            # the new run takes ownership.  Count only the newly accepted run.
            lease_id = uuid.uuid4().hex
            now_text = now.isoformat().replace("+00:00", "Z")
            expires = (now + timedelta(seconds=duration)).isoformat().replace("+00:00", "Z")
            job.update(
                state="running",
                lease_id=lease_id,
                lease_expires_at=expires,
                last_heartbeat_at=now_text,
                run_started_at=now_text,
                last_status=None,
                run_count=int(job.get("run_count") or 0) + 1,
                revision=int(job.get("revision", 0)) + 1,
            )
            return job

        return self._transaction(mutate)

    def heartbeat_job(self, job_id: str, lease_id: str, *, lease_seconds: int = 900) -> dict[str, Any]:
        duration = max(1, int(lease_seconds))

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            job = data.setdefault("jobs", {}).get(job_id)
            if job is None or job.get("state") != "running" or job.get("lease_id") != lease_id:
                raise CronLeaseLostError(f"Cron job '{job_id}' no longer holds lease {lease_id}")
            now = datetime.now(timezone.utc)
            job["last_heartbeat_at"] = now.isoformat().replace("+00:00", "Z")
            job["lease_expires_at"] = (now + timedelta(seconds=duration)).isoformat().replace("+00:00", "Z")
            job["revision"] = int(job.get("revision", 0)) + 1
            return job

        return self._transaction(mutate)

    def complete_job(
        self,
        job_id: str,
        lease_id: str,
        *,
        status: str,
        log_path: str | Path,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed"}:
            raise ValueError("Cron completion status must be 'completed' or 'failed'")
        finished = completed_at or utc_now()

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            job = data.setdefault("jobs", {}).get(job_id)
            if job is None or job.get("state") != "running" or job.get("lease_id") != lease_id:
                raise CronLeaseLostError(f"Cron job '{job_id}' no longer holds lease {lease_id}")
            job.update(
                state=status,
                last_status=status,
                last_run_at=finished,
                next_run_at=next_run_utc(job["cron"], job.get("timezone", "UTC"), _parse_iso(finished)),
                last_log_path=str(log_path),
                lease_id=None,
                lease_expires_at=None,
                last_heartbeat_at=finished,
                revision=int(job.get("revision", 0)) + 1,
            )
            return job

        return self._transaction(mutate)

    def log_dir(self, job_id: str) -> Path:
        path = self.logs_root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def recent_logs(self, job_id: str, limit: int = 5) -> list[Path]:
        bounded = max(1, min(int(limit), 50))
        return sorted(self.log_dir(job_id).glob("*.md"), reverse=True)[:bounded]
