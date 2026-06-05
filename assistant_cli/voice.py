from __future__ import annotations

import base64
import os
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx

from .config import AssistantSettings


SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

BULBUL_V3_SPEAKERS = {
    "shubh",
    "aditya",
    "ritu",
    "priya",
    "neha",
    "rahul",
    "pooja",
    "rohan",
    "simran",
    "kavya",
    "amit",
    "dev",
    "ishita",
    "shreya",
    "ratan",
    "varun",
    "manan",
    "sumit",
    "roopa",
    "kabir",
    "aayan",
    "ashutosh",
    "advait",
    "anand",
    "tanya",
    "tarun",
    "sunny",
    "mani",
    "gokul",
    "vijay",
    "shruti",
    "suhani",
    "mohit",
    "kavitha",
    "rehan",
    "soham",
    "rupali",
}


@dataclass(frozen=True)
class VoiceResult:
    path: Path
    request_id: str
    byte_count: int
    speaker: str
    language: str
    model: str


class SarvamVoice:
    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self.enabled = settings.voice_enabled
        self.speaker = settings.voice_speaker.strip().lower() or "priya"
        self.last_result: VoiceResult | None = None
        self.last_error: str = ""
        self._queue: queue.Queue[str] = queue.Queue()
        self._queue_lock = threading.Lock()
        self._queue_thread: threading.Thread | None = None

    def configured(self) -> bool:
        return bool(self.settings.sarvam_api_key.strip())

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_speaker(self, speaker: str) -> None:
        clean = str(speaker or "").strip().lower()
        if not clean:
            raise ValueError("Speaker name is empty.")
        if self.settings.voice_model == "bulbul:v3" and clean not in BULBUL_V3_SPEAKERS:
            examples = ", ".join(["priya", "ishita", "simran", "kavya", "shreya"])
            raise ValueError(f"Unknown Bulbul v3 speaker. Try one of: {examples}.")
        self.speaker = clean

    def status(self) -> dict[str, str]:
        return {
            "enabled": "yes" if self.enabled else "no",
            "configured": "yes" if self.configured() else "no",
            "speaker": self.speaker,
            "language": self.settings.voice_language,
            "model": self.settings.voice_model,
            "pace": str(self.settings.voice_pace),
            "codec": self.settings.voice_codec,
            "sample_rate": str(self.settings.voice_sample_rate),
            "output_dir": str(self.settings.voice_output_dir),
            "last_file": str(self.last_result.path) if self.last_result else "",
            "last_error": self.last_error,
        }

    def speak(self, text: str, wait: bool = False) -> None:
        if not self.enabled:
            return
        if wait:
            result = self.synthesize(text)
            self.play(result.path, wait=True)
            return

        speech = self._speech_text(text)
        if not speech:
            return
        self._queue.put(speech)
        self._ensure_queue_worker()

    def synthesize(self, text: str) -> VoiceResult:
        if not self.configured():
            raise RuntimeError("SARVAM_API_KEY is missing.")

        speech_text = self._speech_text(text)
        if not speech_text:
            raise RuntimeError("No speakable text was produced.")

        payload = {
            "text": speech_text,
            "target_language_code": self.settings.voice_language,
            "speaker": self.speaker,
            "model": self.settings.voice_model,
            "output_audio_codec": self.settings.voice_codec,
            "speech_sample_rate": self.settings.voice_sample_rate,
            "pace": self.settings.voice_pace,
            "temperature": self.settings.voice_temperature,
        }
        headers = {
            "api-subscription-key": self.settings.sarvam_api_key,
            "Content-Type": "application/json",
        }

        response = httpx.post(SARVAM_TTS_URL, headers=headers, json=payload, timeout=30.0)
        if response.status_code >= 400:
            detail = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"Sarvam TTS failed with HTTP {response.status_code}: {detail}")

        data = response.json()
        audios = data.get("audios") or []
        if not audios:
            raise RuntimeError("Sarvam TTS response did not include audio.")

        audio_bytes = base64.b64decode(str(audios[0]))
        suffix = "." + self.settings.voice_codec.strip().lower().lstrip(".")
        self.settings.voice_output_dir.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("friday-%Y%m%d-%H%M%S-") + uuid4().hex[:8] + suffix
        path = self.settings.voice_output_dir / name
        path.write_bytes(audio_bytes)

        result = VoiceResult(
            path=path,
            request_id=str(data.get("request_id") or ""),
            byte_count=len(audio_bytes),
            speaker=self.speaker,
            language=self.settings.voice_language,
            model=self.settings.voice_model,
        )
        self.last_result = result
        self.last_error = ""
        return result

    def play(self, path: Path, wait: bool = True) -> None:
        if sys.platform.startswith("win") and path.suffix.lower() == ".wav":
            import winsound

            flags = winsound.SND_FILENAME
            if not wait:
                flags |= winsound.SND_ASYNC
            winsound.PlaySound(str(path), flags)
            return

        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return

        for command in (("afplay", str(path)), ("aplay", str(path)), ("ffplay", "-nodisp", "-autoexit", str(path))):
            try:
                import subprocess

                proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if wait:
                    proc.wait()
                return
            except OSError:
                continue
        raise RuntimeError(f"No audio player found for {path}.")

    def _ensure_queue_worker(self) -> None:
        with self._queue_lock:
            if self._queue_thread is not None and self._queue_thread.is_alive():
                return
            self._queue_thread = threading.Thread(target=self._queue_worker, daemon=True)
            self._queue_thread.start()

    def _queue_worker(self) -> None:
        while True:
            try:
                text = self._queue.get(timeout=0.5)
            except queue.Empty:
                return
            try:
                result = self.synthesize(text)
                self.play(result.path, wait=True)
            except Exception as exc:
                self.last_error = str(exc)
            finally:
                self._queue.task_done()

    def _speech_text(self, text: str) -> str:
        lines: list[str] = []
        in_code = False
        for raw in str(text or "").splitlines():
            line = raw.strip()
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code or not line:
                continue
            while line and line[0] in "#->*":
                line = line[1:].strip()
            for marker in ("**", "__", "`", "[", "]", "(", ")", "|"):
                line = line.replace(marker, "")
            if line:
                lines.append(line)

        speech = ". ".join(lines)
        max_chars = max(120, int(self.settings.voice_max_chars))
        if len(speech) <= max_chars:
            return speech
        trimmed = speech[:max_chars].rsplit(" ", 1)[0].strip()
        return trimmed or speech[:max_chars]
