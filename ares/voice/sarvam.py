"""Sarvam AI STT/TTS adapters for Ares voice mode."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import numpy as np

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
DEFAULT_SARVAM_STT_MODEL = "saaras:v3"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3"
DEFAULT_SARVAM_SPEAKER = "shubh"
DEFAULT_SARVAM_LANGUAGE = "en-IN"
DEFAULT_SARVAM_SAMPLE_RATE = 24000
DEFAULT_SARVAM_PACE = 1.0


@dataclass(frozen=True)
class SarvamTranscript:
    text: str
    language_code: str = ""


def sarvam_api_key() -> str:
    """Return the Sarvam API key from the environment."""
    return os.environ.get("SARVAM_API_KEY", "")


class SarvamTranscriber:
    """Speech-to-text using Sarvam Saaras."""

    def __init__(
        self,
        api_key: str = "",
        *,
        model: str = DEFAULT_SARVAM_STT_MODEL,
        language_code: str = DEFAULT_SARVAM_LANGUAGE,
    ) -> None:
        self.api_key = api_key or sarvam_api_key()
        if not self.api_key:
            raise ValueError("Sarvam STT requires SARVAM_API_KEY")
        self.model = model or DEFAULT_SARVAM_STT_MODEL
        self.language_code = language_code or DEFAULT_SARVAM_LANGUAGE

    def transcribe_samples(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe mono float32 samples through Sarvam REST STT."""
        return asyncio.run(self._transcribe_samples_async(samples, sample_rate))

    async def _transcribe_samples_async(self, samples: np.ndarray, sample_rate: int) -> str:
        import httpx
        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            sf.write(tmp_path, np.asarray(samples, dtype=np.float32), sample_rate)
            audio = tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)

        result = await self._transcribe_audio_async(
            "speech.wav",
            audio,
            "audio/wav",
            mode="transcribe",
            language_code=self.language_code,
        )
        return result.text

    def transcribe_file(self, path: str | Path, *, mode: str = "transcribe", language_code: str = "") -> str:
        """Synchronously transcribe an audio file for non-async callers."""
        return asyncio.run(self.transcribe_file_async(path, mode=mode, language_code=language_code)).text

    async def transcribe_file_async(
        self,
        path: str | Path,
        *,
        mode: str = "transcribe",
        language_code: str = "",
    ) -> SarvamTranscript:
        """Transcribe supported audio files (including Telegram OGG/Opus)."""
        file_path = Path(path)
        media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        return await self._transcribe_audio_async(
            file_path.name,
            file_path.read_bytes(),
            media_type,
            mode=mode,
            language_code=language_code or self.language_code,
        )

    async def _transcribe_audio_async(
        self,
        filename: str,
        audio: bytes,
        media_type: str,
        *,
        mode: str,
        language_code: str,
    ) -> SarvamTranscript:
        import httpx

        files = {"file": (filename, audio, media_type)}
        data = {
            "model": self.model,
            "mode": mode,
            "language_code": language_code,
        }
        headers = {"api-subscription-key": self.api_key}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(SARVAM_STT_URL, headers=headers, data=data, files=files)
            if response.is_error:
                raise RuntimeError(f"Sarvam STT HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
        text = (
            payload.get("transcript")
            or payload.get("text")
            or payload.get("transcription")
            or ""
        ).strip()
        return SarvamTranscript(text=text, language_code=str(payload.get("language_code") or ""))

    def transcribe_pcm16(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """Compatibility alias used by older callers."""
        return self.transcribe_samples(samples, sample_rate)


class SarvamTTS:
    """Text-to-speech using Sarvam Bulbul."""

    audio_format = "encoded"

    def __init__(
        self,
        api_key: str = "",
        *,
        speaker: str = DEFAULT_SARVAM_SPEAKER,
        model: str = DEFAULT_SARVAM_TTS_MODEL,
        language_code: str = DEFAULT_SARVAM_LANGUAGE,
        sample_rate: int = DEFAULT_SARVAM_SAMPLE_RATE,
        pace: float = DEFAULT_SARVAM_PACE,
    ) -> None:
        self.api_key = api_key or sarvam_api_key()
        if not self.api_key:
            raise ValueError("Sarvam TTS requires SARVAM_API_KEY")
        self.default_voice = speaker or DEFAULT_SARVAM_SPEAKER
        self.model = model or DEFAULT_SARVAM_TTS_MODEL
        self.language_code = language_code or DEFAULT_SARVAM_LANGUAGE
        self.sample_rate = int(sample_rate or DEFAULT_SARVAM_SAMPLE_RATE)
        self.pace = float(pace or DEFAULT_SARVAM_PACE)

    async def synthesize(self, text: str, voice: str = "") -> bytes:
        """Return encoded audio bytes for ``text``."""
        import httpx

        payload = {
            "text": text,
            "target_language_code": self.language_code,
            "speaker": voice or self.default_voice,
            "model": self.model,
            "speech_sample_rate": self.sample_rate,
            "pace": self.pace,
            "output_audio_codec": "wav",
        }
        if self.model == "bulbul:v3":
            payload["temperature"] = 0.6
        headers = {
            "api-subscription-key": self.api_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(SARVAM_TTS_URL, headers=headers, json=payload)
            if response.is_error:
                raise RuntimeError(f"Sarvam TTS HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()

        audio = data.get("audios", [None])[0] or data.get("audio")
        if not audio:
            raise RuntimeError("Sarvam TTS response did not include audio")
        if isinstance(audio, str):
            return base64.b64decode(audio)
        return bytes(audio)

    async def stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        audio = await self.synthesize(text, voice)
        if audio:
            yield audio

    async def speak(self, text: str, voice: str = "") -> bytes:
        return await self.synthesize(text, voice)

    async def speak_stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        async for chunk in self.stream(text, voice):
            yield chunk
