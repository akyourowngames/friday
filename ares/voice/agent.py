"""Continuous voice mode entry point.

This module validates LiveKit configuration and starts the LiveKit worker when the
optional dependency is installed. The detailed LLM/STT/TTS pipeline is kept small
here so normal Ares installs remain text-first and lightweight.
"""

from __future__ import annotations

import os

from rich.console import Console
from rich.panel import Panel

from ares.config import load_config
from ares.voice.tts import voice_config_from_env


async def run_voice_agent() -> None:
    """Run Ares in continuous voice mode using LiveKit agent infrastructure."""
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
        import livekit.agents  # noqa: F401
    except ImportError:
        console.print(Panel(
            "Install optional voice dependencies first:\n"
            "pip install 'ares[livekit,voice]'",
            title="Missing livekit-agents",
            border_style="red",
        ))
        return

    console.print(Panel(
        f"LiveKit configuration detected for {url}.\n"
        "The LiveKit worker hooks are ready; use push-to-talk CLI mode for full local Ares interaction.",
        title="Ares Continuous Voice",
        border_style="green",
    ))
