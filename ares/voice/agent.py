"""Continuous voice mode for Ares.

The rebuilt loop is intentionally small: listen, transcribe, run Ares, speak,
then go right back to listening. Uses local Whisper for STT and Edge TTS.
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
        "ARES_VOICE_MAX_HISTORY": ("voice_max_history", int),
        "ARES_VOICE_MAX_MEMORIES": ("voice_max_memories", int),
    }
    updates: dict[str, Any] = {}
    for env_name, (field, caster) in casters.items():
        value = os.environ.get(env_name)
        if value is not None:
            updates[field] = caster(value)
    return config.model_copy(update=updates)


_BLUETOOTH_INPUT_MARKERS = (
    "hands-free",
    "hands free",
    "airpods",
    "airdopes",
    "bluetooth",
    "wireless headset",
    " galaxy buds",
    " buds ",
    "bthhfenum",
)


def resolve_input_device(
    device: int | str | None,
    *,
    sample_rate: int = _SAMPLE_RATE,
    prefer_bluetooth: bool = False,
    avoid_bluetooth: bool = False,
) -> tuple[int | str | None, str]:
    """Resolve and validate a microphone instead of trusting a silent default.

    Windows commonly keeps a laptop microphone as the PortAudio default even
    when Bluetooth headphones are the active output. Desktop mode explicitly
    prefers a compatible Bluetooth hands-free input unless the user configured
    a device themselves.
    """
    import sounddevice as sd

    if device is not None:
        info = sd.query_devices(device, "input")
        sd.check_input_settings(
            device=device, channels=1, samplerate=sample_rate, dtype="float32"
        )
        return device, str(info.get("name") or device)

    devices = list(sd.query_devices())
    bluetooth: list[tuple[int, int, str]] = []
    non_bluetooth: list[tuple[int, int, str]] = []
    for index, info in enumerate(devices):
        if int(info.get("max_input_channels", 0)) < 1:
            continue
        name = str(info.get("name") or "")
        folded = f" {name.casefold()} "
        is_bluetooth = any(
            marker in folded for marker in _BLUETOOTH_INPUT_MARKERS
        )
        try:
            sd.check_input_settings(
                device=index, channels=1, samplerate=sample_rate, dtype="float32"
            )
        except Exception:
            continue
        try:
            host_name = str(sd.query_hostapis(info.get("hostapi"))["name"]).casefold()
        except Exception:
            host_name = ""
        score = 0
        if "wasapi" in host_name:
            score += 30
        elif "directsound" in host_name:
            score += 15
        elif "mme" in host_name:
            score += 10
        if int(float(info.get("default_samplerate", 0))) == int(sample_rate):
            score += 20
        if "microphone array" in folded or "realtek" in folded:
            score += 10
        if is_bluetooth:
            bluetooth.append((100 + score, index, name))
        elif "mapper" not in folded and "primary sound" not in folded:
            non_bluetooth.append((score, index, name))

    if prefer_bluetooth and bluetooth:
        _score, index, name = max(bluetooth, key=lambda item: (item[0], -item[1]))
        return index, name

    if avoid_bluetooth:
        try:
            default_info = sd.query_devices(None, "input")
            default_name = str(default_info.get("name") or "")
        except Exception:
            default_name = ""
        folded_default = f" {default_name.casefold()} "
        default_is_bluetooth = any(
            marker in folded_default for marker in _BLUETOOTH_INPUT_MARKERS
        )
        if default_is_bluetooth and non_bluetooth:
            _score, index, name = max(
                non_bluetooth, key=lambda item: (item[0], -item[1])
            )
            return index, name

    # Passing None to PortAudio is important: it binds the stream through the
    # current Windows default route instead of pinning a numeric endpoint that
    # can become stale when headsets connect or disconnect.
    try:
        info = sd.query_devices(None, "input")
        sd.check_input_settings(
            device=None,
            channels=1,
            samplerate=sample_rate,
            dtype="float32",
        )
        return None, str(info.get("name") or "Windows default microphone")
    except Exception:
        return None, "Windows default microphone"


def friendly_input_device_name(name: str) -> str:
    """Return a compact microphone name suitable for the status card."""
    value = re.sub(r"[\r\n]+", " ", str(name or "")).strip()
    if re.search(r"\bmicrophone array\b", value, re.I):
        brand = re.search(r"\(([^()]+)\)", value)
        if brand:
            return f"{brand.group(1).strip()} Microphone Array"[:64]
        return value[:64]
    parenthesized = re.search(r"\(([^()]*(?:AirPods|Airdopes|Buds)[^()]*)\)", value, re.I)
    if parenthesized:
        value = parenthesized.group(1)
    value = re.sub(r"\bHands[- ]Free(?: AG)? Audio\b", "", value, flags=re.I)
    value = re.sub(r"^(?:Headset|Microphone)\s*\(?", "", value, flags=re.I)
    value = value.strip(" ()-–—")
    return re.sub(r"\s+", " ", value)[:64] or "Microphone"


class MicrophoneFrames:
    """Capture microphone audio and expose fixed-size 16 kHz frames."""

    def __init__(
        self,
        *,
        sample_rate: int = _SAMPLE_RATE,
        frame_samples: int = _FRAME_SAMPLES,
        device: int | str | None = None,
        prefer_bluetooth: bool = False,
        follow_system_default: bool = False,
        avoid_bluetooth: bool = False,
        max_seconds: int = 30,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.device = device
        self.prefer_bluetooth = prefer_bluetooth
        self.follow_system_default = follow_system_default
        self.avoid_bluetooth = avoid_bluetooth
        self.selected_device: int | str | None = device
        self.device_name = ""
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

        selected, name = resolve_input_device(
            self.device,
            sample_rate=self.sample_rate,
            prefer_bluetooth=(
                self.prefer_bluetooth and not self.follow_system_default
            ),
            avoid_bluetooth=self.avoid_bluetooth,
        )
        self.selected_device = selected
        self.device_name = friendly_input_device_name(name)
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.frame_samples,
                device=selected,
                callback=self._callback,
            )
            self._stream.start()
        except Exception:
            # A paired Bluetooth endpoint can remain listed after disconnect.
            # Fall back only for automatic selection; explicit user choices
            # should fail loudly instead of silently opening another mic.
            if self.device is not None:
                raise
            fallback, fallback_name = resolve_input_device(
                None, sample_rate=self.sample_rate, prefer_bluetooth=False
            )
            if fallback == selected:
                raise
            self.selected_device = fallback
            self.device_name = friendly_input_device_name(fallback_name)
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.frame_samples,
                device=fallback,
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
            return local
        return backend

    def _create_transcriber(self):
        return WhisperTranscriber(
            self.voice_config.stt_model,
            language=self.voice_config.stt_language,
        )

    def _create_tts(self):
        return EdgeTTS(self.voice_config.tts_voice)

    def _load_vad(self):
        try:
            import webrtcvad
        except ImportError:
            self.console.print("[yellow]webrtcvad not installed; using energy-based voice detection[/yellow]")
            return None
        self.console.print("[dim]Using WebRTC VAD[/dim]")
        return webrtcvad.Vad(2)

    def _display_voice(self) -> str:
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
