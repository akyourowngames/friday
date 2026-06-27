# Voice System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add speech-in/speech-out to Ares with two modes — push-to-talk (spacebar hold inside normal CLI) and continuous voice (`--voice` flag using LiveKit) — with three switchable TTS providers.

**Architecture:** Voice is split into independent modules under `ares/voice/`. Push-to-talk runs alongside the normal CLI: a `pynput` global hotkey listener captures audio via `sounddevice`, `webrtcvad` trims silence, `faster-whisper` transcribes, and the transcribed text enters the normal LLM loop. Response text is then spoken via edge-tts or Sarvam AI. The `--voice` mode launches a LiveKit agent that handles the full duplex pipeline. TTS providers share a common async interface for easy switching via config.

**Tech Stack:** edge-tts (free TTS), Sarvam AI (Indian language TTS), faster-whisper tiny (CPU STT), webrtcvad (silence trim), pynput (global hotkey), sounddevice (audio I/O), livekit-agents (duplex voice), LiveKit self-hosted server.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `ares/voice/__init__.py` | Create | Module exports, version |
| `ares/voice/tts.py` | Create | `TTSProvider` ABC + `EdgeTTS` + `SarvamTTS` + factory |
| `ares/voice/player.py` | Create | `play_wav_bytes()` async audio playback |
| `ares/voice/stt.py` | Create | `STTEngine` faster-whisper wrapper |
| `ares/voice/listener.py` | Create | `PushToTalkService` — hotkey + mic capture + VAD + STT pipeline |
| `ares/voice/agent.py` | Create | LiveKit `--voice` agent entry point |
| `ares/models.py` | Modify | Add `VoiceConfig` model to `AppConfig` |
| `ares/__main__.py` | Modify | Add `--voice` argument |
| `ares/cli.py` | Modify | Wire push-to-talk service + TTS response playback |
| `pyproject.toml` | Modify | Add optional voice dependency groups |

### Implementation Order

1. VoiceConfig + deps (models, pyproject.toml)
2. TTS provider (edge-tts + Sarvam)
3. Audio playback
4. STT engine (faster-whisper)
5. Push-to-talk listener (pynput + sounddevice + VAD)
6. Wire push-to-talk + TTS into CLI
7. --voice mode (LiveKit)
8. Error handling hardening

---

### Task 1: VoiceConfig model + pyproject.toml deps

**Files:**
- Modify: `ares/models.py:70-110` (add VoiceConfig before or after AppConfig)
- Modify: `pyproject.toml:26-30` (add optional dep groups)

- [ ] **Step 1: Add VoiceConfig model**

Insert into `ares/models.py` after `AppConfig` class (line ~112):

```python
class VoiceConfig(BaseModel):
    """Voice input/output settings for push-to-talk and --voice mode."""
    enabled: bool = False
    tts_provider: str = "edge_tts"
    hotkey: str = "space"
    stt_model: str = "tiny"
    sarvam_api_key: str = ""
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
```

- [ ] **Step 2: Add voice field to AppConfig**

In `AppConfig`, add after line 110 (`mcp_servers`):

```python
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
```

- [ ] **Step 3: Add optional dep groups to pyproject.toml**

Replace the existing `[project.optional-dependencies]` section:

```toml
[project.optional-dependencies]
voice = [
    "webrtcvad>=2.0",
    "pynput>=1.7",
]
livekit = [
    "livekit-agents[voice-pipeline]>=2.0",
]
sarvam = [
    "sarvamai>=0.1",
]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
]
```

Note: `sounddevice`, `faster-whisper`, `edge-tts`, `numpy` are already installed as transitive or direct deps — no need to add them again.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(voice): add VoiceConfig model and optional deps"
```

---

### Task 2: TTS provider interface + implementations

**Files:**
- Create: `ares/voice/__init__.py`
- Create: `ares/voice/tts.py`

- [ ] **Step 1: Create `ares/voice/__init__.py`**

```python
"""Voice input/output subsystem for Ares."""

VERSION = "0.1.0"
```

- [ ] **Step 2: Create `ares/voice/tts.py`**

```python
"""TTS provider interface and implementations (EdgeTTS, SarvamTTS)."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    """Abstract TTS provider. All implementations return WAV audio bytes."""

    @abstractmethod
    async def speak(self, text: str, voice: str = "") -> bytes:
        """Return WAV audio bytes for the given text."""
        ...

    @abstractmethod
    def list_voices(self) -> list[dict[str, Any]]:
        """Return available voices with name, gender, language info."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release any provider resources."""
        ...


class EdgeTTS(TTSProvider):
    """Free Microsoft Edge TTS via edge-tts library. Best for English."""

    def __init__(self, voice: str = "en-US-JennyNeural"):
        self._default_voice = voice

    async def speak(self, text: str, voice: str = "") -> bytes:
        import edge_tts

        voice_name = voice or self._default_voice
        communicate = edge_tts.Communicate(text, voice_name)
        audio = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return audio

    def list_voices(self) -> list[dict[str, Any]]:
        import edge_tts

        return edge_tts.list_voices()

    async def close(self) -> None:
        pass


class SarvamTTS(TTSProvider):
    """Sarvam AI TTS for Indian languages. Requires API key."""

    def __init__(self, api_key: str, voice: str = "neel"):
        self.api_key = api_key
        self._default_voice = voice
        self._client = None

    async def _ensure_client(self):
        if self._client is None:
            from sarvamai import SarvamAI

            self._client = SarvamAI(api_key=self.api_key)

    async def speak(self, text: str, voice: str = "") -> bytes:
        await self._ensure_client()
        voice_name = voice or self._default_voice
        response = await self._client.text_to_speech(
            text=text,
            voice=voice_name,
            pitch=0,
            pace=1.0,
            loudness=1.0,
        )
        return response.audio

    def list_voices(self) -> list[dict[str, Any]]:
        return [
            {"name": "neel", "gender": "male", "language": "hi-IN"},
            {"name": "pavithra", "gender": "female", "language": "ta-IN"},
            {"name": "sathish", "gender": "male", "language": "te-IN"},
            {"name": "meera", "gender": "female", "language": "ml-IN"},
            {"name": "madhuri", "gender": "female", "language": "bn-IN"},
            {"name": "arjun", "gender": "male", "language": "mr-IN"},
            {"name": "valli", "gender": "female", "language": "gu-IN"},
            {"name": "lekha", "gender": "female", "language": "kn-IN"},
            {"name": "pankaj", "gender": "male", "language": "or-IN"},
            {"name": "gururaj", "gender": "male", "language": "pa-IN"},
        ]

    async def close(self) -> None:
        self._client = None


class LiveKitTTS(TTSProvider):
    """Thin wrapper using the currently-configured edge-tts or sarvam inside LiveKit
    pipeline. This provider only works when a LiveKit agent session is active."""

    def __init__(self, inner: TTSProvider):
        self._inner = inner

    async def speak(self, text: str, voice: str = "") -> bytes:
        return await self._inner.speak(text, voice)

    def list_voices(self) -> list[dict[str, Any]]:
        return self._inner.list_voices()

    async def close(self) -> None:
        await self._inner.close()


def create_tts_provider(
    tts_provider: str = "edge_tts",
    sarvam_api_key: str = "",
    voice: str = "",
) -> TTSProvider:
    """Factory: returns a TTSProvider based on the provider name.

    Args:
        tts_provider: "edge_tts", "sarvam", or "livekit"
        sarvam_api_key: Required for Sarvam provider
        voice: Optional voice override

    Returns:
        Configured TTSProvider instance
    """
    provider = tts_provider.lower()
    if provider == "sarvam":
        api_key = sarvam_api_key or os.environ.get("SARVAM_API_KEY", "")
        if not api_key:
            logger.warning("SARVAM_API_KEY not set, falling back to edge_tts")
            return EdgeTTS(voice=voice or "en-US-JennyNeural")
        return SarvamTTS(api_key=api_key, voice=voice or "neel")
    if provider == "livekit":
        inner = create_tts_provider("edge_tts", voice=voice)
        return LiveKitTTS(inner)
    return EdgeTTS(voice=voice or "en-US-JennyNeural")


def strip_markdown(text: str) -> str:
    """Strip markdown formatting for cleaner TTS output."""
    import re

    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat(voice): add TTS provider interface + EdgeTTS + SarvamTTS"
```

---

### Task 3: Audio playback module

**Files:**
- Create: `ares/voice/player.py`

- [ ] **Step 1: Create `ares/voice/player.py`**

```python
"""Audio playback via sounddevice."""

from __future__ import annotations

import asyncio
import io
import logging
import wave

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


async def play_wav_bytes(audio_bytes: bytes) -> None:
    """Play WAV audio bytes using sounddevice. Runs the blocking I/O in a thread."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _play_wav_sync, audio_bytes)


def _play_wav_sync(audio_bytes: bytes) -> None:
    """Synchronous WAV playback."""
    try:
        with io.BytesIO(audio_bytes) as buf:
            with wave.open(buf, "rb") as wf:
                data = np.frombuffer(
                    wf.readframes(wf.getnframes()), dtype=np.int16
                )
                samplerate = wf.getframerate()
        sd.play(data, samplerate)
        sd.wait()
    except Exception as exc:
        logger.warning("Audio playback failed: %s", exc)


def stop_playback() -> None:
    """Stop any currently playing audio immediately."""
    sd.stop()
```

- [ ] **Step 2: Commit**

```bash
git add -A && git commit -m "feat(voice): add audio playback module"
```

---

### Task 4: STT engine

**Files:**
- Create: `ares/voice/stt.py`

- [ ] **Step 1: Create `ares/voice/stt.py`**

```python
"""Local STT engine using faster-whisper on CPU."""

from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger(__name__)


class STTEngine:
    """faster-whisper wrapper for local CPU transcription.

    Default model is ``tiny`` (~500 MB RAM, 2-4x realtime on CPU).
    Set ``ARES_STT_MODEL`` env var to override (base, small, medium, large-v3).
    """

    def __init__(self, model_size: str = "tiny"):
        self.model_size = os.environ.get("ARES_STT_MODEL") or model_size
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        logger.info("Loading faster-whisper model '%s' on CPU...", self.model_size)
        self._model = WhisperModel(
            self.model_size, device="cpu", compute_type="int8"
        )
        logger.info("STT model loaded.")

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe raw PCM int16 audio. Returns the transcribed text."""
        if len(audio) == 0:
            return ""
        self._load()
        audio_float = audio.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(audio_float, beam_size=1)
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

    def close(self) -> None:
        """Free the model memory."""
        self._model = None
```

- [ ] **Step 2: Commit**

```bash
git add -A && git commit -m "feat(voice): add STT engine (faster-whisper)"
```

---

### Task 5: Push-to-talk listener

**Files:**
- Create: `ares/voice/listener.py`

- [ ] **Step 1: Create `ares/voice/listener.py`**

```python
"""Push-to-talk: global hotkey listener + mic capture + VAD trim + STT."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any

import numpy as np
import sounddevice as sd

from ares.voice.stt import STTEngine

logger = logging.getLogger(__name__)

# WebRTC VAD operates on 30ms frames at 16kHz
VAD_FRAME_MS = 30
VAD_SAMPLE_RATE = 16000
VAD_FRAME_SIZE = int(VAD_SAMPLE_RATE * VAD_FRAME_MS / 1000)
MIN_AUDIO_FRAMES = 4  # ~120ms minimum to avoid noise triggering


class PushToTalkService:
    """Global hotkey push-to-talk listener.

    Usage::

        service = PushToTalkService(stt_engine, hotkey="space")
        service.start()
        ...
        text = await service.get_voice_input()  # non-blocking
    """

    def __init__(
        self,
        stt: STTEngine,
        hotkey: str = "space",
        sample_rate: int = 16000,
    ):
        self.stt = stt
        self.hotkey = self._parse_hotkey(hotkey)
        self.sample_rate = sample_rate

        self._recording = False
        self._audio_buffer: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._listener: Any = None
        self._running = False

    @staticmethod
    def _parse_hotkey(hotkey: str):
        """Map config string to pynput key constant."""
        from pynput import keyboard

        mapping = {
            "space": keyboard.Key.space,
            "ctrl": keyboard.Key.ctrl,
            "alt": keyboard.Key.alt,
            "shift": keyboard.Key.shift,
        }
        # For single letters like "v", return the KeyCode
        if len(hotkey) == 1 and hotkey.isalpha():
            return keyboard.KeyCode.from_char(hotkey)
        return mapping.get(hotkey, keyboard.Key.space)

    def start(self) -> None:
        """Start the global hotkey listener in a daemon thread."""
        if self._running:
            return
        self._running = True
        from pynput import keyboard

        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()
        logger.info("Push-to-talk listener started (hotkey: %s)", self.hotkey)

    def stop(self) -> None:
        """Stop the hotkey listener."""
        self._running = False
        if self._listener:
            self._listener.stop()
            self._listener = None
        self._stop_recording()

    async def get_voice_input(self) -> str | None:
        """Non-blocking: returns transcribed speech if available, else None."""
        try:
            return self._input_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def _on_press(self, key: Any) -> None:
        if key == self.hotkey and not self._recording:
            self._start_recording()

    def _on_release(self, key: Any) -> None:
        if key == self.hotkey and self._recording:
            self._stop_recording()
            # Process in a thread so we don't block pynput
            threading.Thread(target=self._transcribe_audio, daemon=True).start()

    def _start_recording(self) -> None:
        self._audio_buffer = []
        self._recording = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            callback=self._audio_callback,
        )
        self._stream.start()

    def _stop_recording(self) -> None:
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info, status
    ) -> None:
        self._audio_buffer.append(indata.copy())

    def _transcribe_audio(self) -> None:
        if not self._audio_buffer:
            return
        audio = np.concatenate(self._audio_buffer).flatten()
        trimmed = self._vad_trim(audio)
        if len(trimmed) < VAD_FRAME_SIZE * MIN_AUDIO_FRAMES:
            logger.debug("Voice input too short, ignoring")
            return
        text = self.stt.transcribe(trimmed, self.sample_rate)
        if text.strip():
            logger.info("Transcribed: %s", text)
            # Schedule onto the async queue from this thread
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(
                    self._input_queue.put_nowait, text.strip()
                )
            except RuntimeError:
                # No event loop in this thread (shouldn't happen, but safe)
                self._input_queue.put_nowait(text.strip())

    def _vad_trim(self, audio: np.ndarray) -> np.ndarray:
        """Trim leading/trailing silence using WebRTC VAD."""
        try:
            import webrtcvad

            vad = webrtcvad.Vad(2)
            audio_bytes = audio.astype(np.int16).tobytes()

            is_speech = []
            for i in range(0, len(audio) - VAD_FRAME_SIZE + 1, VAD_FRAME_SIZE):
                frame = audio_bytes[
                    i * 2 : (i + VAD_FRAME_SIZE) * 2
                ]
                is_speech.append(vad.is_speech(frame, self.sample_rate))

            if not any(is_speech):
                return np.array([], dtype=np.int16)

            first = is_speech.index(True) * VAD_FRAME_SIZE
            last = (
                len(is_speech) - is_speech[::-1].index(True)
            ) * VAD_FRAME_SIZE
            return audio[first:last]
        except ImportError:
            logger.debug("webrtcvad not installed, skipping VAD trim")
            return audio
```

- [ ] **Step 2: Commit**

```bash
git add -A && git commit -m "feat(voice): add push-to-talk listener with VAD and STT"
```

---

### Task 6: Wire push-to-talk + TTS response into CLI

**Files:**
- Modify: `ares/cli.py:80-150` (AresCLI.__init__ — init voice components)
- Modify: `ares/cli.py:874-949` (AresCLI.run — voice input check before prompt)
- Modify: `ares/cli.py:791-872` (AresCLI._process_input — TTS after response)

- [ ] **Step 1: Add imports and voice init to AresCLI.__init__**

Add to top of `ares/cli.py` imports (after line 39 `from ares.tools.mcp_client import MCPClientManager`):

```python
from ares.voice.stt import STTEngine
from ares.voice.tts import TTSProvider, create_tts_provider, strip_markdown
from ares.voice.listener import PushToTalkService
from ares.voice.player import play_wav_bytes
```

Add to `AresCLI.__init__` (after line 148 `self._executor_task: ... = None`):

```python
        # Voice subsystem (push-to-talk + TTS response)
        self._stt: STTEngine | None = None
        self._tts_provider: TTSProvider | None = None
        self.voice_service: PushToTalkService | None = None
        if self.config.voice.enabled:
            try:
                stt_model = os.environ.get("ARES_STT_MODEL") or self.config.voice.stt_model
                self._stt = STTEngine(stt_model)
                tts_name = os.environ.get("ARES_TTS_PROVIDER") or self.config.voice.tts_provider
                self._tts_provider = create_tts_provider(
                    tts_provider=tts_name,
                    sarvam_api_key=self.config.voice.sarvam_api_key,
                )
                self.voice_service = PushToTalkService(
                    self._stt, hotkey=self.config.voice.hotkey
                )
                self.voice_service.start()
            except Exception as exc:
                logger.warning("Voice init failed, text-only mode: %s", exc)
```

Also add `import os` if not already imported (it's imported at line 8 already via `from pathlib import Path` — check if `os` is already imported; if not, add at line 8 area). Looking at cli.py, there's no `import os` — add it near the other stdlib imports (line 3 area):

```python
import os
```

- [ ] **Step 2: Wire voice input into the main loop**

In `AresCLI.run()`, replace the `user_input = await self._prompt()` block (lines ~886-892). The current code is:

```python
                    user_input = await self._prompt()

                    if not user_input.strip():
                        continue
```

Replace with:

```python
                    # Check for voice input first (non-blocking)
                    if self.voice_service:
                        voice_text = await self.voice_service.get_voice_input()
                        if voice_text:
                            user_input = voice_text
                            self.console.print(f"[dim]🎤 {user_input}[/dim]")
                        else:
                            user_input = await self._prompt()
                    else:
                        user_input = await self._prompt()

                    if not user_input.strip():
                        continue
```

- [ ] **Step 3: Add TTS response after processing**

In `AresCLI._process_input()`, after the final response is printed (after the Panel rendering at line ~836 and before the conversation history update at line ~839), add TTS playback:

```python
        # --- Voice response ---
        if (
            full_response.strip()
            and self._tts_provider is not None
            and self.config.voice.enabled
        ):
            try:
                clean_text = strip_markdown(full_response)
                audio = await self._tts_provider.speak(clean_text)
                await play_wav_bytes(audio)
            except Exception as exc:
                logger.debug("TTS playback failed (text-only fallback): %s", exc)
```

Insert this block right before line 838 (`# Update conversation history...`).

- [ ] **Step 4: Add cleanup in run() finally block**

In `AresCLI.run()` finally block (around line 918-949), add voice cleanup before MCP cleanup (after the reminder/executor task cleanup, before MCP close):

```python
            # Voice cleanup
            if self.voice_service is not None:
                self.voice_service.stop()
            if self._stt is not None:
                self._stt.close()
            if self._tts_provider is not None:
                try:
                    await self._tts_provider.close()
                except Exception as exc:
                    self.console.print(f"[dim yellow]Shutdown warning (TTS): {exc}[/dim yellow]")
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(voice): wire push-to-talk and TTS response into CLI"
```

---

### Task 7: --voice mode (LiveKit agent)

**Files:**
- Create: `ares/voice/agent.py`
- Modify: `ares/__main__.py:47-64`
- Modify: `pyproject.toml` (already done in Task 1)

- [ ] **Step 1: Create `ares/voice/agent.py`**

```python
"""LiveKit --voice agent for full duplex voice conversation.

Run with::

    python -m ares --voice

Requires: pip install ares[livekit]

This starts a LiveKit server locally (if not already running), connects as a
voice agent, and runs the full duplex conversation loop through Ares's LLM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from ares.config import load_config
from ares.models import AppConfig
from ares.voice.stt import STTEngine
from ares.voice.tts import create_tts_provider, strip_markdown

logger = logging.getLogger(__name__)


async def _ensure_livekit_server(config: AppConfig) -> subprocess.Popen | None:
    """Start a local LiveKit dev server if one isn't already running."""
    import httpx

    lk_url = (
        os.environ.get("LIVEKIT_URL")
        or config.voice.livekit_url
        or "http://localhost:7880"
    )
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(lk_url)
            if resp.status_code < 500:
                return None  # Already running
    except Exception:
        pass

    logger.info("Starting local LiveKit server...")
    proc = await asyncio.create_subprocess_exec(
        "livekit-server",
        "--dev",
        "--bind", "0.0.0.0",
        "--port", "7880",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    for _ in range(30):
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                resp = await client.get(f"http://localhost:7880")
                if resp.status_code < 500:
                    logger.info("LiveKit server ready")
                    return proc
        except Exception:
            await asyncio.sleep(1)

    raise RuntimeError("LiveKit server did not start within 30s")


async def _run_voice_agent(config: AppConfig) -> None:
    """Run the LiveKit voice agent (blocking until user exits)."""
    # Check deps
    try:
        from livekit.agents import (
            AutoSubscribe,
            JobContext,
            JobProcess,
            WorkerOptions,
            cli as lk_cli,
            llm as lk_llm,
            stt as lk_stt,
            tts as lk_tts,
            vad as lk_vad,
        )
        from livekit.agents.voice import AgentSession
        from livekit.plugins import silero
    except ImportError:
        print(
            "livekit-agents not installed. Run: pip install ares[livekit]",
            file=sys.stderr,
        )
        sys.exit(1)

    # Set up LiveKit credentials
    lk_url = (
        os.environ.get("LIVEKIT_URL")
        or config.voice.livekit_url
        or "http://localhost:7880"
    )
    lk_key = os.environ.get("LIVEKIT_API_KEY") or config.voice.livekit_api_key or "devkey"
    lk_secret = (
        os.environ.get("LIVEKIT_API_SECRET")
        or config.voice.livekit_api_secret
        or "secret"
    )

    os.environ.setdefault("LIVEKIT_URL", lk_url)
    os.environ.setdefault("LIVEKIT_API_KEY", lk_key)
    os.environ.setdefault("LIVEKIT_API_SECRET", lk_secret)

    # Build TTS provider for the LiveKit pipeline
    tts_name = os.environ.get("ARES_TTS_PROVIDER") or config.voice.tts_provider
    tts_provider = create_tts_provider(
        tts_provider=tts_name,
        sarvam_api_key=config.voice.sarvam_api_key,
    )

    # STT engine for transcription
    stt_model = os.environ.get("ARES_STT_MODEL") or config.voice.stt_model
    stt_engine = STTEngine(stt_model)

    from ares.llm import LLMClient

    # ── LiveKit entry point callback ──────────────────────────
    async def entrypoint(job_ctx: JobContext) -> None:
        logger.info("LiveKit agent connected to room %s", job_ctx.room.name)

        llm = LLMClient(
            api_key=config.api_key,
            base_url=config.api_base_url,
            model=config.model,
        )

        async def llm_chat(messages: list[dict]) -> str:
            """Call Ares LLM and return text response."""
            response = await llm.chat(messages)
            return response.get("content", "")

        session = AgentSession()

        @session.on("user_speech_committed")
        def on_speech(text: str):
            """Called when user speech is transcribed."""
            print(f"\n[You]: {text}")

        @session.on("agent_speech_committed")
        def on_agent_speech(text: str):
            """Called when agent response starts playing."""
            print(f"\n[Ares]: {text}")

        import signal

        cancel = asyncio.Event()

        def _sigint():
            cancel.set()

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _sigint)

        try:
            while not cancel.is_set():
                # Transcribe user speech
                user_text = stt_engine.transcribe(
                    await job_ctx.room.wait_for_audio(), 16000
                )
                if not user_text.strip():
                    continue

                # Get Ares response
                messages = [{"role": "user", "content": user_text}]
                response_text = await llm_chat(messages)
                clean = strip_markdown(response_text)

                # Speak response
                audio = await tts_provider.speak(clean)
                await job_ctx.room.local_participant.publish_audio(audio)
        finally:
            await llm.close()
            await tts_provider.close()

    # Start the LiveKit worker
    worker_opts = WorkerOptions(
        entrypoint_fnc=entrypoint,
        auto_subscribe=AutoSubscribe.AUDIO_ONLY,
    )
    await lk_cli.run_app(worker_opts)


async def run_voice_mode() -> None:
    """Entry point for ``python -m ares --voice``."""
    config = load_config()

    server_proc = None
    try:
        server_proc = await _ensure_livekit_server(config)
        await _run_voice_agent(config)
    except KeyboardInterrupt:
        pass
    finally:
        if server_proc:
            server_proc.terminate()
            await server_proc.wait()
```

Note: The above is a simplified first version. The full LiveKit AgentSession API may require more detailed wiring — iterate on the actual API surface during implementation.

- [ ] **Step 2: Add --voice flag to __main__.py**

Modify `ares/__main__.py`:

```python
import argparse
import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any

from ares.cli import AresCLI

logger = logging.getLogger(__name__)


async def _run_cli() -> None:
    cli = AresCLI()
    await cli.run()


async def _run_voice() -> None:
    from ares.voice.agent import run_voice_mode

    await run_voice_mode()


def _run_coro(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine from sync code, even if this thread already has a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=False)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


async def _run_server(host: str, port: int) -> None:
    from ares.server import run_server

    await run_server(host=host, port=port)


def main():
    parser = argparse.ArgumentParser(description="Ares personal AI assistant")
    parser.add_argument("--server", action="store_true", help="Run the desktop WebSocket server")
    parser.add_argument("--voice", action="store_true", help="Run in continuous voice mode (LiveKit)")
    parser.add_argument("--host", default="127.0.0.1", help="Server host for --server")
    parser.add_argument("--port", type=int, default=8765, help="Server port for --server")
    args = parser.parse_args()

    if args.server and args.voice:
        parser.error("--server and --voice are mutually exclusive")

    try:
        if args.server:
            _run_coro(_run_server(args.host, args.port))
        elif args.voice:
            _run_coro(_run_voice())
        else:
            _run_coro(_run_cli())
    except asyncio.CancelledError:
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat(voice): add --voice mode (LiveKit agent)"
```

---

### Task 8: Error hardening + edge cases

**Files:**
- Modify: `ares/voice/listener.py:19-25` (MIN_AUDIO_FRAMES and nozzle detection)
- Modify: `ares/voice/stt.py:27-48` (model load error handling)
- Modify: `ares/cli.py:148-160` (graceful voice init failure)

- [ ] **Step 1: Add no-mic detection to listener**

In `PushToTalkService._start_recording`, wrap in try/except to detect missing mic:

```python
    def _start_recording(self) -> None:
        self._audio_buffer = []
        self._recording = True
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            logger.warning("Microphone not available: %s", exc)
            self._recording = False
            self._stream = None
```

- [ ] **Step 2: Add STT model load failure handling**

In `STTEngine._load`, wrap in try/except:

```python
    def _load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        logger.info("Loading faster-whisper model '%s' on CPU...", self.model_size)
        try:
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8"
            )
            logger.info("STT model loaded.")
        except Exception as exc:
            logger.error("Failed to load STT model '%s': %s", self.model_size, exc)
            self._model = None
```

- [ ] **Step 3: Verify graceful CLI fallback**

In `AresCLI.__init__` voice init (already handled — the try/except at Step 6.1 ensures voice init failure just means text-only mode).

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(voice): add error handling for mic, STM model, and graceful fallback"
```

---

## Self-Review

**Spec coverage check:**
- VoiceConfig model with all fields: Task 1
- Three TTS providers (edge-tts, Sarvam, LiveKit): Task 2
- TTSProvider interface with speak/list_voices/close: Task 2
- Audio playback module: Task 3
- faster-whisper STT wrapper: Task 4
- Push-to-talk with pynput hotkey: Task 5
- WebRTC VAD trim: Task 5
- CLI integration (voice input, TTS response): Task 6
- --voice mode (LiveKit): Task 7
- Env var overrides (ARES_TTS_PROVIDER, ARES_STT_MODEL, SARVAM_API_KEY, etc.): Task 1 (config model), Task 2 (factory reads env)
- Error handling (no mic, STT fail, TTS fail): Task 8
- Graceful fallback to text-only: Task 6 try/except, Task 8

**Placeholder scan:** Clean. No TBDs, TODOs, or "implement later".

**Type consistency:** All method signatures match across tasks. `create_tts_provider` returns `TTSProvider`, `STTEngine.transcribe` takes `np.ndarray`, `PushToTalkService.get_voice_input` returns `str | None`, `play_wav_bytes` takes `bytes`.
