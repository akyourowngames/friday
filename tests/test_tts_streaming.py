"""Tests for TTS streaming helpers."""

import asyncio

from ares.voice.tts import EdgeTTS


def test_edge_tts_synthesize_drains_stream(monkeypatch):
    async def fake_stream(self, text, voice=""):
        yield b"abc"
        yield b"def"

    monkeypatch.setattr(EdgeTTS, "stream", fake_stream)
    assert asyncio.run(EdgeTTS().synthesize("hello")) == b"abcdef"


def test_edge_tts_speak_alias(monkeypatch):
    async def fake_stream(self, text, voice=""):
        yield b"audio"

    monkeypatch.setattr(EdgeTTS, "stream", fake_stream)
    assert asyncio.run(EdgeTTS().speak("hello")) == b"audio"


def test_edge_tts_reports_encoded_audio():
    assert EdgeTTS.audio_format == "encoded"
