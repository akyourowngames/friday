"""Desktop voice agent — orchestrates tray, window, hotkey, and voice pipeline.

This is the main entry point for ``python -m ares --desktop``. It wires together
the system tray icon, floating status window, global hotkey listener, and the
existing voice pipeline (MicrophoneFrames, WhisperTranscriber, EdgeTTS,
play_audio_stream) with the existing Agent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any

import numpy as np

from ares.config import load_config
from ares.desktop.history import HistoryStore
from ares.desktop.hotkey import HotkeyListener
from ares.desktop.tray import TrayIcon
from ares.desktop.window import StatusState, StatusWindow
from ares.voice.player import audio_bytes_to_pcm16, play_audio_stream
from ares.voice.stt import WhisperTranscriber, trim_silence
from ares.voice.tts import DEFAULT_EDGE_VOICE, EdgeTTS

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_FRAME_MS = 30
_FRAME_SAMPLES = int(_SAMPLE_RATE * _FRAME_MS / 1000)


class DesktopVoiceAgent:
    """Background desktop voice assistant for Ares."""

    def __init__(self) -> None:
        self.config = load_config()
        self.desktop_config = self.config.desktop
        self.voice_config = self.config.voice

        self._tts = EdgeTTS(self.voice_config.tts_voice or DEFAULT_EDGE_VOICE)
        self._transcriber = WhisperTranscriber(
            self.voice_config.stt_model or "small",
            language=self.voice_config.stt_language,
        )
        self._tts_sample_rate = self.voice_config.tts_sample_rate

        self._history = HistoryStore(max_size=self.desktop_config.history_size)
        self._muted = False
        self._agent = None
        self._conversation_history: list[dict[str, str]] = []

        self._window: StatusWindow | None = None
        self._tray: TrayIcon | None = None
        self._hotkey: HotkeyListener | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()

        # Push-to-talk state
        self._ptt_active = False
        self._ptt_frames: list[np.ndarray] = []
        self._ptt_lock = threading.Lock()
        self._capture = None
        self._processing = False

    async def run(self) -> None:
        """Start the desktop voice agent and run until stopped."""
        self._loop = asyncio.get_running_loop()

        self._window = StatusWindow(opacity=self.desktop_config.window_opacity)
        self._window.set_state(StatusState.IDLE)

        self._tray = TrayIcon(
            on_new_session=self._handle_new_session,
            on_status=self._handle_status,
            on_mute_toggle=self._handle_mute_toggle,
            on_quit=self._handle_quit,
            history_provider=lambda: self._history.recent(),
            mute_state_provider=lambda: self._muted,
        )
        self._tray.start()

        self._hotkey = HotkeyListener(
            hotkey_ptt=self.desktop_config.hotkey_ptt,
            hotkey_mute=self.desktop_config.hotkey_mute,
            hotkey_window=self.desktop_config.hotkey_window,
        )
        self._hotkey.set_callbacks(
            ptt=self._handle_ptt_press,
            ptt_release=self._handle_ptt_release,
            mute=self._handle_mute_toggle,
            window=self._handle_window_toggle,
        )
        self._hotkey.start()

        logger.info("Desktop voice agent started. Press %s to talk.", self.desktop_config.hotkey_ptt)
        await self._stop_event.wait()
        self._cleanup()

    def _cleanup(self) -> None:
        if self._hotkey:
            self._hotkey.stop()
        if self._tray:
            self._tray.stop()
        if self._window:
            self._window.destroy()
        if self._capture is not None:
            try:
                self._capture.close()
            except Exception:
                pass

    # -- Hotkey handlers (called from pynput threads) --

    def _handle_ptt_press(self) -> None:
        if self._processing:
            return
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._start_recording(), self._loop)

    def _handle_ptt_release(self) -> None:
        if not self._ptt_active:
            return
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_recording_and_process(), self._loop)

    def _handle_mute_toggle(self) -> None:
        self._muted = not self._muted
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._update_mute_state(), self._loop)

    def _handle_window_toggle(self) -> None:
        if self._window:
            if self._window._visible:
                self._window.hide()
            else:
                self._window.show()

    def _handle_new_session(self) -> None:
        self._conversation_history.clear()
        logger.info("New session started")

    def _handle_status(self) -> None:
        if self._window:
            self._window.show()

    def _handle_quit(self) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    # -- Voice pipeline --

    async def _start_recording(self) -> None:
        from ares.voice.agent import MicrophoneFrames

        self._ptt_active = True
        with self._ptt_lock:
            self._ptt_frames.clear()

        self._capture = MicrophoneFrames(
            sample_rate=_SAMPLE_RATE,
            frame_samples=_FRAME_SAMPLES,
            device=self.voice_config.mic_device,
            max_seconds=30,
        )
        self._capture.start()

        if self._window:
            self._window.set_state(StatusState.LISTENING)
        if self._tray:
            self._tray.set_state("listening")

        # Read frames while PTT is active
        while self._ptt_active:
            raw = self._capture.read(1)
            if raw:
                with self._ptt_lock:
                    self._ptt_frames.extend(raw)
            await asyncio.sleep(0.01)

    async def _stop_recording_and_process(self) -> None:
        self._ptt_active = False

        if self._capture is not None:
            self._capture.close()
            self._capture = None

        with self._ptt_lock:
            frames = list(self._ptt_frames)
            self._ptt_frames.clear()

        if not frames:
            if self._window:
                self._window.set_state(StatusState.IDLE)
            if self._tray:
                self._tray.set_state("idle")
            return

        audio = np.concatenate(frames).astype(np.float32)
        audio = trim_silence(audio, _SAMPLE_RATE)

        duration_ms = len(audio) * 1000 / _SAMPLE_RATE
        if duration_ms < self.voice_config.min_utterance_ms:
            if self._window:
                self._window.set_state(StatusState.IDLE)
            if self._tray:
                self._tray.set_state("idle")
            return

        self._processing = True
        try:
            await self._transcribe_and_respond(audio)
        finally:
            self._processing = False

    async def _transcribe_and_respond(self, audio: np.ndarray) -> None:
        try:
            if self._window:
                self._window.set_state(StatusState.THINKING)
            if self._tray:
                self._tray.set_state("thinking")

            text = await asyncio.to_thread(
                self._transcriber.transcribe_samples, audio, _SAMPLE_RATE
            )
            text = text.strip()
            if not text:
                if self._window:
                    self._window.set_state(StatusState.IDLE)
                if self._tray:
                    self._tray.set_state("idle")
                return

            logger.info("User said: %s", text)

            response = await self._get_response(text)
            if not response:
                if self._window:
                    self._window.set_state(StatusState.IDLE)
                if self._tray:
                    self._tray.set_state("idle")
                return

            self._history.add(text, response)

            if not self._muted:
                if self._window:
                    self._window.set_state(StatusState.SPEAKING)
                if self._tray:
                    self._tray.set_state("speaking")
                await self._speak(response)

            if self._window:
                self._window.set_state(StatusState.IDLE)
            if self._tray:
                self._tray.set_state("idle")

        except Exception as exc:
            logger.exception("Push-to-talk processing failed")
            if self._window:
                self._window.set_state(StatusState.ERROR, "Error occurred")
            if self._tray:
                self._tray.set_state("idle")

    async def _update_mute_state(self) -> None:
        state = StatusState.MUTED if self._muted else StatusState.IDLE
        if self._window:
            self._window.set_state(state)
        if self._tray:
            self._tray.set_state("muted" if self._muted else "idle")
        logger.info("TTS %s", "muted" if self._muted else "unmuted")

    async def _get_response(self, text: str) -> str:
        agent = self._get_or_create_agent()
        response_parts: list[str] = []
        async for token in agent.run_stream(text, self._conversation_history):
            if token.startswith("[tool"):
                continue
            response_parts.append(token)

        response = "".join(response_parts).strip()
        if response:
            self._conversation_history.append({"role": "user", "content": text})
            self._conversation_history.append({"role": "assistant", "content": response})
            max_history = self.voice_config.voice_max_history
            if len(self._conversation_history) > max_history:
                self._conversation_history = self._conversation_history[-max_history:]
        return response

    def _get_or_create_agent(self) -> Any:
        if self._agent is None:
            from ares.agent import Agent
            from ares.context.conversations import ConversationStore
            from ares.memory import MemoryStore

            self._agent = Agent(
                memory_store=MemoryStore(),
                conversation_store=ConversationStore(),
                api_key=self.config.api_key,
                base_url=self.config.api_base_url,
                model=self.config.model,
                config=self.config,
                is_voice_session=True,
            )
        return self._agent

    async def _speak(self, text: str) -> None:
        text = self._sanitize_tts_text(text)
        if not text:
            return

        voice = self.voice_config.tts_voice or DEFAULT_EDGE_VOICE
        encoded = await self._tts.synthesize(text, voice)
        if not encoded:
            return

        pcm = audio_bytes_to_pcm16(
            encoded, sample_rate=self._tts_sample_rate, speed=1.08
        )
        volume = max(0.1, float(self.voice_config.tts_volume))
        if volume != 1.0 and pcm:
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            samples = np.clip(samples * volume, -32768, 32767).astype(np.int16)
            pcm = samples.tobytes()

        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        await q.put(pcm)
        await q.put(None)
        stop = asyncio.Event()
        await play_audio_stream(q, stop, sample_rate=self._tts_sample_rate)

    @staticmethod
    def _sanitize_tts_text(text: str) -> str:
        text = re.sub(r"\[[^\]]+\]", " ", text or "")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        if not re.search(r"[A-Za-z0-9]", text):
            return ""
        return text


async def run_desktop() -> None:
    """Entry point for ``python -m ares --desktop``."""
    agent = DesktopVoiceAgent()
    await agent.run()
