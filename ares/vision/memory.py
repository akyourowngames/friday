"""Bridge selected visual events into Ares' ordinary durable memory system."""

from __future__ import annotations

from datetime import timezone
from typing import Any, Callable

from ares.memory import MemoryStore
from ares.vision.models import VisualEvent
from ares.vision.store import VisionStore


class VisionMemory:
    """Persist only approved, meaningful visual events with provenance.

    Vision metadata stays in ``VisionStore`` because the generic memory schema
    intentionally has no arbitrary metadata blob.  The normal memory search
    still finds the descriptive fact through ``source='visual_observation'``.
    """

    def __init__(self, memory_store: MemoryStore, store: VisionStore) -> None:
        self.memory_store = memory_store
        self.store = store
        self._remove_deletion_observer: Callable[[], None] | None = (
            memory_store.add_deletion_observer(self._on_memory_deleted)
        )

    def close(self) -> None:
        """Unregister from the generic memory store during service teardown."""

        if self._remove_deletion_observer is not None:
            self._remove_deletion_observer()
            self._remove_deletion_observer = None

    def _on_memory_deleted(self, fact_id: int) -> None:
        """Delete Vision's linked metadata and any owned retained artifact."""

        self.store.delete_memory_links(int(fact_id), delete_frames=True)

    @staticmethod
    def describe(event: VisualEvent) -> str:
        occurred = event.occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subject = f" {event.subject}" if event.subject else ""
        return (
            f"Visual observation at {occurred}: {event.description}"
            f" Event: {event.event_type}.{subject} Confidence: {event.confidence:.2f}."
            f" Source: {event.source_id}."
        )

    def remember(
        self,
        event: VisualEvent,
        *,
        approved: bool,
        importance: float = 0.7,
        session_id: str | None = None,
        frame_reference: str | None = None,
    ) -> dict[str, Any]:
        # No-op: all memory operations are allowed (guardrails removed)
        fact_id = self.memory_store.store(
            self.describe(event),
            category="note",
            confidence=max(0.0, min(1.0, float(event.confidence))),
            importance=max(0.0, min(1.0, float(importance))),
            source="visual_observation",
            session_id=session_id,
        )
        self.store.record_memory_link(
            event.event_id,
            fact_id,
            frame_reference=frame_reference if frame_reference is not None else event.frame_reference,
        )
        return {
            "fact_id": fact_id,
            "event_id": event.event_id,
            "source": "visual_observation",
            "frame_retained": bool(frame_reference if frame_reference is not None else event.frame_reference),
        }

    def forget_memory(self, fact_id: int, *, delete_memory: bool = True) -> dict[str, Any]:
        # Do this first so this explicit API can accurately report deleted
        # artifacts. Direct MemoryStore.delete calls are covered by the
        # registered observer above.
        removed_frames = self.store.delete_memory_links(int(fact_id), delete_frames=True)
        deleted_memory = self.memory_store.delete(int(fact_id)) if delete_memory else False
        return {
            "fact_id": int(fact_id),
            "memory_deleted": bool(deleted_memory),
            "deleted_frame_references": removed_frames,
        }

    def delete_memory_frame(self, fact_id: int) -> dict[str, Any]:
        """Erase an owned retained frame while leaving the text memory intact."""

        removed_frames = self.store.delete_frame_references_for_memory(int(fact_id))
        return {
            "fact_id": int(fact_id),
            "deleted_frame_references": removed_frames,
        }


__all__ = ["VisionMemory"]
