"""Deterministic lightweight object tracking for consecutive vision frames.

This is intentionally a small tracker rather than a dependency on a CV
runtime: detector outputs can retain useful identities even when YOLO only runs
on selected frames.  It is not intended to replace ByteTrack for dense or
occluded scenes; its predictable matching is a solid local V1 fallback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import hypot
from typing import Any

from .events import value_for
from .models import DetectedObject, TrackedEntity


BoundingBox = tuple[float, float, float, float]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalise_box(box: Sequence[float | int]) -> BoundingBox:
    """Return an ``(left, top, right, bottom)`` box with ordered edges.

    Detector providers in Ares use XYXY coordinates.  Ordering the edges here
    makes geometry resilient to an accidental reversed pair without silently
    treating an invalid box as a negative-area match.
    """

    if len(box) != 4:
        raise ValueError("bounding_box must contain exactly four coordinates")
    left, top, right, bottom = (float(value) for value in box)
    return min(left, right), min(top, bottom), max(left, right), max(top, bottom)


# American spelling is convenient for callers outside the existing codebase.
normalize_box = normalise_box


def box_area(box: Sequence[float | int]) -> float:
    left, top, right, bottom = normalise_box(box)
    return max(0.0, right - left) * max(0.0, bottom - top)


def box_center(box: Sequence[float | int]) -> tuple[float, float]:
    left, top, right, bottom = normalise_box(box)
    return (left + right) / 2.0, (top + bottom) / 2.0


def box_iou(first: Sequence[float | int], second: Sequence[float | int]) -> float:
    """Calculate intersection-over-union for two XYXY boxes."""

    left_a, top_a, right_a, bottom_a = normalise_box(first)
    left_b, top_b, right_b, bottom_b = normalise_box(second)
    intersection_left = max(left_a, left_b)
    intersection_top = max(top_a, top_b)
    intersection_right = min(right_a, right_b)
    intersection_bottom = min(bottom_a, bottom_b)
    intersection = max(0.0, intersection_right - intersection_left) * max(0.0, intersection_bottom - intersection_top)
    union = box_area((left_a, top_a, right_a, bottom_a)) + box_area((left_b, top_b, right_b, bottom_b)) - intersection
    return intersection / union if union > 0 else 0.0


def center_distance(first: Sequence[float | int], second: Sequence[float | int]) -> float:
    """Return Euclidean distance between bounding-box centres in pixels."""

    first_x, first_y = box_center(first)
    second_x, second_y = box_center(second)
    return hypot(first_x - second_x, first_y - second_y)


def _frame_dimensions(frame_size: object | None) -> tuple[float, float] | None:
    if frame_size is None:
        return None
    if isinstance(frame_size, Mapping):
        width, height = frame_size.get("width"), frame_size.get("height")
    elif hasattr(frame_size, "width") and hasattr(frame_size, "height"):
        width, height = getattr(frame_size, "width"), getattr(frame_size, "height")
    else:
        try:
            width, height = frame_size  # type: ignore[misc]
        except (TypeError, ValueError):
            raise ValueError("frame_size must provide width and height") from None
    try:
        width_value, height_value = float(width), float(height)
    except (TypeError, ValueError):
        return None
    if width_value <= 0 or height_value <= 0:
        return None
    return width_value, height_value


_REGION_ROWS = ("top", "center", "bottom")
_REGION_COLUMNS = ("left", "center", "right")


def nine_cell_region(
    box: Sequence[float | int],
    frame_size: object | None,
) -> str | None:
    """Map an object's centre to the plan's coarse three-by-three grid."""

    dimensions = _frame_dimensions(frame_size)
    if dimensions is None:
        return None
    width, height = dimensions
    center_x, center_y = box_center(box)
    column = min(2, max(0, int((center_x / width) * 3)))
    row = min(2, max(0, int((center_y / height) * 3)))
    return f"{_REGION_ROWS[row]}_{_REGION_COLUMNS[column]}"


# A more descriptive public spelling for callers that do not care about grid
# implementation details.
approximate_region = nine_cell_region


def _object_attributes(item: DetectedObject | object) -> dict[str, Any]:
    attributes = value_for(item, "attributes", {})
    return dict(attributes) if isinstance(attributes, Mapping) else {}


def _copy_with_tracker_id(item: DetectedObject, tracker_id: str) -> DetectedObject:
    """Return a detection with an assigned ID without mutating caller input."""

    if value_for(item, "tracker_id") == tracker_id:
        return item
    if hasattr(item, "model_copy"):
        return item.model_copy(update={"tracker_id": tracker_id})
    if hasattr(item, "copy"):
        return item.copy(update={"tracker_id": tracker_id})
    return DetectedObject(
        label=str(value_for(item, "label")),
        confidence=float(value_for(item, "confidence", 0.0)),
        bounding_box=tuple(value_for(item, "bounding_box")),
        tracker_id=tracker_id,
        attributes=_object_attributes(item),
    )


def _timestamp(value: datetime | None) -> datetime:
    current = value or utc_now()
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


class ObjectTracker:
    """Assign stable IDs using explicit IDs, IoU, then nearest-centre matching.

    Matching is constrained to the same label and greedily consumes candidates
    sorted by ``(-IoU, distance, existing_id, detection_geometry)``.  That
    makes ties deterministic and avoids an ID depending on incidental dict
    iteration order.
    """

    def __init__(
        self,
        *,
        iou_threshold: float = 0.20,
        distance_threshold: float = 120.0,
        max_missed_frames: int = 5,
        id_prefix: str = "track",
    ) -> None:
        if not 0.0 <= float(iou_threshold) <= 1.0:
            raise ValueError("iou_threshold must be between zero and one")
        if distance_threshold < 0:
            raise ValueError("distance_threshold must be non-negative")
        if max_missed_frames < 0:
            raise ValueError("max_missed_frames must be non-negative")
        self.iou_threshold = float(iou_threshold)
        self.distance_threshold = float(distance_threshold)
        self.max_missed_frames = int(max_missed_frames)
        self.id_prefix = id_prefix.strip() or "track"
        self._tracks: dict[str, TrackedEntity] = {}
        self._missed: dict[str, int] = {}
        self._next_identifier = 1

    @property
    def tracks(self) -> dict[str, TrackedEntity]:
        """Active entities keyed by their persistent tracker ID."""

        return self._tracks

    @property
    def tracked_objects(self) -> dict[str, TrackedEntity]:
        """Alias aligned with ``SceneState.tracked_objects`` in the plan."""

        return self._tracks

    def reset(self) -> None:
        self._tracks.clear()
        self._missed.clear()
        self._next_identifier = 1

    def update(
        self,
        detections: Sequence[DetectedObject],
        *,
        frame_size: object | None = None,
        observed_at: datetime | None = None,
    ) -> list[DetectedObject]:
        """Assign persistent IDs and update active tracked entities.

        The returned list preserves the input order and never mutates detector
        output in place, which keeps it safe to reuse in raw snapshots.
        """

        when = _timestamp(observed_at)
        original = list(detections)
        assignments: dict[int, str] = {}
        matched_track_ids: set[str] = set()

        # Respect a provider-supplied ID when it is already known.  This lets a
        # future ByteTrack provider interoperate with the lightweight fallback.
        for index, detection in enumerate(original):
            explicit_id = value_for(detection, "tracker_id")
            if not explicit_id:
                continue
            tracker_id = str(explicit_id)
            track = self._tracks.get(tracker_id)
            if track is not None and value_for(track, "label") == value_for(detection, "label") and tracker_id not in matched_track_ids:
                assignments[index] = tracker_id
                matched_track_ids.add(tracker_id)

        # Build every viable same-label association before consuming one-to-one
        # pairs.  Sorting detection geometry as a final tiebreak avoids input
        # ordering changing an otherwise identical result.
        candidates: list[tuple[float, float, str, tuple[Any, ...], int]] = []
        for index, detection in enumerate(original):
            if index in assignments:
                continue
            label = str(value_for(detection, "label", ""))
            detection_box = value_for(detection, "bounding_box")
            geometry_key = (
                label,
                *normalise_box(detection_box),
                -float(value_for(detection, "confidence", 0.0)),
                index,
            )
            for tracker_id, track in self._tracks.items():
                if tracker_id in matched_track_ids or value_for(track, "label") != label:
                    continue
                overlap = box_iou(value_for(track, "latest_box"), detection_box)
                distance = center_distance(value_for(track, "latest_box"), detection_box)
                if overlap >= self.iou_threshold or distance <= self.distance_threshold:
                    candidates.append((-overlap, distance, tracker_id, geometry_key, index))

        for _negative_iou, _distance, tracker_id, _geometry, index in sorted(candidates):
            if index in assignments or tracker_id in matched_track_ids:
                continue
            assignments[index] = tracker_id
            matched_track_ids.add(tracker_id)

        # An explicit, unknown provider ID remains useful.  A duplicate explicit
        # ID is made unique so two simultaneous detections never share an entity.
        for index, detection in enumerate(original):
            if index in assignments:
                continue
            explicit_id = value_for(detection, "tracker_id")
            requested_id = str(explicit_id) if explicit_id else ""
            tracker_id = requested_id if requested_id and requested_id not in self._tracks and requested_id not in assignments.values() else self._new_identifier()
            assignments[index] = tracker_id
            matched_track_ids.add(tracker_id)

        assigned_detections: list[DetectedObject] = []
        for index, detection in enumerate(original):
            tracker_id = assignments[index]
            tracked_detection = _copy_with_tracker_id(detection, tracker_id)
            assigned_detections.append(tracked_detection)
            self._upsert_track(tracked_detection, frame_size=frame_size, observed_at=when)
            self._missed[tracker_id] = 0

        for tracker_id in tuple(self._tracks):
            if tracker_id in matched_track_ids:
                continue
            missed = self._missed.get(tracker_id, 0) + 1
            if missed > self.max_missed_frames:
                self._tracks.pop(tracker_id, None)
                self._missed.pop(tracker_id, None)
            else:
                self._missed[tracker_id] = missed
        return assigned_detections

    # Friendly aliases used by detector and service adapters.
    track = update
    assign = update

    def _new_identifier(self) -> str:
        while True:
            tracker_id = f"{self.id_prefix}_{self._next_identifier:06d}"
            self._next_identifier += 1
            if tracker_id not in self._tracks:
                return tracker_id

    def _upsert_track(
        self,
        detection: DetectedObject,
        *,
        frame_size: object | None,
        observed_at: datetime,
    ) -> None:
        tracker_id = str(value_for(detection, "tracker_id"))
        box = tuple(value_for(detection, "bounding_box"))
        region = nine_cell_region(box, frame_size)
        state = _object_attributes(detection)
        existing = self._tracks.get(tracker_id)
        if existing is None:
            self._tracks[tracker_id] = TrackedEntity(
                tracker_id=tracker_id,
                label=str(value_for(detection, "label")),
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                latest_box=box,
                approximate_region=region,
                state=state,
            )
            return
        existing.label = str(value_for(detection, "label"))
        existing.last_seen_at = observed_at
        existing.latest_box = box
        existing.approximate_region = region
        existing.state = state


# The name in the implementation plan is useful at call sites that explicitly
# want the fallback tracker rather than a detector-provider implementation.
PersistentObjectTracker = ObjectTracker
