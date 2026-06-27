"""Continuous voice mode entry point.

This module validates LiveKit configuration and starts the LiveKit worker when the
optional dependency is installed. The detailed LLM/STT/TTS pipeline is kept small
here so normal Ares installs remain text-first and lightweight.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.panel import Panel

from ares.config import load_config
from ares.voice.tts import voice_config_from_env


def _missing_livekit_message() -> str:
    return (
        "Install optional LiveKit voice dependencies first:\n"
        "pip install \"ares[livekit]\"\n"
        "or install the packages directly: livekit-agents livekit-plugins-openai"
    )


async def _entrypoint(ctx) -> None:
    """LiveKit worker job entrypoint for one room/session."""
    from livekit.agents import Agent, AgentSession
    from livekit.plugins import openai

    app_config = load_config()
    voice_config = voice_config_from_env(app_config.voice)

    await ctx.connect()

    session = AgentSession(
        stt=openai.STT(
            model=os.environ.get("ARES_LIVEKIT_STT_MODEL", "gpt-4o-mini-transcribe"),
            api_key=app_config.api_key,
            base_url=app_config.api_base_url,
            language=os.environ.get("ARES_LIVEKIT_STT_LANGUAGE", "en"),
        ),
        llm=openai.LLM(
            model=app_config.model,
            api_key=app_config.api_key,
            base_url=app_config.api_base_url,
        ),
        tts=openai.TTS(
            model=os.environ.get("ARES_LIVEKIT_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=voice_config.tts_voice or os.environ.get("ARES_LIVEKIT_TTS_VOICE", "ash"),
            api_key=app_config.api_key,
            base_url=app_config.api_base_url,
        ),
    )
    agent = Agent(
        instructions=(
            "You are Ares, the user's concise terminal AI assistant. "
            "Answer conversationally for a realtime voice session."
        )
    )
    await session.start(agent=agent, room=ctx.room)


async def run_voice_agent() -> None:
    """Run Ares as a LiveKit Agents worker for continuous voice rooms."""
    console = Console()
    config = voice_config_from_env(load_config().voice)
    url = config.livekit_url or os.environ.get("LIVEKIT_URL", "")
    api_key = config.livekit_api_key or os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = config.livekit_api_secret or os.environ.get("LIVEKIT_API_SECRET", "")
    if not (url and api_key and api_secret):
        console.print(Panel(
            "Continuous voice mode requires LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET.\n"
            "Start a LiveKit server (for example: livekit-server --dev), set the variables, then run:\n"
            "python -m ares --voice",
            title="Ares Voice",
            border_style="yellow",
        ))
        return

    try:
        from livekit.agents import WorkerOptions, cli
        import livekit.plugins.openai  # noqa: F401
    except ImportError:
        console.print(Panel(_missing_livekit_message(), title="Missing LiveKit dependencies", border_style="red"))
        return

    console.print(Panel(
        f"Starting LiveKit worker for {url}.\n"
        "Join a LiveKit room from a client to talk to Ares continuously.",
        title="Ares Continuous Voice",
        border_style="green",
    ))

    original_argv = sys.argv[:]
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--voice":
            sys.argv = [sys.argv[0], "dev"]
        cli.run_app(WorkerOptions(
            entrypoint_fnc=_entrypoint,
            ws_url=url,
            api_key=api_key,
            api_secret=api_secret,
        ))
    finally:
        sys.argv = original_argv
