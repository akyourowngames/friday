"""Tests for LiveKit voice assistant plugins and worker."""

from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# WhisperSTT tests
# ---------------------------------------------------------------------------


class TestWhisperSTT:
    def test_capabilities(self):
        from ares.telephony.livekit_plugins import WhisperSTT

        stt = WhisperSTT(model_name="tiny", language="en")
        assert stt.capabilities.streaming is False
        assert stt.capabilities.interim_results is False

    def test_recognize_returns_speech_event(self):
        from livekit import rtc
        from livekit.agents import stt as livekit_stt

        from ares.telephony.livekit_plugins import WhisperSTT

        stt = WhisperSTT(model_name="tiny")

        # Create a fake audio frame (1 second of silence at 16kHz)
        pcm = np.zeros(16000, dtype=np.int16).tobytes()
        frame = rtc.AudioFrame(data=pcm, sample_rate=16000, num_channels=1, samples_per_channel=16000)

        with patch.object(stt, "_ensure_transcriber") as mock_transcriber:
            mock_transcriber.return_value = MagicMock(transcribe_samples=MagicMock(return_value="hello world"))

            event = asyncio.run(stt.recognize(frame))

            assert isinstance(event, livekit_stt.SpeechEvent)
            assert event.type == livekit_stt.SpeechEventType.FINAL_TRANSCRIPT
            assert len(event.alternatives) == 1
            assert event.alternatives[0].text == "hello world"

    def test_recognize_resamples_audio(self):
        from livekit import rtc

        from ares.telephony.livekit_plugins import WhisperSTT, _SAMPLE_RATE

        stt = WhisperSTT(model_name="tiny")

        # Create a frame at 8kHz (like Twilio audio)
        pcm = np.zeros(8000, dtype=np.int16).tobytes()
        frame = rtc.AudioFrame(data=pcm, sample_rate=8000, num_channels=1, samples_per_channel=8000)

        with patch.object(stt, "_ensure_transcriber") as mock_transcriber:
            mock_transcriber.return_value = MagicMock(transcribe_samples=MagicMock(return_value=""))

            asyncio.run(stt.recognize(frame))

            # Verify transcribe_samples was called with 16kHz samples
            call_args = mock_transcriber.return_value.transcribe_samples.call_args
            assert call_args[1].get("sample_rate") == _SAMPLE_RATE or call_args[0][1] == _SAMPLE_RATE

    def test_recognize_with_buffer_list(self):
        from livekit import rtc
        from livekit.agents import stt as livekit_stt

        from ares.telephony.livekit_plugins import WhisperSTT

        stt = WhisperSTT(model_name="tiny")

        # 16000 int16 samples = 32000 bytes. Split into two frames of 8000 samples each.
        pcm = np.zeros(16000, dtype=np.int16).tobytes()
        half = len(pcm) // 2  # 16000 bytes = 8000 samples
        frame1 = rtc.AudioFrame(data=pcm[:half], sample_rate=16000, num_channels=1, samples_per_channel=8000)
        frame2 = rtc.AudioFrame(data=pcm[half:], sample_rate=16000, num_channels=1, samples_per_channel=8000)

        with patch.object(stt, "_ensure_transcriber") as mock_transcriber:
            mock_transcriber.return_value = MagicMock(transcribe_samples=MagicMock(return_value="test"))

            event = asyncio.run(stt.recognize([frame1, frame2]))

            assert event.alternatives[0].text == "test"


class TestSarvamSTT:
    def test_recognize_returns_sarvam_transcript(self):
        from livekit import rtc

        from ares.telephony.livekit_plugins import SarvamSTT

        async def _test():
            stt = SarvamSTT(api_key="sarvam-test-key", language_code="en-IN")
            frame = rtc.AudioFrame(
                data=np.zeros(16000, dtype=np.int16).tobytes(),
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=16000,
            )
            with patch.object(stt, "_ensure_transcriber") as transcriber:
                transcriber.return_value = MagicMock(
                    transcribe_samples=MagicMock(return_value="namaste ares")
                )
                event = await stt.recognize(frame)
            assert event.alternatives[0].text == "namaste ares"
            assert event.alternatives[0].language == "en-IN"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# EdgeTTSPlugin tests
# ---------------------------------------------------------------------------


class TestEdgeTTSPlugin:
    def test_capabilities(self):
        from ares.telephony.livekit_plugins import EdgeTTSPlugin

        tts = EdgeTTSPlugin(voice="en-US-GuyNeural")
        assert tts.capabilities.streaming is False
        assert tts.sample_rate == 24000
        assert tts.num_channels == 1

    def test_synthesize_returns_chunked_stream(self):
        from livekit.agents import tts as livekit_tts

        from ares.telephony.livekit_plugins import EdgeTTSPlugin

        async def _test():
            tts = EdgeTTSPlugin()
            stream = tts.synthesize("Hello, world!")
            assert isinstance(stream, livekit_tts.ChunkedStream)
            assert stream.input_text == "Hello, world!"

        asyncio.run(_test())


class TestSarvamTTSPlugin:
    @staticmethod
    def _wav_bytes() -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b"\0\0" * 2400)
        return output.getvalue()

    def test_chunked_stream_emits_sarvam_wav_audio(self):
        """Sarvam's encoded WAV result reaches LiveKit as an audio frame."""
        from ares.telephony.livekit_plugins import SarvamTTSPlugin

        async def _test():
            plugin = SarvamTTSPlugin(api_key="sarvam-test-key", speaker="shubh")
            with patch("ares.voice.sarvam.SarvamTTS") as sarvam_tts:
                sarvam_tts.return_value.synthesize = AsyncMock(return_value=self._wav_bytes())
                stream = plugin.synthesize("Hello from Ares")
                frame = await stream.collect()
                assert frame.sample_rate == 24000
                assert frame.num_channels == 1
                assert frame.samples_per_channel == 2400

        asyncio.run(_test())

    def test_chunked_stream_emits_pcm_audio(self):
        """Regression: a generated reply must reach LiveKit as an audio frame."""
        from ares.telephony.livekit_plugins import EdgeTTSPlugin

        async def _test():
            plugin = EdgeTTSPlugin()
            # 100 ms of mono PCM16 at the plugin's 24 kHz sample rate.
            pcm = b"\0\0" * 2400
            with patch(
                "ares.voice.tts.EdgeTTS.synthesize",
                new=AsyncMock(return_value=b"fake-mp3"),
            ), patch(
                "ares.telephony.livekit_plugins._decode_mp3_to_pcm",
                return_value=pcm,
            ):
                stream = plugin.synthesize("Hello from Ares")
                frame = await stream.collect()
                assert frame.sample_rate == 24000
                assert frame.num_channels == 1
                assert frame.samples_per_channel == 2400

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Token generation tests
# ---------------------------------------------------------------------------


class TestTokenGeneration:
    def test_generate_room_token_requires_credentials(self):
        from ares.telephony.livekit_token import generate_room_token

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="API key and secret are required"):
                generate_room_token("user-1", "test-room")

    def test_generate_room_token_with_explicit_credentials(self):
        from ares.telephony.livekit_token import generate_room_token

        token = generate_room_token(
            "user-1",
            "test-room",
            api_key="test-api-key",
            api_secret="test-api-secret-must-be-at-least-32-characters-long!!",
        )

        assert isinstance(token, str)
        assert len(token) > 50
        # JWT tokens have 3 parts separated by dots
        assert token.count(".") == 2

    def test_generate_agent_token_requires_credentials(self):
        from ares.telephony.livekit_token import generate_agent_token

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="API key and secret are required"):
                generate_agent_token("test-room")

    def test_generate_agent_token_with_credentials(self):
        from ares.telephony.livekit_token import generate_agent_token

        token = generate_agent_token(
            "test-room",
            api_key="test-api-key",
            api_secret="test-api-secret-must-be-at-least-32-characters-long!!",
        )

        assert isinstance(token, str)
        assert token.count(".") == 2


# ---------------------------------------------------------------------------
# Agent initialization tests
# ---------------------------------------------------------------------------


class TestAresLiveKitAgent:
    def test_agent_imports(self):
        from ares.telephony.livekit_worker import AresLiveKitAgent

        assert AresLiveKitAgent is not None

    def test_agent_creates_with_config(self):
        from ares.telephony.livekit_worker import AresLiveKitAgent

        config = MagicMock()
        config.api_key = "test-key"
        config.api_base_url = "https://api.example.com/v1"
        config.model = "gpt-4o-mini"
        config.voice = MagicMock()
        config.voice.stt_model = "tiny"
        config.voice.stt_language = ""
        config.voice.tts_voice = "en-US-JennyNeural"

        with patch("ares.telephony.livekit_worker._configure_llm") as mock_llm, \
             patch("ares.telephony.livekit_worker._configure_stt") as mock_stt, \
             patch("ares.telephony.livekit_worker._configure_tts") as mock_tts:

            mock_llm.return_value = MagicMock()
            mock_stt.return_value = MagicMock()
            mock_tts.return_value = MagicMock()

            agent = AresLiveKitAgent(config)

            assert agent._config is config
            mock_llm.assert_called_once_with(config)
            mock_stt.assert_called_once_with(config)
            mock_tts.assert_called_once_with(config)


class TestWorkerPluginStartup:
    def test_llm_requires_startup_registered_plugin(self, monkeypatch):
        import ares.telephony.livekit_worker as worker

        monkeypatch.setattr(worker, "_OPENAI_COMPAT_PLUGIN", None)
        config = SimpleNamespace(api_key="provider-key", api_base_url="https://provider.example/v1", model="model")
        with pytest.raises(RuntimeError, match="not prewarmed"):
            worker._configure_llm(config)

    def test_worker_import_registers_plugin_before_jobs_start(self):
        import ares.telephony.livekit_worker as worker

        assert worker._OPENAI_COMPAT_PLUGIN is not None


class TestWorkerTTSSelection:
    def test_auto_tts_selects_sarvam_when_api_key_is_available(self, monkeypatch):
        from ares.telephony.livekit_plugins import SarvamTTSPlugin
        from ares.telephony.livekit_worker import _configure_tts

        monkeypatch.setenv("SARVAM_API_KEY", "sarvam-test-key")
        config = SimpleNamespace(voice=SimpleNamespace(
            tts_backend="auto",
            sarvam_speaker="shubh",
            sarvam_tts_model="bulbul:v3",
            sarvam_language_code="en-IN",
            sarvam_pace=0.9,
            tts_sample_rate=24000,
        ))

        tts = _configure_tts(config)

        assert isinstance(tts, SarvamTTSPlugin)
        assert tts._speaker == "shubh"

    def test_explicit_sarvam_requires_api_key(self, monkeypatch):
        from ares.telephony.livekit_worker import _configure_tts

        monkeypatch.setenv("SARVAM_API_KEY", "")
        config = SimpleNamespace(voice=SimpleNamespace(tts_backend="sarvam"))

        with pytest.raises(RuntimeError, match="SARVAM_API_KEY"):
            _configure_tts(config)

    def test_auto_stt_selects_sarvam_when_api_key_is_available(self, monkeypatch):
        from ares.telephony.livekit_plugins import SarvamSTT
        from ares.telephony.livekit_worker import _configure_stt

        monkeypatch.setenv("SARVAM_API_KEY", "sarvam-test-key")
        config = SimpleNamespace(voice=SimpleNamespace(
            stt_backend="auto",
            sarvam_stt_model="saaras:v3",
            sarvam_language_code="en-IN",
        ))

        stt = _configure_stt(config)

        assert isinstance(stt, SarvamSTT)

    def test_explicit_sarvam_stt_requires_api_key(self, monkeypatch):
        from ares.telephony.livekit_worker import _configure_stt

        monkeypatch.setenv("SARVAM_API_KEY", "")
        config = SimpleNamespace(voice=SimpleNamespace(stt_backend="sarvam"))

        with pytest.raises(RuntimeError, match="SARVAM_API_KEY"):
            _configure_stt(config)


# ---------------------------------------------------------------------------
# Integration readiness tests
# ---------------------------------------------------------------------------


class TestLiveKitReadiness:
    def test_livekit_packages_installed(self):
        """Verify LiveKit packages are importable."""
        import livekit.agents
        import livekit.api

        assert livekit.agents.__version__ is not None

    def test_openai_plugin_available(self):
        """Verify OpenAI plugin is available for LLM."""
        from livekit.plugins import openai

        assert openai.LLM is not None
