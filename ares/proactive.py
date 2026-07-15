"""Low-noise proactive follow-ups for durable Ares goals."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4


logger = logging.getLogger(__name__)
DeliveryCallback = Callable[[str, dict[str, Any]], Iterable[str] | Awaitable[Iterable[str]]]


def utc_now(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock(value: str, fallback: time) -> time:
    try:
        return time.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    local = now.astimezone()
    start_at = _clock(start, time(22, 0))
    end_at = _clock(end, time(8, 0))
    if start_at == end_at:
        return False
    current = local.time().replace(tzinfo=None)
    if start_at < end_at:
        return start_at <= current < end_at
    return current >= start_at or current < end_at


@dataclass(frozen=True)
class ProactiveDecision:
    """Inspectable outcome of one initiative evaluation."""

    decision: str
    reason: str
    candidate_type: str | None = None
    candidate_id: str | None = None
    confidence: float = 0.0
    message: str = ""
    channels: tuple[str, ...] = ()
    event_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["channels"] = list(self.channels)
        return payload


class ProactiveStore:
    """Audit and atomically claim follow-ups across Ares processes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proactive_events (
                event_id TEXT PRIMARY KEY,
                candidate_type TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                channels_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_daily
                ON proactive_events(decision, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_proactive_candidate
                ON proactive_events(candidate_type, candidate_id, created_at DESC);
            """
        )
        self.conn.commit()

    def claim_send(
        self,
        *,
        candidate_type: str,
        candidate_id: str,
        confidence: float,
        message: str,
        now: datetime,
        cooldown: timedelta,
        maximum_per_day: int,
    ) -> tuple[str | None, str]:
        """Claim a send if global and per-candidate anti-spam rules allow it."""
        if maximum_per_day <= 0:
            return None, "daily proactive messages are disabled"
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        local = now.astimezone()
        local_midnight = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
        day_start = utc_now(local_midnight)
        stale_claim = utc_now(now - timedelta(minutes=15))
        cooldown_start = utc_now(now - cooldown)
        event_id = uuid4().hex
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            count_row = self.conn.execute(
                """SELECT COUNT(*) AS count FROM proactive_events
                   WHERE decision='send' AND created_at>=?
                     AND (status='sent' OR (status='claimed' AND created_at>=?))""",
                (day_start, stale_claim),
            ).fetchone()
            if int(count_row["count"] if count_row is not None else 0) >= maximum_per_day:
                self.conn.rollback()
                return None, "daily proactive message limit reached"

            recent = self.conn.execute(
                """SELECT status, created_at FROM proactive_events
                   WHERE candidate_type=? AND candidate_id=? AND decision='send'
                     AND ((status='sent' AND created_at>=?)
                          OR (status='claimed' AND created_at>=?))
                   ORDER BY created_at DESC LIMIT 1""",
                (candidate_type, candidate_id, cooldown_start, stale_claim),
            ).fetchone()
            if recent is not None:
                self.conn.rollback()
                reason = (
                    "another worker is delivering this follow-up"
                    if recent["status"] == "claimed"
                    else "candidate reminder cooldown is active"
                )
                return None, reason

            self.conn.execute(
                """INSERT INTO proactive_events
                   (event_id, candidate_type, candidate_id, decision, status,
                    confidence, reason, message, channels_json, created_at, completed_at)
                   VALUES (?, ?, ?, 'send', 'claimed', ?, '', ?, '[]', ?, NULL)""",
                (
                    event_id,
                    candidate_type,
                    candidate_id,
                    float(confidence),
                    message,
                    utc_now(now),
                ),
            )
            self.conn.commit()
            return event_id, "claimed"
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def finish(
        self,
        event_id: str,
        *,
        status: str,
        reason: str,
        channels: Iterable[str] = (),
        now: datetime | None = None,
    ) -> None:
        clean_channels = sorted({str(channel).strip() for channel in channels if str(channel).strip()})
        self.conn.execute(
            """UPDATE proactive_events SET status=?, reason=?, channels_json=?, completed_at=?
               WHERE event_id=?""",
            (
                status,
                str(reason)[:1_000],
                json.dumps(clean_channels, ensure_ascii=False),
                utc_now(now),
                event_id,
            ),
        )
        self.conn.commit()

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM proactive_events ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["channels"] = json.loads(item.pop("channels_json"))
            except (TypeError, json.JSONDecodeError):
                item["channels"] = []
            values.append(item)
        return values


class ProactiveService:
    """Evaluate inactive goals and deliver at most one useful nudge per tick."""

    def __init__(
        self,
        *,
        goal_store: Any,
        config: Any,
        deliver: DeliveryCallback,
        store: ProactiveStore | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.goal_store = goal_store
        self.config = config
        self.deliver = deliver
        self.store = store or ProactiveStore(goal_store.conn)
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @staticmethod
    def _message(goal: dict[str, Any], now: datetime, inactive_days: int) -> str:
        title = " ".join(str(goal.get("title") or f"Goal #{goal.get('goal_id')}").split())[:180]
        last_activity = _parse_timestamp(goal.get("last_activity_at"))
        days = max(inactive_days, (now - last_activity).days if last_activity else inactive_days)
        prefix = f"A quick nudge on “{title}”: it has been inactive for {days} days."
        blockers = goal.get("blockers") or []
        if blockers:
            blocker = blockers[0]
            description = blocker.get("description") if isinstance(blocker, dict) else str(blocker)
            return f"{prefix} The current blocker is: {description}. Want me to help clear it?"
        next_action = " ".join(str(goal.get("next_action") or "").split())
        if next_action:
            return f"{prefix} The next action is: {next_action}. Want me to help you take that step?"
        return f"{prefix} It has no concrete next action yet. Want me to turn it into one small step?"

    async def tick(self) -> ProactiveDecision:
        config = self.config
        if not bool(getattr(config, "enabled", True)):
            return ProactiveDecision("no_action", "proactive engine is disabled")
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        if _in_quiet_hours(
            now,
            str(getattr(config, "quiet_hours_start", "22:00")),
            str(getattr(config, "quiet_hours_end", "08:00")),
        ):
            return ProactiveDecision("no_action", "quiet hours are active")

        inactive_days = max(1, int(getattr(config, "inactive_goal_days", 3)))
        cutoff = utc_now(now - timedelta(days=inactive_days))
        candidates = self.goal_store.inactive(before=cutoff, limit=50)
        threshold = float(getattr(config, "min_confidence", 0.8))
        cooldown = timedelta(hours=max(1, int(getattr(config, "reminder_cooldown_hours", 72))))
        last_reason = "no eligible inactive goals"

        for goal in candidates:
            confidence = float(goal.get("confidence", 0.0) or 0.0)
            goal_id = str(goal.get("goal_id") or "")
            if not goal_id or confidence < threshold:
                last_reason = "inactive goals were below the confidence threshold"
                continue
            last_reminder = _parse_timestamp(goal.get("last_reminder_at"))
            if last_reminder is not None and now - last_reminder < cooldown:
                last_reason = "inactive goals are still in reminder cooldown"
                continue

            message = self._message(goal, now, inactive_days)
            event_id, claim_reason = self.store.claim_send(
                candidate_type="goal",
                candidate_id=goal_id,
                confidence=confidence,
                message=message,
                now=now,
                cooldown=cooldown,
                maximum_per_day=int(getattr(config, "max_messages_per_day", 1)),
            )
            if event_id is None:
                last_reason = claim_reason
                continue

            try:
                delivered = self.deliver(message, goal)
                if inspect.isawaitable(delivered):
                    delivered = await delivered
                channels = tuple(sorted({str(value) for value in (delivered or []) if str(value)}))
            except Exception as exc:
                logger.exception("Proactive goal follow-up delivery failed")
                self.store.finish(event_id, status="failed", reason=str(exc), now=now)
                return ProactiveDecision(
                    "no_action",
                    "follow-up delivery failed",
                    "goal",
                    goal_id,
                    confidence,
                    message,
                    event_id=event_id,
                )

            if not channels:
                self.store.finish(
                    event_id,
                    status="failed",
                    reason="no enabled delivery channel accepted the message",
                    now=now,
                )
                return ProactiveDecision(
                    "no_action",
                    "no enabled delivery channel accepted the message",
                    "goal",
                    goal_id,
                    confidence,
                    message,
                    event_id=event_id,
                )

            self.goal_store.mark_reminded(int(goal_id), when=utc_now(now))
            self.store.finish(
                event_id,
                status="sent",
                reason="inactive goal follow-up delivered",
                channels=channels,
                now=now,
            )
            return ProactiveDecision(
                "send",
                "inactive goal follow-up delivered",
                "goal",
                goal_id,
                confidence,
                message,
                channels,
                event_id,
            )

        return ProactiveDecision("no_action", last_reason)

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Proactive initiative tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(30, int(getattr(self.config, "poll_seconds", 900))),
                )
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if self.running or not bool(getattr(self.config, "enabled", True)):
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="ares-proactive-engine")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None


__all__ = ["ProactiveDecision", "ProactiveService", "ProactiveStore"]
