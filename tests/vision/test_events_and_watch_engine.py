from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ares.vision.events import VisionEventBus
from ares.vision.models import DetectedObject, SceneSnapshot, VisualEvent, VisionWatch
from ares.vision.watch_engine import WatchEngine, parse_watch_condition


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def scene_event(
    event_type: str,
    *,
    subject: str | None = "cup",
    previous_state: dict | None = None,
    current_state: dict | None = None,
) -> VisualEvent:
    return VisualEvent(
        event_type=event_type,
        source_id="screen",
        subject=subject,
        description=event_type,
        confidence=0.9,
        previous_state=previous_state,
        current_state=current_state,
        occurred_at=NOW,
    )


@pytest.mark.asyncio
async def test_event_bus_drops_oldest_event_for_slow_subscriber() -> None:
    bus = VisionEventBus(queue_size=1)
    subscription = bus.subscribe(source_id="screen")
    first = scene_event("object_appeared")
    second = scene_event("object_moved")

    assert int(bus.publish(first)) == 1
    assert await bus.publish(second) == 1
    received = await subscription.get()

    assert received.event_id == second.event_id
    assert subscription.dropped_events == 1
    subscription.close()
    assert bus.subscriber_count == 0


def test_watch_engine_completes_movement_watch_once() -> None:
    watch = VisionWatch(
        source_id="screen",
        condition_text="Tell me when the cup moves",
        target_labels=["cup"],
        cooldown_seconds=30,
        remember_event=True,
    )
    moved = scene_event(
        "object_moved",
        previous_state={"label": "cup", "region": "top_left"},
        current_state={"label": "cup", "region": "bottom_right"},
    )
    engine = WatchEngine()

    triggered = engine.evaluate(watch, [moved], now=NOW)

    assert len(triggered) == 1
    assert triggered[0].event_type == "watch_condition_met"
    assert triggered[0].current_state["notify"] is True
    assert triggered[0].current_state["remember_event"] is True
    assert triggered[0].remembered is False
    assert watch.status == "completed"
    assert engine.evaluate(watch, [moved], now=NOW + timedelta(seconds=1)) == []


def test_watch_parser_and_region_entry_rule() -> None:
    rule = parse_watch_condition("Notify me when the cup enters the bottom right region")
    assert rule.condition_type == "enters_region"
    assert rule.target_labels == ["cup"]
    assert rule.region == "bottom_right"

    watch = VisionWatch(source_id="screen", condition_text="cup enters bottom right", condition_type="enters_region", target_labels=["cup"])
    entered = scene_event(
        "object_moved",
        previous_state={"label": "cup", "region": "top_left"},
        current_state={"label": "cup", "region": "bottom_right"},
    )

    assert WatchEngine().evaluate(watch, [entered], now=NOW)
    assert watch.status == "completed"


def test_text_progress_unchanged_and_expired_watches() -> None:
    snapshot = SceneSnapshot(
        source_id="screen",
        captured_at=NOW,
        objects=[DetectedObject(label="download", confidence=0.9, bounding_box=(0, 0, 10, 10))],
        visible_text=["Download complete: 100%"],
    )
    engine = WatchEngine(default_unchanged_seconds=5)
    text_watch = VisionWatch(source_id="screen", condition_text='text contains "complete"', condition_type="text_contains")
    changed_watch = VisionWatch(source_id="screen", condition_text="text changes", condition_type="text_changed")
    progress_watch = VisionWatch(source_id="screen", condition_text="download reaches 100 percent", condition_type="progress_reaches")
    unchanged_watch = VisionWatch(source_id="screen", condition_text="scene remains unchanged for 5 seconds", condition_type="scene_unchanged")
    expired_watch = VisionWatch(
        source_id="screen",
        condition_text="cup moves",
        condition_type="object_moves",
        expires_at=NOW - timedelta(seconds=1),
    )

    assert engine.evaluate(text_watch, snapshot=snapshot, now=NOW)
    assert engine.evaluate(changed_watch, [scene_event("text_changed", subject=None)], now=NOW)
    assert engine.evaluate(progress_watch, snapshot=snapshot, now=NOW)
    assert engine.evaluate(unchanged_watch, snapshot=snapshot, now=NOW) == []
    assert engine.evaluate(unchanged_watch, snapshot=snapshot, now=NOW + timedelta(seconds=5))
    assert engine.evaluate(expired_watch, snapshot=snapshot, now=NOW) == []
    assert expired_watch.status == "expired"


def test_text_contains_uses_current_text_not_text_that_was_removed() -> None:
    watch = VisionWatch(source_id="screen", condition_text='text contains "complete"', condition_type="text_contains")
    snapshot = SceneSnapshot(source_id="screen", visible_text=["Download failed"])
    changed = scene_event(
        "text_changed",
        subject=None,
        previous_state={"visible_text": ["Download complete"]},
        current_state={"visible_text": ["Download failed"]},
    )

    assert WatchEngine().evaluate(watch, [changed], snapshot=snapshot, now=NOW) == []
