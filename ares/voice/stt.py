"""Local speech-to-text helpers built around faster-whisper."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

import numpy as np


class STTEngine:
    """Small async wrapper around faster-whisper."""

    def __init__(self, model_name: str = "tiny", compute_type: str = "int8") -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type)
        return self._model

    def transcribe_file(self, path: str | Path) -> str:
        model = self._ensure_model()
        segments, _info = model.transcribe(str(path), vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()

    def transcribe_pcm16(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            sf.write(tmp_path, samples, sample_rate)
            return self.transcribe_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)


def trim_silence_pcm16(samples: np.ndarray, sample_rate: int = 16000, aggressiveness: int = 2) -> np.ndarray:
    """Trim leading/trailing silence using WebRTC VAD when available."""
    if samples.size == 0:
        return samples
    try:
        import webrtcvad
    except ImportError:
        return samples

    vad = webrtcvad.Vad(aggressiveness)
    frame_ms = 30
    frame_len = int(sample_rate * frame_ms / 1000)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767).astype(np.int16)
    voiced: list[int] = []
    for start in range(0, len(pcm16) - frame_len + 1, frame_len):
        frame = pcm16[start : start + frame_len]
        if vad.is_speech(frame.tobytes(), sample_rate):
            voiced.append(start)
    if not voiced:
        return samples
    first = max(0, voiced[0] - frame_len)
    last = min(len(samples), voiced[-1] + (2 * frame_len))
    return samples[first:last]
