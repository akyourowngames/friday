"""Tests for TTS streaming helpers."""

import asyncio

from ares.voice.tts import EdgeTTS, SarvamTTS


def test_edge_tts_speak_drains_speak_stream(monkeypatch):
    async def fake_stream(self, text, voice=""):
        yield b"abc"
        yield b"def"

    monkeypatch.setattr(EdgeTTS, "speak_stream", fake_stream)
    assert asyncio.run(EdgeTTS().speak("hello")) == b"abcdef"


def test_sarvam_speak_stream_yields_single_simulated_chunk(monkeypatch):
    async def fake_speak(self, text, voice=""):
        return b"audio"

    async def collect():
        monkeypatch.setattr(SarvamTTS, "speak", fake_speak)
        chunks = []
        async for chunk in SarvamTTS(api_key="test").speak_stream("hello"):
            chunks.append(chunk)
        return chunks

    assert asyncio.run(collect()) == [b"audio"]
