"""TTS provider interface and implementations for Edge TTS and Sarvam AI."""

from __future__ import annotations

import base64
import inspect
import os
from abc import ABC, abstractmethod
from typing import Any, Literal

from ares.models import VoiceConfig

ProviderName = Literal["edge_tts", "edge", "sarvam"]


class TTSProvider(ABC):
    """Abstract TTS provider. Implementations return encoded audio bytes."""

    @abstractmethod
    async def speak(self, text: str, voice: str = "") -> bytes:
        """Return audio bytes for *text*."""

    @abstractmethod
    async def list_voices(self) -> list[dict[str, Any]]:
        """Return available voices with name, gender, and language info."""

    async def close(self) -> None:
        """Release any provider resources."""


class EdgeTTS(TTSProvider):
    """Microsoft Edge online TTS via the ``edge-tts`` package."""

    def __init__(self, voice: str = "en-US-JennyNeural") -> None:
        self.default_voice = voice

    async def speak(self, text: str, voice: str = "") -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice or self.default_voice)
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                audio.extend(chunk.get("data", b""))
        return bytes(audio)

    async def list_voices(self) -> list[dict[str, Any]]:
        import edge_tts

        voices = edge_tts.list_voices()
        if inspect.isawaitable(voices):
            voices = await voices
        return list(voices)


class SarvamTTS(TTSProvider):
    """Sarvam AI TTS. Requires ``SARVAM_API_KEY`` or config voice key."""

    def __init__(
        self,
        api_key: str,
        voice: str = "anushka",
        model: str = "bulbul:v2",
        language_code: str = "hi-IN",
    ) -> None:
        if not api_key:
            raise ValueError("Sarvam TTS requires SARVAM_API_KEY or voice.sarvam_api_key")
        self.api_key = api_key
        self.default_voice = voice
        self.model = model
        self.language_code = language_code

    async def speak(self, text: str, voice: str = "") -> bytes:
        import httpx

        payload = {
            "text": text,
            "target_language_code": self.language_code,
            "speaker": voice or self.default_voice,
            "model": self.model,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.sarvam.ai/text-to-speech",
                headers={"api-subscription-key": self.api_key, "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        audio = data.get("audios", [None])[0] or data.get("audio")
        if not audio:
            raise RuntimeError("Sarvam TTS response did not include audio")
        if isinstance(audio, str):
            return base64.b64decode(audio)
        return bytes(audio)

    async def list_voices(self) -> list[dict[str, Any]]:
        return [
            {"name": "anushka", "gender": "female", "language": "hi-IN"},
            {"name": "abhilash", "gender": "male", "language": "hi-IN"},
            {"name": "manisha", "gender": "female", "language": "hi-IN"},
            {"name": "vidya", "gender": "female", "language": "ta-IN"},
            {"name": "arya", "gender": "female", "language": "en-IN"},
        ]


def voice_config_from_env(config: VoiceConfig) -> VoiceConfig:
    """Return a copy of voice config with environment variable overrides applied."""
    updates: dict[str, Any] = {}
    mapping = {
        "ARES_VOICE_ENABLED": ("enabled", lambda v: v.lower() in {"1", "true", "yes", "on"}),
        "ARES_TTS_PROVIDER": ("tts_provider", str),
        "ARES_TTS_VOICE": ("tts_voice", str),
        "ARES_STT_MODEL": ("stt_model", str),
        "ARES_VOICE_HOTKEY": ("hotkey", str),
        "SARVAM_API_KEY": ("sarvam_api_key", str),
        "SARVAM_TTS_MODEL": ("sarvam_tts_model", str),
        "SARVAM_LANGUAGE_CODE": ("sarvam_language_code", str),
        "LIVEKIT_URL": ("livekit_url", str),
        "LIVEKIT_API_KEY": ("livekit_api_key", str),
        "LIVEKIT_API_SECRET": ("livekit_api_secret", str),
    }
    for env_name, (field, caster) in mapping.items():
        value = os.environ.get(env_name)
        if value is not None:
            updates[field] = caster(value)
    return config.model_copy(update=updates)


def create_tts_provider(config: VoiceConfig) -> TTSProvider:
    """Create the configured TTS provider, honoring environment overrides."""
    resolved = voice_config_from_env(config)
    provider = resolved.tts_provider.lower().replace("-", "_")
    if provider in {"edge", "edge_tts"}:
        return EdgeTTS(voice=resolved.tts_voice or "en-US-JennyNeural")
    if provider == "sarvam":
        return SarvamTTS(
            api_key=resolved.sarvam_api_key,
            voice=resolved.tts_voice or "anushka",
            model=resolved.sarvam_tts_model,
            language_code=resolved.sarvam_language_code,
        )
    raise ValueError(f"Unsupported TTS provider: {resolved.tts_provider}")
