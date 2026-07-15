"""Custom LiveKit STT/TTS plugins wrapping Ares voice backends."""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from typing import Any

import numpy as np
from livekit import rtc
from livekit.agents import llm as livekit_llm
from livekit.agents import stt as livekit_stt
from livekit.agents import tts as livekit_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Whisper STT Plugin
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 16000


class WhisperSTT(livekit_stt.STT):
    """LiveKit STT plugin using local faster-whisper."""

    def __init__(self, *, model_name: str = "small", language: str = "") -> None:
        super().__init__(capabilities=livekit_stt.STTCapabilities(streaming=False, interim_results=False))
        self._model_name = model_name
        self._language = language
        self._transcriber: Any = None

    def _ensure_transcriber(self) -> Any:
        if self._transcriber is None:
            from ares.voice.stt import WhisperTranscriber

            self._transcriber = WhisperTranscriber(self._model_name, language=self._language)
        return self._transcriber

    async def _recognize_impl(
        self,
        buffer: livekit_stt.AudioBuffer,
        *,
        language: str | None = None,
        conn_options: livekit_stt.APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> livekit_stt.SpeechEvent:
        """Transcribe a single audio buffer (non-streaming)."""
        transcriber = self._ensure_transcriber()

        # Merge frames into a single numpy array
        if isinstance(buffer, rtc.AudioFrame):
            frames = [buffer]
        else:
            frames = buffer

        pcm_bytes = b"".join(frame.data for frame in frames)
        sample_rate = frames[0].sample_rate if frames else _SAMPLE_RATE
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        # Resample to 16 kHz if needed
        if sample_rate != _SAMPLE_RATE:
            target_len = int(len(samples) * _SAMPLE_RATE / sample_rate)
            samples = np.interp(
                np.linspace(0, len(samples) - 1, target_len),
                np.arange(len(samples)),
                samples,
            ).astype(np.float32)

        text = await asyncio.to_thread(transcriber.transcribe_samples, samples, _SAMPLE_RATE)

        return livekit_stt.SpeechEvent(
            type=livekit_stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                livekit_stt.SpeechData(
                    language=language or self._language or "en",
                    text=text,
                    confidence=1.0,
                )
            ],
        )


class SarvamSTT(livekit_stt.STT):
    """LiveKit STT plugin using Sarvam Saaras v3."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "saaras:v3",
        language_code: str = "en-IN",
    ) -> None:
        super().__init__(capabilities=livekit_stt.STTCapabilities(streaming=False, interim_results=False))
        self._api_key = api_key
        self._model = model or "saaras:v3"
        self._language_code = language_code or "en-IN"
        self._transcriber: Any = None

    def _ensure_transcriber(self) -> Any:
        if self._transcriber is None:
            from ares.voice.sarvam import SarvamTranscriber

            self._transcriber = SarvamTranscriber(
                api_key=self._api_key,
                model=self._model,
                language_code=self._language_code,
            )
        return self._transcriber

    async def _recognize_impl(
        self,
        buffer: livekit_stt.AudioBuffer,
        *,
        language: str | None = None,
        conn_options: livekit_stt.APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> livekit_stt.SpeechEvent:
        if isinstance(buffer, rtc.AudioFrame):
            frames = [buffer]
        else:
            frames = buffer
        if not frames:
            return livekit_stt.SpeechEvent(type=livekit_stt.SpeechEventType.FINAL_TRANSCRIPT)

        pcm_bytes = b"".join(frame.data for frame in frames)
        sample_rate = frames[0].sample_rate
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        text = await asyncio.to_thread(
            self._ensure_transcriber().transcribe_samples,
            samples,
            sample_rate,
        )
        return livekit_stt.SpeechEvent(
            type=livekit_stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                livekit_stt.SpeechData(
                    language=language or self._language_code,
                    text=text,
                    confidence=1.0,
                )
            ],
        )


# ---------------------------------------------------------------------------
# Edge TTS Plugin
# ---------------------------------------------------------------------------


class EdgeTTSPlugin(livekit_tts.TTS):
    """LiveKit TTS plugin using Microsoft Edge TTS."""

    def __init__(self, *, voice: str = "en-US-JennyNeural") -> None:
        super().__init__(
            capabilities=livekit_tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._voice = voice

    def synthesize(
        self,
        text: str,
        *,
        conn_options: livekit_tts.APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> livekit_tts.ChunkedStream:
        """Synthesize text to audio chunks."""
        return EdgeTTSChunkedStream(tts=self, input_text=text, voice=self._voice)


class EdgeTTSChunkedStream(livekit_tts.ChunkedStream):
    """Chunked stream implementation for Edge TTS."""

    def __init__(self, *, tts: EdgeTTSPlugin, input_text: str, voice: str) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=DEFAULT_API_CONNECT_OPTIONS)
        self._voice = voice

    async def _run(self, output_emitter: livekit_tts.AudioEmitter) -> None:
        """Collect all audio from Edge TTS and emit via the AudioEmitter."""
        from ares.voice.tts import EdgeTTS

        edge = EdgeTTS(self._voice)
        request_id = str(uuid.uuid4())

        try:
            audio_bytes = await edge.synthesize(self.input_text, self._voice)
            if not audio_bytes:
                return

            # Edge TTS outputs MP3 — decode to raw PCM
            pcm_data = await asyncio.to_thread(
                _decode_mp3_to_pcm, audio_bytes, self._tts.sample_rate
            )

            if not pcm_data:
                return

            # Initialize the emitter with our audio format
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=self._tts.sample_rate,
                num_channels=self._tts.num_channels,
                mime_type="audio/pcm",
            )
            output_emitter.push(pcm_data)

        except Exception:
            logger.exception("Edge TTS synthesis failed")


# ---------------------------------------------------------------------------
# Sarvam Bulbul TTS Plugin
# ---------------------------------------------------------------------------


class SarvamTTSPlugin(livekit_tts.TTS):
    """LiveKit TTS plugin for Sarvam Bulbul's Indian-language voices."""

    def __init__(
        self,
        *,
        api_key: str = "",
        speaker: str = "shubh",
        model: str = "bulbul:v3",
        language_code: str = "en-IN",
        pace: float = 1.0,
        sample_rate: int = 24000,
    ) -> None:
        super().__init__(
            capabilities=livekit_tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._api_key = api_key
        self._speaker = speaker or "shubh"
        self._model = model or "bulbul:v3"
        self._language_code = language_code or "en-IN"
        self._pace = max(0.5, min(float(pace or 1.0), 2.0))

    def synthesize(
        self,
        text: str,
        *,
        conn_options: livekit_tts.APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> livekit_tts.ChunkedStream:
        return SarvamTTSChunkedStream(tts=self, input_text=text)


class SarvamTTSChunkedStream(livekit_tts.ChunkedStream):
    """Emit Sarvam's base64-decoded WAV response through LiveKit."""

    def __init__(self, *, tts: SarvamTTSPlugin, input_text: str) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=DEFAULT_API_CONNECT_OPTIONS)

    async def _run(self, output_emitter: livekit_tts.AudioEmitter) -> None:
        from ares.voice.sarvam import SarvamTTS

        request_id = str(uuid.uuid4())
        try:
            sarvam = SarvamTTS(
                api_key=self._tts._api_key,
                speaker=self._tts._speaker,
                model=self._tts._model,
                language_code=self._tts._language_code,
                sample_rate=self._tts.sample_rate,
                pace=self._tts._pace,
            )
            audio_bytes = await sarvam.synthesize(self.input_text, self._tts._speaker)
            if not audio_bytes:
                raise RuntimeError("Sarvam TTS returned an empty audio response")

            # Sarvam's REST API returns a base64-encoded WAV response. The
            # Ares adapter decodes base64; LiveKit decodes WAV into frames.
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=self._tts.sample_rate,
                num_channels=self._tts.num_channels,
                mime_type="audio/wav",
            )
            output_emitter.push(audio_bytes)
        except Exception:
            logger.exception("Sarvam TTS synthesis failed")


def _decode_mp3_to_pcm(mp3_data: bytes, target_sample_rate: int = 24000) -> bytes:
    """Decode MP3 bytes to raw PCM16 using ffmpeg or pydub."""
    try:
        import subprocess
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3_data)
            mp3_path = f.name

        proc = subprocess.run(
            [
                "ffmpeg", "-i", mp3_path,
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", str(target_sample_rate), "-ac", "1",
                "pipe:1",
            ],
            capture_output=True, timeout=30,
        )
        os.unlink(mp3_path)

        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: try pydub
    try:
        from pydub import AudioSegment

        audio = AudioSegment.from_mp3(io.BytesIO(mp3_data))
        audio = audio.set_frame_rate(target_sample_rate).set_channels(1).set_sample_width(2)
        return audio.raw_data
    except ImportError:
        logger.warning("Neither ffmpeg nor pydub available for MP3 decoding")
        return b""
