"""Concurrent, resilient scheduling and complete watcher check lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from ares.watcher.ai_analyzer import AIAnalyzer, AutoActionExecutor
from ares.watcher.database import WatcherDatabase
from ares.watcher.detectors import DiffDetector, HashDetector, ThresholdDetector, canonicalize
from ares.watcher.fetchers import BaseFetcher, ToolRunner, default_fetchers
from ares.watcher.models import CheckRun, Event, Monitor, Snapshot, utc_now
from ares.watcher.notifier import NotificationDispatcher
from ares.watcher.queue import EventQueue

logger = logging.getLogger(__name__)
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class WatcherScheduler:
    def __init__(self, db: WatcherDatabase, *, fetchers: dict[str, BaseFetcher] | None = None,
                 notifier: NotificationDispatcher | None = None, analyzer: AIAnalyzer | None = None,
                 on_event: EventCallback | None = None, max_concurrency: int = 8, failure_limit: int = 5,
                 tool_runner: ToolRunner | None = None, allow_mutating_tools: bool = False,
                 max_tool_steps: int = 8, max_tool_output_chars: int = 2_000_000) -> None:
        self.db, self.queue = db, EventQueue(db)
        self.fetchers = fetchers or default_fetchers(
            tool_runner,
            allow_mutating_tools=allow_mutating_tools,
            max_tool_steps=max_tool_steps,
            max_tool_output_chars=max_tool_output_chars,
        )
        self.notifier, self.analyzer = notifier, analyzer or AIAnalyzer()
        self.auto_actions, self.on_event = AutoActionExecutor(), on_event
        self.max_concurrency, self.failure_limit = max(1, max_concurrency), max(1, failure_limit)
        self.running, self._stop = False, asyncio.Event()
        self.instance_id = str(uuid4())
        self._locks: dict[str, asyncio.Lock] = {}

    def should_check(self, monitor: Monitor) -> bool:
        if not monitor.enabled:
            return False
        if monitor.next_check_at is not None:
            now = utc_now() if monitor.next_check_at.tzinfo else datetime.now()
            return monitor.next_check_at <= now
        if monitor.last_checked_at is None:
            return True
        now = utc_now() if monitor.last_checked_at.tzinfo else datetime.now()
        return (now - monitor.last_checked_at).total_seconds() >= monitor.interval_seconds

    async def check_monitor(self, monitor: Monitor, *, force: bool = False) -> Event | None:
        lock = self._locks.setdefault(monitor.id, asyncio.Lock())
        if lock.locked() or (not force and not self.should_check(monitor)):
            return None
        async with lock:
            started, started_clock = utc_now(), time.perf_counter()
            await self._emit("check.started", {"monitor": monitor.public_dict()})
            fetcher = self.fetchers.get(monitor.type)
            if fetcher is None:
                return await self._record_failure(monitor, started, started_clock, f"No fetcher registered for {monitor.type}")
            target = monitor.url or str(monitor.config.get("api_url") or "")
            try:
                timeout = max(1.0, min(float(monitor.config.get("timeout", 30)) + 2, 125.0))
                result = await asyncio.wait_for(fetcher.fetch(target, monitor.config), timeout=timeout)
            except asyncio.TimeoutError:
                return await self._record_failure(monitor, started, started_clock, "Check timed out", status="timeout")
            except Exception as exc:
                return await self._record_failure(monitor, started, started_clock, str(exc))
            if not result.success:
                return await self._record_failure(monitor, started, started_clock, result.error or "Fetch failed", status="timeout" if "timeout" in (result.error or "").lower() else "error", http_status=result.status_code)

            try:
                return await self._process_success(monitor, result, started, started_clock)
            except Exception as exc:
                logger.exception("Watcher post-fetch processing failed for %s", monitor.id)
                return await self._record_failure(monitor, started, started_clock, f"Processing failed: {exc}", http_status=result.status_code)

    async def _process_success(self, monitor: Monitor, result: Any, started: Any, started_clock: float) -> Event | None:
        previous = self.db.get_latest_snapshot(monitor.id)
        content_text = json.dumps(result.content, ensure_ascii=False, sort_keys=True, default=str) if isinstance(result.content, (dict, list)) else str(result.content or "")
        price = self._numeric_value(result.content, monitor)
        content_hash = hashlib.sha256(canonicalize(result.content, monitor.config.get("ignore_patterns")).encode()).hexdigest()
        snapshot = Snapshot(id=str(uuid4()), monitor_id=monitor.id, content_hash=content_hash, content=content_text,
            price_value=price, metadata={**result.metadata, "content_kind":"json" if isinstance(result.content, (dict, list)) else "text"})
        event = self._detect(monitor, previous, snapshot)
        self.db.insert_snapshot(snapshot, retain=int(monitor.config.get("snapshot_retention", 30)))
        duration = round((time.perf_counter() - started_clock) * 1000)
        monitor.last_checked_at, monitor.next_check_at, monitor.last_status = utc_now(), utc_now() + timedelta(seconds=monitor.interval_seconds), "ok"
        monitor.error_count, monitor.last_error, monitor.last_duration_ms = 0, None, duration
        monitor.total_checks += 1
        if event:
            monitor.total_changes += 1
            self.queue.add_event(event)
        self.db.update_monitor(monitor)
        self.db.insert_check_run(CheckRun(str(uuid4()),monitor.id,"ok",started,utc_now(),duration,bool(event),result.status_code,int(result.metadata.get("bytes") or 0)))
        if event:
            analysis = await self.analyzer.analyze(event, monitor)
            if analysis:
                event.ai_summary, event.ai_analyzed = analysis, True
                self.db.update_event_analysis(event.id, analysis)
            action_result = await self.auto_actions.execute(monitor, event)
            if action_result is not None:
                event.ai_summary = ((event.ai_summary + "\n\n") if event.ai_summary else "") + "Auto-action: " + json.dumps(action_result, ensure_ascii=False)
                self.db.update_event_analysis(event.id, event.ai_summary)
            if self.notifier:
                await self.notifier.dispatch(event, monitor)
            await self._emit("alert.created", {"monitor":monitor.public_dict(),"event":event.to_dict()})
        await self._emit("check.completed", {"monitor":monitor.public_dict(),"changed":bool(event),"duration_ms":duration})
        return event

    def _detect(self, monitor: Monitor, previous: Snapshot | None, current: Snapshot) -> Event | None:
        if previous is None:
            return None
        method = str(monitor.config.get("change_detection") or ("threshold" if current.price_value is not None and monitor.config.get("thresholds") else "hash"))
        if method == "threshold":
            thresholds = monitor.config.get("thresholds") or {}
            if any(isinstance(value, dict) for value in thresholds.values()):
                field = str(monitor.config.get("threshold_field") or next(iter(thresholds), "price")); thresholds = thresholds.get(field, {})
            detected = ThresholdDetector().detect(previous.price_value, current.price_value, thresholds)
            event_type = "price_change" if self._threshold_field(monitor) == "price" else "threshold_change"
        elif method == "diff":
            detected = DiffDetector().detect(previous.content, current.content, ignore_patterns=monitor.config.get("ignore_patterns"))
            event_type = "content_change"
        else:
            detected = HashDetector().detect(previous.content, current.content, ignore_patterns=monitor.config.get("ignore_patterns"))
            event_type = "content_change"
        if not detected.changed:
            return None
        old, new = detected.old_value, detected.new_value
        return Event(id=str(uuid4()),monitor_id=monitor.id,event_type=event_type,
            old_value=self._short(old),new_value=self._short(new),change_summary=detected.summary,severity=detected.severity)

    async def _record_failure(self, monitor: Monitor, started: Any, clock: float, error: str, *, status: str = "error", http_status: int | None = None) -> Event | None:
        duration = round((time.perf_counter() - clock) * 1000)
        monitor.error_count += 1; monitor.total_checks += 1; monitor.last_checked_at = utc_now(); monitor.last_status = status
        monitor.last_duration_ms, monitor.last_error = duration, error[:1000]
        retry_base = int(monitor.config.get("retry_base_seconds", 60))
        backoff = min(retry_base * (2 ** max(0, monitor.error_count - 1)), int(monitor.config.get("max_backoff_seconds", 21600)))
        monitor.next_check_at = utc_now() + timedelta(seconds=backoff + random.uniform(0, min(backoff * .1, 30)))
        event = None
        if monitor.error_count >= int(monitor.config.get("failure_limit", self.failure_limit)):
            monitor.enabled = False
            event = Event(id=str(uuid4()),monitor_id=monitor.id,event_type="monitor_paused",old_value=str(monitor.error_count - 1),new_value=str(monitor.error_count),
                change_summary=f"Monitor auto-paused after {monitor.error_count} consecutive failures: {error}",severity="critical")
            self.queue.add_event(event)
        self.db.update_monitor(monitor)
        self.db.insert_check_run(CheckRun(str(uuid4()),monitor.id,status,started,utc_now(),duration,False,http_status,0,error[:1000]))
        if event and self.notifier: await self.notifier.dispatch(event, monitor)
        await self._emit("check.failed", {"monitor":monitor.public_dict(),"error":error,"auto_paused":not monitor.enabled})
        return event

    async def run_once(self) -> list[Event | None]:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        async def run_one(monitor: Monitor) -> Event | None:
            async with semaphore: return await self.check_monitor(monitor)
        due = self.db.claim_due_monitors(self.instance_id, limit=self.max_concurrency * 4)
        return await asyncio.gather(*(run_one(monitor) for monitor in due))

    async def run(self, poll_seconds: float = 5.0) -> None:
        self.running, self._stop = True, asyncio.Event()
        await self._emit("service.started", {})
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                    if self.notifier:
                        await self.notifier.retry_failed()
                except Exception:
                    logger.exception("Watcher scheduler tick failed; the service will retry")
                try: await asyncio.wait_for(self._stop.wait(), timeout=max(.5, poll_seconds))
                except asyncio.TimeoutError: pass
        finally:
            self.running = False
            await self._emit("service.stopped", {})

    def stop(self) -> None:
        self.running = False; self._stop.set()

    async def close(self) -> None:
        self.stop()
        await asyncio.gather(*(fetcher.close() for fetcher in self.fetchers.values()), return_exceptions=True)

    async def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                result = self.on_event(name, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Watcher event subscriber failed for %s", name)

    @staticmethod
    def _short(value: Any, limit: int = 4000) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
        return text if len(text) <= limit else text[:limit - 1] + "…"

    @staticmethod
    def _threshold_field(monitor: Monitor) -> str:
        return str(monitor.config.get("threshold_field") or "price")

    def _numeric_value(self, content: Any, monitor: Monitor) -> float | None:
        field = self._threshold_field(monitor)
        value = content.get(field) if isinstance(content, dict) else content
        if value is None and isinstance(content, dict):
            value = next((item for item in content.values() if isinstance(item, (int, float))), None)
        try: return float(value) if value is not None and not isinstance(value, bool) else None
        except (TypeError, ValueError): return None
