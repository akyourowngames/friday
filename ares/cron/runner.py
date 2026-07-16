"""Cron execution with a claimed lease and one durable terminal outcome."""
from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
import uuid
from contextlib import suppress
from pathlib import Path
from time import perf_counter
from typing import Any

from ares.config import load_config
from ares.cron.policy import expand_variables
from ares.cron.store import CronLeaseLostError, CronStore, utc_now


class CronRunner:
    def __init__(self, store: CronStore | None = None, config=None, on_complete=None, *, lease_seconds: int = 900):
        self.config = config or load_config()
        self.store = store or CronStore(Path(self.config.data_dir).expanduser().parent)
        self.on_complete = on_complete
        self.lease_seconds = max(30, int(lease_seconds))

    def latest_summary(self, job_id: str) -> str:
        logs = self.store.recent_logs(job_id, 1)
        if not logs:
            return ""
        try:
            text = logs[0].read_text(encoding="utf-8")
        except OSError:
            return ""
        marker = "## Summary"
        if marker not in text:
            return ""
        part = text.split(marker, 1)[1]
        for separator in ("\n## ", "\r\n## "):
            if separator in part:
                part = part.split(separator, 1)[0]
        return part.strip()

    async def _heartbeat_loop(self, job_id: str, lease_id: str, stopped: asyncio.Event) -> None:
        # Refresh well before expiration but do not churn the JSON store every
        # second for a job that may run hours.
        interval = max(5, min(60, self.lease_seconds // 3))
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                self.store.heartbeat_job(job_id, lease_id, lease_seconds=self.lease_seconds)
            except CronLeaseLostError:
                return
            except Exception:
                # The runner will still attempt a terminal transition.  A
                # transient heartbeat failure must not turn an otherwise useful
                # run into a silent, orphaned task.
                continue

    async def run_job(self, job_id: str, *, lease_id: str | None = None) -> Path:
        """Run a job once after claiming (or validating) its exclusive lease.

        ``lease_id`` is used by the tool layer when it has already claimed a
        run before scheduling a background task.  This prevents a race between
        acknowledgement and task start.
        """
        if lease_id is None:
            job = self.store.claim_job(job_id, lease_seconds=self.lease_seconds)
            lease_id = str(job["lease_id"])
        else:
            job = self.store.get_job(job_id)
            if not job or job.get("state") != "running" or job.get("lease_id") != lease_id:
                raise CronLeaseLostError(f"Cron job '{job_id}' does not hold the requested lease")

        started = str(job.get("run_started_at") or utc_now())
        start = perf_counter()
        output = ""
        error_text = ""
        status = "completed"
        agent = None
        memory_store = None
        conversation_store = None
        stopped = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(job_id, lease_id, stopped))
        cancelled = False

        try:
            policy = dict(job.get("policy") or {})
            budget = dict(policy.get("budget") or {})
            prompt = expand_variables(str(job["prompt"]), policy)
            previous = self.latest_summary(job_id)
            if previous:
                prompt = f"## Previous Run Summary\n{previous}\n\n## Scheduled Job Prompt\n{prompt}"
            config = self.config.model_copy(deep=True)
            if budget.get("max_iterations"):
                config.agent_max_iterations = int(budget["max_iterations"])
            elif job.get("max_iterations"):
                config.agent_max_iterations = int(job["max_iterations"])
            else:
                config.agent_max_iterations = int(getattr(config, "cron_max_iterations", 10))

            from ares.conversations import ConversationStore
            from ares.memory import MemoryStore
            from ares.agent import Agent

            memory_store = MemoryStore()
            conversation_store = ConversationStore()
            agent = Agent(memory_store, conversation_store, config=config, is_cron_session=True)
            chunks: list[str] = []
            output_chars = 0
            output_limit = int(budget.get("max_output_chars", 2_000_000))
            async with asyncio.timeout(float(budget.get("max_duration_seconds", 86_400))):
                async for chunk in agent.run_stream(prompt, []):
                    # Keep internal tool lifecycle telemetry out of persisted cron
                    # answers and their downstream delivery channels.
                    text = str(chunk)
                    if text.startswith("[tool"):
                        continue
                    remaining = output_limit - output_chars
                    if remaining <= 0:
                        chunks.append("\n[Output stopped at the configured cron budget.]\n")
                        break
                    chunks.append(text[:remaining])
                    output_chars += min(len(text), remaining)
                    if len(text) > remaining:
                        chunks.append("\n[Output stopped at the configured cron budget.]\n")
                        break
            output = "".join(chunks)
        except asyncio.CancelledError:
            cancelled = True
            status = "failed"
            error_text = "Cron run was cancelled."
            output = error_text
        except Exception:
            status = "failed"
            error_text = traceback.format_exc()
            output = error_text
        finally:
            for resource in (agent, conversation_store, memory_store):
                if resource is None:
                    continue
                try:
                    close = getattr(resource, "close", None)
                    if close is None:
                        continue
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    status = "failed"
                    cleanup_error = traceback.format_exc()
                    error_text = f"{error_text}\n{cleanup_error}".strip()
                    output = f"{output}\n{cleanup_error}".strip()
            stopped.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat

        duration = perf_counter() - start
        try:
            log = self._write_log(job, started, status, duration, output, error_text)
        except Exception:
            # A log failure must itself have a visible terminal record.  Use a
            # distinct emergency file so a failed replace cannot erase a prior
            # good log.
            status = "failed"
            error_text = f"{error_text}\nLog write failure:\n{traceback.format_exc()}".strip()
            output = f"{output}\n{error_text}".strip()
            log = self._write_emergency_log(job, started, output)

        try:
            self.store.complete_job(job_id, lease_id, status=status, log_path=log)
        except CronLeaseLostError:
            # Another process recovered/superseded the lease.  Do not falsely
            # claim this stale execution changed its terminal state.
            raise

        notification_statuses = set((job.get("policy") or {}).get("notifications", {}).get("on", ["completed", "failed"]))
        if self.on_complete and status in notification_statuses:
            clean = re.sub(r"\[tool:[^\]]*\]", "", output).strip()
            summary = clean.split("\n\n")[0] if clean else ("Run failed." if status == "failed" else "No output.")
            with suppress(Exception):
                self.on_complete(job["name"], summary, status, duration)
        if cancelled:
            raise asyncio.CancelledError
        return log

    def _write_emergency_log(self, job: dict[str, Any], started: str, output: str) -> Path:
        path = self.store.log_dir(job["id"]) / f"{started.replace(':', '-')}-{uuid.uuid4().hex[:8]}-emergency.md"
        path.write_text(
            f"# Cron Run: {job['name']}\n\n**Status:** failed\n\n## Agent Output\n{output}\n",
            encoding="utf-8",
        )
        return path

    def _write_log(self, job: dict[str, Any], started: str, status: str, duration: float, output: str, error_text: str = "") -> Path:
        directory = self.store.log_dir(job["id"])
        path = directory / f"{started.replace(':', '-')}.md"
        # Multiple recovery paths can share a second.  Preserve every run.
        if path.exists():
            path = directory / f"{started.replace(':', '-')}-{uuid.uuid4().hex[:8]}.md"
        clean = re.sub(r"\[tool:[^\]]*\]", "", output).strip()
        summary = clean.split("\n\n")[0] if clean else ("Run failed." if status == "failed" else "No output.")
        text = (
            f"# Cron Run: {job['name']}\n"
            f"**Job:** {job['id']}\n"
            f"**Run:** {started}\n"
            f"**Status:** {status}\n"
            f"**Duration:** {duration:.1f}s\n\n"
            f"## Prompt\n{job['prompt']}\n\n"
            f"## Agent Output\n{output}\n\n"
            f"## Summary\n{summary}\n\n"
            f"## Run Metadata\n- Model: {getattr(self.config, 'model', '')}\n"
            f"- Retry attempt: {int(job.get('retry_count') or 0) + 1}\n"
            f"- Policy budget: {json.dumps((job.get('policy') or {}).get('budget') or {}, ensure_ascii=False)}\n"
        )
        if error_text:
            text += f"\n## Error\n{error_text}\n"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        return path
