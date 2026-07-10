"""Local speech-to-text helpers for the rebuilt Ares voice loop."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import numpy as np

DEFAULT_STT_MODEL = "small"
_NOISE_TAG_RE = re.compile(r"^\s*[\[(](?:music|noise|silence|applause|inaudible)[\])]\s*$", re.I)


class WhisperTranscriber:
    """Lazy local Whisper transcription through faster-whisper."""

    def __init__(
        self,
        model_name: str = DEFAULT_STT_MODEL,
        *,
        language: str = "",
        compute_type: str = "int8",
    ) -> None:
        self.model_name = model_name or DEFAULT_STT_MODEL
        self.language = language or None
        self.compute_type = compute_type
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type)
        return self._model

    def transcribe_file(
        self,
        path: str | Path,
        *,
        task: str = "transcribe",
        multilingual: bool = False,
    ) -> str:
        """Transcribe or translate a file and return cleaned text.

        ``multilingual`` enables per-segment language handling in newer
        faster-whisper releases, which matters for Hindi-English code-switching.
        """
        model = self._ensure_model()
        kwargs = {
            "vad_filter": True,
            "task": task,
            "condition_on_previous_text": False,
        }
        if self.language:
            kwargs["language"] = self.language
        if multilingual:
            kwargs["multilingual"] = True
        try:
            segments, _info = model.transcribe(str(path), **kwargs)
        except TypeError as exc:
            # Older compatible faster-whisper installs predate the optional
            # per-segment multilingual flag. Translation still works there;
            # retain it as a graceful compatibility path.
            if not multilingual or "multilingual" not in str(exc):
                raise
            kwargs.pop("multilingual", None)
            segments, _info = model.transcribe(str(path), **kwargs)
        return clean_transcript(" ".join(segment.text.strip() for segment in segments))

    def transcribe_samples(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe mono float32 samples by writing a short WAV temp file."""
        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            sf.write(tmp_path, np.asarray(samples, dtype=np.float32), sample_rate)
            return self.transcribe_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def transcribe_pcm16(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """Backward-compatible alias for ``transcribe_samples``."""
        return self.transcribe_samples(samples, sample_rate)


STTEngine = WhisperTranscriber


def clean_transcript(text: str) -> str:
    """Normalize common transcription artifacts without changing meaning."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if _NOISE_TAG_RE.match(text):
        return ""
    return text


def trim_silence(samples: np.ndarray, sample_rate: int = 16000, aggressiveness: int = 2) -> np.ndarray:
    """Trim leading and trailing silence using WebRTC VAD when available."""
    samples = np.asarray(samples, dtype=np.float32)
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


def trim_silence_pcm16(samples: np.ndarray, sample_rate: int = 16000, aggressiveness: int = 2) -> np.ndarray:
    """Backward-compatible alias for ``trim_silence``."""
    return trim_silence(samples, sample_rate, aggressiveness)
