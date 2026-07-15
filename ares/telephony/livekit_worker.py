"""LiveKit real-time voice assistant worker for Ares.

Connects to a LiveKit room, listens for user speech via STT,
processes it through the Ares agent, and speaks the response via TTS.

Usage:
    # Set environment variables first:
    #   LIVEKIT_URL=wss://your-project.livekit.cloud
    #   LIVEKIT_API_KEY=API...
    #   LIVEKIT_API_SECRET=secret...

    python -m ares.telephony.livekit_worker dev

    # Join a browser room in another terminal:
    ares-livekit-room --room my-room --identity your-name
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit.agents import llm as livekit_llm
from livekit.plugins import openai as livekit_openai

from ares.telephony.livekit_plugins import EdgeTTSPlugin, SarvamSTT, SarvamTTSPlugin, WhisperSTT

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "en-US-JennyNeural"
SYSTEM_PROMPT = (
    "You are Ares, a helpful and friendly AI voice assistant. "
    "Keep your responses concise and natural for voice interaction. "
    "Avoid using markdown, bullet points, or long lists. "
    "Speak in complete sentences. Be conversational and warm."
)
# LiveKit's default job executor is thread-based. This import must happen while
# the worker module is loaded on the process main thread; importing it from an
# entrypoint or prewarm callback can be too late and raises "Plugins must be
# registered on the main thread" in LiveKit Agents 1.6.
_OPENAI_COMPAT_PLUGIN: Any = livekit_openai


def _load_config() -> Any:
    """Load Ares configuration."""
    from ares.config import load_config

    return load_config()


def _configure_llm(config: Any) -> livekit_llm.LLM:
    """Create a generic OpenAI-compatible LLM from the configured Ares provider."""
    api_key = config.api_key or os.environ.get("ARES_API_KEY", "")
    base_url = config.api_base_url or os.environ.get("ARES_API_BASE_URL", "")
    model = config.model or "gpt-4o-mini"

    if not api_key:
        raise RuntimeError("Ares model API key is required for the LiveKit voice worker. Configure api_key locally.")
    if _OPENAI_COMPAT_PLUGIN is None:
        raise RuntimeError("LiveKit LLM plugin was not prewarmed before the room job started.")

    return _OPENAI_COMPAT_PLUGIN.LLM(
        model=model,
        api_key=api_key,
        base_url=base_url if base_url else None,
    )


def _configure_stt(config: Any) -> WhisperSTT | SarvamSTT:
    """Create STT from Ares voice config, preferring Sarvam when configured."""
    from ares.voice.sarvam import sarvam_api_key

    voice_config = getattr(config, "voice", None)
    backend = str(getattr(voice_config, "stt_backend", "auto") or "auto").strip().lower()
    if backend not in {"auto", "whisper", "sarvam"}:
        raise RuntimeError("voice.stt_backend must be auto, whisper, or sarvam for the LiveKit worker.")

    api_key = sarvam_api_key()
    if backend == "sarvam" and not api_key:
        raise RuntimeError("SARVAM_API_KEY is required when voice.stt_backend is set to sarvam.")
    if backend == "sarvam" or (backend == "auto" and api_key):
        return SarvamSTT(
            api_key=api_key,
            model=getattr(voice_config, "sarvam_stt_model", "saaras:v3") if voice_config else "saaras:v3",
            language_code=getattr(voice_config, "sarvam_language_code", "en-IN") if voice_config else "en-IN",
        )

    model_name = getattr(voice_config, "stt_model", "small") if voice_config else "small"
    language = getattr(voice_config, "stt_language", "") if voice_config else ""
    return WhisperSTT(model_name=model_name, language=language)


def _configure_tts(config: Any) -> EdgeTTSPlugin | SarvamTTSPlugin:
    """Create TTS from Ares voice config, preferring Sarvam when configured."""
    from ares.voice.sarvam import sarvam_api_key

    voice_config = getattr(config, "voice", None)
    backend = str(getattr(voice_config, "tts_backend", "auto") or "auto").strip().lower()
    if backend not in {"auto", "edge", "sarvam"}:
        raise RuntimeError("voice.tts_backend must be auto, edge, or sarvam for the LiveKit worker.")

    api_key = sarvam_api_key()
    if backend == "sarvam" and not api_key:
        raise RuntimeError("SARVAM_API_KEY is required when voice.tts_backend is set to sarvam.")
    if backend == "sarvam" or (backend == "auto" and api_key):
        return SarvamTTSPlugin(
            api_key=api_key,
            speaker=getattr(voice_config, "sarvam_speaker", "shubh") if voice_config else "shubh",
            model=getattr(voice_config, "sarvam_tts_model", "bulbul:v3") if voice_config else "bulbul:v3",
            language_code=getattr(voice_config, "sarvam_language_code", "en-IN") if voice_config else "en-IN",
            pace=getattr(voice_config, "sarvam_pace", 1.0) if voice_config else 1.0,
            sample_rate=getattr(voice_config, "tts_sample_rate", 24000) if voice_config else 24000,
        )

    voice = getattr(voice_config, "tts_voice", DEFAULT_VOICE) if voice_config else DEFAULT_VOICE
    return EdgeTTSPlugin(voice=voice or DEFAULT_VOICE)


class AresLiveKitAgent(Agent):
    """LiveKit agent that uses Ares for voice assistance."""

    def __init__(
        self,
        config: Any,
        *,
        llm: livekit_llm.LLM | None = None,
        stt: WhisperSTT | None = None,
        tts: EdgeTTSPlugin | None = None,
    ) -> None:
        llm = llm or _configure_llm(config)
        stt = stt or _configure_stt(config)
        tts = tts or _configure_tts(config)

        super().__init__(
            instructions=SYSTEM_PROMPT,
            stt=stt,
            llm=llm,
            tts=tts,
        )
        self._config = config

    async def on_enter(self) -> None:
        """Called when a user joins the room."""
        self.session.generate_reply(
            user_input="Greet the user briefly. Introduce yourself as Ares and ask how you can help them today."
        )


async def _entrypoint(ctx: JobContext) -> None:
    """LiveKit worker entrypoint — called for each room session."""
    logger.info("Connecting to room: %s", ctx.room.name)
    await ctx.connect()

    config = _load_config()
    llm = _configure_llm(config)
    stt = _configure_stt(config)
    tts = _configure_tts(config)

    agent = AresLiveKitAgent(config, llm=llm, stt=stt, tts=tts)

    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
    )
    await session.start(agent=agent, room=ctx.room)

    logger.info("Agent started in room: %s", ctx.room.name)

    # Keep the session alive
    await asyncio.Event().wait()


def _build_worker_options() -> WorkerOptions:
    """Build WorkerOptions from Ares config + env vars."""
    config = _load_config()

    livekit_url = (
        getattr(config.telephony, "livekit_url", "")
        or os.environ.get("LIVEKIT_URL", "")
    )
    api_key = (
        getattr(config.telephony, "livekit_api_key", "")
        or os.environ.get("LIVEKIT_API_KEY", "")
    )
    api_secret = (
        getattr(config.telephony, "livekit_api_secret", "")
        or os.environ.get("LIVEKIT_API_SECRET", "")
    )

    if not livekit_url:
        print("Error: LIVEKIT_URL is not set.", file=sys.stderr)
        print("Set it in your environment or in telephony.livekit_url in Ares config.", file=sys.stderr)
        sys.exit(1)

    if not api_key or not api_secret:
        print("Error: LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required.", file=sys.stderr)
        print("Set them in your environment or in telephony config.", file=sys.stderr)
        sys.exit(1)

    print("Starting Ares LiveKit assistant...")
    print(f"  URL:    {livekit_url}")
    print(f"  Model:  {config.model}")
    print()

    return WorkerOptions(
        entrypoint_fnc=_entrypoint,
        ws_url=livekit_url,
        api_key=api_key,
        api_secret=api_secret,
    )


def main() -> None:
    """Entrypoint for the ``ares-livekit`` command and module execution."""
    # The LiveKit CLI needs an explicit execution mode. ``ares-livekit`` with
    # no arguments should be the fast local-dev worker, while callers can still
    # use any native LiveKit mode (``dev``, ``start``, or ``connect``).
    if len(sys.argv) == 1:
        sys.argv.append("dev")
    options = _build_worker_options()
    cli.run_app(options)


if __name__ == "__main__":
    main()
