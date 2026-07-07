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


def audio_bytes_to_pcm16(audio: bytes, sample_rate: int = 24000, speed: float = 1.0) -> bytes:
    """Decode encoded audio (MP3/WAV/etc.) to mono PCM16 bytes.

    Args:
        audio: Encoded audio bytes (MP3, WAV, etc.)
        sample_rate: Target sample rate for output PCM
        speed: Playback speed multiplier (1.0 = normal, 1.2 = 20% faster)
    """
    import numpy as np

    try:
        import soundfile as sf

        data, source_rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
    except Exception:
        from pydub import AudioSegment

        segment = AudioSegment.from_file(io.BytesIO(audio))
        segment = segment.set_channels(1).set_frame_rate(sample_rate).set_sample_width(2)
        return segment.raw_data

    if int(source_rate) != int(sample_rate) and len(data):
        old = np.arange(len(data))
        new_len = max(1, int(len(data) * sample_rate / source_rate))
        new = np.linspace(0, len(data) - 1, new_len)
        data = np.interp(new, old, data).astype(np.float32)

    # Apply speed by resampling (skip/stretch samples)
    if speed != 1.0 and len(data):
        indices = np.arange(0, len(data), speed).astype(int)
        indices = indices[indices < len(data)]
        data = data[indices]

    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    return pcm.tobytes()


async def play_audio_stream(
    audio_queue: asyncio.Queue[bytes | None],
    stop_event: asyncio.Event,
    sample_rate: int = 24000,
) -> None:
    """Play PCM16 audio chunks from a queue with immediate cancellation support.

    Uses stream.write() which blocks until the audio device consumes the data,
    guaranteeing all audio plays before returning. stop_event interrupts by
    closing the stream from outside.
    """
    import numpy as np
    import sounddevice as sd

    stream = sd.RawOutputStream(
        samplerate=int(sample_rate),
        channels=1,
        dtype="int16",
        blocksize=0,
    )

    try:
        stream.start()
        while not stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue

            if chunk is None:
                break

            # write() blocks until the device consumes the data — no desync
            try:
                stream.write(chunk)
            except Exception:
                break
    finally:
        try:
            stream.stop()
        finally:
            stream.close()
        # Drain any remaining items so the queue doesn't leak
        while not audio_queue.empty():
            try:
                audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
