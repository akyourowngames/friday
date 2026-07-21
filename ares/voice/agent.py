"""Continuous voice mode for Ares.

The rebuilt loop is intentionally small: listen, transcribe, run Ares, speak,
then go right back to listening. It can use local Whisper/Edge or Sarvam AI.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import re
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
from rich.console import Console
from rich.panel import Panel

from ares.config import load_config
from ares.models import VoiceConfig
from ares.voice.player import audio_bytes_to_pcm16, play_audio_stream
from ares.voice.sarvam import SarvamTTS, SarvamTranscriber, sarvam_api_key
from ares.voice.stt import WhisperTranscriber, trim_silence
from ares.voice.tts import DEFAULT_EDGE_VOICE, EdgeTTS

_SAMPLE_RATE = 16000
_FRAME_MS = 30
_FRAME_SAMPLES = int(_SAMPLE_RATE * _FRAME_MS / 1000)
_MAX_SENTENCE_CHARS = 90
_EXIT_PHRASES = {
    "exit voice mode",
    "quit voice mode",
    "stop voice mode",
    "stop listening",
    "goodbye ares",
}


def _env_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _env_mic_device(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def voice_config_from_env(config: VoiceConfig) -> VoiceConfig:
    """Return voice config with supported environment overrides applied."""
    casters: dict[str, tuple[str, Callable[[str], Any]]] = {
        "ARES_VOICE_ENABLED": ("enabled", _env_bool),
        "ARES_STT_BACKEND": ("stt_backend", str),
        "ARES_TTS_BACKEND": ("tts_backend", str),
        "ARES_TTS_VOICE": ("tts_voice", str),
        "ARES_STT_MODEL": ("stt_model", str),
        "ARES_STT_LANGUAGE": ("stt_language", str),
        "ARES_MIC_DEVICE": ("mic_device", _env_mic_device),
        "ARES_VOICE_MIN_UTTERANCE_MS": ("min_utterance_ms", int),
        "ARES_VOICE_SILENCE_TIMEOUT_MS": ("silence_timeout_ms", int),
        "ARES_VOICE_MAX_UTTERANCE_SECONDS": ("max_utterance_seconds", float),
        "ARES_VOICE_START_SPEECH_FRAMES": ("start_speech_frames", int),
        "ARES_VOICE_MIN_VOICED_MS": ("min_voiced_ms", int),
        "ARES_VOICE_MIN_AUDIO_RMS": ("min_audio_rms", float),
        "ARES_VOICE_BARGE_IN": ("barge_in_enabled", _env_bool),
        "ARES_VOICE_BARGE_IN_DELAY_MS": ("barge_in_delay_ms", int),
        "ARES_VOICE_BARGE_IN_MIN_VOICED_MS": ("barge_in_min_voiced_ms", int),
        "ARES_VOICE_TTS_CHUNK_CHARS": ("tts_chunk_chars", int),
        "ARES_TTS_SAMPLE_RATE": ("tts_sample_rate", int),
        "ARES_TTS_VOLUME": ("tts_volume", float),
        "SARVAM_STT_MODEL": ("sarvam_stt_model", str),
        "SARVAM_TTS_MODEL": ("sarvam_tts_model", str),
        "SARVAM_LANGUAGE_CODE": ("sarvam_language_code", str),
        "SARVAM_SPEAKER": ("sarvam_speaker", str),
        "ARES_VOICE_MAX_HISTORY": ("voice_max_history", int),
        "ARES_VOICE_MAX_MEMORIES": ("voice_max_memories", int),
    }
    updates: dict[str, Any] = {}
    for env_name, (field, caster) in casters.items():
        value = os.environ.get(env_name)
        if value is not None:
            updates[field] = caster(value)
    return config.model_copy(update=updates)


class MicrophoneFrames:
    """Capture microphone audio and expose fixed-size 16 kHz frames."""

    def __init__(
        self,
        *,
        sample_rate: int = _SAMPLE_RATE,
        frame_samples: int = _FRAME_SAMPLES,
        device: int | str | None = None,
        max_seconds: int = 30,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.device = device
        self._frames: collections.deque[np.ndarray] = collections.deque(
            maxlen=max_seconds * sample_rate // frame_samples
        )
        self._lock = threading.Lock()
        self._buffer = np.array([], dtype=np.float32)
        self._stream = None
        self._total_frames = 0
        self.last_status = ""

    def start(self) -> None:
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.frame_samples,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        stream.stop()
        stream.close()

    def read(self, count: int = 1) -> list[np.ndarray]:
        with self._lock:
            frames = []
            for _ in range(min(count, len(self._frames))):
                frames.append(self._frames.popleft())
            return frames

    def drain(self) -> None:
        with self._lock:
            self._frames.clear()
            self._buffer = np.array([], dtype=np.float32)

    def unread(self, frames: list[np.ndarray]) -> None:
        """Return consumed frames to the front of the capture queue.

        Barge-in detection has already consumed the beginning of the user's
        interruption. Putting those frames back means the next turn starts
        with the actual spoken words instead of waiting for them to be repeated.
        """
        if not frames:
            return
        with self._lock:
            for frame in reversed(frames):
                self._frames.appendleft(frame)

    @property
    def total_frames(self) -> int:
        with self._lock:
            return self._total_frames

    def _callback(self, indata, _frames, _time, status) -> None:
        if status:
            self.last_status = str(status)

        mono = np.asarray(indata, dtype=np.float32).reshape(-1)
        with self._lock:
            self._buffer = np.concatenate([self._buffer, mono])
            while self._buffer.size >= self.frame_samples:
                frame = self._buffer[: self.frame_samples].copy()
                self._buffer = self._buffer[self.frame_samples :]
                self._frames.append(frame)
                self._total_frames += 1


class ContinuousVoiceAgent:
    """Always-listening Ares voice agent with selectable STT/TTS backends."""

    def __init__(self, voice_name: str | None = None) -> None:
        self.console = Console()
        self.config = load_config()
        self.voice_config = voice_config_from_env(self.config.voice)
        if voice_name:
            self.voice_config.tts_voice = voice_name
        if not self.voice_config.tts_voice:
            self.voice_config.tts_voice = DEFAULT_EDGE_VOICE

        self.stt_backend = self._resolve_backend(self.voice_config.stt_backend, local="whisper")
        self.tts_backend = self._resolve_backend(self.voice_config.tts_backend, local="edge")
        self._tts_backend_explicit = (self.voice_config.tts_backend or "edge").lower().strip() != "auto"
        self.transcriber = self._create_transcriber()
        self.tts = self._create_tts()
        self.tts_sample_rate = self.voice_config.tts_sample_rate
        self.capture = MicrophoneFrames(device=self.voice_config.mic_device)
        self.conversation_history: list[dict[str, str]] = []
        self.agent = None
        self.energy_threshold = 0.0005
        self.vad = self._load_vad()
        self._barge_in_occurred = False

    def _resolve_backend(self, backend: str, *, local: str) -> str:
        backend = (backend or "auto").lower().strip()
        if backend == "auto":
            # Older configs stored ``auto``. TTS should not select Sarvam
            # implicitly; Edge is the stable default for spoken replies.
            if local == "edge":
                return "edge"
            return "sarvam" if sarvam_api_key() else local
        return backend

    def _create_transcriber(self):
        if self.stt_backend == "sarvam":
            return SarvamTranscriber(
                model=self.voice_config.sarvam_stt_model,
                language_code=self.voice_config.sarvam_language_code,
            )
        return WhisperTranscriber(
            self.voice_config.stt_model,
            language=self.voice_config.stt_language,
        )

    def _create_tts(self):
        if self.tts_backend == "sarvam":
            return SarvamTTS(
                speaker=self.voice_config.sarvam_speaker,
                model=self.voice_config.sarvam_tts_model,
                language_code=self.voice_config.sarvam_language_code,
                sample_rate=self.voice_config.tts_sample_rate,
                pace=self.voice_config.sarvam_pace,
            )
        return EdgeTTS(self.voice_config.tts_voice)

    def _fallback_tts_to_edge(self, exc: Exception) -> None:
        """Switch speech output to Edge when Sarvam is unavailable."""
        if getattr(self, "_tts_backend_explicit", False):
            raise exc
        if self.tts_backend == "edge":
            return
        self.console.print(
            f"[yellow]Sarvam TTS unavailable ({type(exc).__name__}); switching speech to Edge TTS[/yellow]"
        )
        self.tts_backend = "edge"
        self.tts = EdgeTTS(self.voice_config.tts_voice)
        self.tts_sample_rate = self.voice_config.tts_sample_rate

    def _load_vad(self):
        try:
            import webrtcvad
        except ImportError:
            self.console.print("[yellow]webrtcvad not installed; using energy-based voice detection[/yellow]")
            return None
        self.console.print("[dim]Using WebRTC VAD[/dim]")
        return webrtcvad.Vad(2)

    def _display_voice(self) -> str:
        if self.tts_backend == "sarvam":
            return self.voice_config.sarvam_speaker
        return self.voice_config.tts_voice

    async def _cooldown_after_speech(self) -> None:
        cooldown = max(0, int(self.voice_config.post_speech_cooldown_ms)) / 1000
        if cooldown:
            await asyncio.sleep(cooldown)
        self._drain()

    async def listen_and_respond(self) -> None:
        self.console.print(
            Panel(
                "[bold green]Ares voice mode active[/bold green]\n"
                f"TTS: [cyan]{self.tts_backend}[/cyan]  Voice: [cyan]{self._display_voice()}[/cyan]\n"
                f"STT: [cyan]{self.stt_backend}[/cyan]  Model: [cyan]{self.voice_config.stt_model}[/cyan]\n"
                "Speak naturally. Say 'stop listening' to exit.\n"
                "[dim]Press Ctrl+C to exit[/dim]",
                title="Ares Voice",
                border_style="green",
            )
        )

        self.capture.start()
        try:
            await self._wait_for_microphone()
            await self._calibrate_energy()
            await self._warm_up_transcriber()
            while True:
                self.console.print("[dim cyan]Listening...[/dim cyan]")
                audio = await self._wait_for_utterance()
                if audio is None:
                    continue

                text = await self._transcribe_audio(audio)
                if not text:
                    continue
                self.console.print(f"[bold]You:[/bold] {text}")

                if self._should_exit(text):
                    await self._speak_once("Voice mode stopped.")
                    return

                self.console.print("[yellow]Thinking...[/yellow]")
                response = await self._respond(text)
                if response and response.strip():
                    self._remember_exchange(text, response)
                if self._barge_in_occurred:
                    self.console.print("[dim cyan]Interrupted — listening to you now...[/dim cyan]")
                    continue
                await self._cooldown_after_speech()
        finally:
            self.capture.close()

    async def _wait_for_microphone(self) -> None:
        self.console.print("[dim]Waiting for microphone...[/dim]")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if self.capture.total_frames > 0:
                self.console.print("[dim green]Microphone ready[/dim green]")
                return
        raise RuntimeError("No microphone audio after 5 seconds. Check device permissions.")

    async def _calibrate_energy(self) -> None:
        if self.vad is not None:
            return
        frames: list[np.ndarray] = []
        for _ in range(20):
            chunk = self._read_frames(1)
            if chunk:
                frames.extend(chunk)
            await asyncio.sleep(0.03)
        if not frames:
            return
        noise = float(np.median([np.mean(frame**2) for frame in frames]))
        self.energy_threshold = max(0.0005, noise * 6.0)

    async def _warm_up_transcriber(self) -> None:
        """Load local Whisper before the first real request reaches it."""
        ensure_model = getattr(self.transcriber, "_ensure_model", None)
        if not callable(ensure_model):
            return
        self.console.print("[dim]Preparing local speech recognition...[/dim]")
        await asyncio.to_thread(ensure_model)

    def _read_frames(self, count: int = 1) -> list[np.ndarray]:
        return self.capture.read(count)

    def _drain(self) -> None:
        self.capture.drain()

    async def _read_frame(self) -> np.ndarray:
        while True:
            frames = self._read_frames(1)
            if frames:
                return frames[0]
            await asyncio.sleep(0.005)

    def _is_speech(self, frame: np.ndarray) -> bool:
        if self.vad is not None:
            try:
                pcm16 = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
                return self.vad.is_speech(pcm16.tobytes(), _SAMPLE_RATE)
            except Exception:
                return False
        return float(np.mean(frame**2)) >= self.energy_threshold

    async def _wait_for_utterance(self) -> np.ndarray | None:
        pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=8)
        consecutive_speech = 0
        candidate: list[np.ndarray] = []
        start_frames = max(1, self.voice_config.start_speech_frames)
        while True:
            frame = await self._read_frame()
            pre_roll.append(frame)
            if self._is_speech(frame):
                if consecutive_speech == 0:
                    candidate = list(pre_roll)
                else:
                    candidate.append(frame)
                consecutive_speech += 1
                if consecutive_speech >= start_frames:
                    speech = candidate
                    voiced_frames = consecutive_speech
                    break
            else:
                consecutive_speech = 0
                candidate = []

        silence = 0
        silence_limit = max(1, int(self.voice_config.silence_timeout_ms / _FRAME_MS))
        max_frames = max(1, int(self.voice_config.max_utterance_seconds * 1000 / _FRAME_MS))
        min_voiced_frames = max(1, int(self.voice_config.min_voiced_ms / _FRAME_MS))

        while len(speech) < max_frames:
            frame = await self._read_frame()
            speech.append(frame)
            if self._is_speech(frame):
                voiced_frames += 1
                silence = 0
            else:
                silence += 1
                if silence >= silence_limit:
                    break

        self._drain()
        if voiced_frames < min_voiced_frames:
            return None

        audio = np.concatenate(speech).astype(np.float32)
        audio = trim_silence(audio, _SAMPLE_RATE)
        duration_ms = len(audio) * 1000 / _SAMPLE_RATE
        if duration_ms < self.voice_config.min_utterance_ms:
            return None
        if float(np.sqrt(np.mean(audio**2))) < self.voice_config.min_audio_rms:
            return None
        return audio

    async def _transcribe_audio(self, audio: np.ndarray) -> str:
        self.console.print("[yellow]Transcribing...[/yellow]")
        return await asyncio.to_thread(self.transcriber.transcribe_samples, audio, _SAMPLE_RATE)

    def _should_exit(self, text: str) -> bool:
        normalized = re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()
        return normalized in _EXIT_PHRASES

    def _get_or_create_agent(self):
        if self.agent is None:
            from ares.agent import Agent
            from ares.context.conversations import ConversationStore
            from ares.memory import MemoryStore

            self.agent = Agent(
                memory_store=MemoryStore(),
                conversation_store=ConversationStore(),
                api_key=self.config.api_key,
                base_url=self.config.api_base_url,
                model=self.config.model,
                config=self.config,
                is_voice_session=True,
            )
        return self.agent

    def _remember_exchange(self, user_text: str, assistant_text: str) -> None:
        self.conversation_history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )
        max_messages = max(2, self.voice_config.voice_max_history)
        if len(self.conversation_history) > max_messages:
            self.conversation_history = self.conversation_history[-max_messages:]

    async def _stream_to_sentences(self, text: str, sentence_q: asyncio.Queue[str | None]) -> str:
        """Stream Ares response text and queue sentence-sized TTS chunks."""
        agent = getattr(self, "agent", None) or self._get_or_create_agent()
        history = getattr(self, "conversation_history", [])
        buffer = ""
        full_response = ""
        first_token = True

        async for token in agent.run_stream(text, history):
            # Tool lifecycle tokens are renderer telemetry, never spoken prose.
            if token.startswith("[tool"):
                continue
            if first_token:
                self.console.print("[bold green]Ares:[/bold green] ", end="")
                first_token = False
            self.console.print(token, end="", highlight=False)
            buffer += token
            full_response += token

            if self._sentence_ready(buffer):
                await sentence_q.put(buffer.strip())
                buffer = ""

        if buffer.strip():
            await sentence_q.put(buffer.strip())
        await sentence_q.put(None)
        if not first_token:
            self.console.print()
        return full_response

    def _sentence_ready(self, text: str) -> bool:
        stripped = text.rstrip()
        limit = max(48, min(_MAX_SENTENCE_CHARS, int(self.voice_config.tts_chunk_chars)))
        if len(stripped) >= limit:
            return True
        # Commas and clauses are safe early hand-off points.  Waiting for a
        # complete long sentence is a large part of the perceived voice lag.
        return len(stripped) >= 36 and bool(re.search(r"(?:[.!?]|[,;:])\s*$", stripped))

    async def _tts_play_pipeline(
        self,
        sentence_q: asyncio.Queue[str | None],
        stop_event: asyncio.Event,
        playback_started: asyncio.Event,
    ) -> None:
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        play_task = asyncio.create_task(play_audio_stream(audio_q, stop_event, sample_rate=self.tts_sample_rate))
        try:
            while not stop_event.is_set():
                sentence = await sentence_q.get()
                if sentence is None:
                    await audio_q.put(None)
                    break

                sentence = self._sanitize_tts_text(sentence)
                if not sentence:
                    continue

                try:
                    encoded = await self._synthesize_with_fallback(sentence)
                    if encoded and not stop_event.is_set():
                        # Decoding MP3/WAV is CPU and may invoke an external
                        # decoder. Keep it off the event loop so VAD can still
                        # notice a barge-in immediately.
                        pcm = await asyncio.to_thread(self._audio_to_pcm, encoded)
                        await audio_q.put(pcm)
                        playback_started.set()
                except Exception as exc:
                    self.console.print(f"[red]TTS error: {type(exc).__name__}: {exc}[/red]")
        finally:
            if stop_event.is_set():
                play_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await play_task
            else:
                await play_task

    async def _barge_in_watcher(
        self,
        stop_event: asyncio.Event,
        playback_started: asyncio.Event,
    ) -> None:
        if not self.voice_config.barge_in_enabled:
            return
        while not playback_started.is_set():
            if stop_event.is_set():
                return
            await asyncio.sleep(0.05)

        await asyncio.sleep(max(0, int(self.voice_config.barge_in_delay_ms)) / 1000)
        consecutive_speech = 0
        detected: list[np.ndarray] = []
        required_frames = max(
            self.voice_config.start_speech_frames,
            int(np.ceil(max(30, self.voice_config.barge_in_min_voiced_ms) / _FRAME_MS)),
        )
        while not stop_event.is_set():
            frames = self._read_frames(1)
            if not frames:
                await asyncio.sleep(0.005)
                continue
            if self._is_speech(frames[0]):
                detected.append(frames[0])
                consecutive_speech += 1
                if consecutive_speech >= required_frames:
                    # Preserve the opening of the interruption for the next
                    # listener pass; dropping it makes the user repeat themself.
                    self.capture.unread(detected)
                    stop_event.set()
                    self._barge_in_occurred = True
                    return
            else:
                consecutive_speech = 0
                detected.clear()

    async def _respond(self, text: str) -> str:
        sentence_q: asyncio.Queue[str | None] = asyncio.Queue()
        stop_event = asyncio.Event()
        playback_started = asyncio.Event()
        self._barge_in_occurred = False
        stream_task = asyncio.create_task(self._stream_to_sentences(text, sentence_q))
        tts_task = asyncio.create_task(self._tts_play_pipeline(sentence_q, stop_event, playback_started))
        barge_task = asyncio.create_task(self._barge_in_watcher(stop_event, playback_started))
        try:
            # Do not wait for a long LLM/tool turn after the user speaks over
            # it.  The previous implementation did exactly that, making
            # "barge-in" feel like it had not worked.
            done, _pending = await asyncio.wait(
                {stream_task, barge_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_event.is_set():
                stream_task.cancel()
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
                with contextlib.suppress(asyncio.CancelledError):
                    await tts_task
                return ""

            full_response = await stream_task
            if stop_event.is_set():
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tts_task
                return ""

            done, _pending = await asyncio.wait(
                {tts_task, barge_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_event.is_set():
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tts_task
                return ""
            await tts_task
            return full_response
        finally:
            if not tts_task.done():
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tts_task
            barge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await barge_task

    async def _speak_once(self, text: str) -> None:
        stop_event = asyncio.Event()
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        encoded = await self._synthesize_with_fallback(text)
        if encoded:
            await q.put(self._audio_to_pcm(encoded, speed=1.0))
        await q.put(None)
        await play_audio_stream(q, stop_event, sample_rate=self.tts_sample_rate)
        await self._cooldown_after_speech()

    async def _synthesize_with_fallback(self, text: str) -> bytes:
        text = self._sanitize_tts_text(text)
        if not text:
            return b""
        try:
            return await self.tts.synthesize(text, self._display_voice())
        except Exception as exc:
            if self.tts_backend != "sarvam":
                raise
            self._fallback_tts_to_edge(exc)
            return await self.tts.synthesize(text, self._display_voice())

    def _audio_to_pcm(self, audio: bytes, speed: float = 1.08) -> bytes:
        if getattr(self.tts, "audio_format", "encoded") == "pcm16":
            return self._amplify_pcm16(audio)
        pcm = audio_bytes_to_pcm16(audio, sample_rate=self.tts_sample_rate, speed=speed)
        return self._amplify_pcm16(pcm)

    def _amplify_pcm16(self, pcm: bytes) -> bytes:
        volume = max(0.1, float(self.voice_config.tts_volume))
        if volume == 1.0 or not pcm:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        samples = np.clip(samples * volume, -32768, 32767).astype(np.int16)
        return samples.tobytes()

    def _sanitize_tts_text(self, text: str) -> str:
        text = re.sub(r"\[[^\]]+\]", " ", text or "")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        if not re.search(r"[A-Za-z0-9\u0900-\u097F\u0980-\u09FF\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F\u0A80-\u0AFF\u0A00-\u0A7F\u0B00-\u0B7F]", text):
            return ""
        return text


async def run_voice_agent(
    voice_name: str | None = None,
    *,
    stt_backend: str | None = None,
    tts_backend: str | None = None,
    barge_in: bool | None = None,
) -> None:
    agent = ContinuousVoiceAgent(voice_name)
    if barge_in is not None:
        agent.voice_config.barge_in_enabled = barge_in
    if stt_backend:
        agent.voice_config.stt_backend = stt_backend
        agent.stt_backend = agent._resolve_backend(stt_backend, local="whisper")
        agent.transcriber = agent._create_transcriber()
    if tts_backend:
        agent.voice_config.tts_backend = tts_backend
        agent.tts_backend = agent._resolve_backend(tts_backend, local="edge")
        agent._tts_backend_explicit = tts_backend != "auto"
        agent.tts = agent._create_tts()
        agent.tts_sample_rate = agent.voice_config.tts_sample_rate
    await agent.listen_and_respond()
