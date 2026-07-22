"""Desktop voice agent — orchestrates tray, window, hotkey, and voice pipeline.

This is the main entry point for ``python -m ares --desktop``. It wires together
the system tray icon, floating status window, global hotkey listener, and the
existing voice pipeline (MicrophoneFrames, WhisperTranscriber, EdgeTTS,
play_audio_stream) with the existing Agent.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import ctypes
import logging
import os
import re
import threading
import unicodedata
from typing import Any

import numpy as np

from ares.config import load_config, save_config
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


class DesktopAlreadyRunningError(RuntimeError):
    """Raised when another Ares desktop voice process owns the singleton."""


@contextlib.contextmanager
def _desktop_instance_guard(
    mutex_name: str = "Local\\Ares.DesktopVoice.v1",
):
    """Hold a crash-safe per-user Windows mutex for desktop mode."""
    if os.name != "nt":
        yield
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise DesktopAlreadyRunningError(
            "Ares desktop is already running. Use its tray icon instead of starting a second copy."
        )
    try:
        yield
    finally:
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


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
        self._audio_task: asyncio.Task[None] | None = None
        self._command_task: asyncio.Task[None] | None = None
        self._stt_warmup_task: asyncio.Task[None] | None = None
        self._command_queue: asyncio.Queue[str] = asyncio.Queue()
        self._active_turn_task: asyncio.Task[Any] | None = None
        self._speech_stop_event: asyncio.Event | None = None
        self._speaking = False
        self._speaking_started_at = 0.0
        self._barge_voiced_frames = 0
        self._barge_in_progress = False
        self._barge_candidate_frames: collections.deque[np.ndarray] = (
            collections.deque(maxlen=40)
        )

        # Always-on local wake-word state. Audio is transcribed locally by the
        # same Whisper backend used for push-to-talk.
        # Keep 600 ms so Bluetooth packet latency does not clip the beginning
        # of "Hey Jarvis" before the speech gate opens.
        self._wake_pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=20)
        self._wake_speech: list[np.ndarray] = []
        self._wake_consecutive_speech = 0
        self._wake_voiced_frames = 0
        self._wake_silence_frames = 0
        self._wake_transcribing = False
        self._wake_task: asyncio.Task[None] | None = None
        self._wake_armed_until = 0.0
        self._wake_detector: Any | None = None
        self._command_vad = self._create_command_vad()
        self._ambient_rms_samples: collections.deque[float] = collections.deque(
            maxlen=200
        )

    async def run(self) -> None:
        """Start the desktop voice agent and run until stopped."""
        self._loop = asyncio.get_running_loop()

        # StatusWindow owns a small child process whose main thread runs Tk.
        # Its event loop cannot block voice, tray, or other desktop apps.
        self._window = StatusWindow(
            opacity=self.desktop_config.window_opacity,
            window_x=self.desktop_config.window_x,
            window_y=self.desktop_config.window_y,
            auto_hide_seconds=self.desktop_config.auto_hide_seconds,
            hotkey_label=self.desktop_config.hotkey_ptt.replace("+", " + ").title(),
            wake_word_hint=(
                self.desktop_config.wake_words[0].title()
                if self.desktop_config.wake_word_enabled and self.desktop_config.wake_words
                else ""
            ),
        )
        try:
            self._window.set_state(StatusState.IDLE)

            self._tray = TrayIcon(
                on_new_session=self._handle_new_session,
                on_status=self._handle_status,
                on_mute_toggle=self._handle_mute_toggle,
                on_wake_toggle=self._handle_wake_toggle,
                on_barge_toggle=self._handle_barge_toggle,
                on_quit=self._handle_quit,
                history_provider=lambda: self._history.recent(),
                mute_state_provider=lambda: self._muted,
                wake_state_provider=lambda: self.desktop_config.wake_word_enabled,
                barge_state_provider=lambda: self.voice_config.barge_in_enabled,
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

            self._window.set_state(StatusState.THINKING, "Connecting microphone…")
            await self._start_audio_capture()
            if self.desktop_config.wake_word_enabled:
                self._window.set_state(
                    StatusState.THINKING, "Loading Hey Jarvis detector…"
                )
                await self._start_wake_word_detector()
            self._window.set_state(StatusState.IDLE)
            self._audio_task = asyncio.create_task(
                self._audio_loop(), name="ares-desktop-audio"
            )
            self._command_task = asyncio.create_task(
                self._command_loop(), name="ares-desktop-commands"
            )
            # Wake-word processing is live before this background optimization
            # starts. The model lock makes an early command safely wait for the
            # same load instead of racing a second Whisper instance.
            self._stt_warmup_task = asyncio.create_task(
                self._warmup_transcriber(), name="ares-desktop-stt-warmup"
            )

            wake_hint = (
                f" or say {self.desktop_config.wake_words[0]!r}"
                if self.desktop_config.wake_word_enabled and self.desktop_config.wake_words
                else ""
            )
            logger.info(
                "Desktop voice agent started. Press %s to talk%s.",
                self.desktop_config.hotkey_ptt,
                wake_hint,
            )
            await self._stop_event.wait()
        finally:
            # asyncio.run() cancels its main task during Ctrl+C. Always stop
            # every owned resource so no UI process or database lock survives.
            await self._cleanup()

    async def _cleanup(self) -> None:
        tasks = {
            task
            for task in (
                self._wake_task,
                self._audio_task,
                self._command_task,
                self._stt_warmup_task,
                self._active_turn_task,
            )
            if task is not None and task is not asyncio.current_task()
        }
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._hotkey:
            self._hotkey.stop()
        if self._tray:
            self._tray.stop()
        if self._window:
            self._window.destroy()
        if self._capture is not None:
            try:
                await asyncio.to_thread(self._capture.close)
            except Exception:
                pass
        if self._agent is not None:
            try:
                await self._agent.close()
            except Exception:
                logger.exception("Desktop agent cleanup failed")
            finally:
                conversation_store = getattr(self._agent, "conversation_store", None)
                memory_store = getattr(self._agent, "memory_store", None)
                if conversation_store is not None:
                    conversation_store.close()
                if memory_store is not None:
                    memory_store.close()
                self._agent = None

    # -- Hotkey handlers (called from pynput threads) --

    def _handle_ptt_press(self) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._start_recording(), self._loop)

    def _handle_ptt_release(self) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._stop_recording_and_process(), self._loop)

    def _handle_mute_toggle(self) -> None:
        self._muted = not self._muted
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._update_mute_state(), self._loop)

    def _handle_window_toggle(self) -> None:
        if self._window:
            self._window.toggle()

    def _handle_wake_toggle(self) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._toggle_wake_word(), self._loop)

    def _handle_barge_toggle(self) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._toggle_barge_in(), self._loop)

    def _handle_new_session(self) -> None:
        self._conversation_history.clear()
        if self._window:
            self._window.clear_session()
            self._window.set_state(StatusState.IDLE)
        logger.info("New session started")

    def _handle_status(self) -> None:
        if self._window:
            self._window.show()

    def _handle_quit(self) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    # -- Voice pipeline --

    async def _start_audio_capture(self) -> None:
        from ares.voice.agent import MicrophoneFrames

        if self._capture is not None:
            return
        self._capture = MicrophoneFrames(
            sample_rate=_SAMPLE_RATE,
            frame_samples=_FRAME_SAMPLES,
            device=self.voice_config.mic_device,
            prefer_bluetooth=self.desktop_config.prefer_bluetooth_microphone,
            follow_system_default=self.desktop_config.follow_system_default_microphone,
            avoid_bluetooth=self.desktop_config.avoid_bluetooth_microphone,
            max_seconds=30,
        )
        # Bluetooth audio endpoints can take several seconds to switch into
        # hands-free mode. Never block the desktop asyncio/UI coordinator while
        # Windows opens that device.
        await asyncio.to_thread(self._capture.start)
        logger.info(
            "Desktop microphone selected: %s (device %s)",
            self._capture.device_name,
            self._capture.selected_device,
        )
        if self._window:
            self._window.set_microphone_name(self._capture.device_name)

    async def _start_wake_word_detector(self) -> None:
        if self._wake_detector is not None:
            return
        from ares.voice.wakeword import OpenWakeWordDetector

        self._wake_detector = await asyncio.to_thread(
            OpenWakeWordDetector,
            threshold=self.desktop_config.wake_detection_threshold,
        )
        logger.info(
            "openWakeWord Hey Jarvis detector ready (threshold %.2f)",
            self.desktop_config.wake_detection_threshold,
        )

    async def _warmup_transcriber(self) -> None:
        try:
            await asyncio.to_thread(self._transcriber.warmup)
            logger.info("Local Whisper model ready")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Local Whisper warmup failed")

    async def _audio_loop(self) -> None:
        """Route one microphone stream to PTT, wake-word, and barge-in logic."""
        while True:
            self._expire_wake_window()
            if self._capture is None:
                await asyncio.sleep(0.02)
                continue
            frames = self._capture.read(4)
            if not frames:
                await asyncio.sleep(0.005)
                continue
            for frame in frames:
                if self._ptt_active:
                    with self._ptt_lock:
                        self._ptt_frames.append(frame)
                    continue
                if self._consume_barge_in_frame(frame):
                    continue
                if self._wake_transcribing:
                    continue
                if self._wake_armed_until > 0:
                    self._consume_wake_frame(frame)
                elif self.desktop_config.wake_word_enabled:
                    if not self._speaking:
                        self._record_ambient_level(frame)
                    self._wake_pre_roll.append(frame)
                    if self._wake_detector and self._wake_detector.process(frame):
                        await self._handle_wake_word_detection()
                elif self._speaking:
                    # Keep only a short pre-roll while Ares is talking. Do not
                    # transcribe speaker echo as if it were the user's voice.
                    self._wake_pre_roll.append(frame)
            await asyncio.sleep(0)

    async def _handle_wake_word_detection(self) -> None:
        if self._loop is None:
            return
        await self._interrupt_current_turn(show_listening=True)
        detection_score = float(getattr(self._wake_detector, "last_score", 0.0))
        # Start the command recording from clean audio after the model has
        # consumed "Hey Jarvis". This avoids sending the wake phrase back to
        # Whisper as if it were the user's request.
        self._reset_wake_detector()
        self._wake_armed_until = self._loop.time() + float(
            self.desktop_config.wake_command_timeout_seconds
        )
        if self._window:
            self._window.set_state(StatusState.AWAKE, "Yes? I'm listening")
            self._window.set_transcript("Hey Jarvis")
        if self._tray:
            self._tray.set_state("awake")
        logger.info(
            "Hey Jarvis detected (score %.3f)",
            detection_score,
        )

    @staticmethod
    def _create_command_vad() -> Any | None:
        try:
            import webrtcvad

            # Mode 1 retains quiet/narrow-band Bluetooth speech better than
            # the aggressive modes while still rejecting steady background.
            return webrtcvad.Vad(1)
        except ImportError:
            return None

    def _frame_is_command_speech(self, frame: np.ndarray) -> bool:
        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        # Post-wake false positives are cheap; false rejections lose the entire
        # command. Use a deliberately sensitive floor for Bluetooth headsets.
        ambient = (
            float(np.median(self._ambient_rms_samples))
            if self._ambient_rms_samples
            else 0.0005
        )
        energy_speech = rms >= max(0.0007, ambient * 3.0)
        if self._command_vad is None or samples.size != _FRAME_SAMPLES:
            return energy_speech
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
        try:
            return bool(
                self._command_vad.is_speech(pcm16.tobytes(), _SAMPLE_RATE)
            ) or energy_speech
        except Exception:
            return energy_speech

    def _record_ambient_level(self, frame: np.ndarray) -> None:
        samples = np.asarray(frame, dtype=np.float32).reshape(-1)
        if samples.size:
            rms = float(np.sqrt(np.mean(samples**2)))
            self._ambient_rms_samples.append(rms)

    def _frame_is_speech(
        self,
        frame: np.ndarray,
        *,
        barge_in: bool = False,
        wake_word: bool = False,
    ) -> bool:
        rms = float(np.sqrt(np.mean(np.asarray(frame, dtype=np.float32) ** 2)))
        threshold = max(0.002, float(self.voice_config.min_audio_rms))
        if wake_word:
            threshold = max(
                0.0012,
                float(self.voice_config.min_audio_rms)
                * float(self.desktop_config.wake_sensitivity),
            )
        elif barge_in:
            # The former fixed 0.006 floor rejected normal speech from this
            # machine's Realtek array. Use the measured quiet-room level and a
            # conservative fraction of the configured speech floor instead.
            ambient = (
                float(np.median(self._ambient_rms_samples))
                if self._ambient_rms_samples
                else 0.0005
            )
            threshold = max(0.0012, ambient * 3.0, threshold * 0.65)
        return rms >= threshold

    def _consume_barge_in_frame(self, frame: np.ndarray) -> bool:
        """Detect an interruption and seed command capture with its opening."""
        if (
            not self._speaking
            or not self.voice_config.barge_in_enabled
            or self._barge_in_progress
        ):
            self._barge_voiced_frames = 0
            self._barge_candidate_frames.clear()
            return False
        if self._loop is None:
            return False
        delay = max(0, int(self.voice_config.barge_in_delay_ms)) / 1000
        if self._loop.time() - self._speaking_started_at < delay:
            return False
        if self._frame_is_speech(frame, barge_in=True):
            self._barge_voiced_frames += 1
            self._barge_candidate_frames.append(frame)
        else:
            self._barge_voiced_frames = 0
            self._barge_candidate_frames.clear()
        required = max(
            int(self.voice_config.start_speech_frames),
            int(np.ceil(max(30, self.voice_config.barge_in_min_voiced_ms) / _FRAME_MS)),
        )
        if self._barge_voiced_frames >= required:
            captured = list(self._barge_candidate_frames)
            self._barge_voiced_frames = 0
            self._barge_candidate_frames.clear()
            self._barge_in_progress = True
            self._reset_wake_detector()
            # Preserve the beginning of a short interruption such as "stop".
            # Without this, the detector consumed the word and then listened
            # only for whatever came after it, usually producing empty text.
            self._wake_pre_roll.extend(captured)
            self._wake_speech = captured
            self._wake_consecutive_speech = len(captured)
            self._wake_voiced_frames = len(captured)
            self._wake_silence_frames = 0
            self._wake_armed_until = self._loop.time() + float(
                self.desktop_config.wake_command_timeout_seconds
            )
            asyncio.create_task(
                self._interrupt_current_turn(show_listening=True),
                name="ares-desktop-barge-in",
            )
            return True
        return False

    def _expire_wake_window(self) -> None:
        if self._wake_armed_until <= 0 or self._loop is None:
            return
        if self._loop.time() <= self._wake_armed_until:
            return
        self._wake_armed_until = 0.0
        self._reset_wake_detector()
        if not self._processing and not self._ptt_active:
            if self._window:
                self._window.set_state(StatusState.IDLE)
            if self._tray:
                self._tray.set_state("idle")

    def _consume_wake_frame(self, frame: np.ndarray) -> None:
        if self._wake_transcribing:
            return
        self._wake_pre_roll.append(frame)
        is_speech = self._frame_is_command_speech(frame)
        if not self._wake_speech:
            if is_speech:
                self._wake_consecutive_speech += 1
                wake_start_frames = min(
                    2, max(1, int(self.voice_config.start_speech_frames))
                )
                if self._wake_consecutive_speech >= wake_start_frames:
                    self._wake_speech = list(self._wake_pre_roll)
                    self._wake_voiced_frames = self._wake_consecutive_speech
            else:
                self._wake_consecutive_speech = 0
            return

        self._wake_speech.append(frame)
        if is_speech:
            self._wake_voiced_frames += 1
            self._wake_silence_frames = 0
        else:
            self._wake_silence_frames += 1

        silence_limit = max(
            1, int(self.desktop_config.wake_silence_timeout_ms / _FRAME_MS)
        )
        max_frames = max(1, int(self.voice_config.max_utterance_seconds * 1000 / _FRAME_MS))
        if self._wake_silence_frames < silence_limit and len(self._wake_speech) < max_frames:
            return

        min_voiced = max(
            1, int(min(120, int(self.voice_config.min_voiced_ms)) / _FRAME_MS)
        )
        speech = self._wake_speech
        voiced = self._wake_voiced_frames
        self._reset_wake_detector()
        if voiced < min_voiced:
            return
        audio = trim_silence(np.concatenate(speech).astype(np.float32), _SAMPLE_RATE)
        if len(audio) * 1000 / _SAMPLE_RATE < self.desktop_config.wake_min_utterance_ms:
            return
        # The command has already been captured inside a valid wake window.
        # Stop the expiry clock while Whisper is working so model load or a
        # slow CPU can never invalidate the completed recording.
        self._wake_armed_until = 0.0
        if self._window:
            self._window.set_state(StatusState.THINKING, "Transcribing your request…")
        if self._tray:
            self._tray.set_state("thinking")
        self._wake_transcribing = True
        self._wake_task = asyncio.create_task(
            self._handle_wake_audio(audio), name="ares-desktop-wake-transcribe"
        )

    def _reset_wake_detector(self) -> None:
        self._wake_pre_roll.clear()
        self._wake_speech = []
        self._wake_consecutive_speech = 0
        self._wake_voiced_frames = 0
        self._wake_silence_frames = 0
        if self._wake_detector is not None:
            self._wake_detector.reset()

    async def _handle_wake_audio(self, audio: np.ndarray) -> None:
        try:
            text = await self._transcribe_command_audio(audio)
            text = str(text or "").strip()
            if self._loop is None:
                return
            if not text:
                self._rearm_after_empty_transcript()
                return
            command = self._command_after_wake_word(text)
            if command is not None:
                await self._interrupt_current_turn(show_listening=True)
                if command:
                    self._wake_armed_until = 0.0
                    if self._window:
                        self._window.set_transcript(command)
                    await self._command_queue.put(command)
                else:
                    self._rearm_after_empty_transcript()
                return
            # This audio was captured only after a confirmed wake activation;
            # it remains valid regardless of how long transcription took.
            self._wake_armed_until = 0.0
            if self._window:
                self._window.set_transcript(text)
            await self._command_queue.put(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Wake-word transcription failed")
            self._rearm_after_empty_transcript()
        finally:
            self._wake_transcribing = False
            self._wake_task = None

    async def _transcribe_command_audio(self, audio: np.ndarray) -> str:
        translate = bool(self.desktop_config.translate_speech_to_english)
        return await asyncio.to_thread(
            self._transcriber.transcribe_samples,
            audio,
            _SAMPLE_RATE,
            task="translate" if translate else "transcribe",
            multilingual=translate,
        )

    def _rearm_after_empty_transcript(self) -> None:
        if self._loop is None:
            return
        self._reset_wake_detector()
        self._wake_armed_until = self._loop.time() + float(
            self.desktop_config.wake_command_timeout_seconds
        )
        if self._window:
            self._window.set_state(
                StatusState.AWAKE, "I didn't catch that — please try again"
            )
        if self._tray:
            self._tray.set_state("awake")

    def _command_after_wake_word(self, text: str) -> str | None:
        patterns: list[str] = []
        for phrase in sorted(self.desktop_config.wake_words, key=len, reverse=True):
            raw_words = [word for word in str(phrase).strip().split() if word]
            words = [re.escape(word) for word in raw_words]
            if raw_words and raw_words[-1].casefold() == "jarvis":
                # These are common Whisper spellings from narrow-band
                # Bluetooth headset audio, not arbitrary fuzzy matches.
                words[-1] = r"(?:jarvis|jervis|jarves|jarviz)"
            if words:
                separator = r"(?:\s|[,.:;!?—-])+"
                patterns.append(r"\b" + separator.join(words) + r"\b")
        if not patterns:
            return None

        wake_pattern = re.compile("(?:" + "|".join(patterns) + ")", re.IGNORECASE)
        match = wake_pattern.search(text)
        if match is None:
            return None

        # Whisper may emit "Jarvis, hey Jarvis" when the phrase is repeated
        # or when Bluetooth audio overlaps two chunks.  Consume every leading
        # wake phrase so it never becomes a command sent to the agent.
        command = text[match.end():]
        trim_chars = " \t,.:;!?—-"
        while True:
            command = command.lstrip(trim_chars)
            repeated = wake_pattern.match(command)
            if repeated is None:
                break
            command = command[repeated.end():]
        return command.strip(trim_chars)

    async def _command_loop(self) -> None:
        while True:
            text = await self._command_queue.get()
            while self._processing:
                await asyncio.sleep(0.03)
            turn = asyncio.create_task(
                self._process_text_command(text), name="ares-desktop-wake-command"
            )
            try:
                await turn
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    turn.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await turn
                    raise
            except Exception:
                logger.exception("Wake-word command failed")
                if self._window:
                    self._window.set_state(StatusState.ERROR, "Voice command failed")
                if self._tray:
                    self._tray.set_state("idle")

    async def _process_text_command(self, text: str) -> None:
        current = asyncio.current_task()
        self._active_turn_task = current
        self._processing = True
        try:
            await self._respond_to_text(text)
        finally:
            self._processing = False
            if self._active_turn_task is current:
                self._active_turn_task = None

    async def _interrupt_current_turn(self, *, show_listening: bool = False) -> None:
        stop = self._speech_stop_event
        if stop is not None:
            stop.set()
        task = self._active_turn_task
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if show_listening and self._window:
            self._window.set_state(StatusState.LISTENING, "Interrupted — listening")
        if show_listening and self._tray:
            self._tray.set_state("listening")

    async def _start_recording(self) -> None:
        await self._interrupt_current_turn()
        wake_task = self._wake_task
        if wake_task is not None and wake_task is not asyncio.current_task() and not wake_task.done():
            wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wake_task
        self._wake_transcribing = False
        self._ptt_active = True
        with self._ptt_lock:
            self._ptt_frames.clear()
        self._reset_wake_detector()

        if self._window:
            self._window.set_state(StatusState.LISTENING)
        if self._tray:
            self._tray.set_state("listening")

    async def _stop_recording_and_process(self) -> None:
        self._ptt_active = False

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

        current = asyncio.current_task()
        self._active_turn_task = current
        self._processing = True
        try:
            await self._transcribe_and_respond(audio)
        finally:
            self._processing = False
            if self._active_turn_task is current:
                self._active_turn_task = None

    async def _transcribe_and_respond(self, audio: np.ndarray) -> None:
        try:
            if self._window:
                self._window.set_state(StatusState.THINKING)
            if self._tray:
                self._tray.set_state("thinking")

            text = await self._transcribe_command_audio(audio)
            text = text.strip()
            if not text:
                if self._window:
                    self._window.set_state(StatusState.IDLE)
                if self._tray:
                    self._tray.set_state("idle")
                return

            logger.info("User said: %s", text)
            await self._respond_to_text(text)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Push-to-talk processing failed")
            if self._window:
                self._window.set_state(StatusState.ERROR, "Error occurred")
            if self._tray:
                self._tray.set_state("idle")

    async def _respond_to_text(self, text: str) -> None:
        if self._window:
            self._window.set_transcript(text)
            self._window.set_state(StatusState.THINKING)
        if self._tray:
            self._tray.set_state("thinking")

        response = await self._get_response(text)
        if not response:
            if self._window:
                self._window.set_state(StatusState.IDLE)
            if self._tray:
                self._tray.set_state("idle")
            return

        self._history.add(text, response)
        if self._tray:
            self._tray.refresh_menu()
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

    async def _update_mute_state(self) -> None:
        state = StatusState.MUTED if self._muted else StatusState.IDLE
        if self._window:
            self._window.set_state(state)
        if self._tray:
            self._tray.set_state("muted" if self._muted else "idle")
            self._tray.refresh_menu()
        logger.info("TTS %s", "muted" if self._muted else "unmuted")

    async def _toggle_wake_word(self) -> None:
        self.desktop_config.wake_word_enabled = not self.desktop_config.wake_word_enabled
        save_config(self.config)
        self._wake_armed_until = 0.0
        self._reset_wake_detector()
        if self.desktop_config.wake_word_enabled and self._wake_detector is None:
            if self._window:
                self._window.set_state(StatusState.THINKING, "Loading Hey Jarvis detector…")
            await self._start_wake_word_detector()
        if self._window:
            self._window.set_wake_word_hint(
                self.desktop_config.wake_words[0].title()
                if self.desktop_config.wake_word_enabled and self.desktop_config.wake_words
                else ""
            )
            state = StatusState.IDLE
            label = (
                f"Wake word on — say {self.desktop_config.wake_words[0]}"
                if self.desktop_config.wake_word_enabled and self.desktop_config.wake_words
                else "Wake word off — use push-to-talk"
            )
            self._window.set_state(state, label)
        if self._tray:
            self._tray.refresh_menu()

    async def _toggle_barge_in(self) -> None:
        self.voice_config.barge_in_enabled = not self.voice_config.barge_in_enabled
        save_config(self.config)
        self._barge_voiced_frames = 0
        self._barge_candidate_frames.clear()
        self._barge_in_progress = False
        enabled = self.voice_config.barge_in_enabled
        if self._window:
            self._window.set_state(
                StatusState.IDLE,
                "Interruption on — speak while Ares is talking"
                if enabled
                else "Interruption off",
            )
        if self._tray:
            self._tray.refresh_menu()
        logger.info("Barge-in %s", "enabled" if enabled else "disabled")

    async def _get_response(self, text: str) -> str:
        agent = self._get_or_create_agent()
        response_parts: list[str] = []
        pending_ui = ""
        async for token in agent.run_stream(text, self._conversation_history):
            if token.startswith("[tool_start:"):
                if self._window and self.desktop_config.tool_panel_enabled:
                    self._window.tool_started(
                        token.removeprefix("[tool_start:").removesuffix("]")
                    )
                continue
            if token.startswith("[tool_progress:"):
                inner = token.removeprefix("[tool_progress:").removesuffix("]")
                name, _, detail = inner.partition(":")
                if self._window and self.desktop_config.tool_panel_enabled:
                    self._window.tool_progress(name, detail)
                continue
            if token.startswith("[tool:"):
                inner = token.removeprefix("[tool:").removesuffix("]")
                name, _, payload = inner.partition(":")
                if self._window and self.desktop_config.tool_panel_enabled:
                    self._window.tool_result(name, payload)
                continue
            response_parts.append(token)
            pending_ui += self._sanitize_display_text(token)
            if self._window and len(pending_ui) >= 24:
                self._window.append_response(pending_ui)
                pending_ui = ""

        if self._window and pending_ui:
            self._window.append_response(pending_ui)

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

            memory_store = MemoryStore()
            conversation_store = ConversationStore(
                db_path=memory_store.db_path,
                connection=memory_store.conn,
            )
            try:
                self._agent = Agent(
                    memory_store=memory_store,
                    conversation_store=conversation_store,
                    api_key=self.config.api_key,
                    base_url=self.config.api_base_url,
                    model=self.config.model,
                    config=self.config,
                    is_voice_session=True,
                )
            except BaseException:
                # Agent construction initializes several stores. A failed
                # constructor must not leave its connection locking a retry.
                if memory_store.conn.in_transaction:
                    memory_store.conn.rollback()
                conversation_store.close()
                memory_store.close()
                raise
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
        self._speech_stop_event = stop
        self._speaking = True
        self._speaking_started_at = asyncio.get_running_loop().time()
        self._barge_voiced_frames = 0
        self._barge_candidate_frames.clear()
        self._barge_in_progress = False
        try:
            await play_audio_stream(q, stop, sample_rate=self._tts_sample_rate)
        finally:
            self._speaking = False
            self._speech_stop_event = None
            self._barge_voiced_frames = 0
            self._barge_candidate_frames.clear()
            self._barge_in_progress = False

    @staticmethod
    def _sanitize_display_text(text: str) -> str:
        """Remove emoji glyphs while preserving useful paths and prose."""
        cleaned: list[str] = []
        for char in text or "":
            if char in {"\u200d", "\ufe0e", "\ufe0f", "\u20e3"}:
                continue
            if unicodedata.category(char) in {"So", "Sk", "Cs", "Co"}:
                continue
            cleaned.append(char)
        return "".join(cleaned)

    @staticmethod
    def _sanitize_tts_text(text: str) -> str:
        text = DesktopVoiceAgent._sanitize_display_text(text or "")
        text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
        text = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]+\)", r"\1", text)
        text = re.sub(r"(?:https?://|www\.)\S+", " ", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)

        # Speech should describe the content, not read formatting, paths, or
        # decorative symbols aloud. Keep ordinary sentence punctuation.
        cleaned: list[str] = []
        for char in text:
            category = unicodedata.category(char)
            if category.startswith("S"):
                cleaned.append(" ")
            elif category.startswith("P") and char not in ".,!?;'":
                cleaned.append(" ")
            else:
                cleaned.append(char)
        text = "".join(cleaned)
        text = re.sub(r"(?<=\w)\.(?=\w)", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        if not re.search(r"[A-Za-z0-9]", text):
            return ""
        return text


async def run_desktop() -> None:
    """Entry point for ``python -m ares --desktop``."""
    with _desktop_instance_guard():
        agent = DesktopVoiceAgent()
        await agent.run()
