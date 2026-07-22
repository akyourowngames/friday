"""Dedicated streaming wake-word detection using openWakeWord and ONNX."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np


_FRAME_SAMPLES = 1280  # 80 ms at 16 kHz, openWakeWord's recommended frame size
_MODEL_FILES = (
    "melspectrogram.onnx",
    "embedding_model.onnx",
    "hey_jarvis_v0.1.onnx",
)


class OpenWakeWordDetector:
    """Low-latency local detector for the official ``hey_jarvis`` model."""

    def __init__(
        self,
        *,
        threshold: float = 0.30,
        model_directory: str | Path = "~/.ares/models/openwakeword",
        cooldown_seconds: float = 1.5,
    ) -> None:
        self.threshold = max(0.05, min(float(threshold), 0.95))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.model_directory = Path(model_directory).expanduser()
        self._audio_buffer = np.array([], dtype=np.float32)
        self._last_activation = 0.0
        self.last_score = 0.0
        self._model = self._load_model()

    def _load_model(self) -> Any:
        try:
            import openwakeword
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError(
                "Desktop wake words require openwakeword. Install Ares with the desktop and voice extras."
            ) from exc

        self.model_directory.mkdir(parents=True, exist_ok=True)
        required = [self.model_directory / filename for filename in _MODEL_FILES]
        if not all(path.exists() for path in required):
            openwakeword.utils.download_models(
                ["hey_jarvis"], target_directory=str(self.model_directory)
            )
        missing = [path.name for path in required if not path.exists()]
        if missing:
            raise RuntimeError(
                "openWakeWord model download is incomplete: " + ", ".join(missing)
            )

        return Model(
            wakeword_models=[str(self.model_directory / "hey_jarvis_v0.1.onnx")],
            inference_framework="onnx",
            melspec_model_path=str(self.model_directory / "melspectrogram.onnx"),
            embedding_model_path=str(self.model_directory / "embedding_model.onnx"),
        )

    def process(self, frame: np.ndarray) -> bool:
        """Consume float32 16 kHz mono audio and report an activation."""
        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        if samples.size:
            self._audio_buffer = np.concatenate((self._audio_buffer, samples))

        activated = False
        while self._audio_buffer.size >= _FRAME_SAMPLES:
            chunk = self._audio_buffer[:_FRAME_SAMPLES]
            self._audio_buffer = self._audio_buffer[_FRAME_SAMPLES:]
            pcm16 = np.clip(chunk, -1.0, 1.0)
            pcm16 = (pcm16 * 32767.0).astype(np.int16)
            predictions = self._model.predict(pcm16)
            score = max((float(value) for value in predictions.values()), default=0.0)
            self.last_score = score
            now = time.monotonic()
            if (
                score >= self.threshold
                and now - self._last_activation >= self.cooldown_seconds
            ):
                self._last_activation = now
                activated = True
        return activated

    def reset(self) -> None:
        self._audio_buffer = np.array([], dtype=np.float32)
        self.last_score = 0.0
        self._model.reset()

