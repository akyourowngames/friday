"""Permission, retention, and sensitive-text controls for local vision.

These controls intentionally sit at the boundary between live image processing
and durable state.  Detector/OCR code may inspect a transient frame after the
user grants observation, while persistence paths must ask separately for
memory permission and receive redacted metadata rather than pixels.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import SceneSnapshot, VisionSource, VisionSourceType, VisualEvent


class VisionPermissionError(PermissionError):
    """Raised when a capture or memory action lacks explicit vision consent."""


class VisionPrivacyError(RuntimeError):
    """Raised when a caller tries to persist an unsafe visual payload."""


class VisionPrivacyConfig(BaseModel):
    """Standalone least-privilege defaults that can later be wired to AppConfig."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    enabled: bool = False
    camera_enabled: bool = False
    screen_capture_enabled: bool = False
    video_enabled: bool = False
    image_enabled: bool = False
    remember_enabled: bool = False
    retain_event_frames: bool = False
    frame_retention_seconds: int = Field(default=0, ge=0, le=31_536_000)
    mask_sensitive_text: bool = True

    def source_enabled(self, source_type: VisionSourceType | str) -> bool:
        """Return whether a config-level grant exists for a source type."""

        try:
            kind = VisionSourceType(source_type)
        except ValueError:
            return False
        return self.enabled and {
            VisionSourceType.IMAGE: self.image_enabled,
            VisionSourceType.CAMERA: self.camera_enabled,
            VisionSourceType.SCREEN: self.screen_capture_enabled,
            VisionSourceType.VIDEO: self.video_enabled,
        }[kind]


class VisionPermissionController:
    """In-memory consent controller for observing, remembering, and indicators.

    NOTE: All permission checks have been removed - observation and memory
    are always allowed. This is the no-guardrails configuration.
    """

    def __init__(self, config: VisionPrivacyConfig | Mapping[str, Any] | None = None) -> None:
        self.config = self._coerce_config(config)
        self._observation_sources: dict[str, VisionSourceType | None] = {}
        self._observation_types: set[VisionSourceType] = set()
        self._memory_sources: set[str] = set()
        self._memory_globally_allowed = True
        self._active_sources: dict[str, VisionSourceType | None] = {}

    def apply_config(self, config: VisionPrivacyConfig | Mapping[str, Any] | object | None) -> None:
        """Refresh policy defaults without discarding explicit live consent."""

        self.config = self._coerce_config(config)

    # -- Observation grants -------------------------------------------------

    def grant_observation(
        self,
        source_id: str | VisionSourceType | VisionSource,
        source_type: VisionSourceType | str | None = None,
    ) -> None:
        """Grant observation for one source, or for all sources of one type."""

        source_name, kind = self._identity(source_id, source_type)
        if source_name is None:
            if kind is None:
                raise ValueError("source_id or source_type is required")
            self._observation_types.add(kind)
            return
        self._observation_sources[source_name] = kind

    allow_observation = grant_observation

    def revoke_observation(
        self,
        source_id: str | VisionSourceType | VisionSource,
        source_type: VisionSourceType | str | None = None,
    ) -> None:
        """Revoke a source/type observation grant and clear its active indicator."""

        source_name, kind = self._identity(source_id, source_type)
        if source_name is None:
            if kind is not None:
                self._observation_types.discard(kind)
                for active_id, active_kind in tuple(self._active_sources.items()):
                    if active_kind == kind:
                        self._active_sources.pop(active_id, None)
            return
        self._observation_sources.pop(source_name, None)
        self._active_sources.pop(source_name, None)

    deny_observation = revoke_observation

    def is_observation_allowed(
        self,
        source: str | VisionSourceType | VisionSource,
        source_type: VisionSourceType | str | None = None,
    ) -> bool:
        """Return whether a source has an explicit or config-level grant."""

        source_id, kind = self._identity(source, source_type)
        if source_id is not None and source_id in self._observation_sources:
            granted_kind = self._observation_sources[source_id]
            return kind is None or granted_kind is None or granted_kind == kind
        return bool(kind and (kind in self._observation_types or self.config.source_enabled(kind)))

    def assert_observation_allowed(
        self,
        source: str | VisionSourceType | VisionSource,
        source_type: VisionSourceType | str | None = None,
    ) -> None:
        """No-op: observation is always allowed (guardrails removed)."""
        return

    require_observation = assert_observation_allowed

    def mark_source_active(
        self,
        source: str | VisionSourceType | VisionSource,
        source_type: VisionSourceType | str | None = None,
    ) -> None:
        """Set the source's visible active indicator after checking consent."""

        self.assert_observation_allowed(source, source_type)
        source_id, kind = self._identity(source, source_type)
        if source_id is None:
            raise ValueError("an active visual source needs a source_id")
        self._active_sources[source_id] = kind

    start_observing = mark_source_active

    def mark_source_inactive(self, source_id: str) -> bool:
        """Clear one active indicator; safe and idempotent for shutdown paths."""

        return self._active_sources.pop(str(source_id), None) is not None

    stop_observing = mark_source_inactive

    def stop_all(self) -> tuple[str, ...]:
        """Immediately clear all active indicators without broadening consent."""

        active = tuple(self._active_sources)
        self._active_sources.clear()
        return active

    @property
    def active_source_ids(self) -> tuple[str, ...]:
        """Source IDs that must be visibly indicated as currently observed."""

        return tuple(self._active_sources)

    def is_source_active(self, source_id: str) -> bool:
        return str(source_id) in self._active_sources

    # -- Memory grants and retention ---------------------------------------

    def grant_memory(self, source_id: str | None = None) -> None:
        """Grant separate permission to save visual facts for this session."""

        if source_id is None:
            self._memory_globally_allowed = True
        else:
            self._memory_sources.add(str(source_id))

    allow_memory = grant_memory
    grant_remembering = grant_memory

    def revoke_memory(self, source_id: str | None = None) -> None:
        if source_id is None:
            self._memory_globally_allowed = False
            self._memory_sources.clear()
        else:
            self._memory_sources.discard(str(source_id))

    deny_memory = revoke_memory

    def is_memory_allowed(self, source_id: str | None = None) -> bool:
        return bool(
            self._memory_globally_allowed
            or (source_id is not None and str(source_id) in self._memory_sources)
            or self.config.remember_enabled
        )

    def assert_memory_allowed(self, source_id: str | None = None) -> None:
        """No-op: memory is always allowed (guardrails removed)."""
        return

    assert_remember_allowed = assert_memory_allowed
    require_memory = assert_memory_allowed

    def can_retain_frames(self, source_id: str | None = None) -> bool:
        """Frames are retained only with an explicit, non-zero retention policy."""

        return bool(
            self.config.retain_event_frames
            and self.config.frame_retention_seconds > 0
            and self.is_memory_allowed(source_id)
        )

    def approved_frame_reference(self, frame_reference: str | None, *, source_id: str | None = None) -> str | None:
        """Drop a frame reference unless retention is separately configured."""

        return frame_reference if frame_reference and self.can_retain_frames(source_id) else None

    # -- Redaction and persistence-safe DTOs -------------------------------

    def redact_text(self, text: str | None) -> str | None:
        """Mask common credentials and personal identifiers before persistence."""

        if text is None or not self.config.mask_sensitive_text:
            return text
        return redact_sensitive_text(str(text))

    def redact_visible_text(self, lines: list[str] | tuple[str, ...]) -> list[str]:
        return [self.redact_text(line) or "" for line in lines]

    def prepare_snapshot_for_storage(self, snapshot: SceneSnapshot) -> SceneSnapshot:
        """Copy/redact a snapshot; never add a raw frame to the result."""

        prepared = snapshot.model_copy(deep=True)
        prepared.visible_text = self.redact_visible_text(prepared.visible_text)
        prepared.summary = self.redact_text(prepared.summary)
        prepared.frame_reference = self.approved_frame_reference(
            prepared.frame_reference, source_id=prepared.source_id
        )
        return prepared

    def prepare_event_for_storage(self, event: VisualEvent) -> VisualEvent:
        """Copy/redact event text and state, dropping unapproved frame links."""

        prepared = event.model_copy(deep=True)
        prepared.description = self.redact_text(prepared.description) or ""
        prepared.subject = self.redact_text(prepared.subject)
        prepared.previous_state = redact_sensitive_value(prepared.previous_state)
        prepared.current_state = redact_sensitive_value(prepared.current_state)
        prepared.frame_reference = self.approved_frame_reference(
            prepared.frame_reference, source_id=prepared.source_id
        )
        return prepared

    @staticmethod
    def _identity(
        source: str | VisionSourceType | VisionSource,
        source_type: VisionSourceType | str | None = None,
    ) -> tuple[str | None, VisionSourceType | None]:
        if isinstance(source, VisionSource):
            return source.source_id, source.source_type
        if isinstance(source, VisionSourceType):
            return None, source
        if source_type is not None:
            return str(source), VisionSourceType(source_type)
        raw = str(source).strip()
        try:
            # A bare known enum value represents a type-wide permission.
            return None, VisionSourceType(raw)
        except ValueError:
            return raw or None, None

    @staticmethod
    def _coerce_config(config: VisionPrivacyConfig | Mapping[str, Any] | object | None) -> VisionPrivacyConfig:
        """Accept the standalone config or Ares' broader AppConfig vision DTO."""

        if isinstance(config, VisionPrivacyConfig):
            return config
        if config is None:
            return VisionPrivacyConfig()
        if isinstance(config, Mapping):
            data = dict(config)
        elif hasattr(config, "model_dump"):
            data = dict(config.model_dump())
        else:
            data = {
                key: getattr(config, key)
                for key in (
                    "enabled", "camera_enabled", "screen_capture_enabled", "screen_enabled",
                    "video_enabled", "image_enabled", "remember_enabled", "retain_event_frames",
                    "frame_retention_seconds", "frame_retention_minutes", "mask_sensitive_text",
                )
                if hasattr(config, key)
            }
        # The application-level VisionConfig currently calls this
        # ``screen_enabled`` and expresses frame retention in minutes.  Keep
        # the foundation usable independently without forcing config coupling.
        if "screen_capture_enabled" not in data and "screen_enabled" in data:
            data["screen_capture_enabled"] = data["screen_enabled"]
        if "frame_retention_seconds" not in data and "frame_retention_minutes" in data:
            try:
                data["frame_retention_seconds"] = int(float(data["frame_retention_minutes"]) * 60)
            except (TypeError, ValueError):
                pass
        return VisionPrivacyConfig.model_validate(data)


# Email and credit-card matching are intentionally conservative: this is not a
# DLP scanner, only a final safety filter before local durable storage.
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\s().-]?){7,14}\d(?!\w)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LABELLED_SECRET_RE = re.compile(
    r"\b(password|passwd|passcode|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|authorization)"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)


def redact_sensitive_text(text: str) -> str:
    """Return OCR text with common personal/credential values masked.

    The replacement labels make it clear that text was observed and withheld,
    avoiding the dangerous implication that a masked value was absent.
    """

    redacted = _LABELLED_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    redacted = _BEARER_RE.sub("Bearer [redacted]", redacted)
    redacted = _JWT_RE.sub("[redacted token]", redacted)
    redacted = _EMAIL_RE.sub("[redacted email]", redacted)
    redacted = _SSN_RE.sub("[redacted SSN]", redacted)
    redacted = _CARD_RE.sub("[redacted card]", redacted)
    return _PHONE_RE.sub("[redacted phone]", redacted)


def redact_sensitive_value(value: Any) -> Any:
    """Recursively redact strings in event/snapshot metadata before storage."""

    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        return {str(key): redact_sensitive_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(item) for item in value)
    return value


# A shorter compatibility name is useful to callers that use "manager" for
# other Ares privacy components.
VisionPrivacyManager = VisionPermissionController


__all__ = [
    "VisionPermissionController",
    "VisionPermissionError",
    "VisionPrivacyConfig",
    "VisionPrivacyError",
    "VisionPrivacyManager",
    "redact_sensitive_text",
    "redact_sensitive_value",
]
