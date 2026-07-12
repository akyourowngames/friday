"""Ares voice-agent bridge used by a LiveKit worker or transcript gateway."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ares.telephony.prompts import TELEPHONY_SYSTEM_PROMPT


class AresVoiceAgent:
    """Turn call transcripts into concise Ares responses with normal tools.

    A deployed LiveKit worker feeds STT text into ``respond`` and streams the
    returned response into its TTS/audio track.  The same method is useful for
    a Twilio Media Stream gateway and for deterministic integration tests.
    """

    def __init__(self, agent: Any | None = None, *, maximum_sentences: int = 4) -> None:
        self.agent = agent
        self.maximum_sentences = maximum_sentences
        self._interruptions: set[str] = set()

    def interrupt(self, call_id: str) -> None:
        self._interruptions.add(call_id)

    async def respond(self, call_id: str, transcript: str, history: list[dict[str, str]] | None = None) -> str:
        if call_id in self._interruptions:
            self._interruptions.discard(call_id)
            return ""
        if self.agent is None:
            return "I can help with that. What would you like to do next?"
        call_history = list(history or [])[-10:]
        prompt = f"{TELEPHONY_SYSTEM_PROMPT}\n\nCaller said: {transcript.strip()}"
        chunks: list[str] = []
        async for token in self.agent.run_stream(prompt, call_history):
            if call_id in self._interruptions:
                self._interruptions.discard(call_id)
                return ""
            if not token.startswith("[tool"):
                chunks.append(token)
        return self._voice_safe("".join(chunks))

    def _voice_safe(self, value: str) -> str:
        plain = re.sub(r"[`*_#>]", "", str(value or "")).strip()
        sentences = re.split(r"(?<=[.!?])\s+", plain)
        return " ".join(sentence for sentence in sentences[: self.maximum_sentences] if sentence).strip()


def livekit_worker_available() -> bool:
    try:
        import livekit.agents  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def livekit_credential_diagnostic(config: Any) -> dict[str, bool | str]:
    """Verify that local LiveKit credentials can sign a room token.

    Signing is deliberately local: this checks the configured API-key/secret
    pairing without contacting LiveKit, creating a room, or exposing a JWT.
    """
    telephony = getattr(config, "telephony", config)
    url = str(getattr(telephony, "livekit_url", "") or "")
    api_key = str(getattr(telephony, "livekit_api_key", "") or "")
    api_secret = str(getattr(telephony, "livekit_api_secret", "") or "")
    if not (url and api_key and api_secret):
        return {"configured": False, "signed_token": False, "error": "LiveKit URL, API key, and API secret are required."}
    try:
        from livekit import api

        token = (
            api.AccessToken(api_key, api_secret)
            .with_identity("ares-local-diagnostic")
            .with_grants(api.VideoGrants(room_join=True, room="ares-diagnostic"))
            .to_jwt()
        )
    except Exception as exc:
        return {"configured": True, "signed_token": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"configured": True, "signed_token": bool(token), "error": ""}


async def run_livekit_worker(*_args: Any, **_kwargs: Any) -> None:
    """Entry point reserved for a configured LiveKit Agents deployment.

    The core application intentionally does not start a cloud media worker
    until ``livekit-agents`` is installed and the user supplies LiveKit
    credentials.  This avoids a hidden network dependency in normal CLI use.
    """
    if not livekit_worker_available():
        raise RuntimeError("LiveKit worker support requires installing the livekit-agents package.")
    await asyncio.Future()
