"""Detector interface and lazy default-provider factory for Ares Vision."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import DetectedObject, VisionFrame
from .providers.base import VisionDependencyError


@runtime_checkable
class VisionDetector(Protocol):
    """Replaceable object detector contract from the V1 architecture plan."""

    async def detect(
        self,
        frame: VisionFrame,
        prompts: list[str] | None = None,
    ) -> list[DetectedObject]: ...


class UnavailableVisionDetector:
    """A clear fail-loud detector used when no local model is configured."""

    def __init__(self, dependency: str = "ultralytics") -> None:
        self.dependency = dependency

    async def detect(
        self,
        frame: VisionFrame,
        prompts: list[str] | None = None,
    ) -> list[DetectedObject]:
        raise VisionDependencyError(self.dependency)


def filter_detections_by_prompts(
    objects: list[DetectedObject],
    prompts: list[str] | None,
) -> list[DetectedObject]:
    """Apply an optional simple label filter without changing detector output."""

    wanted = [str(prompt).strip().casefold() for prompt in (prompts or []) if str(prompt).strip()]
    if not wanted:
        return list(objects)
    return [
        item
        for item in objects
        if any(prompt == item.label.casefold() or prompt in item.label.casefold() or item.label.casefold() in prompt for prompt in wanted)
    ]


def create_default_detector(**kwargs: Any) -> VisionDetector:
    """Construct the YOLO provider without importing/loading it until used."""

    from .providers.ultralytics_provider import UltralyticsVisionDetector

    return UltralyticsVisionDetector(**kwargs)


# Service code sometimes uses this semantic spelling.
DetectorUnavailableError = VisionDependencyError


__all__ = [
    "DetectorUnavailableError",
    "UnavailableVisionDetector",
    "VisionDetector",
    "create_default_detector",
    "filter_detections_by_prompts",
]
