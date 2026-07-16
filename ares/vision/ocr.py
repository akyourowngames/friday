"""Lazy OCR boundary for extracting visible text from selected frames."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from .models import VisionFrame
from .providers.base import VisionDependencyError, VisionProviderError, require_optional_dependency


@runtime_checkable
class VisionOCR(Protocol):
    """OCR abstraction used by the service so PaddleOCR remains replaceable."""

    async def read(self, frame: VisionFrame) -> list[str]: ...


class NullOCR:
    """Explicit no-op OCR provider useful when OCR is intentionally disabled."""

    async def read(self, frame: VisionFrame) -> list[str]:
        return []


class PaddleOCRReader:
    """PaddleOCR implementation that imports and initialises only on first use."""

    def __init__(
        self,
        *,
        language: str = "en",
        use_angle_classification: bool = True,
        reader: Any | None = None,
    ) -> None:
        self.language = str(language).strip() or "en"
        self.use_angle_classification = bool(use_angle_classification)
        self._reader = reader
        self._load_lock: asyncio.Lock | None = None

    @property
    def loaded(self) -> bool:
        return self._reader is not None

    async def warmup(self) -> None:
        await self._ensure_reader()

    async def read(self, frame: VisionFrame) -> list[str]:
        """Read non-empty text lines from a transient frame in a worker thread."""

        if frame.image is None:
            raise ValueError("OCR requires a VisionFrame with an in-memory image")
        reader = await self._ensure_reader()
        try:
            result = await asyncio.to_thread(self._read_sync, reader, frame.image)
        except VisionDependencyError:
            raise
        except Exception as exc:
            raise VisionProviderError(f"PaddleOCR could not read the frame: {exc}") from exc
        return _extract_text_lines(result)

    # A descriptive alias that is easier to discover from tool/service code.
    read_text = read

    async def _ensure_reader(self) -> Any:
        if self._reader is not None:
            return self._reader
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        async with self._load_lock:
            if self._reader is None:
                self._reader = await asyncio.to_thread(self._load_reader)
        return self._reader

    def _load_reader(self) -> Any:
        module = require_optional_dependency("paddleocr", package_name="paddleocr")
        constructor = getattr(module, "PaddleOCR", None)
        if constructor is None:
            raise VisionDependencyError("paddleocr", detail="The installed package does not expose PaddleOCR.")
        try:
            return constructor(
                lang=self.language,
                use_angle_cls=self.use_angle_classification,
                show_log=False,
            )
        except TypeError:
            # PaddleOCR 3.x removed/renamed a few constructor switches.  A
            # minimal language-only construction keeps this provider usable
            # across supported local releases.
            return constructor(lang=self.language)

    def _read_sync(self, reader: Any, image: Any) -> Any:
        # PaddleOCR accepts ndarray inputs.  Convert a Pillow image only here,
        # avoiding an import-time NumPy dependency in the vision package.
        payload = image
        if hasattr(image, "convert") and not hasattr(image, "shape"):
            import numpy as np

            payload = np.asarray(image.convert("RGB"))
        if hasattr(reader, "ocr"):
            try:
                return reader.ocr(payload, cls=self.use_angle_classification)
            except TypeError:
                return reader.ocr(payload)
        if hasattr(reader, "predict"):
            return reader.predict(payload)
        raise VisionProviderError("Configured PaddleOCR reader exposes neither ocr() nor predict().")


def _extract_text_lines(result: Any) -> list[str]:
    """Normalise PaddleOCR 2.x/3.x-style results into ordered unique strings."""

    lines: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in lines:
            lines.append(text)

    def walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            add(value)
            return
        if isinstance(value, dict):
            # PaddleOCR 3 predictions tend to expose text_recognition_texts.
            for key in ("text", "texts", "rec_text", "rec_texts", "text_recognition_texts"):
                if key not in value:
                    continue
                candidate = value[key]
                if isinstance(candidate, str):
                    add(candidate)
                elif isinstance(candidate, Iterable):
                    for item in candidate:
                        add(item)
            return
        if isinstance(value, tuple):
            # PaddleOCR 2: (bounding_box, (text, confidence)).  Avoid adding
            # coordinates/confidence as text while still handling simple tuples.
            if len(value) == 2 and isinstance(value[1], (tuple, list)) and value[1]:
                candidate = value[1][0]
                if isinstance(candidate, str):
                    add(candidate)
                    return
            for item in value:
                walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    return lines


def create_default_ocr(**kwargs: Any) -> PaddleOCRReader:
    """Create the default lazy OCR provider without importing PaddleOCR yet."""

    return PaddleOCRReader(**kwargs)


# Public aliases make dependency injection at service boundaries more natural.
PaddleOCRProvider = PaddleOCRReader
OCRUnavailableError = VisionDependencyError


__all__ = [
    "NullOCR",
    "OCRUnavailableError",
    "PaddleOCRProvider",
    "PaddleOCRReader",
    "VisionOCR",
    "create_default_ocr",
]
