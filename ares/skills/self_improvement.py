"""Hermes-style reviewed procedural learning for Ares."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


IMPROVEMENT_STATES = frozenset({"pending_approval", "active", "rejected", "archived"})
IMPROVEMENT_KINDS = frozenset({"workflow", "style", "pitfall", "technique"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _key(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", str(value or "").casefold()))


class SelfImprovementStore:
    """Persist and retrieve procedural lessons learned after completed turns.

    Outcome-aware reflection proposes lessons, but only approved lessons are
    retrieved into future turns. Records stay separate from executable skill
    files, making review, rejection, archival, and audit straightforward.
    """

    def __init__(self, connection: sqlite3.Connection, config: Any | None = None) -> None:
        self.conn = connection
        self.config = config
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS self_improvement_candidates (
                improvement_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_key TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                confidence REAL NOT NULL DEFAULT 0.5,
                existing_skill TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source_conversation_id TEXT,
                source_reflection_id TEXT,
                applied_skill TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_self_improvement_status "
            "ON self_improvement_candidates(status, updated_at)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_self_improvement_key "
            "ON self_improvement_candidates(canonical_key, kind)"
        )
        # ``approved`` was the transient state used by an earlier prototype;
        # approved lessons are usable and therefore migrate to ``active``.
        self.conn.execute(
            """UPDATE self_improvement_candidates SET status='active', updated_at=?
               WHERE status='approved'""",
            (utc_now(),),
        )
        self.conn.commit()

    @staticmethod
    def _json_list(raw: Any) -> list[Any]:
        try:
            value = json.loads(str(raw or "[]"))
        except (TypeError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def _row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = self._json_list(result.pop("evidence_json", "[]"))
        return result

    def stage(
        self,
        *,
        title: str,
        kind: str,
        summary: str,
        rationale: str,
        evidence: str,
        evidence_grounded: bool,
        confidence: float,
        existing_skill: str | None,
        source_conversation_id: str | None,
        source_reflection_id: str | None,
    ) -> dict[str, Any] | None:
        """Stage or reinforce one reusable procedure for Hermes review."""

        if not bool(getattr(self.config, "enabled", True)):
            return None
        title = " ".join(str(title or "").split()).strip()
        summary = " ".join(str(summary or "").split()).strip()
        rationale = " ".join(str(rationale or "").split()).strip()
        evidence = " ".join(str(evidence or "").split()).strip()
        kind = str(kind or "workflow").strip().casefold()
        if not title or not summary:
            return None
        if kind not in IMPROVEMENT_KINDS:
            kind = "workflow"
        confidence = max(0.0, min(float(confidence), 1.0))
        canonical = _key(f"{title} {summary}")
        now = utc_now()
        evidence_key = hashlib.sha256(
            f"{canonical}\0{_key(evidence)}\0{source_conversation_id or 'global'}".encode("utf-8")
        ).hexdigest()
        evidence_record = {
            "key": evidence_key,
            "evidence": evidence,
            "conversation_id": source_conversation_id,
            "reflection_id": source_reflection_id,
            "grounded": bool(evidence_grounded),
            "created_at": now,
        }
        row = self.conn.execute(
            """SELECT * FROM self_improvement_candidates
               WHERE canonical_key=? AND kind=? AND status IN ('active', 'pending_approval')
               ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                        improvement_id DESC LIMIT 1""",
            (canonical, kind),
        ).fetchone()
        if row is None:
            initial_status = (
                "pending_approval"
                if bool(getattr(self.config, "approval_required", True))
                else "active"
            )
            cursor = self.conn.execute(
                """INSERT INTO self_improvement_candidates
                   (canonical_key, title, kind, summary, rationale, evidence_json,
                    confidence, existing_skill, status, source_conversation_id,
                    source_reflection_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    canonical,
                    title,
                    kind,
                    summary,
                    rationale,
                    json.dumps([evidence_record], ensure_ascii=False),
                    confidence,
                    existing_skill,
                    initial_status,
                    source_conversation_id,
                    source_reflection_id,
                    now,
                    now,
                ),
            )
            improvement_id = int(cursor.lastrowid)
        else:
            improvement_id = int(row["improvement_id"])
            records = self._json_list(row["evidence_json"])
            if not any(
                isinstance(item, dict) and item.get("key") == evidence_key for item in records
            ):
                records.append(evidence_record)
                count = int(row["occurrence_count"] or 1)
                combined = (float(row["confidence"]) * count + confidence) / (count + 1)
                self.conn.execute(
                    """UPDATE self_improvement_candidates
                       SET evidence_json=?, occurrence_count=?, confidence=?,
                           summary=?, rationale=?, existing_skill=COALESCE(?, existing_skill),
                           updated_at=? WHERE improvement_id=?""",
                    (
                        json.dumps(records, ensure_ascii=False),
                        count + 1,
                        combined,
                        summary,
                        rationale,
                        existing_skill,
                        now,
                        improvement_id,
                    ),
                )
        self.conn.commit()
        self._trim_active()
        return self.get(improvement_id)

    def approve(self, improvement_id: int) -> dict[str, Any] | None:
        """Activate one reviewed proposal without rewriting executable files."""
        now = utc_now()
        self.conn.execute(
            """UPDATE self_improvement_candidates
               SET status='active', reviewed_at=?, updated_at=?
               WHERE improvement_id=? AND status='pending_approval'""",
            (now, now, int(improvement_id)),
        )
        self.conn.commit()
        self._trim_active()
        return self.get(improvement_id)

    def reject(self, improvement_id: int) -> dict[str, Any] | None:
        """Reject one proposed procedure while retaining its audit evidence."""
        now = utc_now()
        self.conn.execute(
            """UPDATE self_improvement_candidates
               SET status='rejected', reviewed_at=?, updated_at=?
               WHERE improvement_id=? AND status='pending_approval'""",
            (now, now, int(improvement_id)),
        )
        self.conn.commit()
        return self.get(improvement_id)

    def get(self, improvement_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM self_improvement_candidates WHERE improvement_id=?",
            (int(improvement_id),),
        ).fetchone()
        return self._row(row) if row else None

    def list(self, *, status: str = "active", limit: int = 100) -> list[dict[str, Any]]:
        normalized = str(status or "active").strip().casefold()
        if normalized not in IMPROVEMENT_STATES:
            raise ValueError(f"Unsupported self-improvement state: {status}")
        rows = self.conn.execute(
            """SELECT * FROM self_improvement_candidates WHERE status=?
               ORDER BY updated_at DESC, improvement_id DESC LIMIT ?""",
            (normalized, max(1, min(int(limit), 1_000))),
        ).fetchall()
        return [self._row(row) for row in rows]

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        query_terms = set(_key(query).split())
        candidates = self.list(status="active", limit=max(25, int(limit) * 8))
        scored: list[tuple[float, dict[str, Any]]] = []
        for candidate in candidates:
            terms = set(_key(f"{candidate['title']} {candidate['summary']}").split())
            overlap = len(query_terms & terms) / max(1, len(query_terms | terms))
            recurrence = min(int(candidate.get("occurrence_count") or 1) / 5.0, 1.0)
            score = overlap * 0.75 + recurrence * 0.15 + float(candidate["confidence"]) * 0.10
            if overlap > 0 or not query_terms:
                item = dict(candidate)
                item["_relevance"] = round(score, 6)
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        return [item for _, item in scored[: max(1, min(int(limit), 20))]]

    def archive(self, improvement_id: int) -> dict[str, Any] | None:
        self.conn.execute(
            """UPDATE self_improvement_candidates SET status='archived', updated_at=?
               WHERE improvement_id=?""",
            (utc_now(), int(improvement_id)),
        )
        self.conn.commit()
        return self.get(improvement_id)

    def _trim_active(self) -> None:
        maximum = max(1, int(getattr(self.config, "max_active", 100)))
        self.conn.execute(
            """UPDATE self_improvement_candidates SET status='archived', updated_at=?
               WHERE status='active' AND improvement_id NOT IN (
                   SELECT improvement_id FROM self_improvement_candidates
                   WHERE status='active'
                   ORDER BY occurrence_count DESC, confidence DESC, updated_at DESC LIMIT ?
               )""",
            (utc_now(), maximum),
        )
        self.conn.commit()


__all__ = ["IMPROVEMENT_KINDS", "IMPROVEMENT_STATES", "SelfImprovementStore"]
