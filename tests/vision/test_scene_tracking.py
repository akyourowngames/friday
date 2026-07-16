from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ares.vision.models import DetectedObject, SceneSnapshot
from ares.vision.scene import SceneDiffer
from ares.vision.tracker import ObjectTracker, nine_cell_region


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def detected(label: str, box: tuple[int, int, int, int], *, tracker_id: str | None = None) -> DetectedObject:
    return DetectedObject(label=label, confidence=0.91, bounding_box=box, tracker_id=tracker_id)


def test_tracker_keeps_ids_when_detector_order_changes() -> None:
    tracker = ObjectTracker(iou_threshold=0.1, distance_threshold=50)
    first = tracker.update(
        [detected("cup", (0, 0, 30, 30)), detected("cup", (120, 0, 150, 30))],
        frame_size=(300, 300),
        observed_at=NOW,
    )
    second = tracker.update(
        [detected("cup", (125, 0, 155, 30)), detected("cup", (5, 0, 35, 30))],
        frame_size=(300, 300),
        observed_at=NOW + timedelta(seconds=1),
    )

    assert second[0].tracker_id == first[1].tracker_id
    assert second[1].tracker_id == first[0].tracker_id
    assert nine_cell_region(second[1].bounding_box, (300, 300)) == "top_left"
    assert tracker.tracks[first[0].tracker_id].last_seen_at == NOW + timedelta(seconds=1)


def test_scene_differ_emits_changes_once_for_repeated_comparison() -> None:
    previous = SceneSnapshot(
        source_id="desk-camera",
        captured_at=NOW,
        objects=[detected("cup", (0, 0, 30, 30), tracker_id="cup-1")],
        visible_text=["Download 90%"],
    )
    current = SceneSnapshot(
        source_id="desk-camera",
        captured_at=NOW + timedelta(seconds=1),
        objects=[
            detected("cup", (120, 0, 150, 30), tracker_id="cup-1"),
            detected("book", (10, 150, 100, 250), tracker_id="book-1"),
        ],
        visible_text=["Download 100%"],
    )
    differ = SceneDiffer(frame_size=(300, 300), movement_threshold=10, duplicate_window_seconds=5)

    events = differ.diff(previous, current)
    event_types = {event.event_type for event in events}

    assert {"object_appeared", "object_moved", "text_changed", "scene_changed"} <= event_types
    moved = next(event for event in events if event.event_type == "object_moved")
    assert moved.subject == "cup"
    assert moved.previous_state["region"] == "top_left"
    assert moved.current_state["region"] == "top_center"
    assert differ.diff(previous, current, now=NOW + timedelta(seconds=2)) == []


def test_first_scene_is_a_baseline_not_a_burst_of_appear_events() -> None:
    snapshot = SceneSnapshot(source_id="screen", objects=[detected("laptop", (1, 1, 100, 100))])

    assert SceneDiffer().diff(None, snapshot) == []
