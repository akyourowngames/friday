"""Durable, evidence-gated reflection after normal conversation turns."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from ares.followups import FollowUpStore, future_utc
from ares.llm import LLMClient
from ares.memory_policy import memory_rejection_reason


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class NewMemory(BaseModel):
    fact_text: str
    category: Literal["preference", "fact", "belief", "habit", "relationship", "note"] = "note"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str


class MemoryUpdate(BaseModel):
    fact_id: int
    fact_text: str | None = None
    category: Literal["preference", "fact", "belief", "habit", "relationship", "note"] | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str


class GoalMilestone(BaseModel):
    title: str
    target_date: str | None = None


class NewGoal(BaseModel):
    title: str
    description: str = ""
    category: str = "general"
    priority: Literal["low", "normal", "high"] = "normal"
    target_date: str | None = None
    milestones: list[GoalMilestone] = Field(default_factory=list)
    next_action: str
    blockers: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str


class GoalProgress(BaseModel):
    goal_id: int
    note: str
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    next_action: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str


class CompletedGoal(BaseModel):
    goal_id: int
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    evidence: str


class ProfileUpdate(BaseModel):
    section: Literal["Identity", "Preferences", "Current Projects", "Notes"]
    key: str
    value: str = ""
    operation: Literal["upsert", "remove"] = "upsert"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str


class CommitmentChange(BaseModel):
    commitment_id: int | None = None
    description: str
    owner: Literal["user", "ares", "shared"] = "user"
    status: Literal["pending", "completed", "cancelled"] = "pending"
    due_at: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str


class FollowUpOpportunity(BaseModel):
    description: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: str = ""
    eligible_at: str | None = None
    cooldown_hours: int | None = Field(default=None, ge=1, le=8_760)


class FollowUpResolution(BaseModel):
    follow_up_id: str
    status: Literal["resolved", "dismissed", "cancelled"] = "resolved"
    resolution: str
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence: str


class ReflectionResult(BaseModel):
    new_memories: list[NewMemory] = Field(default_factory=list)
    updated_memories: list[MemoryUpdate] = Field(default_factory=list)
    new_goals: list[NewGoal] = Field(default_factory=list)
    goal_progress: list[GoalProgress] = Field(default_factory=list)
    completed_goals: list[CompletedGoal] = Field(default_factory=list)
    profile_updates: list[ProfileUpdate] = Field(default_factory=list)
    commitments: list[CommitmentChange] = Field(default_factory=list)
    follow_up_opportunities: list[FollowUpOpportunity] = Field(default_factory=list)
    follow_up_resolutions: list[FollowUpResolution] = Field(default_factory=list)


class ReflectionStore:
    """Persist reflection inputs and outcomes so a crash cannot lose the job."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS reflection_runs (
                reflection_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                extracted_json TEXT,
                outcomes_json TEXT,
                error TEXT
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reflection_pending ON reflection_runs(status, created_at)"
        )
        # A process may have stopped after claiming but before finishing.
        self.conn.execute("UPDATE reflection_runs SET status='pending' WHERE status='running'")
        self.conn.commit()

    def enqueue(self, scope: str, user_text: str, assistant_text: str) -> str:
        reflection_id = uuid4().hex
        self.conn.execute(
            """INSERT INTO reflection_runs
               (reflection_id, scope, user_text, assistant_text, status, attempts,
                created_at, started_at, completed_at, extracted_json, outcomes_json, error)
               VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, NULL, NULL)""",
            (reflection_id, scope, user_text, assistant_text, utc_now()),
        )
        self.conn.commit()
        return reflection_id

    def get(self, reflection_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM reflection_runs WHERE reflection_id=?", (reflection_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def pending(self, *, scope: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        where = " AND scope=?" if scope is not None else ""
        params: list[Any] = [scope] if scope is not None else []
        rows = self.conn.execute(
            f"""SELECT * FROM reflection_runs WHERE status='pending'{where}
                ORDER BY created_at LIMIT ?""",
            [*params, max(1, min(int(limit), 100))],
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_running(self, reflection_id: str) -> dict[str, Any] | None:
        self.conn.execute(
            """UPDATE reflection_runs SET status='running', attempts=attempts+1,
               started_at=?, error=NULL WHERE reflection_id=? AND status='pending'""",
            (utc_now(), reflection_id),
        )
        self.conn.commit()
        return self.get(reflection_id)

    def complete(
        self,
        reflection_id: str,
        result: ReflectionResult,
        outcomes: list[dict[str, Any]],
    ) -> None:
        self.conn.execute(
            """UPDATE reflection_runs SET status='completed', completed_at=?,
               extracted_json=?, outcomes_json=?, error=NULL WHERE reflection_id=?""",
            (
                utc_now(),
                result.model_dump_json(),
                json.dumps(outcomes, ensure_ascii=False, sort_keys=True),
                reflection_id,
            ),
        )
        self.conn.commit()

    def fail(self, reflection_id: str, error: str, *, retry: bool) -> None:
        self.conn.execute(
            """UPDATE reflection_runs SET status=?, completed_at=?, error=?
               WHERE reflection_id=?""",
            ("pending" if retry else "failed", utc_now(), str(error)[:2_000], reflection_id),
        )
        self.conn.commit()


def _normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _supported_evidence(evidence: str, user_text: str) -> bool:
    needle = _normalized(evidence)
    return len(needle) >= 3 and needle in _normalized(user_text)


class ConversationReflector:
    """Extract structured changes with a separate, tool-free model call."""

    def __init__(self, llm_client: Any, config: Any) -> None:
        self.llm = llm_client
        self.config = config

    @staticmethod
    def _parse_json(text: str) -> ReflectionResult:
        clean = str(text or "").strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, re.DOTALL)
            if not match:
                raise ValueError("Reflection model did not return a JSON object")
            payload = json.loads(match.group())
        return ReflectionResult.model_validate(payload)

    async def extract(
        self,
        *,
        user_text: str,
        assistant_text: str,
        existing_memories: list[dict[str, Any]],
        active_goals: list[dict[str, Any]],
        pending_commitments: list[dict[str, Any]],
        open_followups: list[dict[str, Any]],
        profile_text: str,
    ) -> ReflectionResult:
        state = {
            "existing_memories": [
                {
                    "fact_id": item.get("fact_id"),
                    "fact_text": item.get("fact_text"),
                    "category": item.get("category"),
                }
                for item in existing_memories[:10]
            ],
            "active_goals": [
                {
                    "goal_id": item.get("goal_id"),
                    "title": item.get("title"),
                    "progress_percent": item.get("progress_percent"),
                    "revision": item.get("revision"),
                    "next_action": item.get("next_action"),
                }
                for item in active_goals[:12]
            ],
            "pending_commitments": [
                {
                    "commitment_id": item.get("commitment_id"),
                    "description": item.get("description"),
                    "owner": item.get("owner"),
                }
                for item in pending_commitments[:12]
            ],
            "open_followups": [
                {
                    "follow_up_id": item.get("follow_up_id"),
                    "description": item.get("description"),
                    "status": item.get("status"),
                }
                for item in open_followups[:12]
            ],
            "profile": profile_text[:4_000],
        }
        prompt = (
            "You are Ares' conservative conversation reflection process. Return ONLY one JSON object matching "
            "this schema: new_memories, updated_memories, new_goals, goal_progress, completed_goals, "
            "profile_updates, commitments, follow_up_opportunities, follow_up_resolutions; every value is an array.\n\n"
            "Only the USER text is evidence. Every proposed mutation must contain an exact short excerpt from "
            "the user text in its evidence field and a calibrated confidence from 0 to 1. Assistant text can "
            "help interpret the turn but is never evidence. Do not extract temporary requests, tool state, moods, "
            "guesses, or facts about the world. Update an existing record by ID instead of duplicating it. Mark a "
            "goal completed only when the user explicitly says that outcome is finished. Create a goal only for a "
            "clear durable outcome; include 2-5 milestones and one small specific next_action. A commitment is an "
            "explicit promise or obligation, not every request. A follow-up opportunity is a concrete future "
            "check-in that would help the user, not a generic suggestion. Resolve an open follow-up only when "
            "the user explicitly completes, dismisses, or cancels it. If nothing durable changed, return empty arrays.\n\n"
            f"CURRENT STATE:\n{json.dumps(state, ensure_ascii=False)}\n\n"
            f"USER:\n{user_text[:8_000]}\n\nASSISTANT:\n{assistant_text[:8_000]}\n"
        )
        response = await asyncio.wait_for(
            self.llm.chat([{"role": "user", "content": prompt}], tools=[]),
            timeout=float(getattr(self.config, "timeout_seconds", 45)),
        )
        return self._parse_json(str(response.get("content") or "{}"))


class ReflectionApplier:
    """Apply validated changes independently and audit every skip or mutation."""

    def __init__(
        self,
        *,
        memory_store: Any,
        goal_store: Any,
        commitment_store: Any,
        follow_up_store: FollowUpStore,
        profile_manager: Any,
        config: Any,
    ) -> None:
        self.memory_store = memory_store
        self.goal_store = goal_store
        self.commitment_store = commitment_store
        self.follow_up_store = follow_up_store
        self.profile_manager = profile_manager
        self.config = config

    def apply(
        self,
        result: ReflectionResult,
        *,
        user_text: str,
        reflection_id: str,
        scope: str,
    ) -> list[dict[str, Any]]:
        threshold = float(getattr(self.config, "min_confidence", 0.75))
        completion_threshold = float(getattr(self.config, "completion_min_confidence", 0.90))
        outcomes: list[dict[str, Any]] = []

        def record(kind: str, action: str, **details: Any) -> None:
            outcomes.append({"kind": kind, "action": action, **details})

        def allowed(kind: str, confidence: float, evidence: str, minimum: float = threshold) -> bool:
            if confidence < minimum:
                record(kind, "skipped", reason="low_confidence", confidence=confidence)
                return False
            if not _supported_evidence(evidence, user_text):
                record(kind, "skipped", reason="unsupported_evidence")
                return False
            return True

        for item in result.new_memories:
            if not allowed("new_memory", item.confidence, item.evidence):
                continue
            rejection = memory_rejection_reason(
                item.fact_text, category=item.category, confidence=item.confidence,
            )
            if rejection:
                record("new_memory", "skipped", reason=rejection)
                continue
            suggestions = self.memory_store.suggest_merge(item.fact_text, category=item.category)
            if any(candidate.get("kind") == "duplicate" for candidate in suggestions):
                record("new_memory", "skipped", reason="duplicate")
                continue
            if any(candidate.get("kind") == "possible_conflict" for candidate in suggestions):
                record("new_memory", "skipped", reason="possible_conflict_requires_id")
                continue
            try:
                fact_id = self.memory_store.store(
                    item.fact_text,
                    category=item.category,
                    confidence=item.confidence,
                    importance=item.importance,
                    source="conversation_reflection",
                    source_conversation_id=scope,
                    source_reflection_id=reflection_id,
                )
                record("new_memory", "created", fact_id=fact_id)
            except Exception as exc:
                record("new_memory", "error", error=str(exc)[:500])

        for item in result.updated_memories:
            if not allowed("updated_memory", item.confidence, item.evidence):
                continue
            existing = self.memory_store.get(item.fact_id)
            if existing is None:
                record("updated_memory", "skipped", reason="not_found", fact_id=item.fact_id)
                continue
            new_text = item.fact_text or existing.get("fact_text", "")
            category = item.category or existing.get("category", "note")
            rejection = memory_rejection_reason(new_text, category=category, confidence=item.confidence)
            if rejection:
                record("updated_memory", "skipped", reason=rejection, fact_id=item.fact_id)
                continue
            try:
                self.memory_store.update(
                    item.fact_id,
                    fact_text=item.fact_text,
                    category=item.category,
                    confidence=item.confidence,
                    importance=item.importance,
                    source="conversation_reflection",
                )
                record("updated_memory", "updated", fact_id=item.fact_id)
            except Exception as exc:
                record("updated_memory", "error", fact_id=item.fact_id, error=str(exc)[:500])

        for item in result.new_goals:
            if not allowed("new_goal", item.confidence, item.evidence):
                continue
            duplicate = next(
                (
                    goal for goal in self.goal_store.search(item.title, limit=10)
                    if _normalized(goal.get("title", "")) == _normalized(item.title)
                    and goal.get("status") in {"active", "paused"}
                ),
                None,
            )
            if duplicate is not None:
                record("new_goal", "skipped", reason="duplicate", goal_id=duplicate["goal_id"])
                continue
            try:
                goal = self.goal_store.create(
                    item.title,
                    description=item.description,
                    category=item.category,
                    priority=item.priority,
                    target_date=item.target_date,
                    source="reflection",
                    confidence=item.confidence,
                    milestones=[milestone.model_dump() for milestone in item.milestones],
                    next_action=item.next_action,
                    blockers=item.blockers,
                    source_conversation_id=scope,
                )
                record("new_goal", "created", goal_id=goal.get("goal_id"))
            except Exception as exc:
                record("new_goal", "error", error=str(exc)[:500])

        for item in result.goal_progress:
            if not allowed("goal_progress", item.confidence, item.evidence):
                continue
            goal = self.goal_store.get(item.goal_id)
            if goal is None:
                record("goal_progress", "skipped", reason="not_found", goal_id=item.goal_id)
                continue
            try:
                updated = self.goal_store.record_progress(
                    item.goal_id,
                    note=item.note,
                    progress_percent=item.progress_percent,
                    expected_revision=goal["revision"],
                )
                if item.next_action and updated is not None:
                    updated = self.goal_store.update(
                        item.goal_id,
                        expected_revision=updated["revision"],
                        next_action=item.next_action,
                    )
                record("goal_progress", "updated", goal_id=item.goal_id)
            except Exception as exc:
                record("goal_progress", "error", goal_id=item.goal_id, error=str(exc)[:500])

        for item in result.completed_goals:
            if not allowed(
                "completed_goal", item.confidence, item.evidence, minimum=completion_threshold,
            ):
                continue
            goal = self.goal_store.get(item.goal_id)
            if goal is None:
                record("completed_goal", "skipped", reason="not_found", goal_id=item.goal_id)
                continue
            if goal.get("status") == "completed":
                record("completed_goal", "skipped", reason="already_completed", goal_id=item.goal_id)
                continue
            try:
                self.goal_store.complete(item.goal_id, expected_revision=goal["revision"])
                record("completed_goal", "completed", goal_id=item.goal_id)
            except Exception as exc:
                record("completed_goal", "error", goal_id=item.goal_id, error=str(exc)[:500])

        profile_candidates: list[ProfileUpdate] = []
        for item in result.profile_updates:
            if allowed("profile_update", item.confidence, item.evidence):
                profile_candidates.append(item)
        try:
            applied_profile = self.profile_manager.apply_updates(profile_candidates)
            for item in applied_profile:
                record("profile_update", "updated", section=item["section"], key=item["key"])
        except Exception as exc:
            record("profile_update", "error", error=str(exc)[:500])

        for item in result.commitments:
            if not allowed("commitment", item.confidence, item.evidence):
                continue
            try:
                if item.commitment_id is not None:
                    existing = self.commitment_store.get(item.commitment_id)
                    if existing is None:
                        record("commitment", "skipped", reason="not_found", commitment_id=item.commitment_id)
                        continue
                    updated = self.commitment_store.update(
                        item.commitment_id,
                        description=item.description,
                        owner=item.owner,
                        status=item.status,
                        due_at=item.due_at,
                        confidence=item.confidence,
                    )
                    record("commitment", "updated", commitment_id=updated.get("commitment_id"))
                elif item.status == "pending":
                    created = self.commitment_store.create(
                        item.description,
                        owner=item.owner,
                        due_at=item.due_at,
                        confidence=item.confidence,
                        source_conversation_id=scope,
                        source_reflection_id=reflection_id,
                    )
                    record("commitment", "created", commitment_id=created.get("commitment_id"))
                else:
                    record("commitment", "skipped", reason="missing_id_for_terminal_status")
            except Exception as exc:
                record("commitment", "error", error=str(exc)[:500])

        for item in result.follow_up_opportunities:
            if not allowed("follow_up", item.confidence, item.evidence):
                continue
            try:
                created = self.follow_up_store.create(
                    item.description,
                    confidence=item.confidence,
                    source_conversation_id=scope,
                    source_reflection_id=reflection_id,
                    eligible_at=item.eligible_at or future_utc(
                        int(getattr(self.config, "follow_up_delay_hours", 24))
                    ),
                    cooldown_hours=item.cooldown_hours or int(
                        getattr(self.config, "follow_up_cooldown_hours", 72)
                    ),
                    evidence=item.evidence,
                )
                record("follow_up", "created", follow_up_id=created.get("follow_up_id"))
            except Exception as exc:
                record("follow_up", "error", error=str(exc)[:500])

        for item in result.follow_up_resolutions:
            if not allowed("follow_up_resolution", item.confidence, item.evidence):
                continue
            try:
                resolved = self.follow_up_store.resolve(
                    item.follow_up_id,
                    status=item.status,
                    resolution=item.resolution,
                )
                if resolved is None:
                    record(
                        "follow_up_resolution", "skipped",
                        reason="not_found_or_closed", follow_up_id=item.follow_up_id,
                    )
                else:
                    record(
                        "follow_up_resolution", item.status,
                        follow_up_id=item.follow_up_id,
                    )
            except Exception as exc:
                record(
                    "follow_up_resolution", "error",
                    follow_up_id=item.follow_up_id, error=str(exc)[:500],
                )
        return outcomes


class ReflectionService:
    """Queue reflection after replies while preserving same-session ordering."""

    def __init__(
        self,
        *,
        memory_store: Any,
        goal_store: Any,
        commitment_store: Any,
        profile_manager: Any,
        config: Any,
        llm_client: Any | None = None,
        follow_up_store: FollowUpStore | None = None,
    ) -> None:
        self.config = config
        self.store = ReflectionStore(memory_store.conn)
        self.follow_up_store = follow_up_store or FollowUpStore(connection=memory_store.conn)
        self._owns_llm = llm_client is None
        # ``config`` is normally ReflectionConfig, which contains extraction
        # policy but deliberately not model credentials.  Agent supplies its
        # already-configured LLM client in production.  For standalone use,
        # only pass a config through when it is a full application config;
        # otherwise let LLMClient load the shared app configuration itself.
        if llm_client is not None:
            self.llm = llm_client
        elif all(hasattr(config, field) for field in ("api_key", "api_base_url", "model")):
            self.llm = LLMClient(config=config)
        else:
            self.llm = LLMClient()
        self.reflector = ConversationReflector(self.llm, config)
        self.applier = ReflectionApplier(
            memory_store=memory_store,
            goal_store=goal_store,
            commitment_store=commitment_store,
            follow_up_store=self.follow_up_store,
            profile_manager=profile_manager,
            config=config,
        )
        self.memory_store = memory_store
        self.goal_store = goal_store
        self.commitment_store = commitment_store
        self.profile_manager = profile_manager
        self._tasks: set[asyncio.Task] = set()
        self._scope_tasks: dict[str, asyncio.Task] = {}
        self._apply_lock = asyncio.Lock()

    async def _process(self, reflection_id: str) -> None:
        job = self.store.mark_running(reflection_id)
        if job is None or job.get("status") != "running":
            return
        try:
            result = await self.reflector.extract(
                user_text=job["user_text"],
                assistant_text=job["assistant_text"],
                existing_memories=self.memory_store.search(job["user_text"], limit=10),
                active_goals=self.goal_store.list_all(statuses=["active", "paused"], limit=12),
                pending_commitments=self.commitment_store.list_pending(limit=12),
                open_followups=self.follow_up_store.list_open(limit=12),
                profile_text=self.profile_manager.read(),
            )
            async with self._apply_lock:
                outcomes = self.applier.apply(
                    result,
                    user_text=job["user_text"],
                    reflection_id=reflection_id,
                    scope=job["scope"],
                )
                self.store.complete(reflection_id, result, outcomes)
        except (ValidationError, ValueError, json.JSONDecodeError, asyncio.TimeoutError) as exc:
            attempts = int((self.store.get(reflection_id) or {}).get("attempts", 1))
            maximum = int(getattr(self.config, "max_attempts", 3))
            self.store.fail(reflection_id, str(exc), retry=attempts < maximum)
        except Exception as exc:
            attempts = int((self.store.get(reflection_id) or {}).get("attempts", 1))
            maximum = int(getattr(self.config, "max_attempts", 3))
            self.store.fail(reflection_id, str(exc), retry=attempts < maximum)

    def enqueue_turn(self, *, scope: str | None, user_text: str, assistant_text: str) -> str | None:
        if not bool(getattr(self.config, "enabled", True)) or not user_text.strip():
            return None
        scope_key = str(scope or "global")
        reflection_id = self.store.enqueue(scope_key, user_text, assistant_text)
        previous = self._scope_tasks.get(scope_key)

        async def run_after_previous() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass
            await self._process(reflection_id)

        task = asyncio.create_task(run_after_previous(), name=f"ares-reflection-{reflection_id[:8]}")
        self._tasks.add(task)
        self._scope_tasks[scope_key] = task

        def done(completed: asyncio.Task) -> None:
            self._tasks.discard(completed)
            if self._scope_tasks.get(scope_key) is completed:
                self._scope_tasks.pop(scope_key, None)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done)
        return reflection_id

    async def before_turn(self, scope: str | None) -> None:
        scope_key = str(scope or "global")
        active = self._scope_tasks.get(scope_key)
        if active is not None:
            try:
                await active
            except Exception:
                pass
        for job in self.store.pending(scope=scope_key, limit=3):
            await self._process(str(job["reflection_id"]))

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._owns_llm:
            await self.llm.close()


__all__ = [
    "ConversationReflector",
    "ReflectionApplier",
    "ReflectionResult",
    "ReflectionService",
    "ReflectionStore",
]
