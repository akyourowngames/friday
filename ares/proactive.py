"""Low-noise proactive initiative over durable Ares state."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ares.context_blend import (
    format_goals,
    format_memories,
    format_recent_conversations,
    truncate_to_tokens,
)


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


def _parse_date(value: Any) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


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


def _clean(value: Any, maximum: int = 500) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _ensure_column(conn: sqlite3.Connection, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(proactive_events)")}
    if column not in columns:
        conn.execute(f"ALTER TABLE proactive_events ADD COLUMN {column} {definition}")


@dataclass(frozen=True)
class ProactiveCandidate:
    """One deterministic reason that Ares could take initiative."""

    candidate_type: str
    candidate_id: str
    entity_type: str
    entity_id: str
    title: str
    reason: str
    confidence: float
    proposed_message: str
    priority: int
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def delivery_payload(self) -> dict[str, Any]:
        return {
            **self.data,
            "candidate_type": self.candidate_type,
            "candidate_id": self.candidate_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
        }


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


class InitiativeDecision(BaseModel):
    decision: Literal["send", "no_action"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=1_000)
    message: str = Field(default="", max_length=2_000)


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
                candidate_json TEXT NOT NULL DEFAULT '{}',
                context_json TEXT NOT NULL DEFAULT '{}',
                model_decision_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_daily
                ON proactive_events(decision, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_proactive_candidate
                ON proactive_events(candidate_type, candidate_id, created_at DESC);
            """
        )
        _ensure_column(self.conn, "candidate_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(self.conn, "context_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(self.conn, "model_decision_json", "TEXT NOT NULL DEFAULT '{}'")
        self.conn.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def record_decision(
        self,
        *,
        candidate: ProactiveCandidate,
        decision: InitiativeDecision,
        context: str,
        now: datetime,
        status: str = "evaluated",
        outcome_reason: str | None = None,
    ) -> str:
        if status not in {"evaluated", "blocked"}:
            raise ValueError("decision audit status must be evaluated or blocked")
        event_id = uuid4().hex
        self.conn.execute(
            """INSERT INTO proactive_events
               (event_id, candidate_type, candidate_id, decision, status,
                confidence, reason, message, channels_json, candidate_json,
                context_json, model_decision_json, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)""",
            (
                event_id,
                candidate.candidate_type,
                candidate.candidate_id,
                decision.decision,
                status,
                float(decision.confidence),
                _clean(outcome_reason or decision.reason, 1_000),
                _clean(decision.message, 2_000),
                self._json(candidate.as_dict()),
                self._json({"context": context}),
                self._json(decision.model_dump()),
                utc_now(now),
                utc_now(now),
            ),
        )
        self.conn.commit()
        return event_id

    def blocking_reason(
        self,
        *,
        candidate_type: str,
        candidate_id: str,
        now: datetime,
        send_cooldown: timedelta,
        decision_cooldown: timedelta,
        failed_retry: timedelta,
    ) -> str | None:
        row = self.conn.execute(
            """SELECT decision, status, created_at FROM proactive_events
               WHERE candidate_type=? AND candidate_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (candidate_type, candidate_id),
        ).fetchone()
        if row is None:
            return None
        created = _parse_timestamp(row["created_at"])
        if created is None:
            return None
        age = now - created
        if row["status"] == "claimed" and age < timedelta(minutes=15):
            return "another worker is delivering this follow-up"
        if row["decision"] == "send" and row["status"] == "sent" and age < send_cooldown:
            return "candidate reminder cooldown is active"
        if row["status"] == "failed" and age < failed_retry:
            return "failed delivery retry cooldown is active"
        if row["status"] == "blocked" and age < decision_cooldown:
            return "blocked initiative cooldown is active"
        if row["decision"] == "no_action" and age < decision_cooldown:
            return "initiative decision cooldown is active"
        return None

    def claim_send(
        self,
        *,
        candidate: ProactiveCandidate,
        decision: InitiativeDecision,
        context: str,
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
                (
                    candidate.candidate_type,
                    candidate.candidate_id,
                    cooldown_start,
                    stale_claim,
                ),
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
                    confidence, reason, message, channels_json, candidate_json,
                    context_json, model_decision_json, created_at, completed_at)
                   VALUES (?, ?, ?, 'send', 'claimed', ?, ?, ?, '[]', ?, ?, ?, ?, NULL)""",
                (
                    event_id,
                    candidate.candidate_type,
                    candidate.candidate_id,
                    float(decision.confidence),
                    _clean(decision.reason, 1_000),
                    _clean(decision.message, 2_000),
                    self._json(candidate.as_dict()),
                    self._json({"context": context}),
                    self._json(decision.model_dump()),
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
            "SELECT * FROM proactive_events ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for source, target, fallback in (
                ("channels_json", "channels", []),
                ("candidate_json", "candidate", {}),
                ("context_json", "initiative_context", {}),
                ("model_decision_json", "model_decision", {}),
            ):
                try:
                    item[target] = json.loads(item.pop(source))
                except (TypeError, json.JSONDecodeError):
                    item.pop(source, None)
                    item[target] = fallback
            values.append(item)
        return values


class ProactiveService:
    """Evaluate durable initiative signals and deliver at most one useful nudge."""

    def __init__(
        self,
        *,
        goal_store: Any,
        config: Any,
        deliver: DeliveryCallback,
        commitment_store: Any | None = None,
        follow_up_store: Any | None = None,
        memory_store: Any | None = None,
        profile_manager: Any | None = None,
        conversation_store: Any | None = None,
        llm_client: Any | None = None,
        store: ProactiveStore | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.goal_store = goal_store
        self.commitment_store = commitment_store
        self.follow_up_store = follow_up_store
        self.memory_store = memory_store
        self.profile_manager = profile_manager
        self.conversation_store = conversation_store
        self.llm = llm_client
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
    def _goal_data(goal: dict[str, Any]) -> dict[str, Any]:
        return {
            key: goal.get(key)
            for key in (
                "goal_id", "title", "priority", "status", "target_date",
                "progress_percent", "next_action", "blockers", "confidence",
                "last_activity_at", "last_reminder_at",
            )
        }

    @staticmethod
    def _commitment_data(commitment: dict[str, Any]) -> dict[str, Any]:
        return {
            key: commitment.get(key)
            for key in (
                "commitment_id", "description", "owner", "status", "due_at",
                "confidence", "last_activity_at", "last_reminder_at",
                "source_conversation_id", "source_reflection_id",
            )
        }

    @staticmethod
    def _follow_up_data(follow_up: dict[str, Any]) -> dict[str, Any]:
        return {
            key: follow_up.get(key)
            for key in (
                "follow_up_id", "description", "status", "confidence",
                "source_conversation_id", "source_reflection_id", "eligible_at",
                "cooldown_hours", "last_attempt_at", "last_delivered_at",
            )
        }

    def collect_candidates(self, *, now: datetime | None = None) -> list[ProactiveCandidate]:
        """Create a stable, deterministic candidate list without using an LLM."""
        current = now or self.now_provider()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        local_today = current.astimezone().date()
        due_window = max(0, int(getattr(self.config, "due_soon_days", 7)))
        inactive_days = max(1, int(getattr(self.config, "inactive_goal_days", 3)))
        candidates: list[ProactiveCandidate] = []

        goals = self.goal_store.list_all(statuses=["active"], limit=200)
        for goal in goals:
            goal_id = str(goal.get("goal_id") or "")
            if not goal_id:
                continue
            title = _clean(goal.get("title") or f"Goal #{goal_id}", 180)
            confidence = float(goal.get("confidence", 0.0) or 0.0)
            target = _parse_date(goal.get("target_date"))
            next_action = _clean(goal.get("next_action"), 300)
            if target is not None:
                days = (target - local_today).days
                if days < 0:
                    overdue = abs(days)
                    message = f"“{title}” was due {target.isoformat()} and is {overdue} day"
                    message += "s overdue." if overdue != 1 else " overdue."
                    if next_action:
                        message += f" The next action is: {next_action}."
                    message += " Want me to help move it forward?"
                    candidates.append(ProactiveCandidate(
                        "goal_overdue", goal_id, "goal", goal_id, title,
                        f"goal deadline passed {overdue} day(s) ago", confidence,
                        message, 0, self._goal_data(goal),
                    ))
                elif days <= due_window:
                    timing = "today" if days == 0 else f"in {days} day(s)"
                    message = f"“{title}” is due {timing} ({target.isoformat()})."
                    if next_action:
                        message += f" The next action is: {next_action}."
                    message += " Want me to help with that step?"
                    candidates.append(ProactiveCandidate(
                        "goal_due_soon", goal_id, "goal", goal_id, title,
                        f"goal is due {timing}", confidence, message, 1,
                        self._goal_data(goal),
                    ))

            for index, blocker in enumerate((goal.get("blockers") or [])[:5]):
                description = _clean(
                    blocker.get("description") if isinstance(blocker, dict) else blocker,
                    500,
                )
                if not description:
                    continue
                blocker_id = (
                    str(blocker.get("blocker_id") or index + 1)
                    if isinstance(blocker, dict) else str(index + 1)
                )
                candidates.append(ProactiveCandidate(
                    "goal_blocker", f"{goal_id}:{blocker_id}", "goal", goal_id,
                    title, f"active blocker: {description}", confidence,
                    f"“{title}” is blocked by: {description}. Want me to help clear it?",
                    2, self._goal_data(goal),
                ))

            last_activity = _parse_timestamp(goal.get("last_activity_at"))
            if last_activity is None or current - last_activity >= timedelta(days=inactive_days):
                days = max(
                    inactive_days,
                    (current - last_activity).days if last_activity is not None else inactive_days,
                )
                message = f"A quick nudge on “{title}”: it has been inactive for {days} days."
                if next_action:
                    message += f" The next action is: {next_action}."
                else:
                    message += " It has no concrete next action yet."
                message += " Want me to help with the next step?"
                candidates.append(ProactiveCandidate(
                    "goal_inactive", goal_id, "goal", goal_id, title,
                    f"goal has been inactive for {days} days", confidence,
                    message, 6, self._goal_data(goal),
                ))

        if self.commitment_store is not None:
            commitment_inactive_days = max(
                1, int(getattr(self.config, "inactive_commitment_days", 3))
            )
            for item in self.commitment_store.list_pending(limit=200):
                commitment_id = str(item.get("commitment_id") or "")
                if not commitment_id:
                    continue
                description = _clean(item.get("description"), 500)
                confidence = float(item.get("confidence", 0.0) or 0.0)
                due_at = _parse_timestamp(item.get("due_at"))
                candidate_type = "commitment_pending"
                priority = 5
                reason = "unfinished commitment is still pending"
                timing = ""
                eligible = False
                if due_at is not None and due_at < current:
                    eligible = True
                    candidate_type = "commitment_overdue"
                    priority = 1
                    reason = "pending commitment is overdue"
                    timing = f" It was due {due_at.astimezone().strftime('%Y-%m-%d %H:%M')}."
                elif due_at is not None and due_at <= current + timedelta(days=due_window):
                    eligible = True
                    candidate_type = "commitment_due_soon"
                    priority = 3
                    reason = "pending commitment is due soon"
                    timing = f" It is due {due_at.astimezone().strftime('%Y-%m-%d %H:%M')}."
                else:
                    last_activity = _parse_timestamp(item.get("last_activity_at"))
                    if last_activity is not None:
                        inactive = (current - last_activity).days
                        if inactive >= commitment_inactive_days:
                            eligible = True
                            reason = f"unfinished commitment has been inactive for {inactive} days"
                if not eligible:
                    continue
                message = f"You still have this commitment pending: “{description}”.{timing}"
                message += " Want me to help you close it out?"
                candidates.append(ProactiveCandidate(
                    candidate_type, commitment_id, "commitment", commitment_id,
                    description, reason, confidence, message, priority,
                    self._commitment_data(item),
                ))

        if self.follow_up_store is not None:
            for item in self.follow_up_store.list_eligible(now=utc_now(current), limit=100):
                follow_up_id = str(item.get("follow_up_id") or "")
                if not follow_up_id:
                    continue
                description = _clean(item.get("description"), 800)
                candidates.append(ProactiveCandidate(
                    "reflection_follow_up", follow_up_id, "follow_up", follow_up_id,
                    description, "reflection identified an eligible follow-up",
                    float(item.get("confidence", 0.0) or 0.0),
                    f"Following up on this: {description}",
                    4, self._follow_up_data(item),
                ))

        return sorted(
            candidates,
            key=lambda item: (
                item.priority,
                -item.confidence,
                item.candidate_type,
                item.candidate_id,
            ),
        )

    def build_initiative_context(self, candidate: ProactiveCandidate) -> str:
        """Build a bounded, read-only context specifically for one candidate."""
        budget = max(
            400,
            min(int(getattr(self.config, "initiative_context_token_budget", 1_800)), 8_000),
        )
        slice_budget = max(100, budget // 5)
        query = " ".join((candidate.title, candidate.reason, candidate.proposed_message))
        memories = (
            self.memory_store.search(query, limit=8, scope="all")
            if self.memory_store is not None else []
        )
        profile = (
            self.profile_manager.get_context(token_budget=slice_budget)
            if self.profile_manager is not None else ""
        )
        conversations = (
            self.conversation_store.get_recent_context_messages(limit=10)
            if self.conversation_store is not None else []
        )
        goals = self.goal_store.list_all(statuses=["active"], limit=10)
        sections = [
            "## Initiative Candidate\n" + json.dumps(
                candidate.as_dict(), ensure_ascii=False, sort_keys=True, default=str,
            ),
            profile or "## User Profile\nNo saved profile details.",
            format_memories(memories, token_budget=slice_budget)
            or "## What I know about you\nNo relevant durable memories.",
            format_recent_conversations(conversations, token_budget=slice_budget)
            or "## Recent Conversations\nNo recent conversation context.",
            format_goals(goals, token_budget=slice_budget)
            or "## Goals\nNo active goals.",
        ]
        return truncate_to_tokens("\n\n".join(sections), budget)

    @staticmethod
    def _parse_model_decision(content: str) -> InitiativeDecision:
        clean = str(content or "").strip()
        if clean.startswith("'''"):
            clean = re.sub(r"^'''(?:json)?\s*|\s*'''$", "", clean, flags=re.IGNORECASE)
        if clean.startswith(chr(96) * 3):
            clean = re.sub(
                "^" + chr(96) * 3 + r"(?:json)?\s*|\s*" + chr(96) * 3 + "$",
                "",
                clean,
                flags=re.IGNORECASE,
            )
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, re.DOTALL)
            if not match:
                raise ValueError("initiative model did not return a JSON object")
            payload = json.loads(match.group())
        if not isinstance(payload, dict):
            raise ValueError("initiative model did not return a JSON object")
        # A no_action response may use JSON null because there is no message to
        # deliver. Normalize it to the schema's empty-message default.
        if payload.get("message") is None:
            payload["message"] = ""
        return InitiativeDecision.model_validate(payload)

    async def _decide(
        self,
        candidate: ProactiveCandidate,
        context: str,
    ) -> InitiativeDecision:
        if self.llm is None:
            return InitiativeDecision(
                decision="no_action",
                confidence=0.0,
                reason="initiative decision model is unavailable",
            )
        prompt = (
            "You are Ares' low-noise initiative gate. Decide whether one proactive message would be "
            "useful right now. Return ONLY JSON with decision (send or no_action), confidence (0 to 1), "
            "reason, and message. Choose no_action when the candidate is stale, redundant, too vague, "
            "poorly supported, or likely to interrupt without value. Never claim work was performed. "
            "If sending, write one concise, specific follow-up grounded in the candidate and context.\n\n"
            f"{context}"
        )
        try:
            response = await asyncio.wait_for(
                self.llm.chat([{"role": "user", "content": prompt}], tools=[]),
                timeout=float(getattr(self.config, "decision_timeout_seconds", 30)),
            )
            decision = self._parse_model_decision(str(response.get("content") or "{}"))
            if decision.decision == "send" and not _clean(decision.message, 2_000):
                return InitiativeDecision(
                    decision="no_action",
                    confidence=decision.confidence,
                    reason="initiative model returned an empty message",
                )
            return decision
        except Exception as exc:
            # Proactive initiative is optional. Provider refusal/empty errors
            # already degrade to no_action and should not pollute normal chat.
            logger.debug(
                "Proactive initiative decision skipped after %s: %r",
                type(exc).__name__, exc,
            )
            return InitiativeDecision(
                decision="no_action",
                confidence=0.0,
                reason=f"initiative decision failed: {type(exc).__name__}",
            )

    def _entity_in_cooldown(
        self,
        candidate: ProactiveCandidate,
        now: datetime,
        cooldown: timedelta,
    ) -> bool:
        if candidate.entity_type == "follow_up":
            return False
        last_reminder = _parse_timestamp(candidate.data.get("last_reminder_at"))
        return last_reminder is not None and now - last_reminder < cooldown

    def _candidate_cooldown(self, candidate: ProactiveCandidate) -> timedelta:
        if candidate.entity_type == "follow_up":
            hours = int(candidate.data.get("cooldown_hours") or 72)
        else:
            hours = int(getattr(self.config, "reminder_cooldown_hours", 72))
        return timedelta(hours=max(1, min(hours, 8_760)))

    def _mark_delivered(self, candidate: ProactiveCandidate, now: datetime) -> None:
        when = utc_now(now)
        if candidate.entity_type == "goal":
            self.goal_store.mark_reminded(int(candidate.entity_id), when=when)
        elif candidate.entity_type == "commitment" and self.commitment_store is not None:
            self.commitment_store.mark_reminded(int(candidate.entity_id), when=when)
        elif candidate.entity_type == "follow_up" and self.follow_up_store is not None:
            self.follow_up_store.defer(
                candidate.entity_id,
                eligible_at=utc_now(now + self._candidate_cooldown(candidate)),
                delivered=True,
                when=now,
            )

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
        if int(getattr(config, "max_messages_per_day", 1)) <= 0:
            return ProactiveDecision("no_action", "daily proactive messages are disabled")

        threshold = float(getattr(config, "min_confidence", 0.8))
        decision_cooldown = timedelta(
            hours=max(1, int(getattr(config, "decision_cooldown_hours", 24)))
        )
        failed_retry = timedelta(
            hours=max(1, int(getattr(config, "failed_delivery_retry_hours", 1)))
        )
        candidate: ProactiveCandidate | None = None
        last_reason = "no eligible proactive candidates"
        for item in self.collect_candidates(now=now):
            if item.confidence < threshold:
                last_reason = "proactive candidates were below the confidence threshold"
                continue
            send_cooldown = self._candidate_cooldown(item)
            if self._entity_in_cooldown(item, now, send_cooldown):
                last_reason = "candidate entity reminder cooldown is active"
                continue
            blocked = self.store.blocking_reason(
                candidate_type=item.candidate_type,
                candidate_id=item.candidate_id,
                now=now,
                send_cooldown=send_cooldown,
                decision_cooldown=decision_cooldown,
                failed_retry=failed_retry,
            )
            if blocked:
                last_reason = blocked
                continue
            candidate = item
            break

        if candidate is None:
            return ProactiveDecision("no_action", last_reason)

        context = self.build_initiative_context(candidate)
        model_decision = await self._decide(candidate, context)
        if model_decision.decision != "send" or model_decision.confidence < threshold:
            if model_decision.decision == "send":
                model_decision = InitiativeDecision(
                    decision="no_action",
                    confidence=model_decision.confidence,
                    reason="initiative decision was below the confidence threshold",
                )
            event_id = self.store.record_decision(
                candidate=candidate,
                decision=model_decision,
                context=context,
                now=now,
            )
            return ProactiveDecision(
                "no_action",
                model_decision.reason,
                candidate.candidate_type,
                candidate.candidate_id,
                model_decision.confidence,
                model_decision.message,
                event_id=event_id,
            )

        cooldown = self._candidate_cooldown(candidate)
        event_id, claim_reason = self.store.claim_send(
            candidate=candidate,
            decision=model_decision,
            context=context,
            now=now,
            cooldown=cooldown,
            maximum_per_day=int(getattr(config, "max_messages_per_day", 1)),
        )
        if event_id is None:
            audit_id = self.store.record_decision(
                candidate=candidate,
                decision=model_decision,
                context=context,
                now=now,
                status="blocked",
                outcome_reason=claim_reason,
            )
            return ProactiveDecision(
                "no_action",
                claim_reason,
                candidate.candidate_type,
                candidate.candidate_id,
                model_decision.confidence,
                model_decision.message,
                event_id=audit_id,
            )

        try:
            delivered = self.deliver(model_decision.message, candidate.delivery_payload())
            if inspect.isawaitable(delivered):
                delivered = await delivered
            channels = tuple(sorted({str(value) for value in (delivered or []) if str(value)}))
        except Exception as exc:
            logger.exception("Proactive follow-up delivery failed")
            self.store.finish(event_id, status="failed", reason=str(exc), now=now)
            return ProactiveDecision(
                "no_action",
                "follow-up delivery failed",
                candidate.candidate_type,
                candidate.candidate_id,
                model_decision.confidence,
                model_decision.message,
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
                candidate.candidate_type,
                candidate.candidate_id,
                model_decision.confidence,
                model_decision.message,
                event_id=event_id,
            )

        self._mark_delivered(candidate, now)
        reason = f"{candidate.candidate_type} follow-up delivered"
        self.store.finish(event_id, status="sent", reason=reason, channels=channels, now=now)
        return ProactiveDecision(
            "send",
            reason,
            candidate.candidate_type,
            candidate.candidate_id,
            model_decision.confidence,
            model_decision.message,
            channels,
            event_id,
        )

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


__all__ = [
    "InitiativeDecision",
    "ProactiveCandidate",
    "ProactiveDecision",
    "ProactiveService",
    "ProactiveStore",
]
