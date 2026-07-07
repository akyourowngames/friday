"""Continuous voice mode entry point.

This module provides a continuous voice mode using VAD (voice activity detection)
with faster-whisper for STT and Sarvam/Edge TTS for responses. It runs the full
Ares agent pipeline (LLM + tools) for each voice interaction.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import threading
import numpy as np
from rich.console import Console
from rich.panel import Panel

from ares.config import load_config
from ares.voice.tts import voice_config_from_env, create_tts_provider
from ares.voice.stt import STTEngine, trim_silence_pcm16
from ares.voice.player import audio_bytes_to_pcm16, play_audio_bytes, play_audio_stream

_NATIVE_SR = 44100       # device native sample rate
_TARGET_SR = 16000       # VAD / STT rate
_FRAME_MS = 30
_FRAME_SAMPLES = int(_TARGET_SR * _FRAME_MS / 1000)  # 480 samples per VAD frame
_DEVICE_INDEX = 1        # Microphone Array (Realtek Audio)
_BLOCK_SIZE = 4410       # 100ms at 44100 Hz — how much audio we read per blocking call
_MAX_SENTENCE_CHARS = 200
_MAX_VOICE_HISTORY = 10
_TTS_SAMPLE_RATE = 24000


class ContinuousVoiceAgent:
    """Continuous voice agent with VAD-based listening and full Ares agent pipeline."""

    def __init__(self, tts_provider_name: str = None):
        self.console = Console()
        self.config = load_config()
        self.voice_config = voice_config_from_env(self.config.voice)

        if tts_provider_name:
            self.voice_config.tts_provider = tts_provider_name

        self.stt = STTEngine(self.voice_config.stt_model)
        self.tts = create_tts_provider(self.voice_config)
        self.conversation_history: list[dict] = []
        self.agent = None

        # VAD
        self.vad = None
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(2)
            self.console.print("[dim]Using WebRTC VAD[/dim]")
        except ImportError:
            self.console.print("[yellow]webrtcvad not installed, using energy VAD[/yellow]")

        # Thread-safe queue of 480-sample frames at 16kHz
        self._frame_q: collections.deque[np.ndarray] = collections.deque()
        self._lock = threading.Lock()
        self._total_frames = 0
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ #
    # Blocking-read capture thread
    # ------------------------------------------------------------------ #

    def _capture_thread(self) -> None:
        """Runs in a background thread: blocking reads → resample → queue VAD frames."""
        # Resample buffer: holds leftover samples between blocks
        resample_buf = np.array([], dtype=np.float32)

        import sounddevice as sd

        with sd.InputStream(
            device=_DEVICE_INDEX,
            samplerate=_NATIVE_SR,
            channels=1,
            dtype="float32",
            blocksize=_BLOCK_SIZE,
        ) as stream:
            while not self._stop_event.is_set():
                # Blocking read — returns exactly _BLOCK_SIZE samples
                data, overflowed = stream.read(_BLOCK_SIZE)
                if overflowed:
                    self.console.print("[red]Audio buffer overflow![/red]")

                mono = data[:, 0] if data.ndim > 1 else data.reshape(-1)

                # Resample: 44100 → 16000
                ratio = _TARGET_SR / _NATIVE_SR
                # Append new samples to leftover buffer
                resample_buf = np.concatenate([resample_buf, mono])
                target_len = int(len(resample_buf) * ratio)
                if target_len >= _FRAME_SAMPLES:
                    indices = np.linspace(0, len(resample_buf) - 1, target_len)
                    resampled = np.interp(indices, np.arange(len(resample_buf)), resample_buf).astype(np.float32)
                    # How many samples did we actually consume from resample_buf?
                    consumed = int(target_len / ratio)
                    resample_buf = resample_buf[consumed:]

                    # Split into 480-sample VAD frames
                    for i in range(0, len(resampled) - _FRAME_SAMPLES + 1, _FRAME_SAMPLES):
                        frame = resampled[i : i + _FRAME_SAMPLES]
                        with self._lock:
                            self._frame_q.append(frame.copy())
                            self._total_frames += 1

    # ------------------------------------------------------------------ #
    # Queue helpers
    # ------------------------------------------------------------------ #

    def _read_frames(self, count: int) -> list[np.ndarray]:
        with self._lock:
            out = []
            for _ in range(min(count, len(self._frame_q))):
                out.append(self._frame_q.popleft())
            return out

    def _drain(self) -> None:
        with self._lock:
            self._frame_q.clear()

    def _is_speech(self, frame: np.ndarray) -> bool:
        if self.vad:
            try:
                pcm16 = (frame * 32767).astype(np.int16)
                return self.vad.is_speech(pcm16.tobytes(), _TARGET_SR)
            except Exception:
                return False
        return float(np.mean(frame ** 2)) > 0.0005

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    async def listen_and_respond(self) -> None:
        self.console.print(Panel(
            f"[bold green]Continuous voice mode active[/bold green]\n"
            f"TTS: [cyan]{self.voice_config.tts_provider}[/cyan]  "
            f"Voice: [cyan]{self.voice_config.tts_voice}[/cyan]\n"
            f"Speak naturally — I'm always listening.\n"
            f"[dim]Press Ctrl+C to exit[/dim]",
            title="Ares Voice",
            border_style="green",
        ))

        # Start capture thread
        cap_thread = threading.Thread(target=self._capture_thread, daemon=True)
        cap_thread.start()

        # Wait for frames to appear
        self.console.print("[dim]Waiting for mic…[/dim]")
        for _ in range(100):  # 5 seconds
            await asyncio.sleep(0.05)
            with self._lock:
                n = self._total_frames
            if n > 0:
                break
        else:
            self.console.print("[red]ERROR: No mic data after 5s. Check Windows mic permissions.[/red]")
            self._stop_event.set()
            return

        self.console.print("[dim green]Mic OK[/dim green]")

        try:
            while True:
                try:
                    self.console.print("[dim cyan]Listening…[/dim cyan]")
                    speech = await self._wait_for_speech()
                    if not speech:
                        continue

                    audio = np.concatenate(speech)
                    audio = trim_silence_pcm16(audio, _TARGET_SR)

                    if duration < 0.3:
                        continue

                    self.console.print("[yellow]Transcribing…[/yellow]")
                    text = await asyncio.to_thread(self.stt.transcribe_pcm16, audio, _TARGET_SR)
                    if not text or not text.strip():
                        continue
                    self.console.print(f"[bold]You:[/bold] {text}")

                    self.console.print("[yellow]Thinking…[/yellow]")
                    response = await self._respond(text)
                    if response and response.strip():
                        self._remember_exchange(text, response)

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self.console.print(f"[red]Error: {type(e).__name__}: {e}[/red]")
                    await asyncio.sleep(0.5)
        finally:
            self._stop_event.set()

    # ------------------------------------------------------------------ #
    # VAD
    # ------------------------------------------------------------------ #

    async def _wait_for_speech(self) -> list[np.ndarray] | None:
        # Phase 1: wait for speech onset
        while True:
            frames = self._read_frames(1)
            if frames:
                if self._is_speech(frames[0]):
                    speech_buf = list(frames)
                    break
            else:
                await asyncio.sleep(0.01)

        # Phase 2: collect until silence
        silence = 0
        max_silence = 30  # 900ms
        min_frames = 5    # 150ms

        while True:
            frames = self._read_frames(1)
            if not frames:
                await asyncio.sleep(0.005)
                continue
            frame = frames[0]
            if self._is_speech(frame):
                silence = 0
                speech_buf.append(frame)
            else:
                silence += 1
                speech_buf.append(frame)
                if silence >= max_silence and len(speech_buf) >= min_frames:
                    self._drain()
                    return speech_buf

    # ------------------------------------------------------------------ #
    # Agent pipeline
    # ------------------------------------------------------------------ #

    def _get_or_create_agent(self):
        """Create one Agent per voice session so memory and clients are reused."""
        if self.agent is None:
            from ares.agent import Agent
            from ares.memory import MemoryStore
            from ares.conversations import ConversationStore

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
        self.conversation_history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ])
        max_history = getattr(self.voice_config, "voice_max_history", _MAX_VOICE_HISTORY)
        if len(self.conversation_history) > max_history:
            self.conversation_history = self.conversation_history[-max_history:]

    async def _stream_to_sentences(self, text: str, sentence_q: asyncio.Queue[str | None]) -> str:
        """Stream LLM tokens to the console and emit sentence-sized TTS chunks."""
        agent = getattr(self, "agent", None) or self._get_or_create_agent()
        history = getattr(self, "conversation_history", [])
        buffer = ""
        full_response = ""
        first_token = True
        sentence_count = 0

        async for token in agent.run_stream(text, history):
            if token.startswith("[tool:"):
                continue
            if first_token:
                self.console.print("[bold green]Ares:[/bold green] ", end="")
                first_token = False
            self.console.print(token, end="", highlight=False)
            buffer += token
            full_response += token

            if self._sentence_ready(buffer):
                sentence_count += 1
                self.console.print(f"\n[dim]  [sentence {sentence_count}: {len(buffer.strip())} chars][/dim]", end="")
                await sentence_q.put(buffer.strip())
                buffer = ""

        if buffer.strip():
            sentence_count += 1
            await sentence_q.put(buffer.strip())
        await sentence_q.put(None)
        self.console.print(f"\n[dim]  [total: {sentence_count} sentences][/dim]")
        if not first_token:
            self.console.print()
        return full_response

    def _sentence_ready(self, text: str) -> bool:
        stripped = text.rstrip()
        return len(stripped) >= _MAX_SENTENCE_CHARS or stripped.endswith(('.', '!', '?', '\n'))

    async def _tts_play_pipeline(self, sentence_q: asyncio.Queue[str | None], stop_event: asyncio.Event) -> None:
        """Convert sentence chunks to audio and play them as soon as they are ready."""
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        play_task = asyncio.create_task(
            play_audio_stream(audio_q, stop_event, sample_rate=_TTS_SAMPLE_RATE)
        )
        self.console.print("[dim]TTS pipeline started[/dim]")
        try:
            while not stop_event.is_set():
                sentence = await sentence_q.get()
                if sentence is None:
                    self.console.print("[dim]TTS: end of stream[/dim]")
                    await audio_q.put(None)
                    break
                self.console.print(f"[dim]TTS: got sentence ({len(sentence)} chars)[/dim]")
                encoded = bytearray()
                async for chunk in self.tts.speak_stream(sentence, self.voice_config.tts_voice):
                    if stop_event.is_set():
                        break
                    encoded.extend(chunk)
                self.console.print(f"[dim]TTS: encoded {len(encoded)} bytes[/dim]")
                if encoded and not stop_event.is_set():
                    pcm = audio_bytes_to_pcm16(bytes(encoded), sample_rate=_TTS_SAMPLE_RATE, speed=1.2)
                    self.console.print(f"[dim]TTS: PCM {len(pcm)} bytes, putting in queue[/dim]")
                    await audio_q.put(pcm)
        finally:
            if stop_event.is_set():
                self.console.print("[dim]TTS: cancelled (barge-in)[/dim]")
                play_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await play_task
            else:
                self.console.print("[dim]TTS: waiting for playback to finish[/dim]")
                await play_task

    async def _barge_in_watcher(self, stop_event: asyncio.Event, play_start: float) -> None:
        """Watch mic frames during playback and stop speech when the user talks."""
        self.console.print("[dim]Barge-in: waiting 500ms...[/dim]")
        while asyncio.get_event_loop().time() - play_start < 0.5:
            if stop_event.is_set():
                return
            await asyncio.sleep(0.05)
        self.console.print("[dim]Barge-in: monitoring active[/dim]")

        consecutive_speech = 0
        while not stop_event.is_set():
            frames = self._read_frames(1)
            if not frames:
                await asyncio.sleep(0.005)
                continue
            if self._is_speech(frames[0]):
                consecutive_speech += 1
                if consecutive_speech >= 2:
                    self.console.print("[yellow]>>> Barge-in detected[/yellow]")
                    stop_event.set()
                    self._drain()
                    return
            else:
                consecutive_speech = 0

    async def _respond(self, text: str) -> str:
        """Run LLM streaming, sentence TTS, playback, and barge-in concurrently."""
        sentence_q: asyncio.Queue[str | None] = asyncio.Queue()
        stop_event = asyncio.Event()
        self.console.print("[dim]Respond: starting pipeline[/dim]")
        stream_task = asyncio.create_task(self._stream_to_sentences(text, sentence_q))
        tts_task = asyncio.create_task(self._tts_play_pipeline(sentence_q, stop_event))
        barge_task = asyncio.create_task(self._barge_in_watcher(stop_event, asyncio.get_event_loop().time()))
        try:
            full_response = await stream_task
            self.console.print(f"[dim]Respond: stream done, barge={stop_event.is_set()}[/dim]")
            if stop_event.is_set():
                tts_task.cancel()
                return full_response
            await tts_task
            self.console.print("[dim]Respond: playback done[/dim]")
            return full_response
        finally:
            barge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await barge_task

    async def _get_agent_response(self, text: str) -> str:
        try:
            from ares.agent import Agent
            from ares.memory import MemoryStore
            from ares.conversations import ConversationStore

            agent = Agent(
                memory_store=MemoryStore(),
                conversation_store=ConversationStore(),
                api_key=self.config.api_key,
                base_url=self.config.api_base_url,
                model=self.config.model,
                config=self.config,
            )

            full = ""
            async for token in agent.run_stream(text, []):
                if not token.startswith("[tool:"):
                    full += token
            return full

        except Exception as e:
            self.console.print(f"[red]Agent error: {e}[/red]")
            return f"Sorry, I encountered an error: {e}"

    async def _stream_agent_response(self, text: str) -> str:
        """Stream agent response tokens to console in real-time."""
        try:
            agent = self._get_or_create_agent()

            full = ""
            first_token = True
            async for token in agent.run_stream(text, self.conversation_history):
                if token.startswith("[tool:"):
                    continue
                if first_token:
                    self.console.print(f"[bold green]Ares:[/bold green] ", end="")
                    first_token = False
                self.console.print(token, end="", highlight=False)
                full += token

            if not first_token:
                self.console.print()  # newline after streaming
            return full

        except Exception as e:
            self.console.print(f"\n[red]Agent error: {e}[/red]")
            return f"Sorry, I encountered an error: {e}"


async def run_voice_agent(tts_provider: str = None) -> None:
    agent = ContinuousVoiceAgent(tts_provider)
    await agent.listen_and_respond()
