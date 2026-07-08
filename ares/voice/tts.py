"""Edge TTS speech synthesis for Ares voice mode."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

DEFAULT_EDGE_VOICE = "en-US-JennyNeural"


class EdgeTTS:
    """Small wrapper around the ``edge-tts`` package."""

    audio_format = "encoded"

    def __init__(self, voice: str = DEFAULT_EDGE_VOICE) -> None:
        self.default_voice = voice or DEFAULT_EDGE_VOICE

    async def synthesize(self, text: str, voice: str = "") -> bytes:
        """Return encoded audio bytes for ``text``."""
        audio = bytearray()
        async for chunk in self.stream(text, voice):
            audio.extend(chunk)
        return bytes(audio)

    async def stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        """Yield encoded audio chunks from Edge TTS."""
        import edge_tts

        text = (text or "").strip()
        if not text:
            return

        communicate = edge_tts.Communicate(text, voice or self.default_voice)
        async for chunk in communicate.stream():
            if chunk.get("type") != "audio":
                continue
            data = chunk.get("data", b"")
            if data:
                yield data

    async def list_voices(self) -> list[dict[str, Any]]:
        """Return voices reported by Edge TTS."""
        import edge_tts

        voices = edge_tts.list_voices()
        if inspect.isawaitable(voices):
            voices = await voices
        return list(voices)

    async def speak(self, text: str, voice: str = "") -> bytes:
        """Backward-compatible alias for ``synthesize``."""
        return await self.synthesize(text, voice)

    async def speak_stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        """Backward-compatible alias for ``stream``."""
        async for chunk in self.stream(text, voice):
            yield chunk
