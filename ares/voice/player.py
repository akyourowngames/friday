"""Audio playback helpers for voice responses."""

from __future__ import annotations

import asyncio
import io
import tempfile
from pathlib import Path


def _play_with_sounddevice(audio: bytes, speed: float = 1.0) -> None:
    import sounddevice as sd
    import soundfile as sf

    data, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
    # Speed up by playing at a higher effective sample rate
    sd.play(data, int(sample_rate * speed))
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


async def play_audio_bytes(audio: bytes, speed: float = 1.0) -> None:
    """Play encoded audio bytes without blocking the event loop.

    Args:
        audio: Encoded audio bytes (MP3, WAV, etc.)
        speed: Playback speed multiplier (1.0 = normal, 1.2 = 20% faster)
    """
    if not audio:
        return
    try:
        await asyncio.to_thread(_play_with_sounddevice, audio, speed)
    except Exception:
        await asyncio.to_thread(_play_with_pydub, audio)
