"""Scene snapshots, deterministic differencing, and duplicate suppression."""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .events import make_visual_event, value_for
from .models import DetectedObject, SceneSnapshot, VisualEvent
from .tracker import box_iou, center_distance, nine_cell_region, normalise_box


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _objects(snapshot: SceneSnapshot | object) -> list[DetectedObject]:
    raw = value_for(snapshot, "objects", [])
    return list(raw or [])


def _visible_text(snapshot: SceneSnapshot | object) -> list[str]:
    raw = value_for(snapshot, "visible_text", [])
    return [str(item) for item in (raw or []) if str(item).strip()]


def normalise_visible_text(lines: Sequence[str]) -> tuple[str, ...]:
    """Normalise OCR whitespace/case so trivial OCR jitter is not an event."""

    return tuple(" ".join(str(line).split()).casefold() for line in lines if str(line).strip())


def _attributes(item: DetectedObject | object) -> dict[str, Any]:
    raw = value_for(item, "attributes", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def object_state(item: DetectedObject | object, frame_size: object | None = None) -> dict[str, Any]:
    """Capture just enough object information to explain a visual event."""

    attributes = _attributes(item)
    box = tuple(value_for(item, "bounding_box"))
    region = attributes.get("region") or attributes.get("approximate_region") or nine_cell_region(box, frame_size)
    return {
        "label": str(value_for(item, "label", "object")),
        "tracker_id": value_for(item, "tracker_id"),
        "bounding_box": box,
        "region": region,
        "attributes": attributes,
    }


def _label(item: DetectedObject | object) -> str:
    return str(value_for(item, "label", "object"))


def _event_type(value: VisualEvent | object) -> str:
    raw = value_for(value, "event_type", "")
    return str(getattr(raw, "value", raw)).strip()


def _tracker_id(item: DetectedObject | object) -> str | None:
    raw = value_for(item, "tracker_id")
    return str(raw) if raw not in {None, ""} else None


def _object_sort_key(item: DetectedObject | object, index: int) -> tuple[Any, ...]:
    return (
        _label(item).casefold(),
        _tracker_id(item) or "",
        *normalise_box(value_for(item, "bounding_box")),
        -float(value_for(item, "confidence", 0.0)),
        index,
    )


def match_objects(
    previous: Sequence[DetectedObject],
    current: Sequence[DetectedObject],
    *,
    iou_threshold: float = 0.05,
    distance_threshold: float = 240.0,
) -> list[tuple[int, int]]:
    """Match objects by persistent ID, then deterministic IoU/nearest pairing."""

    old_items, new_items = list(previous), list(current)
    old_by_id: dict[str, list[int]] = defaultdict(list)
    new_by_id: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(old_items):
        if identifier := _tracker_id(item):
            old_by_id[identifier].append(index)
    for index, item in enumerate(new_items):
        if identifier := _tracker_id(item):
            new_by_id[identifier].append(index)

    pairs: list[tuple[int, int]] = []
    used_old: set[int] = set()
    used_new: set[int] = set()
    for identifier in sorted(set(old_by_id) & set(new_by_id)):
        old_indices = sorted(old_by_id[identifier], key=lambda index: _object_sort_key(old_items[index], index))
        new_indices = sorted(new_by_id[identifier], key=lambda index: _object_sort_key(new_items[index], index))
        for old_index, new_index in zip(old_indices, new_indices):
            if _label(old_items[old_index]) != _label(new_items[new_index]):
                continue
            pairs.append((old_index, new_index))
            used_old.add(old_index)
            used_new.add(new_index)

    candidates: list[tuple[float, float, tuple[Any, ...], tuple[Any, ...], int, int]] = []
    for old_index, old_item in enumerate(old_items):
        if old_index in used_old:
            continue
        old_box = value_for(old_item, "bounding_box")
        for new_index, new_item in enumerate(new_items):
            if new_index in used_new or _label(old_item) != _label(new_item):
                continue
            new_box = value_for(new_item, "bounding_box")
            overlap = box_iou(old_box, new_box)
            distance = center_distance(old_box, new_box)
            if overlap >= iou_threshold or distance <= distance_threshold:
                candidates.append(
                    (
                        -overlap,
                        distance,
                        _object_sort_key(old_item, old_index),
                        _object_sort_key(new_item, new_index),
                        old_index,
                        new_index,
                    )
                )
    for _negative_overlap, _distance, _old_key, _new_key, old_index, new_index in sorted(candidates):
        if old_index in used_old or new_index in used_new:
            continue
        pairs.append((old_index, new_index))
        used_old.add(old_index)
        used_new.add(new_index)
    return sorted(pairs, key=lambda pair: (_object_sort_key(old_items[pair[0]], pair[0]), _object_sort_key(new_items[pair[1]], pair[1])))


def _human_region(region: object | None) -> str | None:
    return str(region).replace("_", " ") if region else None


class SceneDiffer:
    """Compare two source-local snapshots and emit meaningful visual events.

    Each instance owns a small recent-signature cache.  Reusing it per source
    suppresses repeated detector outputs (especially the same movement across
    overlapping capture intervals) without persisting every raw frame.
    """

    def __init__(
        self,
        *,
        movement_threshold: float = 24.0,
        matching_iou_threshold: float = 0.05,
        matching_distance_threshold: float = 240.0,
        duplicate_window_seconds: float = 3.0,
        event_cache_size: int = 512,
        frame_size: object | None = None,
    ) -> None:
        if movement_threshold < 0:
            raise ValueError("movement_threshold must be non-negative")
        if not 0.0 <= matching_iou_threshold <= 1.0:
            raise ValueError("matching_iou_threshold must be between zero and one")
        if matching_distance_threshold < 0:
            raise ValueError("matching_distance_threshold must be non-negative")
        if duplicate_window_seconds < 0:
            raise ValueError("duplicate_window_seconds must be non-negative")
        if event_cache_size < 1:
            raise ValueError("event_cache_size must be at least one")
        self.movement_threshold = float(movement_threshold)
        self.matching_iou_threshold = float(matching_iou_threshold)
        self.matching_distance_threshold = float(matching_distance_threshold)
        self.duplicate_window_seconds = float(duplicate_window_seconds)
        self.event_cache_size = int(event_cache_size)
        self.frame_size = frame_size
        self._recent_signatures: OrderedDict[str, datetime] = OrderedDict()
        self._recent_events: deque[VisualEvent] = deque(maxlen=int(event_cache_size))

    @property
    def recent_events(self) -> tuple[VisualEvent, ...]:
        return tuple(self._recent_events)

    def reset(self) -> None:
        self._recent_signatures.clear()
        self._recent_events.clear()

    def diff(
        self,
        previous: SceneSnapshot | None,
        current: SceneSnapshot,
        *,
        frame_size: object | None = None,
        now: datetime | None = None,
    ) -> list[VisualEvent]:
        """Return appeared, disappeared, moved, text, and summary events.

        A first snapshot establishes a baseline; it intentionally does not say
        that every already-visible item "appeared" when a camera starts.
        """

        if previous is None:
            return []
        source_id = str(value_for(current, "source_id"))
        previous_source = str(value_for(previous, "source_id"))
        if source_id != previous_source:
            raise ValueError("scene snapshots must belong to the same source")
        dimensions = frame_size if frame_size is not None else self.frame_size
        occurred_at = _aware(now or value_for(current, "captured_at"))
        frame_reference = value_for(current, "frame_reference")
        previous_objects, current_objects = _objects(previous), _objects(current)
        pairs = match_objects(
            previous_objects,
            current_objects,
            iou_threshold=self.matching_iou_threshold,
            distance_threshold=self.matching_distance_threshold,
        )
        old_matched = {old_index for old_index, _new_index in pairs}
        new_matched = {new_index for _old_index, new_index in pairs}
        candidates: list[VisualEvent] = []

        for index, item in sorted(
            ((index, item) for index, item in enumerate(current_objects) if index not in new_matched),
            key=lambda pair: _object_sort_key(pair[1], pair[0]),
        ):
            state = object_state(item, dimensions)
            candidates.append(
                make_visual_event(
                    event_type="object_appeared",
                    source_id=source_id,
                    subject=_label(item),
                    description=f"{_label(item).capitalize()} appeared.",
                    confidence=float(value_for(item, "confidence", 0.0)),
                    previous_state=None,
                    current_state=state,
                    occurred_at=occurred_at,
                    frame_reference=frame_reference,
                )
            )

        for index, item in sorted(
            ((index, item) for index, item in enumerate(previous_objects) if index not in old_matched),
            key=lambda pair: _object_sort_key(pair[1], pair[0]),
        ):
            state = object_state(item, dimensions)
            candidates.append(
                make_visual_event(
                    event_type="object_disappeared",
                    source_id=source_id,
                    subject=_label(item),
                    description=f"{_label(item).capitalize()} disappeared.",
                    confidence=float(value_for(item, "confidence", 0.0)),
                    previous_state=state,
                    current_state=None,
                    occurred_at=occurred_at,
                    frame_reference=frame_reference,
                )
            )

        for old_index, new_index in pairs:
            old_item, new_item = previous_objects[old_index], current_objects[new_index]
            old_box, new_box = value_for(old_item, "bounding_box"), value_for(new_item, "bounding_box")
            old_state, new_state = object_state(old_item, dimensions), object_state(new_item, dimensions)
            moved_distance = center_distance(old_box, new_box)
            region_changed = old_state.get("region") and new_state.get("region") and old_state["region"] != new_state["region"]
            if moved_distance < self.movement_threshold and not region_changed:
                continue
            old_region, new_region = _human_region(old_state.get("region")), _human_region(new_state.get("region"))
            if old_region and new_region and old_region != new_region:
                description = f"{_label(new_item).capitalize()} moved from {old_region} to {new_region}."
            else:
                description = f"{_label(new_item).capitalize()} moved."
            candidates.append(
                make_visual_event(
                    event_type="object_moved",
                    source_id=source_id,
                    subject=_label(new_item),
                    description=description,
                    confidence=(float(value_for(old_item, "confidence", 0.0)) + float(value_for(new_item, "confidence", 0.0))) / 2.0,
                    previous_state=old_state,
                    current_state={**new_state, "distance_moved": round(moved_distance, 3), "iou": round(box_iou(old_box, new_box), 4)},
                    occurred_at=occurred_at,
                    frame_reference=frame_reference,
                )
            )

        previous_text, current_text = _visible_text(previous), _visible_text(current)
        if normalise_visible_text(previous_text) != normalise_visible_text(current_text):
            candidates.append(
                make_visual_event(
                    event_type="text_changed",
                    source_id=source_id,
                    subject=None,
                    description="Visible text changed.",
                    confidence=0.85,
                    previous_state={"visible_text": previous_text},
                    current_state={"visible_text": current_text},
                    occurred_at=occurred_at,
                    frame_reference=frame_reference,
                )
            )

        retained = [event for event in candidates if not self._is_duplicate(event, occurred_at)]
        if retained:
            changed_types = [_event_type(event) for event in retained]
            changed_subjects = [value_for(event, "subject") for event in retained]
            scene_event = make_visual_event(
                event_type="scene_changed",
                source_id=source_id,
                subject=None,
                description="Scene changed: " + ", ".join(item.replace("_", " ") for item in changed_types) + ".",
                confidence=max(float(value_for(event, "confidence", 0.0)) for event in retained),
                previous_state={"event_types": changed_types, "subjects": changed_subjects, "snapshot_id": value_for(previous, "snapshot_id")},
                current_state={"event_types": changed_types, "subjects": changed_subjects, "snapshot_id": value_for(current, "snapshot_id")},
                occurred_at=occurred_at,
                frame_reference=frame_reference,
            )
            if not self._is_duplicate(scene_event, occurred_at):
                retained.append(scene_event)
        self._recent_events.extend(retained)
        return retained

    compare = diff

    def _is_duplicate(self, event: VisualEvent, occurred_at: datetime) -> bool:
        signature = self._signature(event)
        previous = self._recent_signatures.get(signature)
        self._recent_signatures[signature] = occurred_at
        self._recent_signatures.move_to_end(signature)
        while len(self._recent_signatures) > self.event_cache_size:
            self._recent_signatures.popitem(last=False)
        if previous is None:
            return False
        elapsed = (occurred_at - previous).total_seconds()
        return 0.0 <= elapsed <= self.duplicate_window_seconds

    @staticmethod
    def _signature(event: VisualEvent) -> str:
        event_type = _event_type(event)
        source_id = str(value_for(event, "source_id", ""))
        subject = str(value_for(event, "subject", "") or "")
        previous_state = value_for(event, "previous_state") or {}
        current_state = value_for(event, "current_state") or {}
        if event_type in {"object_appeared", "object_moved"}:
            state = current_state
            box = state.get("bounding_box") if isinstance(state, Mapping) else None
            position = tuple(round(value / 10.0) for value in normalise_box(box)) if box else ()
            return f"{event_type}|{source_id}|{state.get('tracker_id') or subject}|{state.get('region')}|{position}"
        if event_type == "object_disappeared":
            state = previous_state
            return f"{event_type}|{source_id}|{state.get('tracker_id') or subject}"
        if event_type == "text_changed":
            lines = current_state.get("visible_text", []) if isinstance(current_state, Mapping) else []
            return f"{event_type}|{source_id}|{normalise_visible_text(lines)}"
        if event_type == "scene_changed":
            types = current_state.get("event_types", []) if isinstance(current_state, Mapping) else []
            subjects = current_state.get("subjects", []) if isinstance(current_state, Mapping) else []
            return f"{event_type}|{source_id}|{tuple(types)}|{tuple(subjects)}"
        return f"{event_type}|{source_id}|{subject}"


SceneComparator = SceneDiffer


def diff_snapshots(
    previous: SceneSnapshot | None,
    current: SceneSnapshot,
    **kwargs: Any,
) -> list[VisualEvent]:
    """Convenience one-shot comparison for callers without scene state."""

    return SceneDiffer(**kwargs).diff(previous, current)
