"""Tests for continuous voice real-time streaming helpers."""

import asyncio
from unittest.mock import MagicMock

import numpy as np

from ares.models import VoiceConfig
from ares.voice import agent as voice_agent
from ares.voice.agent import ContinuousVoiceAgent, _MAX_SENTENCE_CHARS, run_voice_agent


def test_stream_to_sentences_skips_tool_tokens_and_flushes():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.conversation_history = []
        agent.voice_config = VoiceConfig()

        async def run_stream(text, history):
            yield "[tool:shell:echo hi]"
            yield "Hello "
            yield "world."

        agent.agent = MagicMock()
        agent.agent.run_stream = run_stream
        agent._sentence_ready = lambda text: ContinuousVoiceAgent._sentence_ready(agent, text)

        q = asyncio.Queue()
        result = await ContinuousVoiceAgent._stream_to_sentences(agent, "test", q)
        queued = []
        while not q.empty():
            queued.append(await q.get())
        return result, queued

    result, queued = asyncio.run(run())
    assert result == "Hello world."
    assert queued[-1] is None
    assert "Hello world." in queued


def test_stream_to_sentences_max_length_flushes():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.conversation_history = []
        agent.voice_config = VoiceConfig()
        long_text = "x" * (_MAX_SENTENCE_CHARS + 5)

        async def run_stream(text, history):
            yield long_text

        agent.agent = MagicMock()
        agent.agent.run_stream = run_stream
        agent._sentence_ready = lambda text: ContinuousVoiceAgent._sentence_ready(agent, text)
        q = asyncio.Queue()
        await ContinuousVoiceAgent._stream_to_sentences(agent, "test", q)
        return await q.get()

    assert asyncio.run(run()) == "x" * (_MAX_SENTENCE_CHARS + 5)


def test_barge_in_watcher_sets_stop_event():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.voice_config = VoiceConfig(
            barge_in_enabled=True,
            barge_in_delay_ms=0,
            barge_in_min_voiced_ms=60,
            start_speech_frames=2,
        )
        agent._read_frames = MagicMock(return_value=[np.ones(480, dtype=np.float32)])
        agent._is_speech = MagicMock(return_value=True)
        agent.capture = MagicMock()
        agent._barge_in_occurred = False
        stop = asyncio.Event()
        playback_started = asyncio.Event()
        playback_started.set()
        await ContinuousVoiceAgent._barge_in_watcher(agent, stop, playback_started)
        return stop.is_set(), agent.capture.unread, agent._barge_in_occurred

    is_set, unread, interrupted = asyncio.run(run())
    assert is_set
    assert interrupted is True
    unread.assert_called_once()


def test_barge_in_watcher_returns_when_disabled():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.voice_config = VoiceConfig(barge_in_enabled=False)
        agent._read_frames = MagicMock(return_value=[np.ones(480, dtype=np.float32)])
        agent._is_speech = MagicMock(return_value=True)
        stop = asyncio.Event()
        playback_started = asyncio.Event()
        playback_started.set()
        await ContinuousVoiceAgent._barge_in_watcher(agent, stop, playback_started)
        return stop.is_set(), agent._read_frames

    is_set, read_frames = asyncio.run(run())
    assert is_set is False
    read_frames.assert_not_called()


def test_run_voice_agent_preserves_saved_barge_setting_without_a_cli_override(monkeypatch):
    captured = []

    class FakeVoiceAgent:
        def __init__(self, _voice_name=None):
            self.voice_config = VoiceConfig(barge_in_enabled=True)

        async def listen_and_respond(self):
            captured.append(self.voice_config.barge_in_enabled)

    monkeypatch.setattr(voice_agent, "ContinuousVoiceAgent", FakeVoiceAgent)

    asyncio.run(run_voice_agent(barge_in=None))
    asyncio.run(run_voice_agent(barge_in=False))

    assert captured == [True, False]


def test_barge_in_cancels_a_long_response_without_waiting_for_completion():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.voice_config = VoiceConfig(barge_in_enabled=True)
        agent._barge_in_occurred = False
        cancelled = {"stream": False, "tts": False}

        async def stream(_text, _queue):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled["stream"] = True
                raise

        async def tts(_queue, _stop, _started):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled["tts"] = True
                raise

        async def barge(stop, _started):
            await asyncio.sleep(0)
            agent._barge_in_occurred = True
            stop.set()

        agent._stream_to_sentences = stream
        agent._tts_play_pipeline = tts
        agent._barge_in_watcher = barge
        result = await ContinuousVoiceAgent._respond(agent, "hello")
        return result, cancelled

    result, cancelled = asyncio.run(run())
    assert result == ""
    assert cancelled == {"stream": True, "tts": True}


def test_warm_up_transcriber_loads_local_whisper_before_first_turn():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.transcriber = MagicMock()
        await ContinuousVoiceAgent._warm_up_transcriber(agent)
        return agent.transcriber._ensure_model, agent.console

    ensure_model, console = asyncio.run(run())
    ensure_model.assert_called_once()
    console.print.assert_called_once()


def test_wait_for_utterance_skips_low_energy_audio():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.voice_config = VoiceConfig(
            start_speech_frames=2,
            min_voiced_ms=60,
            min_utterance_ms=60,
            min_audio_rms=0.01,
            silence_timeout_ms=60,
        )
        agent._drain = MagicMock()
        frames = [np.ones(480, dtype=np.float32) * 0.001 for _ in range(5)]
        frames.extend([np.zeros(480, dtype=np.float32) for _ in range(3)])
        iterator = iter(frames)

        async def read_frame():
            return next(iterator)

        agent._read_frame = read_frame
        agent._is_speech = lambda frame: bool(np.max(frame) > 0)
        return await ContinuousVoiceAgent._wait_for_utterance(agent)

    assert asyncio.run(run()) is None


def test_tts_sarvam_failure_falls_back_to_edge(monkeypatch):
    class FailingSarvamTTS:
        audio_format = "pcm16"

        async def synthesize(self, text, voice=""):
            raise RuntimeError("failed to establish link to worker")

    class FakeEdgeTTS:
        audio_format = "encoded"

        def __init__(self, voice):
            self.default_voice = voice

        async def synthesize(self, text, voice=""):
            return b"edge-audio"

    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.voice_config = VoiceConfig(tts_voice="en-US-GuyNeural")
        agent.tts_backend = "sarvam"
        agent._tts_backend_explicit = False
        agent.tts = FailingSarvamTTS()
        agent.tts_sample_rate = agent.voice_config.tts_sample_rate
        agent._display_voice = lambda: ContinuousVoiceAgent._display_voice(agent)
        agent._fallback_tts_to_edge = lambda exc: ContinuousVoiceAgent._fallback_tts_to_edge(agent, exc)
        return await ContinuousVoiceAgent._synthesize_with_fallback(agent, "hello")

    monkeypatch.setattr(voice_agent, "EdgeTTS", FakeEdgeTTS)

    assert asyncio.run(run()) == b"edge-audio"


def test_explicit_sarvam_tts_failure_does_not_fall_back(monkeypatch):
    class FailingSarvamTTS:
        audio_format = "encoded"

        async def synthesize(self, text, voice=""):
            raise RuntimeError("sarvam rejected request")

    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.voice_config = VoiceConfig(tts_voice="en-US-GuyNeural")
        agent.tts_backend = "sarvam"
        agent._tts_backend_explicit = True
        agent.tts = FailingSarvamTTS()
        agent._display_voice = lambda: ContinuousVoiceAgent._display_voice(agent)
        agent._fallback_tts_to_edge = lambda exc: ContinuousVoiceAgent._fallback_tts_to_edge(agent, exc)
        await ContinuousVoiceAgent._synthesize_with_fallback(agent, "hello")

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        assert "sarvam rejected request" in str(exc)
    else:
        raise AssertionError("explicit Sarvam TTS failure should not fall back")


def test_audio_to_pcm_amplifies_edge_audio(monkeypatch):
    agent = MagicMock(spec=ContinuousVoiceAgent)
    agent.voice_config = VoiceConfig(tts_volume=2.0)
    agent.tts = MagicMock(audio_format="encoded")
    agent.tts_sample_rate = 24000
    agent._amplify_pcm16 = lambda pcm: ContinuousVoiceAgent._amplify_pcm16(agent, pcm)
    monkeypatch.setattr(voice_agent, "audio_bytes_to_pcm16", lambda *args, **kwargs: np.array([1000], dtype=np.int16).tobytes())

    amplified = ContinuousVoiceAgent._audio_to_pcm(agent, b"encoded")

    assert np.frombuffer(amplified, dtype=np.int16).tolist() == [2000]


def test_sanitize_tts_text_skips_emoji_only_chunks():
    agent = MagicMock(spec=ContinuousVoiceAgent)

    assert ContinuousVoiceAgent._sanitize_tts_text(agent, "😄 👋") == ""
    assert ContinuousVoiceAgent._sanitize_tts_text(agent, "Hey Krish! 😄") == "Hey Krish! 😄"


def test_cooldown_after_speech_drains_mic(monkeypatch):
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.voice_config = VoiceConfig(post_speech_cooldown_ms=0)
        agent._drain = MagicMock()
        await ContinuousVoiceAgent._cooldown_after_speech(agent)
        return agent._drain

    drain = asyncio.run(run())
    drain.assert_called_once()
