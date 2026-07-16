from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ares.memory import MemoryStore
from ares.vision.events import make_visual_event
from ares.vision.service import VisionService


def test_direct_memory_delete_cleans_linked_vision_artifact(
    tmp_path: Path,
    fake_embedding_provider,
) -> None:
    memory = MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    service = VisionService(database_path=tmp_path / "vision.db", memory_store=memory)
    frame = service.store.artifact_root / "events" / "approved-frame.webp"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"local visual artifact")

    try:
        service.create_source(
            source_id="desk-camera",
            source_type="camera",
            grant_observe=True,
            grant_remember=True,
        )
        event = make_visual_event(
            event_type="object_appeared",
            source_id="desk-camera",
            subject="cup",
            description="A cup appeared on the desk.",
            confidence=0.92,
            frame_reference=str(frame),
        )
        service.store.save_event(event)
        fact_id = memory.store("Visual observation: a cup appeared.", source="visual_observation")
        service.store.record_memory_link(event.event_id, fact_id, frame_reference=str(frame))

        assert frame.exists()
        assert service.store.frame_references_for_memory(fact_id) == [str(frame)]
        assert memory.delete(fact_id) is True

        assert not frame.exists()
        assert service.store.frame_references_for_memory(fact_id) == []
        stored_event = service.store.get_event(event.event_id)
        assert stored_event is not None
        assert stored_event.remembered is False
        assert stored_event.frame_reference is None
        assert memory.delete(fact_id) is False
    finally:
        service.close()
        memory.close()


def test_bulk_memory_delete_also_cleans_linked_vision_artifact(
    tmp_path: Path,
    fake_embedding_provider,
) -> None:
    memory = MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    service = VisionService(database_path=tmp_path / "vision.db", memory_store=memory)
    frame = service.store.artifact_root / "events" / "bulk-approved-frame.webp"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"local visual artifact")

    try:
        service.create_source(source_id="screen", source_type="screen", grant_observe=True)
        event = make_visual_event(
            event_type="text_changed",
            source_id="screen",
            subject=None,
            description="The screen text changed.",
            confidence=0.88,
        )
        service.store.save_event(event)
        fact_id = memory.store("Visual observation: screen text changed.", source="visual_observation")
        service.store.record_memory_link(event.event_id, fact_id, frame_reference=str(frame))

        assert memory.bulk_delete([fact_id]) == 1
        assert not frame.exists()
        assert service.store.frame_references_for_memory(fact_id) == []
    finally:
        service.close()
        memory.close()


def test_erasing_a_source_also_removes_its_visual_memories(
    tmp_path: Path,
    fake_embedding_provider,
) -> None:
    memory = MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    service = VisionService(database_path=tmp_path / "vision.db", memory_store=memory)
    frame = service.store.artifact_root / "events" / "source-erasure.webp"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"local visual artifact")

    try:
        service.create_source(source_id="desk", source_type="camera", grant_observe=True)
        event = make_visual_event(
            event_type="object_moved",
            source_id="desk",
            subject="charger",
            description="The charger moved beside the notebook.",
            confidence=0.91,
            occurred_at=datetime.now(timezone.utc),
            frame_reference=str(frame),
        )
        service.store.save_event(event)
        fact_id = memory.store("Visual observation: charger beside notebook.", source="visual_observation")
        service.store.record_memory_link(event.event_id, fact_id, frame_reference=str(frame))

        assert service.delete_source("desk") is True
        assert memory.get(fact_id) is None
        assert service.store.get_event(event.event_id) is None
        assert not frame.exists()
    finally:
        service.close()
        memory.close()


def test_deleting_a_saved_frame_keeps_its_text_memory_but_clears_all_frame_handles(
    tmp_path: Path,
    fake_embedding_provider,
) -> None:
    memory = MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    service = VisionService(database_path=tmp_path / "vision.db", memory_store=memory)
    frame = service.store.artifact_root / "events" / "memory-frame-only.webp"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"local visual artifact")

    try:
        service.create_source(source_id="desk", source_type="camera", grant_observe=True)
        event = make_visual_event(
            event_type="object_moved",
            source_id="desk",
            subject="charger",
            description="The charger moved beside the notebook.",
            confidence=0.91,
            frame_reference=str(frame),
        )
        service.store.save_event(event)
        fact_id = memory.store("Visual observation: charger beside notebook.", source="visual_observation")
        service.store.record_memory_link(event.event_id, fact_id, frame_reference=str(frame))

        result = service.delete_memory_frame(fact_id)

        assert memory.get(fact_id) is not None
        assert result["deleted_frame_references"] == [str(frame)]
        assert service.store.frame_references_for_memory(fact_id) == []
        assert service.store.get_event(event.event_id).frame_reference is None
        assert not frame.exists()
    finally:
        service.close()
        memory.close()
