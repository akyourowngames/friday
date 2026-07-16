"""Typed, serialisable domain models for Ares Vision.

The vision pipeline handles image pixels in memory, but its durable boundary is
intentionally metadata-only.  In particular, :class:`VisionFrame` keeps its
image as a Pydantic private attribute so a normal ``model_dump()`` can never
accidentally write a raw camera or screen frame to SQLite, logs, or memory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from collections.abc import Mapping
from typing import Any, Iterable, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


def utc_now() -> datetime:
    """Return an aware UTC timestamp for persisted visual metadata."""

    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime:
    """Normalise a timestamp to aware UTC, treating naive values as UTC."""

    current = value or utc_now()
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def new_vision_id(kind: str) -> str:
    """Create readable, collision-resistant identifiers for vision records."""

    prefix = "".join(character for character in str(kind).strip().lower() if character.isalnum() or character == "_")
    return f"vision_{prefix or 'item'}_{uuid4().hex}"


class VisionSourceType(str, Enum):
    """The supported origin kinds for a visual observation."""

    IMAGE = "image"
    CAMERA = "camera"
    SCREEN = "screen"
    VIDEO = "video"


class VerificationStatus(str, Enum):
    """Conservative outcomes for an evidence-first visual verification."""

    PASSED = "passed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class VisionModel(BaseModel):
    """Shared Pydantic behaviour for durable vision DTOs.

    Ignoring unknown fields keeps old/new locally stored records loadable while
    preventing an accidental arbitrary payload (especially a raw image) from
    crossing a durable model boundary.  Validation on assignment still
    protects stateful tracker/watch objects after loading.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)


BoundingBox = tuple[int, int, int, int]

_RAW_FRAME_KEYS = frozenset({
    "image", "raw_image", "raw_frame", "frame", "pixels", "pixel_data",
    "frame_data", "image_data", "image_bytes", "screenshot",
})


def ensure_no_raw_frame_data(value: Any, *, location: str = "metadata") -> Any:
    """Reject in-memory pixels/bytes before they can reach durable DTO fields."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{location} must not contain raw frame bytes")
    # NumPy-like arrays have ``shape``; Pillow images expose ``getbands`` and
    # ``size``.  Duck typing avoids importing either optional runtime here.
    if hasattr(value, "shape") or (hasattr(value, "getbands") and hasattr(value, "size")):
        raise ValueError(f"{location} must not contain an in-memory image")
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).strip().casefold()
            child_location = f"{location}.{key}" if location else str(key)
            if key_text in _RAW_FRAME_KEYS and item is not None:
                raise ValueError(f"{child_location} must not contain raw frame data")
            ensure_no_raw_frame_data(item, location=child_location)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            ensure_no_raw_frame_data(item, location=f"{location}[{index}]")
    return value


def normalise_bounding_box(value: Sequence[int | float]) -> BoundingBox:
    """Return a finite integer XYXY box without assuming edge ordering.

    Some detector/tracker adapters can provide reversed corners transiently;
    geometry utilities normalise ordering when calculating area or overlap.
    Keeping the stored tuple in provider order preserves the source observation.
    """

    if len(value) != 4:
        raise ValueError("bounding_box must contain exactly four coordinates")
    converted: list[int] = []
    for coordinate in value:
        try:
            number = float(coordinate)
        except (TypeError, ValueError) as exc:
            raise ValueError("bounding_box coordinates must be numeric") from exc
        if not isfinite(number):
            raise ValueError("bounding_box coordinates must be finite")
        converted.append(int(round(number)))
    return tuple(converted)  # type: ignore[return-value]


def bounding_box_center(value: Sequence[int | float]) -> tuple[float, float]:
    """Return the centre of an XYXY bounding box."""

    left, top, right, bottom = normalise_bounding_box(value)
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def bounding_box_area(value: Sequence[int | float]) -> int:
    """Return a non-negative area even when a provider reverses an edge."""

    left, top, right, bottom = normalise_bounding_box(value)
    return abs(right - left) * abs(bottom - top)


def bounding_box_iou(first: Sequence[int | float], second: Sequence[int | float]) -> float:
    """Calculate intersection-over-union for two XYXY bounding boxes."""

    left_a, top_a, right_a, bottom_a = normalise_bounding_box(first)
    left_b, top_b, right_b, bottom_b = normalise_bounding_box(second)
    left_a, right_a = min(left_a, right_a), max(left_a, right_a)
    top_a, bottom_a = min(top_a, bottom_a), max(top_a, bottom_a)
    left_b, right_b = min(left_b, right_b), max(left_b, right_b)
    top_b, bottom_b = min(top_b, bottom_b), max(top_b, bottom_b)
    intersection_width = max(0, min(right_a, right_b) - max(left_a, left_b))
    intersection_height = max(0, min(bottom_a, bottom_b) - max(top_a, top_b))
    intersection = intersection_width * intersection_height
    union = bounding_box_area((left_a, top_a, right_a, bottom_a)) + bounding_box_area((left_b, top_b, right_b, bottom_b)) - intersection
    return intersection / union if union else 0.0


def approximate_region(
    value: Sequence[int | float],
    frame_size: tuple[int | float, int | float] | object | None,
) -> str | None:
    """Map a box to the plan's coarse top/centre/bottom × left/centre/right grid."""

    if frame_size is None:
        return None
    if isinstance(frame_size, dict):
        width, height = frame_size.get("width"), frame_size.get("height")
    elif hasattr(frame_size, "width") and hasattr(frame_size, "height"):
        width, height = getattr(frame_size, "width"), getattr(frame_size, "height")
    else:
        try:
            width, height = frame_size  # type: ignore[misc]
        except (TypeError, ValueError):
            return None
    try:
        width_value, height_value = float(width), float(height)
    except (TypeError, ValueError):
        return None
    if width_value <= 0 or height_value <= 0:
        return None
    centre_x, centre_y = bounding_box_center(value)
    column = min(2, max(0, int((centre_x / width_value) * 3)))
    row = min(2, max(0, int((centre_y / height_value) * 3)))
    return ("top", "center", "bottom")[row] + "_" + ("left", "center", "right")[column]


def _image_dimensions(image: Any) -> tuple[int | None, int | None]:
    """Read common Pillow/NumPy-like image dimensions without importing CV deps."""

    if image is None:
        return None, None
    size = getattr(image, "size", None)
    if isinstance(size, tuple) and len(size) >= 2:
        try:
            return int(size[0]), int(size[1])
        except (TypeError, ValueError):
            pass
    shape = getattr(image, "shape", None)
    if shape is not None and len(shape) >= 2:
        try:
            return int(shape[1]), int(shape[0])
        except (TypeError, ValueError):
            pass
    return None, None


class VisionFrame(VisionModel):
    """An in-memory captured frame with safe, metadata-only serialisation.

    ``image`` is accepted as a constructor argument and exposed as a property,
    but is deliberately excluded from Pydantic fields.  Pass pixels directly
    to a detector/OCR provider; persist only a separately approved
    ``frame_reference`` on a snapshot or event.
    """

    frame_id: str = Field(default_factory=lambda: new_vision_id("frame"), min_length=1, max_length=200)
    source_id: str = Field(default="image", min_length=1, max_length=200)
    source_type: VisionSourceType = VisionSourceType.IMAGE
    captured_at: datetime = Field(default_factory=utc_now)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    content_type: str = Field(default="image/png", min_length=1, max_length=200)
    frame_reference: str | None = Field(default=None, max_length=2_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _image: Any = PrivateAttr(default=None)

    def __init__(self, /, **data: Any) -> None:
        image = data.pop("image", data.pop("raw_frame", None))
        inferred_width, inferred_height = _image_dimensions(image)
        if data.get("width") is None and inferred_width is not None:
            data["width"] = inferred_width
        if data.get("height") is None and inferred_height is not None:
            data["height"] = inferred_height
        super().__init__(**data)
        self._image = image

    @field_validator("captured_at")
    @classmethod
    def _normalise_captured_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("metadata")
    @classmethod
    def _safe_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return ensure_no_raw_frame_data(value, location="metadata")

    @property
    def image(self) -> Any:
        """The transient Pillow/NumPy image, or ``None`` after disposal."""

        return self._image

    @property
    def raw_frame(self) -> Any:
        """Compatibility alias for integrations that call pixels a raw frame."""

        return self._image

    @property
    def has_image(self) -> bool:
        return self._image is not None

    def set_image(self, image: Any) -> None:
        """Attach an in-memory image and update omitted dimensions only."""

        self._image = image
        width, height = _image_dimensions(image)
        if self.width is None and width is not None:
            self.width = width
        if self.height is None and height is not None:
            self.height = height

    def clear_image(self) -> None:
        """Release this frame's pixel reference as soon as processing finishes."""

        self._image = None

    def persistent_dict(self) -> dict[str, Any]:
        """Return durable metadata; raw pixels are never included."""

        return self.model_dump(mode="json")

    @classmethod
    def from_image(
        cls,
        image: Any,
        *,
        source_id: str = "image",
        source_type: VisionSourceType = VisionSourceType.IMAGE,
        **metadata: Any,
    ) -> "VisionFrame":
        return cls(source_id=source_id, source_type=source_type, image=image, **metadata)


class DetectedObject(VisionModel):
    """One common-object detector output in XYXY pixel coordinates."""

    tracker_id: str | None = Field(default=None, min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    bounding_box: BoundingBox
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("label")
    @classmethod
    def _normalise_label(cls, value: str) -> str:
        normalised = value.strip()
        if not normalised:
            raise ValueError("label must not be blank")
        return normalised

    @field_validator("bounding_box", mode="before")
    @classmethod
    def _normalise_box(cls, value: Sequence[int | float]) -> BoundingBox:
        return normalise_bounding_box(value)

    @field_validator("attributes")
    @classmethod
    def _safe_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        return ensure_no_raw_frame_data(value, location="attributes")

    @property
    def center(self) -> tuple[float, float]:
        return bounding_box_center(self.bounding_box)

    @property
    def area(self) -> int:
        return bounding_box_area(self.bounding_box)

    def region(self, frame_size: tuple[int | float, int | float] | object | None) -> str | None:
        return approximate_region(self.bounding_box, frame_size)


class SceneSnapshot(VisionModel):
    """The durable semantic state of a source at one point in time."""

    snapshot_id: str = Field(default_factory=lambda: new_vision_id("snapshot"), min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    captured_at: datetime = Field(default_factory=utc_now)
    objects: list[DetectedObject] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    summary: str | None = Field(default=None, max_length=10_000)
    frame_reference: str | None = Field(default=None, max_length=2_000)

    @field_validator("captured_at")
    @classmethod
    def _normalise_captured_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("visible_text")
    @classmethod
    def _clean_visible_text(cls, value: Iterable[str]) -> list[str]:
        return [line.strip() for line in value if str(line).strip()]


class VisualEvent(VisionModel):
    """An important semantic scene change, not a raw frame record."""

    event_id: str = Field(default_factory=lambda: new_vision_id("event"), min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime = Field(default_factory=utc_now)
    subject: str | None = Field(default=None, max_length=500)
    description: str = Field(min_length=1, max_length=10_000)
    confidence: float = Field(ge=0.0, le=1.0)
    previous_state: dict[str, Any] | None = None
    current_state: dict[str, Any] | None = None
    frame_reference: str | None = Field(default=None, max_length=2_000)
    remembered: bool = False

    @field_validator("occurred_at")
    @classmethod
    def _normalise_occurred_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("event_type", "source_id", "description")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        normalised = value.strip()
        if not normalised:
            raise ValueError("event text fields must not be blank")
        return normalised

    @field_validator("previous_state", "current_state")
    @classmethod
    def _safe_event_state(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return ensure_no_raw_frame_data(value, location="event_state") if value is not None else None


def visual_event_public_dict(event: VisualEvent) -> dict[str, Any]:
    """Return a visual event without internal retained-frame handles.

    Frame references are only for the local retention subsystem.  They must
    never turn a tool/API/WebSocket response into an artifact-discovery API.
    """

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): scrub(item)
                for key, item in value.items()
                if str(key).casefold() not in {"frame_reference", "frame_path", "artifact_path"}
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    data = event.model_dump(mode="json")
    data.pop("frame_reference", None)
    return scrub(data)


class TrackedEntity(VisionModel):
    """A lightweight object identity maintained between selected detections."""

    tracker_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    latest_box: BoundingBox
    approximate_region: str | None = Field(default=None, max_length=100)
    state: dict[str, Any] = Field(default_factory=dict)

    @field_validator("first_seen_at", "last_seen_at")
    @classmethod
    def _normalise_seen_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("latest_box", mode="before")
    @classmethod
    def _normalise_latest_box(cls, value: Sequence[int | float]) -> BoundingBox:
        return normalise_bounding_box(value)

    @field_validator("state")
    @classmethod
    def _safe_state(cls, value: dict[str, Any]) -> dict[str, Any]:
        return ensure_no_raw_frame_data(value, location="tracked_state")


class VisionWatch(VisionModel):
    """A durable, user-created condition evaluated against visual events."""

    watch_id: str = Field(default_factory=lambda: new_vision_id("watch"), min_length=1, max_length=200)
    user_id: str = Field(default="default", min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    condition_text: str = Field(min_length=1, max_length=4_000)
    condition_type: str = Field(default="semantic", min_length=1, max_length=100)
    target_labels: list[str] = Field(default_factory=list)
    status: str = Field(default="active", min_length=1, max_length=100)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    cooldown_seconds: int = Field(default=0, ge=0, le=31_536_000)
    notify: bool = True
    remember_event: bool = False

    @field_validator("created_at", "expires_at")
    @classmethod
    def _normalise_watch_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_validator("target_labels")
    @classmethod
    def _clean_target_labels(cls, value: Iterable[str]) -> list[str]:
        # Keep ordering stable but avoid repeat labels from a natural-language parser.
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            label = str(item).strip()
            key = label.casefold()
            if label and key not in seen:
                result.append(label)
                seen.add(key)
        return result

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and ensure_utc(now) >= self.expires_at


class VisionSource(VisionModel):
    """A configured local visual source; it never embeds its latest frame."""

    source_id: str = Field(min_length=1, max_length=200)
    source_type: VisionSourceType = VisionSourceType.IMAGE
    status: str = Field(default="stopped", min_length=1, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    name: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_captured_at: datetime | None = None

    @field_validator("created_at", "updated_at", "last_captured_at")
    @classmethod
    def _normalise_source_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_validator("config")
    @classmethod
    def _safe_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return ensure_no_raw_frame_data(value, location="source_config")


class VerificationResult(VisionModel):
    """Evidence returned by a visual verification, never an automatic action."""

    status: VerificationStatus = VerificationStatus.UNCERTAIN
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


__all__ = [
    "BoundingBox",
    "DetectedObject",
    "SceneSnapshot",
    "TrackedEntity",
    "VerificationResult",
    "VerificationStatus",
    "VisionFrame",
    "VisionModel",
    "VisionSource",
    "VisionSourceType",
    "VisionWatch",
    "VisualEvent",
    "approximate_region",
    "bounding_box_area",
    "bounding_box_center",
    "bounding_box_iou",
    "ensure_utc",
    "ensure_no_raw_frame_data",
    "new_vision_id",
    "normalise_bounding_box",
    "utc_now",
    "visual_event_public_dict",
]
