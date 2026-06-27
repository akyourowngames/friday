"""Audio playback helpers for voice responses."""

from __future__ import annotations

import asyncio
import io
import tempfile
from pathlib import Path


def _play_with_sounddevice(audio: bytes) -> None:
    import sounddevice as sd
    import soundfile as sf

    data, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
    sd.play(data, sample_rate)
    sd.wait()


def _play_with_pydub(audio: bytes) -> None:
    from pydub import AudioSegment
    from pydub.playback import play

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(audio)
        tmp_path = Path(tmp.name)
    try:
        play(AudioSegment.from_file(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)


async def play_audio_bytes(audio: bytes) -> None:
    """Play encoded audio bytes without blocking the event loop."""
    if not audio:
        return
    try:
        await asyncio.to_thread(_play_with_sounddevice, audio)
    except Exception:
        await asyncio.to_thread(_play_with_pydub, audio)
