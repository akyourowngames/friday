"""Lazy Ultralytics YOLO detector provider.

No Ultralytics, OpenCV, Torch, or model weights are imported/downloaded when
this module is imported.  They are only touched by ``detect``/``warmup`` after
the user has requested visual observation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..models import DetectedObject, VisionFrame
from .base import VisionDependencyError, VisionProviderError, require_optional_dependency


def _to_list(value: Any) -> list[Any]:
    """Convert Torch/NumPy/list tensor-like data to a regular Python list."""

    current = value
    for method in ("detach", "cpu"):
        candidate = getattr(current, method, None)
        if callable(candidate):
            current = candidate()
    convert = getattr(current, "tolist", None)
    if callable(convert):
        current = convert()
    if isinstance(current, tuple):
        return list(current)
    if isinstance(current, list):
        return current
    try:
        return list(current)
    except TypeError:
        return [current]


class UltralyticsVisionDetector:
    """Common-object detector backed by a local Ultralytics YOLO model."""

    def __init__(
        self,
        model_name: str = "yolo26n.pt",
        *,
        confidence_threshold: float = 0.25,
        device: str | int | None = None,
        model: Any | None = None,
    ) -> None:
        if not 0.0 <= float(confidence_threshold) <= 1.0:
            raise ValueError("confidence_threshold must be between zero and one")
        self.model_name = str(model_name).strip() or "yolo26n.pt"
        self.confidence_threshold = float(confidence_threshold)
        self.device = device
        self._model = model
        self._load_lock: asyncio.Lock | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    async def warmup(self) -> None:
        await self._ensure_model()

    async def detect(
        self,
        frame: VisionFrame,
        prompts: list[str] | None = None,
    ) -> list[DetectedObject]:
        if frame.image is None:
            raise ValueError("Object detection requires a VisionFrame with an in-memory image")
        model = await self._ensure_model()
        try:
            objects = await asyncio.to_thread(self._detect_sync, model, frame.image)
        except VisionDependencyError:
            raise
        except Exception as exc:
            raise VisionProviderError(f"Ultralytics detection failed: {exc}") from exc
        return self._filter_prompts(objects, prompts)

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> Any:
        module = require_optional_dependency("ultralytics", package_name="ultralytics")
        constructor = getattr(module, "YOLO", None)
        if constructor is None:
            raise VisionDependencyError("ultralytics", detail="The installed package does not expose YOLO.")
        try:
            return constructor(self.model_name)
        except Exception as exc:
            raise VisionProviderError(
                f"Could not load local YOLO model '{self.model_name}': {exc}"
            ) from exc

    def _detect_sync(self, model: Any, image: Any) -> list[DetectedObject]:
        kwargs: dict[str, Any] = {"conf": self.confidence_threshold, "verbose": False}
        if self.device is not None:
            kwargs["device"] = self.device
        results = model.predict(image, **kwargs) if hasattr(model, "predict") else model(image, **kwargs)
        result_items = _to_list(results)
        detections: list[DetectedObject] = []
        for result in result_items:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            coordinates = _to_list(getattr(boxes, "xyxy", []))
            confidences = _to_list(getattr(boxes, "conf", []))
            classes = _to_list(getattr(boxes, "cls", []))
            names = getattr(result, "names", getattr(model, "names", {}))
            for index, raw_box in enumerate(coordinates):
                if not isinstance(raw_box, (list, tuple)) or len(raw_box) < 4:
                    continue
                confidence = float(confidences[index]) if index < len(confidences) else 0.0
                if confidence < self.confidence_threshold:
                    continue
                class_id = int(float(classes[index])) if index < len(classes) else -1
                if isinstance(names, dict):
                    label = names.get(class_id, str(class_id))
                elif isinstance(names, (list, tuple)) and 0 <= class_id < len(names):
                    label = names[class_id]
                else:
                    label = str(class_id)
                detections.append(
                    DetectedObject(
                        label=str(label),
                        confidence=max(0.0, min(1.0, confidence)),
                        bounding_box=tuple(int(round(float(item))) for item in raw_box[:4]),
                        attributes={"class_id": class_id, "provider": "ultralytics"},
                    )
                )
        return detections

    @staticmethod
    def _filter_prompts(
        objects: list[DetectedObject], prompts: list[str] | None
    ) -> list[DetectedObject]:
        wanted = [str(prompt).strip().casefold() for prompt in (prompts or []) if str(prompt).strip()]
        if not wanted:
            return objects
        return [
            item
            for item in objects
            if any(
                prompt == item.label.casefold()
                or prompt in item.label.casefold()
                or item.label.casefold() in prompt
                for prompt in wanted
            )
        ]


# Familiar aliases make the provider easy to inject in a V1 service while
# preserving a descriptive canonical implementation name.
UltralyticsDetector = UltralyticsVisionDetector
YOLODetector = UltralyticsVisionDetector


__all__ = ["UltralyticsDetector", "UltralyticsVisionDetector", "YOLODetector"]
