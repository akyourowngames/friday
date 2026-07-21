"""Staged observations, candidates, and explainable durable-memory promotion."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

MEMORY_STATES = frozenset({
    "observed",
    "candidate",
    "promoted",
    "rejected",
    "superseded",
    "archived",
})
ACTIVE_CANDIDATE_STATES = ("candidate",)
_EXPLICIT_REMEMBER_RE = re.compile(
    r"\b(?:remember(?:\s+this|\s+that)?|save\s+this|keep\s+this\s+in\s+mind|"
    r"don['’]?t\s+forget|update\s+what\s+you\s+know\s+about\s+me)\b",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_memory_text(value: str) -> str:
    """Return a conservative lexical key used only for exact reinforcement."""

    return " ".join(re.findall(r"[a-z0-9']+", str(value or "").casefold()))


def explicit_memory_request(user_text: str) -> bool:
    """Return whether the user explicitly asked Ares to remember this turn."""

    return bool(_EXPLICIT_REMEMBER_RE.search(str(user_text or "")))


def explicit_memory_content(user_text: str) -> str:
    """Extract the payload following an explicit remember directive."""

    text = " ".join(str(user_text or "").split()).strip()
    match = _EXPLICIT_REMEMBER_RE.search(text)
    if match is None:
        return ""
    content = text[match.end():].lstrip(" :-,—").strip()
    return (content or text)[:4_000]


def _validated_state(state: str) -> str:
    normalized = str(state or "").strip().casefold()
    if normalized not in MEMORY_STATES:
        raise ValueError(f"Unsupported memory lifecycle state: {state}")
    return normalized


def _bounded(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(number, 1.0))


class MemoryLifecycleStore:
    """SQLite-backed observation/candidate lifecycle over an existing MemoryStore."""

    def __init__(self, memory_store: Any, config: Any | None = None) -> None:
        self.memory_store = memory_store
        self.conn: sqlite3.Connection = memory_store.conn
        self.config = config
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_key TEXT NOT NULL,
                canonical_text TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'note',
                confidence REAL NOT NULL DEFAULT 0.5,
                importance REAL NOT NULL DEFAULT 0.5,
                evidence TEXT NOT NULL,
                evidence_key TEXT NOT NULL,
                evidence_grounded INTEGER NOT NULL DEFAULT 0,
                explicit_user_request INTEGER NOT NULL DEFAULT 0,
                source_conversation_id TEXT,
                source_message_id TEXT,
                source_reflection_id TEXT,
                project TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                occurred_at TEXT NOT NULL,
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'observed',
                promoted_fact_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(canonical_key, category, evidence_key)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_candidates (
                candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_key TEXT NOT NULL,
                canonical_text TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'note',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                unique_session_count INTEGER NOT NULL DEFAULT 1,
                unique_query_count INTEGER NOT NULL DEFAULT 0,
                average_relevance REAL NOT NULL DEFAULT 0.0,
                confidence REAL NOT NULL DEFAULT 0.5,
                importance REAL NOT NULL DEFAULT 0.5,
                promotion_score REAL NOT NULL DEFAULT 0.0,
                contradiction_state TEXT NOT NULL DEFAULT 'none',
                explicit_user_request INTEGER NOT NULL DEFAULT 0,
                project TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'candidate',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                reviewed_at TEXT,
                promoted_fact_id INTEGER,
                decision_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_candidate_observations (
                candidate_id INTEGER NOT NULL,
                observation_id INTEGER NOT NULL,
                PRIMARY KEY(candidate_id, observation_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_candidate_queries (
                candidate_id INTEGER NOT NULL,
                query_key TEXT NOT NULL,
                relevance REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, query_key)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_promotion_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                score REAL NOT NULL DEFAULT 0.0,
                explanation_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_observations_status ON memory_observations(status, occurred_at)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_observations_candidate_key ON memory_observations(canonical_key, category)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_candidates_status ON memory_candidates(status, promotion_score, last_seen_at)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_candidates_key ON memory_candidates(canonical_key, category)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_candidate_observations_observation ON memory_candidate_observations(observation_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_candidate_queries_candidate "
            "ON memory_candidate_queries(candidate_id, created_at)"
        )
        self.conn.commit()

    @property
    def promotion_config(self) -> Any:
        return getattr(self.config, "promotion", self.config)

    def stage_observation(
        self,
        canonical_text: str,
        *,
        category: str = "note",
        confidence: float = 0.5,
        importance: float = 0.5,
        evidence: str,
        evidence_grounded: bool,
        source_conversation_id: str | None = None,
        source_message_id: str | None = None,
        source_reflection_id: str | None = None,
        project: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        occurred_at: str | None = None,
        expires_at: str | None = None,
        explicit_user_request: bool = False,
    ) -> dict[str, Any]:
        """Create one provenance-preserving observation and reinforce a candidate.

        Equivalent evidence from the same conversation is idempotent. Semantic
        proximity is intentionally not enough to merge candidates.
        """

        text = " ".join(str(canonical_text or "").split()).strip()
        key = normalize_memory_text(text)
        if not key:
            raise ValueError("Memory observation text is required")
        category = str(category or "note").strip().casefold()
        confidence = _bounded(confidence, 0.5)
        importance = _bounded(importance, 0.5)
        evidence = " ".join(str(evidence or "").split()).strip()
        if not evidence:
            raise ValueError("Memory observation evidence is required")
        now = utc_now()
        occurred = occurred_at or now
        source_scope = str(source_conversation_id or "global")
        evidence_key = hashlib.sha256(
            f"{key}\0{category}\0{normalize_memory_text(evidence)}\0{source_scope}".encode("utf-8")
        ).hexdigest()
        normalized_tags = self.memory_store._normalize_tags(tags)

        existing_observation = self.conn.execute(
            """SELECT * FROM memory_observations
               WHERE canonical_key=? AND category=? AND evidence_key=?""",
            (key, category, evidence_key),
        ).fetchone()
        if existing_observation is not None:
            candidate = self._candidate_for_observation(int(existing_observation["observation_id"]))
            return {
                "observation": dict(existing_observation),
                "candidate": candidate,
                "reinforced": False,
                "idempotent": True,
            }

        cursor = self.conn.execute(
            """INSERT INTO memory_observations
               (canonical_key, canonical_text, category, confidence, importance,
                evidence, evidence_key, evidence_grounded, explicit_user_request,
                source_conversation_id, source_message_id, source_reflection_id,
                project, tags_json, occurred_at, expires_at, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observed', ?, ?)""",
            (
                key,
                text,
                category,
                confidence,
                importance,
                evidence,
                evidence_key,
                int(bool(evidence_grounded)),
                int(bool(explicit_user_request)),
                source_conversation_id,
                source_message_id,
                source_reflection_id,
                project,
                json.dumps(normalized_tags, ensure_ascii=False),
                occurred,
                expires_at,
                now,
                now,
            ),
        )
        observation_id = int(cursor.lastrowid)
        candidate = self._active_candidate(key, category)
        evidence_record = {
            "observation_id": observation_id,
            "evidence": evidence,
            "grounded": bool(evidence_grounded),
            "conversation_id": source_conversation_id,
            "message_id": source_message_id,
            "reflection_id": source_reflection_id,
            "occurred_at": occurred,
        }
        reinforced = candidate is not None
        if candidate is None:
            candidate_cursor = self.conn.execute(
                """INSERT INTO memory_candidates
                   (canonical_key, canonical_text, category, evidence_json,
                    occurrence_count, unique_session_count, confidence, importance,
                    explicit_user_request, project, tags_json, expires_at, status,
                    first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)""",
                (
                    key,
                    text,
                    category,
                    json.dumps([evidence_record], ensure_ascii=False),
                    confidence,
                    importance,
                    int(bool(explicit_user_request)),
                    project,
                    json.dumps(normalized_tags, ensure_ascii=False),
                    expires_at,
                    occurred,
                    occurred,
                ),
            )
            candidate_id = int(candidate_cursor.lastrowid)
        else:
            candidate_id = int(candidate["candidate_id"])
            evidence_records = self._evidence_records(candidate)
            evidence_records.append(evidence_record)
            occurrences = int(candidate["occurrence_count"]) + 1
            prior_weight = max(1, int(candidate["occurrence_count"]))
            combined_confidence = (
                float(candidate["confidence"]) * prior_weight + confidence
            ) / (prior_weight + 1)
            sessions = {
                str(item.get("conversation_id") or "global") for item in evidence_records
            }
            merged_tags = self.memory_store._normalize_tags([
                *self._json_list(candidate["tags_json"]),
                *normalized_tags,
            ])
            self.conn.execute(
                """UPDATE memory_candidates
                   SET canonical_text=?, evidence_json=?, occurrence_count=?,
                       unique_session_count=?, confidence=?, importance=?,
                       explicit_user_request=?, project=COALESCE(?, project),
                       tags_json=?, expires_at=COALESCE(?, expires_at),
                       last_seen_at=?, status='candidate'
                   WHERE candidate_id=?""",
                (
                    text,
                    json.dumps(evidence_records, ensure_ascii=False),
                    occurrences,
                    max(1, len(sessions)),
                    round(combined_confidence, 6),
                    max(float(candidate["importance"]), importance),
                    int(bool(candidate["explicit_user_request"]) or explicit_user_request),
                    project,
                    json.dumps(merged_tags, ensure_ascii=False),
                    expires_at,
                    occurred,
                    candidate_id,
                ),
            )
        self.conn.execute(
            "INSERT INTO memory_candidate_observations(candidate_id, observation_id) VALUES (?, ?)",
            (candidate_id, observation_id),
        )
        self.conn.execute(
            "UPDATE memory_observations SET status='candidate', updated_at=? WHERE observation_id=?",
            (now, observation_id),
        )
        self.conn.commit()
        return {
            "observation": self.get_observation(observation_id),
            "candidate": self.get_candidate(candidate_id),
            "reinforced": reinforced,
            "idempotent": False,
        }

    def _active_candidate(self, key: str, category: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM memory_candidates
               WHERE canonical_key=? AND category=? AND status='candidate'
               ORDER BY candidate_id DESC LIMIT 1""",
            (key, category),
        ).fetchone()

    def _candidate_for_observation(self, observation_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT c.* FROM memory_candidates AS c
               JOIN memory_candidate_observations AS link
                 ON link.candidate_id=c.candidate_id
               WHERE link.observation_id=? ORDER BY c.candidate_id DESC LIMIT 1""",
            (int(observation_id),),
        ).fetchone()
        return self._candidate_dict(row) if row else None

    @staticmethod
    def _json_list(raw: Any) -> list[Any]:
        try:
            value = json.loads(str(raw or "[]"))
        except (TypeError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def _evidence_records(self, candidate: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in self._json_list(candidate["evidence_json"]) if isinstance(item, dict)]

    def _candidate_dict(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = self._evidence_records(result)
        result["tags"] = self._json_list(result.get("tags_json"))
        try:
            decision = json.loads(result.get("decision_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            decision = {}
        result["decision"] = decision if isinstance(decision, dict) else {}
        result.pop("evidence_json", None)
        result.pop("tags_json", None)
        result.pop("decision_json", None)
        return result

    def get_observation(self, observation_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM memory_observations WHERE observation_id=?",
            (int(observation_id),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["tags"] = self._json_list(result.pop("tags_json", "[]"))
        return result

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM memory_candidates WHERE candidate_id=?",
            (int(candidate_id),),
        ).fetchone()
        return self._candidate_dict(row) if row else None

    def list_candidates(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status is not None:
            where = "WHERE status=?"
            params.append(_validated_state(status))
        params.append(max(1, min(int(limit), 1_000)))
        rows = self.conn.execute(
            f"""SELECT * FROM memory_candidates {where}
                ORDER BY last_seen_at DESC, candidate_id DESC LIMIT ?""",
            params,
        ).fetchall()
        return [self._candidate_dict(row) for row in rows]

    def set_candidate_state(
        self,
        candidate_id: int,
        state: str,
        *,
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        normalized = _validated_state(state)
        now = utc_now()
        self.conn.execute(
            """UPDATE memory_candidates
               SET status=?, reviewed_at=?, decision_json=? WHERE candidate_id=?""",
            (
                normalized,
                now,
                json.dumps(decision or {}, ensure_ascii=False, sort_keys=True),
                int(candidate_id),
            ),
        )
        if normalized in {"rejected", "archived", "superseded"}:
            self.conn.execute(
                """UPDATE memory_observations SET status=?, updated_at=?
                   WHERE observation_id IN (
                       SELECT observation_id FROM memory_candidate_observations
                       WHERE candidate_id=?
                   )""",
                (normalized, now, int(candidate_id)),
            )
        self.conn.commit()
        return self.get_candidate(candidate_id)

    def record_promotion_decision(
        self,
        candidate_id: int,
        *,
        action: str,
        score: float,
        explanation: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """INSERT INTO memory_promotion_events
               (candidate_id, action, score, explanation_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                int(candidate_id),
                str(action),
                _bounded(score),
                json.dumps(explanation, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )
        self.conn.execute(
            """UPDATE memory_candidates SET promotion_score=?, decision_json=?
               WHERE candidate_id=?""",
            (
                _bounded(score),
                json.dumps(explanation, ensure_ascii=False, sort_keys=True),
                int(candidate_id),
            ),
        )
        self.conn.commit()


class MemoryPromotionService:
    """Automatically promote candidates while retaining score diagnostics."""

    def __init__(self, lifecycle: MemoryLifecycleStore, config: Any | None = None) -> None:
        self.lifecycle = lifecycle
        self.memory_store = lifecycle.memory_store
        self.config = config or lifecycle.promotion_config

    def score(self, candidate: dict[str, Any]) -> dict[str, Any]:
        occurrence_target = max(1, int(getattr(self.config, "min_occurrences", 2)))
        session_target = max(1, int(getattr(self.config, "min_unique_sessions", 2)))
        query_count = max(0, int(candidate.get("unique_query_count") or 0))
        confidence = _bounded(candidate.get("confidence"), 0.5)
        # Until candidates have retrieval feedback, grounded confidence is a
        # neutral prior rather than a zero that makes recurrence unpromotable.
        retrieval = (
            _bounded(candidate.get("average_relevance"))
            if query_count
            else min(confidence, 0.75)
        )
        frequency = min(int(candidate.get("occurrence_count") or 0) / occurrence_target, 1.0)
        sessions = min(int(candidate.get("unique_session_count") or 0) / session_target, 1.0)
        query_diversity = min(query_count / 3.0, 1.0)
        importance = _bounded(candidate.get("importance"), 0.5)
        evidence = candidate.get("evidence") or []
        grounded_ratio = (
            sum(1 for item in evidence if item.get("grounded")) / len(evidence)
            if evidence else 0.0
        )
        evidence_quality = (confidence * 0.75) + (grounded_ratio * 0.25)
        try:
            seen = datetime.fromisoformat(str(candidate.get("last_seen_at") or "").replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age_days = max((datetime.now(timezone.utc) - seen.astimezone(timezone.utc)).total_seconds() / 86_400, 0)
        except ValueError:
            age_days = 365.0
        freshness = math.exp(-age_days / 180.0)
        components = {
            "retrieval_relevance": round(retrieval, 6),
            "occurrence_frequency": round(frequency, 6),
            "unique_sessions": round(sessions, 6),
            "query_diversity": round(query_diversity, 6),
            "importance": round(importance, 6),
            "evidence_quality": round(evidence_quality, 6),
            "freshness": round(freshness, 6),
        }
        total = (
            0.30 * retrieval
            + 0.20 * frequency
            + 0.15 * sessions
            + 0.10 * query_diversity
            + 0.10 * importance
            + 0.10 * evidence_quality
            + 0.05 * freshness
        )
        return {"score": round(_bounded(total), 6), "components": components}

    def explain(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.lifecycle.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"Memory candidate #{candidate_id} was not found")
        scoring = self.score(candidate)
        reference_score = float(getattr(self.config, "reference_score", 0.72))
        return {
            "candidate_id": int(candidate_id),
            **scoring,
            "reference_score": reference_score,
            "eligible": True,
            "automatic": True,
            "status": candidate.get("status"),
        }

    def evaluate(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.lifecycle.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"Memory candidate #{candidate_id} was not found")
        explanation = self.explain(candidate_id)
        if not bool(getattr(self.config, "enabled", True)):
            action = "deferred"
            explanation["reason"] = "promotion_disabled"
        else:
            return self._promote(candidate, explanation)

        self.lifecycle.record_promotion_decision(
            candidate_id,
            action=action,
            score=explanation["score"],
            explanation=explanation,
        )
        return {"action": action, "candidate": self.lifecycle.get_candidate(candidate_id), "explanation": explanation}

    def _promote(
        self,
        candidate: dict[str, Any],
        explanation: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_id = int(candidate["candidate_id"])
        suggestions = self.memory_store.suggest_merge(
            candidate["canonical_text"], category=candidate.get("category", "note")
        )
        duplicate = next((item for item in suggestions if item.get("kind") == "duplicate"), None)
        conflicts = [item for item in suggestions if item.get("kind") == "possible_conflict"]
        if conflicts:
            explanation["conflicting_fact_ids"] = [int(item["fact_id"]) for item in conflicts]

        if duplicate:
            fact_id = int(duplicate["fact_id"])
            action = "linked_existing"
        else:
            evidence = candidate.get("evidence") or []
            first = evidence[0] if evidence else {}
            fact_id = self.memory_store.store(
                candidate["canonical_text"],
                category=candidate.get("category", "note"),
                confidence=float(candidate.get("confidence") or 0.5),
                importance=float(candidate.get("importance") or 0.5),
                source="conversation_reflection",
                source_conversation_id=first.get("conversation_id"),
                source_reflection_id=first.get("reflection_id"),
                source_message_id=first.get("message_id"),
                tags=candidate.get("tags") or [],
                expires_at=candidate.get("expires_at"),
                project=candidate.get("project"),
                source_candidate_id=candidate_id,
            )
            action = "promoted"
            for conflict in conflicts:
                other_id = int(conflict["fact_id"])
                confidence = float(conflict.get("confidence") or 0.5)
                self.memory_store._add_relation(
                    fact_id, other_id, "contradiction", confidence
                )
                self.memory_store._add_relation(
                    other_id, fact_id, "contradiction", confidence
                )
            if conflicts:
                self.memory_store.conn.commit()
        now = utc_now()
        self.lifecycle.conn.execute(
            """UPDATE memory_candidates
               SET status='promoted', promoted_fact_id=?, reviewed_at=?,
                   decision_json=? WHERE candidate_id=?""",
            (
                fact_id,
                now,
                json.dumps(explanation, ensure_ascii=False, sort_keys=True),
                candidate_id,
            ),
        )
        self.lifecycle.conn.execute(
            """UPDATE memory_observations
               SET status='promoted', promoted_fact_id=?, updated_at=?
               WHERE observation_id IN (
                   SELECT observation_id FROM memory_candidate_observations
                   WHERE candidate_id=?
               )""",
            (fact_id, now, candidate_id),
        )
        self.lifecycle.conn.commit()
        explanation["automatic"] = True
        explanation["promoted_fact_id"] = fact_id
        self.lifecycle.record_promotion_decision(
            candidate_id,
            action=action,
            score=explanation["score"],
            explanation=explanation,
        )
        return {
            "action": action,
            "fact_id": fact_id,
            "candidate": self.lifecycle.get_candidate(candidate_id),
            "explanation": explanation,
        }


__all__ = [
    "ACTIVE_CANDIDATE_STATES",
    "MEMORY_STATES",
    "MemoryLifecycleStore",
    "MemoryPromotionService",
    "explicit_memory_content",
    "explicit_memory_request",
    "normalize_memory_text",
]
