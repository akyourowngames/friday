"""Continuous voice mode entry point.

This module provides a continuous voice mode using VAD (voice activity detection)
with faster-whisper for STT and Sarvam/Edge TTS for responses. It runs the full
Ares agent pipeline (LLM + tools) for each voice interaction.
"""

from __future__ import annotations

import asyncio
import collections
import threading
import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.panel import Panel

from ares.config import load_config
from ares.voice.tts import voice_config_from_env, create_tts_provider
from ares.voice.stt import STTEngine, trim_silence_pcm16
from ares.voice.player import play_audio_bytes

_NATIVE_SR = 44100       # device native sample rate
_TARGET_SR = 16000       # VAD / STT rate
_FRAME_MS = 30
_FRAME_SAMPLES = int(_TARGET_SR * _FRAME_MS / 1000)  # 480 samples per VAD frame
_DEVICE_INDEX = 1        # Microphone Array (Realtek Audio)
_BLOCK_SIZE = 4410       # 100ms at 44100 Hz — how much audio we read per blocking call


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

        self.console.print(f"[dim green]Mic OK — {n} frames so far[/dim green]")

        try:
            while True:
                try:
                    self.console.print("[dim cyan]Listening…[/dim cyan]")
                    speech = await self._wait_for_speech()
                    if not speech:
                        continue

                    audio = np.concatenate(speech)
                    audio = trim_silence_pcm16(audio, _TARGET_SR)
                    duration = len(audio) / _TARGET_SR
                    self.console.print(f"[dim]Captured {duration:.1f}s[/dim]")

                    if duration < 0.3:
                        continue

                    self.console.print("[yellow]Transcribing…[/yellow]")
                    text = await asyncio.to_thread(self.stt.transcribe_pcm16, audio, _TARGET_SR)
                    if not text or not text.strip():
                        continue
                    self.console.print(f"[bold]You:[/bold] {text}")

                    self.console.print("[yellow]Thinking…[/yellow]")
                    response = await self._get_agent_response(text)
                    if not response or not response.strip():
                        continue
                    self.console.print(f"[bold green]Ares:[/bold green] {response}")

                    self.console.print("[yellow]Speaking…[/yellow]")
                    audio_bytes = await self.tts.speak(response, self.voice_config.tts_voice)
                    if audio_bytes:
                        await play_audio_bytes(audio_bytes, speed=1.2)

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
        poll = 0
        while True:
            frames = self._read_frames(1)
            if frames:
                frame = frames[0]
                is_sp = self._is_speech(frame)
                poll += 1
                if poll <= 10 or poll % 50 == 0:
                    energy = float(np.mean(frame ** 2))
                    with self._lock:
                        q = len(self._frame_q)
                        total = self._total_frames
                    self.console.print(
                        f"[dim]#{poll} energy={energy:.8f} speech={is_sp} queue={q} total={total}[/dim]"
                    )
                if is_sp:
                    speech_buf = list(frames)
                    self.console.print(f"[green]>>> Speech detected at frame #{poll}[/green]")
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
                    dur = len(speech_buf) * _FRAME_MS / 1000
                    self.console.print(f"[green]>>> Utterance: {len(speech_buf)} frames, {dur:.1f}s[/green]")
                    self._drain()
                    return speech_buf

    # ------------------------------------------------------------------ #
    # Agent pipeline
    # ------------------------------------------------------------------ #

    async def _get_agent_response(self, text: str) -> str:
        try:
            from ares.agent import Agent
            from ares.memory import MemoryStore
            from ares.tools.tasks import TaskStore
            from ares.conversations import ConversationStore

            agent = Agent(
                memory_store=MemoryStore(),
                task_store=TaskStore(),
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


async def run_voice_agent(tts_provider: str = None) -> None:
    agent = ContinuousVoiceAgent(tts_provider)
    await agent.listen_and_respond()
