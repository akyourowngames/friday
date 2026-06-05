from __future__ import annotations

import io
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np

from .config import AssistantSettings


SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    path: Path
    request_id: str
    language_code: str
    duration_seconds: float
    byte_count: int


class SarvamTranscriber:
    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.sarvam_api_key.strip())

    def transcribe_file(self, path: Path) -> TranscriptResult:
        if not self.configured():
            raise RuntimeError("SARVAM_API_KEY is missing.")

        audio_path = Path(path)
        data = {
            "model": self.settings.stt_model,
            "mode": self.settings.stt_mode,
            "language_code": self.settings.stt_language,
        }
        headers = {"api-subscription-key": self.settings.sarvam_api_key}
        with audio_path.open("rb") as handle:
            files = {"file": (audio_path.name, handle, "audio/wav")}
            response = httpx.post(SARVAM_STT_URL, headers=headers, data=data, files=files, timeout=35.0)

        if response.status_code >= 400:
            detail = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"Sarvam STT failed with HTTP {response.status_code}: {detail}")

        payload = response.json()
        transcript = str(payload.get("transcript") or "").strip()
        return TranscriptResult(
            text=transcript,
            path=audio_path,
            request_id=str(payload.get("request_id") or ""),
            language_code=str(payload.get("language_code") or ""),
            duration_seconds=_wav_duration(audio_path),
            byte_count=audio_path.stat().st_size,
        )


class AudioRecorder:
    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self._frames: list[np.ndarray] = []
        self._stream: object | None = None
        self._started_at = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        import sounddevice as sd

        with self._lock:
            if self._stream is not None:
                return
            self._frames = []
            self._started_at = time.perf_counter()
            blocksize = max(800, int(self.settings.stt_sample_rate * 0.05))
            stream = sd.InputStream(
                samplerate=self.settings.stt_sample_rate,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
                callback=self._callback,
            )
            stream.start()
            self._stream = stream

    def stop(self) -> Path | None:
        with self._lock:
            stream = self._stream
            self._stream = None
            started_at = self._started_at
        if stream is not None:
            stream.stop()
            stream.close()

        duration = time.perf_counter() - started_at if started_at else 0.0
        if duration < self.settings.stt_min_seconds:
            return None

        with self._lock:
            frames = list(self._frames)
        if not frames:
            return None

        samples = np.concatenate(frames).reshape(-1)
        max_samples = int(self.settings.stt_sample_rate * self.settings.stt_max_seconds)
        if samples.shape[0] > max_samples:
            samples = samples[:max_samples]
        return self._write_wav(samples)

    def _callback(self, indata, frames, time_info, status) -> None:
        with self._lock:
            self._frames.append(indata.copy())

    def _write_wav(self, samples: np.ndarray) -> Path:
        self.settings.stt_output_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.stt_output_dir / f"friday-input-{int(time.time())}-{uuid4().hex[:8]}.wav"
        wav_bytes = samples_to_wav(samples, self.settings.stt_sample_rate)
        path.write_bytes(wav_bytes)
        return path


class SpaceHoldToTalk:
    def __init__(
        self,
        settings: AssistantSettings,
        on_transcript,
        on_status,
    ) -> None:
        self.settings = settings
        self.on_transcript = on_transcript
        self.on_status = on_status
        self.transcriber = SarvamTranscriber(settings)
        self.recorder = AudioRecorder(settings)
        self._keyboard = None
        self._recording = False
        self._max_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.settings.voice_input_enabled:
            return False
        if not self.transcriber.configured():
            self.on_status("Voice input disabled: SARVAM_API_KEY is missing.")
            return False

        try:
            import keyboard
        except ImportError:
            self.on_status("Voice input disabled: install the `keyboard` package.")
            return False

        self._keyboard = keyboard
        keyboard.add_hotkey(self.settings.voice_hotkey, self._toggle_recording, suppress=True)
        return True

    def stop(self) -> None:
        if self._keyboard is not None:
            try:
                self._keyboard.unhook_all()
            except Exception:
                pass
        self._keyboard = None
        with self._lock:
            recording = self._recording
            self._recording = False
        if recording:
            try:
                self.recorder.stop()
            except Exception:
                pass

    def _toggle_recording(self) -> None:
        with self._lock:
            recording = self._recording
        if recording:
            threading.Thread(target=self._finish_recording, daemon=True).start()
        else:
            threading.Thread(target=self._begin_recording, daemon=True).start()

    def _begin_recording(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recording = True
        try:
            self.on_status("listening... press Ctrl+Space again to send")
            self.recorder.start()
            self._max_timer = threading.Timer(self.settings.stt_max_seconds, self._finish_recording)
            self._max_timer.daemon = True
            self._max_timer.start()
        except Exception as exc:
            with self._lock:
                self._recording = False
            self.on_status(f"Voice input error: {exc}")

    def _finish_recording(self) -> None:
        try:
            max_timer = self._max_timer
            self._max_timer = None
            if max_timer is not None:
                max_timer.cancel()
            path = self.recorder.stop()
            with self._lock:
                self._recording = False
            if path is None:
                self.on_status("voice ignored: clip too short")
                return
            self.on_status("transcribing...")
            result = self.transcriber.transcribe_file(path)
            if not result.text:
                self.on_status("voice heard no transcript")
                return
            self.on_transcript(result.text)
        except Exception as exc:
            with self._lock:
                self._recording = False
            self.on_status(f"Voice input error: {exc}")


def samples_to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm = np.asarray(samples, dtype=np.int16).reshape(-1)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
        return frames / float(rate or 1)
    except wave.Error:
        return 0.0
