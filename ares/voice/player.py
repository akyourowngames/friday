"""Audio playback helpers for voice responses."""

from __future__ import annotations

import asyncio
import io
import tempfile
import threading
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


def audio_bytes_to_pcm16(audio: bytes, sample_rate: int = 24000) -> bytes:
    """Decode encoded audio (MP3/WAV/etc.) to mono PCM16 bytes."""
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
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    return pcm.tobytes()


async def play_audio_stream(
    audio_queue: asyncio.Queue[bytes | None],
    stop_event: asyncio.Event,
    sample_rate: int = 24000,
    speed: float = 1.2,
) -> None:
    """Play PCM16 audio chunks from a queue with immediate cancellation support.

    The queue receives raw little-endian mono PCM16 chunks and a ``None``
    sentinel when synthesis is complete. ``stop_event`` is used by barge-in
    detection to stop playback without waiting for a full clip to finish.
    """
    import numpy as np
    import sounddevice as sd

    ring_buffer = bytearray()
    buffer_lock = threading.Lock()

    def callback(outdata, frames, _time_info, _status) -> None:
        nonlocal ring_buffer
        needed = frames * 2
        with buffer_lock:
            if len(ring_buffer) >= needed:
                data = bytes(ring_buffer[:needed])
                del ring_buffer[:needed]
            else:
                data = bytes(ring_buffer)
                ring_buffer.clear()
                data += b"\x00" * (needed - len(data))

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        outdata[:] = samples.reshape(-1, 1)

    stream = sd.OutputStream(
        samplerate=int(sample_rate * speed),
        channels=1,
        dtype="float32",
        latency="low",
        callback=callback,
    )

    try:
        stream.start()
        while not stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue

            if chunk is None:
                for _ in range(200):
                    with buffer_lock:
                        empty = not ring_buffer
                    if empty or stop_event.is_set():
                        break
                    await asyncio.sleep(0.01)
                break

            with buffer_lock:
                ring_buffer.extend(chunk)
    finally:
        try:
            stream.stop()
        finally:
            stream.close()
        while not audio_queue.empty():
            try:
                audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
