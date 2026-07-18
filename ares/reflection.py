"""Durable, outcome-aware reflection after normal conversation turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError

from ares.followups import FollowUpStore, future_utc
from ares.llm import LLMClient, normalize_provider, provider_for_model
from ares.memory_lifecycle import (
    MemoryLifecycleStore,
    MemoryPromotionService,
    explicit_memory_content,
    explicit_memory_request,
)
from ares.self_improvement import SelfImprovementStore


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reflection_timezone(config: Any) -> tuple[str, ZoneInfo]:
    configured = str(
        getattr(config, "local_timezone", "")
        or getattr(config, "timezone", "")
    ).strip()
    if not configured:
        try:
            from tzlocal import get_localzone_name

            configured = get_localzone_name()
        except Exception:
            configured = str(datetime.now().astimezone().tzinfo or "UTC")
    try:
        return configured, ZoneInfo(configured)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid reflection local timezone: {configured}") from exc


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


class SkillLearning(BaseModel):
    """One reusable, user-grounded procedural learning from a completed turn."""

    title: str
    kind: Literal["workflow", "style", "pitfall", "technique"] = "workflow"
    summary: str
    rationale: str = ""
    existing_skill: str | None = None
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    evidence: str


class OutcomeReview(BaseModel):
    """Audit of what actually happened, separate from what was promised."""

    status: Literal["succeeded", "partially_succeeded", "failed", "unknown"] = "unknown"
    summary: str
    evidence: str = ""
    reusable_lesson: str = ""


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
    outcome_reviews: list[OutcomeReview] = Field(default_factory=list)
    skill_learnings: list[SkillLearning] = Field(default_factory=list)


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
                outcome_summary TEXT NOT NULL DEFAULT '',
                job_type TEXT NOT NULL DEFAULT 'turn',
                compaction_checkpoint TEXT,
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
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(reflection_runs)").fetchall()
        }
        if "job_type" not in columns:
            self.conn.execute(
                "ALTER TABLE reflection_runs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'turn'"
            )
        if "compaction_checkpoint" not in columns:
            self.conn.execute(
                "ALTER TABLE reflection_runs ADD COLUMN compaction_checkpoint TEXT"
            )
        if "outcome_summary" not in columns:
            self.conn.execute(
                "ALTER TABLE reflection_runs ADD COLUMN outcome_summary TEXT NOT NULL DEFAULT ''"
            )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_compaction_checkpoints (
                checkpoint TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                reflection_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reflection_pending ON reflection_runs(status, created_at)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reflection_checkpoint "
            "ON reflection_runs(compaction_checkpoint)"
        )
        # A process may have stopped after claiming but before finishing.
        self.conn.execute("UPDATE reflection_runs SET status='pending' WHERE status='running'")
        self.conn.commit()

    def enqueue(
        self,
        scope: str,
        user_text: str,
        assistant_text: str,
        *,
        outcome_summary: str = "",
        job_type: str = "turn",
        compaction_checkpoint: str | None = None,
    ) -> str:
        reflection_id = uuid4().hex
        self.conn.execute(
            """INSERT INTO reflection_runs
               (reflection_id, scope, user_text, assistant_text, outcome_summary, job_type,
                compaction_checkpoint, status, attempts,
                created_at, started_at, completed_at, extracted_json, outcomes_json, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, NULL, NULL)""",
            (
                reflection_id,
                scope,
                user_text,
                assistant_text,
                str(outcome_summary or "")[:12_000],
                str(job_type or "turn"),
                compaction_checkpoint,
                utc_now(),
            ),
        )
        self.conn.commit()
        return reflection_id

    def enqueue_compaction(
        self,
        scope: str,
        user_text: str,
        assistant_text: str,
        checkpoint: str,
    ) -> str | None:
        """Durably enqueue a pre-compaction capture exactly once."""
        existing = self.conn.execute(
            "SELECT status FROM memory_compaction_checkpoints WHERE checkpoint=?",
            (checkpoint,),
        ).fetchone()
        if existing is not None:
            return None
        reflection_id = uuid4().hex
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """INSERT INTO memory_compaction_checkpoints
                   (checkpoint, scope, reflection_id, status, created_at)
                   VALUES (?, ?, ?, 'pending', ?)""",
                (checkpoint, scope, reflection_id, utc_now()),
            )
            self.conn.execute(
                """INSERT INTO reflection_runs
                   (reflection_id, scope, user_text, assistant_text, job_type,
                    compaction_checkpoint, status, attempts, created_at)
                   VALUES (?, ?, ?, ?, 'compaction', ?, 'pending', 0, ?)""",
                (
                    reflection_id,
                    scope,
                    user_text,
                    assistant_text,
                    checkpoint,
                    utc_now(),
                ),
            )
            self.conn.commit()
            return reflection_id
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return None
        except Exception:
            self.conn.rollback()
            raise

    def finish_compaction(
        self,
        checkpoint: str | None,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        if not checkpoint:
            return
        self.conn.execute(
            """UPDATE memory_compaction_checkpoints
               SET status=?, completed_at=?, error=? WHERE checkpoint=?""",
            (
                status,
                utc_now() if status in {"completed", "failed"} else None,
                (str(error)[:2_000] if error else None),
                checkpoint,
            ),
        )
        self.conn.commit()

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
                ORDER BY created_at, rowid LIMIT ?""",
            [*params, max(1, min(int(limit), 100))],
        ).fetchall()
        return [dict(row) for row in rows]

    def pending_scopes(self, *, limit: int = 100) -> list[str]:
        """Return scopes that have durable work waiting to be resumed.

        ``ReflectionService`` deliberately starts workers lazily because it is
        also constructed by synchronous embedders.  This small query lets the
        first asynchronous turn resume work left behind by an earlier process.
        """
        rows = self.conn.execute(
            """SELECT scope, MIN(rowid) AS queue_order
               FROM reflection_runs
               WHERE status='pending'
               GROUP BY scope
               ORDER BY queue_order
               LIMIT ?""",
            (max(1, min(int(limit), 1_000)),),
        ).fetchall()
        return [str(row["scope"]) for row in rows]

    def claim_next(self, scope: str) -> dict[str, Any] | None:
        """Atomically claim the oldest runnable job in one session scope.

        A later job must not leapfrog an older ``pending`` *or* ``running``
        job.  The guard matters when two service instances share the durable
        queue (for example while integrations are being restarted).  SQLite's
        ``rowid`` provides a stable insertion order even when several rows have
        the same second-resolution ``created_at`` timestamp.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                """SELECT queued.rowid AS queue_order, queued.*
                   FROM reflection_runs AS queued
                   WHERE queued.scope=?
                     AND queued.status='pending'
                     AND NOT EXISTS (
                         SELECT 1
                         FROM reflection_runs AS earlier
                         WHERE earlier.scope=queued.scope
                           AND earlier.rowid < queued.rowid
                           AND earlier.status IN ('pending', 'running')
                     )
                   ORDER BY queued.rowid
                   LIMIT 1""",
                (scope,),
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None
            reflection_id = str(row["reflection_id"])
            updated = self.conn.execute(
                """UPDATE reflection_runs SET status='running', attempts=attempts+1,
                   started_at=?, error=NULL WHERE reflection_id=? AND status='pending'""",
                (utc_now(), reflection_id),
            )
            self.conn.commit()
            if updated.rowcount != 1:
                return None
            job = dict(row)
            job["status"] = "running"
            job["attempts"] = int(job.get("attempts") or 0) + 1
            return job
        except Exception:
            self.conn.rollback()
            raise

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

    def requeue_preempted(self, reflection_id: str) -> None:
        """Return a cancelled background review to the durable FIFO head."""
        self.conn.execute(
            """UPDATE reflection_runs
               SET status='pending', started_at=NULL,
                   attempts=MAX(attempts - 1, 0),
                   error='preempted by foreground turn'
               WHERE reflection_id=? AND status='running'""",
            (str(reflection_id),),
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
        outcome_summary: str = "",
    ) -> ReflectionResult:
        timezone_name, local_timezone = _reflection_timezone(self.config)
        current_local_datetime = datetime.now(local_timezone).isoformat(timespec="seconds")
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
            "You are Ares' background conversation reflection process. Return ONLY one JSON object matching "
            "this schema: new_memories, updated_memories, new_goals, goal_progress, completed_goals, "
            "profile_updates, commitments, follow_up_opportunities, follow_up_resolutions, "
            "outcome_reviews, skill_learnings; "
            "every value is an array.\n\n"
            "All confidence and importance values must be JSON numbers from 0 to 1, never labels. "
            "Memory category must be preference, fact, belief, habit, relationship, or note. "
            "Skill kind must be workflow, style, pitfall, or technique. "
            "Goal priority must be low, normal, or high. Omit unknown optional fields.\n\n"
            "Use the completed user/assistant turn as evidence. Every proposed mutation should contain a short "
            "supporting excerpt in its evidence field and a calibrated confidence from 0 to 1. Capture whatever "
            "would help Ares act better in a future turn. Update an existing record by ID instead of duplicating it. Mark a "
            "goal completed only when the user explicitly says that outcome is finished. Create a goal only for a "
            "clear durable outcome; include 2-5 milestones and one small specific next_action. A commitment is an "
            "explicit promise or obligation, not every request. A follow-up opportunity is a concrete future "
            "check-in that would help the user, not a generic suggestion. Resolve an open follow-up only when "
            "the user explicitly completes, dismisses, or cancels it. For every follow_up_opportunity, eligible_at "
            "must be a timezone-aware ISO-8601 timestamp with an explicit UTC offset; interpret relative phrases "
            f"using the current local datetime {current_local_datetime} in timezone {timezone_name}. "
            "Never return a naive timestamp. A skill_learning is only for a reusable workflow/style correction, "
            "pitfall, or non-trivial technique that would improve a future task of the same class. Prefer updating "
            "an existing named skill, never turn a one-off task or temporary environment failure into a skill, and "
            "ground it in a concrete user, assistant, or tool-outcome excerpt. Compare the promised result with "
            "the supplied ACTUAL_OUTCOMES. Add an outcome_review when tools or actions ran, label it succeeded, "
            "partially_succeeded, failed, or unknown, and derive skill learnings from observed success/failure—not "
            "from confident assistant prose. Skill learnings are proposals requiring Hermes review before use and "
            "remain in the procedural-learning store rather than editing executable skill files. "
            "If nothing durable changed, return empty arrays.\n\n"
            f"CURRENT STATE:\n{json.dumps(state, ensure_ascii=False)}\n\n"
            f"USER:\n{user_text[:8_000]}\n\nASSISTANT:\n{assistant_text[:8_000]}\n\n"
            f"ACTUAL_OUTCOMES:\n{outcome_summary[:12_000] or 'No tool/action outcomes were recorded.'}\n"
        )
        chat_kwargs: dict[str, Any] = {}
        if isinstance(self.llm, LLMClient):
            chat_kwargs = {"max_tokens": 1_500, "temperature": 0.1}
            review_model = str(getattr(self.config, "model", "") or "").strip()
            review_provider = provider_for_model(review_model) if review_model else None
            if (
                review_model
                and (
                    review_provider is None
                    or normalize_provider(review_provider)
                    == normalize_provider(getattr(self.llm, "provider", None))
                )
            ):
                chat_kwargs.update({
                    "model": review_model,
                    "fallback_model": self.llm.model,
                })
        response = await asyncio.wait_for(
            self.llm.chat(
                [{"role": "user", "content": prompt}], tools=[], **chat_kwargs
            ),
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
        lifecycle_store: MemoryLifecycleStore,
        promotion_service: MemoryPromotionService,
        self_improvement_store: SelfImprovementStore,
    ) -> None:
        self.memory_store = memory_store
        self.goal_store = goal_store
        self.commitment_store = commitment_store
        self.follow_up_store = follow_up_store
        self.profile_manager = profile_manager
        self.config = config
        self.lifecycle_store = lifecycle_store
        self.promotion_service = promotion_service
        self.self_improvement_store = self_improvement_store

    def apply(
        self,
        result: ReflectionResult,
        *,
        user_text: str,
        reflection_id: str,
        scope: str,
        outcome_summary: str = "",
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []

        def record(kind: str, action: str, **details: Any) -> None:
            outcomes.append({"kind": kind, "action": action, **details})

        for item in result.new_memories:
            try:
                staged = self.lifecycle_store.stage_observation(
                    item.fact_text,
                    category=item.category,
                    confidence=item.confidence,
                    importance=item.importance,
                    evidence=item.evidence,
                    evidence_grounded=_supported_evidence(item.evidence, user_text),
                    source_conversation_id=scope,
                    source_reflection_id=reflection_id,
                    explicit_user_request=explicit_memory_request(user_text),
                )
                candidate = staged.get("candidate") or {}
                decision = self.promotion_service.evaluate(int(candidate["candidate_id"]))
                record(
                    "new_memory",
                    decision.get("action", "candidate"),
                    observation_id=(staged.get("observation") or {}).get("observation_id"),
                    candidate_id=candidate.get("candidate_id"),
                    fact_id=decision.get("fact_id"),
                    reinforced=bool(staged.get("reinforced")),
                    idempotent=bool(staged.get("idempotent")),
                )
            except Exception as exc:
                record("new_memory", "error", error=str(exc)[:500])

        for item in result.updated_memories:
            existing = self.memory_store.get(item.fact_id)
            if existing is None:
                record("updated_memory", "skipped", reason="not_found", fact_id=item.fact_id)
                continue
            try:
                self.memory_store.update(
                    item.fact_id,
                    fact_text=item.fact_text,
                    category=item.category,
                    confidence=item.confidence,
                    importance=item.importance,
                    source="conversation_reflection",
                    session_id=None,
                    source_conversation_id=scope,
                    source_reflection_id=reflection_id,
                )
                record("updated_memory", "updated", fact_id=item.fact_id)
            except Exception as exc:
                record("updated_memory", "error", fact_id=item.fact_id, error=str(exc)[:500])

        for item in result.new_goals:
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
            profile_candidates.append(item)
        try:
            applied_profile = self.profile_manager.apply_updates(profile_candidates)
            for item in applied_profile:
                record("profile_update", "updated", section=item["section"], key=item["key"])
        except Exception as exc:
            record("profile_update", "error", error=str(exc)[:500])

        for item in result.commitments:
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

        for review in result.outcome_reviews:
            record(
                "outcome_review",
                "reviewed",
                status=review.status,
                summary=review.summary[:1_000],
                reusable_lesson=review.reusable_lesson[:1_000],
            )

        evidence_corpus = "\n".join(
            part for part in (user_text, outcome_summary) if str(part or "").strip()
        )
        for item in result.skill_learnings:
            try:
                staged = self.self_improvement_store.stage(
                    title=item.title,
                    kind=item.kind,
                    summary=item.summary,
                    rationale=item.rationale,
                    evidence=item.evidence,
                    evidence_grounded=_supported_evidence(item.evidence, evidence_corpus),
                    confidence=item.confidence,
                    existing_skill=item.existing_skill,
                    source_conversation_id=scope,
                    source_reflection_id=reflection_id,
                )
                if staged is None:
                    record("skill_learning", "skipped", reason="self_improvement_disabled")
                else:
                    record(
                        "skill_learning",
                        str(staged.get("status") or "pending_approval"),
                        improvement_id=staged.get("improvement_id"),
                    )
            except Exception as exc:
                record("skill_learning", "error", error=str(exc)[:500])
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
        memory_config: Any | None = None,
    ) -> None:
        self.config = config
        self.memory_config = memory_config
        self.store = ReflectionStore(memory_store.conn)
        self.lifecycle_store = MemoryLifecycleStore(memory_store, memory_config)
        self.promotion_service = MemoryPromotionService(
            self.lifecycle_store,
            getattr(memory_config, "promotion", None),
        )
        self.self_improvement_store = SelfImprovementStore(
            memory_store.conn,
            getattr(memory_config, "self_improvement", None),
        )
        timezone_name = str(
            getattr(config, "local_timezone", "")
            or getattr(config, "timezone", "")
        ).strip() or None
        self.follow_up_store = follow_up_store or FollowUpStore(
            connection=memory_store.conn,
            timezone_name=timezone_name,
        )
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
            lifecycle_store=self.lifecycle_store,
            promotion_service=self.promotion_service,
            self_improvement_store=self.self_improvement_store,
        )
        self.memory_store = memory_store
        self.goal_store = goal_store
        self.commitment_store = commitment_store
        self.profile_manager = profile_manager
        self._tasks: set[asyncio.Task] = set()
        self._scope_tasks: dict[str, asyncio.Task] = {}
        self._apply_lock = asyncio.Lock()
        self._foreground_idle = asyncio.Event()
        self._foreground_idle.set()
        self._foreground_turns = 0
        self._closed = False

    def _augment_automatic_learning(
        self,
        result: ReflectionResult,
        *,
        user_text: str,
        outcome_summary: str = "",
    ) -> ReflectionResult:
        """Honor explicit memory and reusable correction signals deterministically."""

        if outcome_summary and not result.outcome_reviews:
            try:
                payload = json.loads(outcome_summary)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            tool_outcomes = payload.get("tool_outcomes") if isinstance(payload, dict) else []
            tool_outcomes = tool_outcomes if isinstance(tool_outcomes, list) else []
            execution_record = payload.get("execution_record") if isinstance(payload, dict) else {}
            execution_record = execution_record if isinstance(execution_record, dict) else {}
            def execution_count(name: str) -> int:
                try:
                    return int(execution_record.get(name) or 0)
                except (TypeError, ValueError):
                    return 0

            meaningful_execution = bool(
                tool_outcomes
                or execution_count("tool_call_count") > 0
                or execution_count("agent_count") > 0
                or str(execution_record.get("kind") or "ordinary") != "ordinary"
            )
            if meaningful_execution:
                statuses = [
                    str(item.get("status") or "unknown").casefold()
                    for item in tool_outcomes
                    if isinstance(item, dict)
                ]
                completed = sum(status in {"completed", "succeeded", "success"} for status in statuses)
                failed = sum(status in {"failed", "failure", "error", "timed_out", "blocked"} for status in statuses)
                execution_status = str(execution_record.get("status") or "").casefold()
                if statuses and completed == len(statuses) and execution_status not in {
                    "partial", "partial_or_failed", "failed", "timed_out", "blocked"
                }:
                    review_status = "succeeded"
                elif statuses and failed == len(statuses):
                    review_status = "failed"
                elif completed or failed or execution_status in {"partial", "partial_or_failed"}:
                    review_status = "partially_succeeded"
                elif execution_status in {"succeeded", "completed", "success"}:
                    review_status = "succeeded"
                elif execution_status in {"failed", "timed_out", "blocked"}:
                    review_status = "failed"
                else:
                    review_status = "unknown"
                evidence_parts = [
                    str(item.get("result") or "")[:500]
                    for item in tool_outcomes[:3]
                    if isinstance(item, dict) and item.get("result")
                ]
                result.outcome_reviews.append(OutcomeReview(
                    status=review_status,
                    summary=(
                        f"Observed {len(tool_outcomes)} tool outcome(s): "
                        f"{completed} completed, {failed} failed."
                        if tool_outcomes
                        else f"Observed execution status: {execution_status or 'unknown'}."
                    ),
                    evidence=" | ".join(evidence_parts)[:1_500],
                ))

        capture = getattr(self.memory_config, "capture", None)
        if (
            bool(getattr(capture, "explicit_remember_fast_path", True))
            and explicit_memory_request(user_text)
        ):
            content = explicit_memory_content(user_text)
            known = {_normalized(item.fact_text) for item in result.new_memories}
            if content and _normalized(content) not in known:
                result.new_memories.append(NewMemory(
                    fact_text=content,
                    category="note",
                    importance=0.9,
                    confidence=1.0,
                    evidence=content,
                ))

        correction_signal = re.search(
            r"\b(?:always|never|from now on|do not|don['’]t|"
            r"when (?:you|we|working|handling|doing))\b",
            user_text,
            re.IGNORECASE,
        )
        if correction_signal and not result.skill_learnings:
            summary = " ".join(user_text.split()).strip()[:1_000]
            result.skill_learnings.append(SkillLearning(
                title="Direct user workflow correction",
                kind="workflow",
                summary=summary,
                rationale="The user gave a reusable instruction that should improve future work.",
                confidence=1.0,
                evidence=summary,
            ))
        return result

    async def _process_job(self, job: dict[str, Any]) -> str:
        """Run one already-claimed job and return its queue outcome.

        Returning ``retry`` rather than immediately looping is intentional:
        retryable failures keep their place at the head of the per-scope queue
        and are resumed by the next lightweight worker kick.  That prevents a
        failing provider from spinning in the background or allowing later
        state mutations to overtake the failed turn.
        """
        reflection_id = str(job["reflection_id"])
        try:
            result = await self.reflector.extract(
                user_text=job["user_text"],
                assistant_text=job["assistant_text"],
                outcome_summary=str(job.get("outcome_summary") or ""),
                # Reflection is a background convenience task.  Its context
                # lookup must not initialize or run the embedding model on
                # the CLI event loop after a reply has already been shown.
                # FTS is sufficient here because duplicate detection during
                # application remains lexical as well.
                existing_memories=self.memory_store.search(
                    job["user_text"], limit=10, semantic=False
                ),
                active_goals=self.goal_store.list_all(statuses=["active", "paused"], limit=12),
                pending_commitments=self.commitment_store.list_pending(limit=12),
                open_followups=self.follow_up_store.list_open(limit=12),
                profile_text=self.profile_manager.read(),
            )
            result = self._augment_automatic_learning(
                result,
                user_text=str(job["user_text"]),
                outcome_summary=str(job.get("outcome_summary") or ""),
            )
            async with self._apply_lock:
                outcomes = self.applier.apply(
                    result,
                    user_text=job["user_text"],
                    reflection_id=reflection_id,
                    scope=job["scope"],
                    outcome_summary=str(job.get("outcome_summary") or ""),
                )
                self.store.complete(reflection_id, result, outcomes)
                self.store.finish_compaction(
                    job.get("compaction_checkpoint"), status="completed"
                )
            return "completed"
        except (ValidationError, ValueError, json.JSONDecodeError, asyncio.TimeoutError) as exc:
            attempts = int((self.store.get(reflection_id) or {}).get("attempts", 1))
            maximum = int(getattr(self.config, "max_attempts", 3))
            retry = attempts < maximum
            self.store.fail(reflection_id, str(exc), retry=retry)
            self.store.finish_compaction(
                job.get("compaction_checkpoint"),
                status="pending" if retry else "failed",
                error=str(exc),
            )
            return "retry" if retry else "failed"
        except Exception as exc:
            attempts = int((self.store.get(reflection_id) or {}).get("attempts", 1))
            maximum = int(getattr(self.config, "max_attempts", 3))
            retry = attempts < maximum
            self.store.fail(reflection_id, str(exc), retry=retry)
            self.store.finish_compaction(
                job.get("compaction_checkpoint"),
                status="pending" if retry else "failed",
                error=str(exc),
            )
            return "retry" if retry else "failed"

    async def _process(self, reflection_id: str) -> str:
        """Process one explicit job for compatibility with older embedders."""
        job = self.store.mark_running(reflection_id)
        if job is None or job.get("status") != "running":
            return "skipped"
        return await self._process_job(job)

    async def _run_scope_worker(self, scope_key: str) -> None:
        """Drain one durable session queue without blocking conversation turns."""
        while True:
            await self._foreground_idle.wait()
            idle_delay = max(0.0, float(getattr(self.config, "idle_delay_seconds", 0.35)))
            if idle_delay:
                await asyncio.sleep(idle_delay)
            if not self._foreground_idle.is_set():
                continue
            job = self.store.claim_next(scope_key)
            if job is None:
                return
            try:
                outcome = await self._process_job(job)
            except asyncio.CancelledError:
                self.store.requeue_preempted(str(job["reflection_id"]))
                self.store.finish_compaction(
                    job.get("compaction_checkpoint"), status="pending",
                    error="preempted by foreground turn",
                )
                raise
            if outcome == "retry":
                # The pending head deliberately blocks later jobs until a
                # future kick retries it, preserving causal session ordering.
                return

    def _track_scope_task(self, scope_key: str, task: asyncio.Task[None]) -> asyncio.Task[None]:
        self._tasks.add(task)
        self._scope_tasks[scope_key] = task

        def done(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            if self._scope_tasks.get(scope_key) is completed:
                self._scope_tasks.pop(scope_key, None)
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done)
        return task

    def _ensure_scope_worker(self, scope_key: str) -> asyncio.Task[None] | None:
        """Ensure one background worker is responsible for this session queue."""
        active = self._scope_tasks.get(scope_key)
        if active is not None and not active.done():
            return active
        # This service is only driven from async request paths.  Retaining the
        # existing ``create_task`` behavior keeps direct synchronous use of
        # ``enqueue_turn`` explicitly unsupported instead of silently losing a
        # persisted job in an event-loop-less caller.
        task = asyncio.create_task(
            self._run_scope_worker(scope_key), name=f"ares-reflection-scope-{scope_key[:32]}"
        )
        return self._track_scope_task(scope_key, task)

    def _resume_pending_workers(self) -> None:
        """Kick workers for rows recovered from a prior process or retry."""
        for scope_key in self.store.pending_scopes():
            self._ensure_scope_worker(scope_key)

    def enqueue_turn(
        self,
        *,
        scope: str | None,
        user_text: str,
        assistant_text: str,
        outcome_summary: str = "",
    ) -> str | None:
        if bool(getattr(self, "_closed", False)):
            return None
        if not bool(getattr(self.config, "enabled", True)) or not user_text.strip():
            return None
        scope_key = str(scope or "global")
        reflection_id = self.store.enqueue(
            scope_key,
            user_text,
            assistant_text,
            outcome_summary=outcome_summary,
        )
        self._ensure_scope_worker(scope_key)
        return reflection_id

    def enqueue_compaction(self, *, scope: str | None, messages: list[dict[str, Any]]) -> str | None:
        """Checkpoint durable memory capture before lossy context compaction.

        The checkpoint hash makes repeated compaction attempts idempotent. The
        work remains asynchronous and uses the same per-scope FIFO as ordinary
        post-turn reflection, so it never races earlier state mutations.
        """
        if not bool(getattr(self.config, "enabled", True)) or not messages:
            return None
        user_parts: list[str] = []
        assistant_parts: list[str] = []
        checkpoint_parts: list[str] = []
        for message in messages:
            role = str(message.get("role") or "")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            normalized = " ".join(content.split())
            checkpoint_parts.append(f"{role}:{normalized}")
            if role == "user":
                user_parts.append(content)
            elif role == "assistant":
                assistant_parts.append(content)
        if not user_parts:
            return None
        scope_key = str(scope or "global")
        if bool(getattr(self, "_closed", False)):
            return
        digest_source = f"{scope_key}\n" + "\n".join(checkpoint_parts)
        checkpoint = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        reflection_id = self.store.enqueue_compaction(
            scope_key,
            "\n\n".join(user_parts),
            "\n\n".join(assistant_parts),
            checkpoint,
        )
        if reflection_id is not None:
            self._ensure_scope_worker(scope_key)
        return reflection_id

    async def before_turn(
        self,
        scope: str | None,
        *,
        synchronize: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        """Give a foreground turn priority over durable background review.

        Normal chat preempts and requeues an active review. ``synchronize`` is
        an opt-in, bounded barrier for explicit state inspection; its shielded
        timeout does not cancel the review it deliberately chose to await.
        """
        if bool(getattr(self, "_closed", False)):
            return
        scope_key = str(scope or "global")
        foreground_idle = getattr(self, "_foreground_idle", None)
        if foreground_idle is None:
            foreground_idle = asyncio.Event()
            foreground_idle.set()
            self._foreground_idle = foreground_idle
        foreground_idle.clear()
        if not synchronize:
            self._foreground_turns = int(getattr(self, "_foreground_turns", 0)) + 1
            # Reflection is useful only when it stays off the user's critical
            # path. Cancel any in-flight review and durably requeue it; the
            # normal foreground reply always gets provider priority.
            active_tasks = tuple(
                task for task in self._scope_tasks.values() if not task.done()
            )
            for task in active_tasks:
                task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            return
        # Explicit state-inspection callers can request a bounded flush. This
        # is never used by normal chat turns and does not cancel active work.
        foreground_idle.set()
        self._resume_pending_workers()
        active = self._ensure_scope_worker(scope_key)
        if active is None:
            self._foreground_idle.clear()
            return
        if timeout_seconds is None:
            timeout_seconds = 0.25
        timeout = max(0.0, min(float(timeout_seconds), 5.0))
        if timeout <= 0:
            self._foreground_idle.clear()
            return
        try:
            await asyncio.wait_for(asyncio.shield(active), timeout=timeout)
        except asyncio.TimeoutError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            self._foreground_idle.clear()

    def after_turn(self) -> None:
        """Release background reviews only after foreground delivery finishes."""
        if bool(getattr(self, "_closed", False)):
            return
        self._foreground_turns = max(0, int(getattr(self, "_foreground_turns", 0)) - 1)
        if self._foreground_turns:
            return
        self._foreground_idle.set()
        self._resume_pending_workers()

    async def close(self) -> None:
        if bool(getattr(self, "_closed", False)):
            return
        self._foreground_idle.set()
        self._resume_pending_workers()
        try:
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
            if self._owns_llm:
                await self.llm.close()
        finally:
            self._closed = True


__all__ = [
    "ConversationReflector",
    "ReflectionApplier",
    "ReflectionResult",
    "ReflectionService",
    "ReflectionStore",
]
