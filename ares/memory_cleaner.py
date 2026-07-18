"""Memory cleanup through merge and archival rather than silent deletion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ares.memory_policy import memory_rejection_reason


class MemoryCleaner:
    """Merge duplicates and archive ordinary stale/policy-invalid facts."""

    DEDUP_SIMILARITY_THRESHOLD = 0.3
    STALE_DAYS = 90
    LOW_IMPORTANCE_THRESHOLD = 0.2
    MIN_ACCESS_COUNT = 2

    def __init__(self, memory_store: Any, stale_days: int | None = None):
        self.memory_store = memory_store
        if stale_days is not None:
            self.STALE_DAYS = stale_days

    def cleanup(self) -> dict:
        """Run full cleanup: dedup, merge, and prune. Returns stats."""
        stats = {
            "duplicates_merged": 0,
            "policy_pruned": 0,
            "stale_pruned": 0,
            "policy_archived": 0,
            "stale_archived": 0,
            "total_before": 0,
            "total_after": 0,
        }

        all_memories = self.memory_store.list_all()
        stats["total_before"] = len(all_memories)

        stats["policy_pruned"] = self._prune_policy_violations(all_memories)
        stats["policy_archived"] = stats["policy_pruned"]
        all_memories = self.memory_store.list_all()
        stats["duplicates_merged"] = self._dedup_similar(all_memories)
        stats["stale_pruned"] = self._prune_stale()
        stats["stale_archived"] = stats["stale_pruned"]

        stats["total_after"] = len(self.memory_store.list_all())
        return stats

    def _prune_policy_violations(self, memories: list[dict]) -> int:
        """Archive memories that now violate deterministic memory policy."""
        pruned = 0
        for mem in memories:
            reason = memory_rejection_reason(
                mem.get("fact_text", ""),
                category=mem.get("category", "note"),
                confidence=float(mem.get("confidence", 1.0) or 1.0),
            )
            if reason:
                archive = getattr(self.memory_store, "archive", None)
                if callable(archive):
                    archive(mem["fact_id"], reason=f"memory policy: {reason}")
                else:
                    # Compatibility stores predating V3 only expose delete.
                    self.memory_store.delete(mem["fact_id"])
                pruned += 1
        return pruned

    def _dedup_similar(self, memories: list[dict]) -> int:
        """Find similar memories and merge them."""
        merged = 0
        seen: set[int] = set()

        for mem_a in memories:
            if mem_a["fact_id"] in seen:
                continue

            similar = self._find_similar(mem_a)
            if len(similar) < 2:
                continue

            best = max(similar, key=lambda m: m.get("importance", 0.5))
            others = [m for m in similar if m["fact_id"] != best["fact_id"]]

            merge_memories = getattr(self.memory_store, "merge_memories", None)
            active_others = [other for other in others if other["fact_id"] not in seen]
            if active_others and callable(merge_memories):
                merge_memories(best["fact_id"], [other["fact_id"] for other in active_others])
                merged += len(active_others)
                seen.update(other["fact_id"] for other in active_others)
            else:
                merged_text = best["fact_text"]
                for other in active_others:
                    merged_text += f" Also: {other['fact_text']}"
                    archive = getattr(self.memory_store, "archive", None)
                    if callable(archive):
                        archive(other["fact_id"], reason=f"merged into memory {best['fact_id']}")
                    else:
                        self.memory_store.delete(other["fact_id"])
                    seen.add(other["fact_id"])
                    merged += 1
                if merged_text != best["fact_text"]:
                    self.memory_store.update(best["fact_id"], fact_text=merged_text)
            seen.add(best["fact_id"])

        return merged

    def _find_similar(self, memory: dict) -> list[dict]:
        """Find memories similar to the given one using vector search."""
        results = self.memory_store.search(memory["fact_text"], limit=10)
        similar = []
        for r in results:
            if r.get("_score", 1.0) < self.DEDUP_SIMILARITY_THRESHOLD:
                similar.append(r)
        return similar

    def _prune_stale(self) -> int:
        """Archive old, low-importance, rarely-accessed memories."""
        pruned = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.STALE_DAYS)

        all_memories = self.memory_store.list_all()
        for mem in all_memories:
            importance = mem.get("importance", 0.5)
            access_count = mem.get("access_count", 0)
            created_at = mem.get("created_at", "")

            if importance >= self.LOW_IMPORTANCE_THRESHOLD:
                continue
            if access_count >= self.MIN_ACCESS_COUNT:
                continue

            try:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created > cutoff:
                    continue
            except (ValueError, TypeError):
                continue

            archive = getattr(self.memory_store, "archive", None)
            if callable(archive):
                archive(mem["fact_id"], reason="stale low-confidence lifecycle cleanup")
            else:
                self.memory_store.delete(mem["fact_id"])
            pruned += 1

        return pruned
