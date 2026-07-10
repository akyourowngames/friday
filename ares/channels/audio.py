"""Reliable speech-to-English transcription for remote Ares channels."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ares.models import TelegramConfig


class AudioTranscriptionError(RuntimeError):
    """A user-safe failure while turning a remote recording into text."""


@dataclass(frozen=True)
class EnglishTranscript:
    """The English representation of a voice note and the backend that made it."""

    text: str
    backend: str
    detected_language: str = ""


class EnglishAudioTranscriber:
    """Transcribe Hindi, English, and Hinglish voice notes into English text.

    Sarvam is preferred only when a key is explicitly configured because its
    current Saaras model is tuned for Indian language code-switching. Local
    multilingual faster-whisper remains the offline, no-account fallback.
    """

    def __init__(self, config_provider: Callable[[], TelegramConfig]) -> None:
        self._config_provider = config_provider
        self._whisper: WhisperTranscriber | None = None
        self._whisper_model_name = ""
        self._lock = asyncio.Lock()

    async def transcribe_to_english(
        self,
        path: str | Path,
        *,
        duration_seconds: int = 0,
    ) -> EnglishTranscript:
        config = self._config_provider()
        if not config.audio_transcription_enabled:
            raise AudioTranscriptionError("Audio transcription is disabled in Telegram settings.")
        if duration_seconds and duration_seconds > config.max_audio_duration_seconds:
            raise AudioTranscriptionError(
                f"That recording is longer than the configured {config.max_audio_duration_seconds // 60} minute limit."
            )

        backend = (config.audio_stt_backend or "auto").strip().lower()
        if backend not in {"auto", "sarvam", "whisper"}:
            raise AudioTranscriptionError("Telegram audio backend must be auto, sarvam, or whisper.")

        # Sarvam REST is designed for short interactive files. For a longer
        # recording, automatic mode stays reliable by choosing local Whisper
        # rather than forcing a request past the provider's synchronous limit.
        prefer_sarvam = backend == "sarvam" or (
            backend == "auto" and bool(os.environ.get("SARVAM_API_KEY")) and duration_seconds <= 30
        )
        if prefer_sarvam:
            try:
                return await self._transcribe_with_sarvam(path)
            except Exception as exc:
                if backend == "sarvam":
                    raise AudioTranscriptionError(f"Sarvam transcription failed: {exc}") from exc
                # Auto mode is intentionally fault tolerant: a service outage,
                # bad key, or unsupported provider response falls back locally.

        try:
            return await self._transcribe_with_whisper(path, config.audio_stt_model)
        except ModuleNotFoundError as exc:
            raise AudioTranscriptionError(
                "Local voice transcription is unavailable. Install Ares with the voice extra or configure SARVAM_API_KEY."
            ) from exc
        except Exception as exc:
            raise AudioTranscriptionError(f"Local voice transcription failed: {exc}") from exc

    async def _transcribe_with_sarvam(self, path: str | Path) -> EnglishTranscript:
        from ares.voice.sarvam import SarvamTranscriber
        from ares.voice.stt import clean_transcript

        transcriber = SarvamTranscriber(model="saaras:v3", language_code="unknown")
        result = await transcriber.transcribe_file_async(
            path,
            mode="translate",
            language_code="unknown",
        )
        text = clean_transcript(result.text)
        if not text:
            raise AudioTranscriptionError("No understandable speech was found in that recording.")
        return EnglishTranscript(text=text, backend="sarvam", detected_language=result.language_code)

    async def _transcribe_with_whisper(self, path: str | Path, model_name: str) -> EnglishTranscript:
        from ares.voice.stt import WhisperTranscriber, clean_transcript

        async with self._lock:
            if self._whisper is None or self._whisper_model_name != model_name:
                self._whisper = WhisperTranscriber(model_name or "small")
                self._whisper_model_name = model_name or "small"
            text = await asyncio.to_thread(
                self._whisper.transcribe_file,
                path,
                task="translate",
                multilingual=True,
            )
        text = clean_transcript(text)
        if not text:
            raise AudioTranscriptionError("No understandable speech was found in that recording.")
        return EnglishTranscript(text=text, backend="whisper")
