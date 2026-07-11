"""Tests for streaming audio playback."""

import asyncio
from contextlib import suppress
import sys
import types

from ares.voice import player


class FakeRawOutputStream:
    def __init__(self, *args, **kwargs):
        self.started = False
        self.written = bytearray()
        self.write_calls = 0

    def start(self):
        self.started = True

    def write(self, data):
        self.write_calls += 1
        self.written.extend(data)

    def stop(self):
        self.started = False

    def close(self):
        pass


def test_play_audio_stream_completes_with_sentinel(monkeypatch):
    async def run():
        q = asyncio.Queue()
        stop = asyncio.Event()
        await q.put(b"\x00\x00" * 120)
        await q.put(None)
        await player.play_audio_stream(q, stop, sample_rate=24000)

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawOutputStream=FakeRawOutputStream))
    asyncio.run(run())


def test_play_audio_stream_stops_on_event(monkeypatch):
    async def run():
        q = asyncio.Queue()
        stop = asyncio.Event()
        await q.put(b"\x00\x00" * 24000)
        task = asyncio.create_task(player.play_audio_stream(q, stop, sample_rate=24000))
        await asyncio.sleep(0.01)
        stop.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawOutputStream=FakeRawOutputStream))
    asyncio.run(run())


def test_play_audio_stream_splits_long_audio_into_interruptible_blocks(monkeypatch):
    stream = FakeRawOutputStream()

    def output_stream(*_args, **_kwargs):
        return stream

    async def run():
        q = asyncio.Queue()
        stop = asyncio.Event()
        await q.put(b"\x00\x00" * 24000)
        await q.put(None)
        await player.play_audio_stream(q, stop, sample_rate=24000)

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawOutputStream=output_stream))
    asyncio.run(run())
    # One second of PCM was delivered in 20 ms blocks, not a monolithic write.
    assert len(stream.written) == 48000
    assert stream.write_calls >= 40
