"""Push-to-talk microphone capture for the terminal CLI."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

import numpy as np

from ares.models import VoiceConfig
from ares.voice.stt import STTEngine, trim_silence_pcm16
from ares.voice.tts import voice_config_from_env

TranscriptHandler = Callable[[str], Awaitable[None]]


class PushToTalkService:
    """Capture audio while a key is held and submit transcripts to a callback."""

    def __init__(self, config: VoiceConfig, on_transcript: TranscriptHandler) -> None:
        self.config = voice_config_from_env(config)
        self.on_transcript = on_transcript
        self.sample_rate = 16000
        self.channels = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener = None
        self._stream = None
        self._frames: list[np.ndarray] = []
        self._recording = False
        self._lock = threading.Lock()
        self._stt = STTEngine(self.config.stt_model)

    def start(self) -> None:
        """Start the global hotkey listener (pynput) with fallback logging."""
        try:
            from pynput import keyboard

            self._loop = asyncio.get_running_loop()
            key_name = self.config.hotkey.lower()

            def matches(key) -> bool:
                if key_name == "space":
                    return key == keyboard.Key.space
                return getattr(key, "char", None) == key_name

            def on_press(key) -> None:
                if matches(key):
                    self._start_recording()

            def on_release(key) -> None:
                if matches(key):
                    self._stop_recording()

            self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._listener.daemon = True
            self._listener.start()
        except Exception as exc:
            # pynput may not work in all terminal environments; log and continue
            # Voice capture via CLI prompt fallback is handled elsewhere.
            import logging
            logging.getLogger(__name__).warning(
                "pynput hotkey listener failed to start: %s. "
                "Use the CLI prompt to send voice commands instead.", exc,
            )

    def close(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._stop_stream()

    def _start_recording(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._recording = True
        import sounddevice as sd

        def callback(indata, _frames, _time, _status) -> None:
            with self._lock:
                if self._recording:
                    self._frames.append(indata.copy().reshape(-1))

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    def _stop_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()
            stream.close()

    def _stop_recording(self) -> None:
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            frames = list(self._frames)
            self._frames = []
        self._stop_stream()
        if not frames or self._loop is None:
            return
        samples = np.concatenate(frames).astype(np.float32)
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._transcribe(samples)))

    async def _transcribe(self, samples: np.ndarray) -> None:
        samples = trim_silence_pcm16(samples, sample_rate=self.sample_rate)
        if samples.size < self.sample_rate // 4:
            return
        text = await asyncio.to_thread(self._stt.transcribe_pcm16, samples, self.sample_rate)
        if text:
            await self.on_transcript(text)
