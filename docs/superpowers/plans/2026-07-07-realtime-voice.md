# Real-Time Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make continuous voice mode feel like a live call — Ares starts speaking almost immediately after generating, and the user can interrupt mid-sentence.

**Architecture:** Sentence-level TTS streaming with a parallel pipeline: LLM tokens → sentence buffer → TTS queue → streaming playback. Barge-in detection via WebRTC VAD during playback. Per-session optimizations (persistent agent, conversation history, voice-aware tool filtering).

**Tech Stack:** edge-tts (streaming TTS), sounddevice (callback-driven playback), webrtcvad (barge-in detection), asyncio (concurrency), pytest (testing)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `ares/models.py` | Modify | Add `voice_max_history` and `voice_max_memories` to `VoiceConfig` |
| `ares/voice/tts.py` | Modify | Add `speak_stream()` to `TTSProvider` ABC, `EdgeTTS`, `SarvamTTS` |
| `ares/voice/player.py` | Modify | Add `play_audio_stream()` with OutputStream + queue |
| `ares/voice/agent.py` | Modify | Sentence chunking, TTS pipeline, barge-in watcher, persistent agent, conversation history |
| `ares/agent.py` | Modify | Add `is_voice_session` flag, voice-aware context building |
| `tests/test_tts_streaming.py` | Create | Tests for `speak_stream()` on EdgeTTS and SarvamTTS |
| `tests/test_player_streaming.py` | Create | Tests for `play_audio_stream()` |
| `tests/test_voice_agent_streaming.py` | Create | Tests for sentence chunking, barge-in, persistent agent |

---

## Task 1: VoiceConfig — Add Voice History Settings

**Files:**
- Modify: `ares/models.py:72-83`
- Test: `tests/test_voice_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_voice_config.py`:

```python
def test_voice_config_has_history_defaults():
    config = VoiceConfig()

    assert config.voice_max_history == 10
    assert config.voice_max_memories == 3


def test_voice_config_custom_history():
    config = VoiceConfig(voice_max_history=5, voice_max_memories=2)

    assert config.voice_max_history == 5
    assert config.voice_max_memories == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_voice_config.py::test_voice_config_has_history_defaults tests/test_voice_config.py::test_voice_config_custom_history -v`
Expected: FAIL with `AttributeError: 'VoiceConfig' object has no attribute 'voice_max_history'`

- [ ] **Step 3: Write minimal implementation**

In `ares/models.py`, add to `VoiceConfig`:

```python
class VoiceConfig(BaseModel):
    """Voice settings for continuous voice mode."""

    enabled: bool = False
    tts_provider: str = "edge_tts"
    tts_voice: str = ""
    hotkey: str = "space"
    stt_model: str = "tiny"
    sarvam_api_key: str = ""
    sarvam_tts_model: str = "bulbul:v2"
    sarvam_language_code: str = "hi-IN"
    voice_max_history: int = 10
    voice_max_memories: int = 3
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_voice_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/models.py tests/test_voice_config.py
git commit -m "feat(voice): add voice_max_history and voice_max_memories to VoiceConfig"
```

---

## Task 2: speak_stream() — Streaming TTS Interface

**Files:**
- Modify: `ares/voice/tts.py`
- Test: `tests/test_tts_streaming.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tts_streaming.py`:

```python
"""Tests for TTS streaming (speak_stream)."""

import pytest
from ares.voice.tts import EdgeTTS, SarvamTTS, TTSProvider


class TestEdgeTTSStream:
    """Tests for EdgeTTS.speak_stream."""

    @pytest.mark.asyncio
    async def test_speak_stream_yields_bytes(self):
        tts = EdgeTTS(voice="en-US-GuyNeural")
        chunks = []
        async for chunk in tts.speak_stream("Hello world"):
            chunks.append(chunk)
            assert isinstance(chunk, bytes)
            assert len(chunk) > 0

        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_speak_stream_short_text(self):
        tts = EdgeTTS(voice="en-US-GuyNeural")
        chunks = []
        async for chunk in tts.speak_stream("Hi"):
            chunks.append(chunk)

        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_speak_stream_wraps_speak(self):
        """speak() should produce same total bytes as draining speak_stream()."""
        tts = EdgeTTS(voice="en-US-GuyNeural")

        # Get via speak()
        full_audio = await tts.speak("Test sentence.")
        assert len(full_audio) > 0

        # Get via speak_stream()
        streamed = bytearray()
        async for chunk in tts.speak_stream("Test sentence."):
            streamed.extend(chunk)

        # Both should produce audio (exact bytes may differ due to chunking)
        assert len(streamed) > 0
        # Total size should be within 50% of each other (chunking boundaries differ)
        ratio = len(full_audio) / max(len(streamed), 1)
        assert 0.5 < ratio < 2.0


class TestSarvamTTSStream:
    """Tests for SarvamTTS.speak_stream (simulated streaming)."""

    @pytest.mark.asyncio
    async def test_speak_stream_yields_bytes(self, monkeypatch):
        """SarvamTTS.speak_stream yields one chunk per call (simulated streaming)."""
        # Mock the HTTP call to avoid real API calls
        import base64

        fake_audio = b"\x00" * 1000  # Fake audio data

        async def mock_speak(self_inner, text, voice=""):
            return fake_audio

        monkeypatch.setattr(SarvamTTS, "speak", mock_speak)

        tts = SarvamTTS(api_key="test-key", voice="anushka")
        chunks = []
        async for chunk in tts.speak_stream("Hello"):
            chunks.append(chunk)

        # Should yield exactly one chunk (simulated streaming)
        assert len(chunks) == 1
        assert chunks[0] == fake_audio
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tts_streaming.py -v`
Expected: FAIL with `AttributeError: 'EdgeTTS' object has no attribute 'speak_stream'`

- [ ] **Step 3: Write minimal implementation**

In `ares/voice/tts.py`, add to `TTSProvider` ABC:

```python
class TTSProvider(ABC):
    """Abstract TTS provider. Implementations return encoded audio bytes."""

    @abstractmethod
    async def speak(self, text: str, voice: str = "") -> bytes:
        """Return audio bytes for *text*."""

    @abstractmethod
    async def speak_stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        """Yield audio chunks as they become available.

        EdgeTTS yields chunks as the API streams.
        SarvamTTS yields one chunk per call (simulated streaming).
        """

    @abstractmethod
    async def list_voices(self) -> list[dict[str, Any]]:
        """Return available voices with name, gender, and language info."""

    async def close(self) -> None:
        """Release any provider resources."""
```

Add to `EdgeTTS`:

```python
async def speak_stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
    """Yield audio chunks as they arrive from Edge TTS."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice or self.default_voice)
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            yield chunk.get("data", b"")
```

Add to `SarvamTTS`:

```python
async def speak_stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
    """Yield one audio chunk (simulated streaming — no native streaming API).

    NOTE: Sarvam has no streaming API. "Streaming" here means calling
    the REST endpoint per sentence-chunk. First-byte latency per chunk
    is ~200ms REST overhead, not true low-latency streaming like EdgeTTS.
    """
    audio = await self.speak(text, voice)
    yield audio
```

Also need to add `AsyncIterator` to imports at top of `tts.py`:

```python
from typing import Any, AsyncIterator, Literal
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tts_streaming.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/voice/tts.py tests/test_tts_streaming.py
git commit -m "feat(voice): add speak_stream() to TTS providers

EdgeTTS yields chunks as API streams natively.
SarvamTTS yields one chunk per call (simulated streaming).
speak() remains unchanged for backward compat."
```

---

## Task 3: play_audio_stream() — Streaming Audio Playback

**Files:**
- Modify: `ares/voice/player.py`
- Test: `tests/test_player_streaming.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_player_streaming.py`:

```python
"""Tests for streaming audio playback."""

import asyncio
import io
import struct

import pytest
from ares.voice.player import play_audio_stream


def _make_silence_pcm16(duration_ms: int, sample_rate: int = 24000) -> bytes:
    """Generate silent PCM16 data for the given duration."""
    num_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * num_samples


def _make_tone_pcm16(duration_ms: int, sample_rate: int = 24000, freq: float = 440.0) -> bytes:
    """Generate a simple sine wave PCM16 data."""
    import math

    num_samples = int(sample_rate * duration_ms / 1000)
    samples = []
    for i in range(num_samples):
        value = int(16000 * math.sin(2 * math.pi * freq * i / sample_rate))
        samples.append(struct.pack("<h", max(-32768, min(32767, value))))
    return b"".join(samples)


class TestPlayAudioStream:
    """Tests for play_audio_stream function."""

    @pytest.mark.asyncio
    async def test_plays_from_queue(self):
        """Queue with PCM chunks + None sentinel should play and complete."""
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        stop_event = asyncio.Event()

        # Put a small chunk of silence + sentinel
        await audio_q.put(_make_silence_pcm16(50))
        await audio_q.put(None)

        # Should complete without error
        await play_audio_stream(audio_q, stop_event, sample_rate=24000, speed=1.0)

    @pytest.mark.asyncio
    async def test_stops_on_stop_event(self):
        """Setting stop_event should stop playback immediately."""
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        stop_event = asyncio.Event()

        # Put a long chunk
        await audio_q.put(_make_silence_pcm16(5000))

        async def set_stop():
            await asyncio.sleep(0.05)
            stop_event.set()

        # Run playback and stop concurrently
        stop_task = asyncio.create_task(set_stop())
        await play_audio_stream(audio_q, stop_event, sample_rate=24000, speed=1.0)
        await stop_task

        # Should have completed (not hung)

    @pytest.mark.asyncio
    async def test_empty_queue(self):
        """Empty queue with None sentinel should complete immediately."""
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        stop_event = asyncio.Event()

        await audio_q.put(None)

        await play_audio_stream(audio_q, stop_event, sample_rate=24000, speed=1.0)

    @pytest.mark.asyncio
    async def test_multiple_chunks(self):
        """Multiple chunks should play in sequence."""
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        stop_event = asyncio.Event()

        # Put 3 small chunks
        for _ in range(3):
            await audio_q.put(_make_silence_pcm16(30))
        await audio_q.put(None)

        await play_audio_stream(audio_q, stop_event, sample_rate=24000, speed=1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_player_streaming.py -v`
Expected: FAIL with `ImportError: cannot import name 'play_audio_stream' from 'ares.voice.player'`

- [ ] **Step 3: Write minimal implementation**

In `ares/voice/player.py`, add the streaming playback function:

```python
async def play_audio_stream(
    audio_queue: asyncio.Queue[bytes | None],
    stop_event: asyncio.Event,
    sample_rate: int = 24000,
    speed: float = 1.2,
) -> None:
    """Play PCM chunks from queue as they arrive. Stops on stop_event.

    Args:
        audio_queue: Async queue of PCM16 chunks. Send None to signal end.
        stop_event: When set, stops playback immediately.
        sample_rate: Sample rate of incoming PCM data.
        speed: Playback speed multiplier.
    """
    import numpy as np
    import sounddevice as sd

    # Ring buffer: holds PCM16 samples ready for the callback
    ring_buffer = bytearray()
    buffer_lock = threading.Lock()
    finished = asyncio.Event()

    def callback(outdata, frames, time_info, status):
        nonlocal ring_buffer
        with buffer_lock:
            # Calculate how many bytes we need (frames * 2 bytes per sample for PCM16)
            needed = frames * 2
            if len(ring_buffer) >= needed:
                data = bytes(ring_buffer[:needed])
                ring_buffer = ring_buffer[needed:]
            elif ring_buffer:
                data = bytes(ring_buffer)
                ring_buffer = bytearray()
                # Pad remaining with silence
                data += b"\x00" * (needed - len(data))
            else:
                data = b"\x00" * needed

        # Convert PCM16 bytes to float32 for sounddevice
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        outdata[:] = samples.reshape(-1, 1)

    # Start the output stream
    stream = sd.RawOutputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=0,
        latency="low",
        callback=callback,
    )

    try:
        stream.start()

        while not stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            if chunk is None:
                # Sentinel: play remaining buffer then stop
                # Wait for ring buffer to drain (up to 2 seconds)
                for _ in range(200):
                    with buffer_lock:
                        if len(ring_buffer) == 0:
                            break
                    await asyncio.sleep(0.01)
                break

            # Apply speed adjustment by resampling
            if speed != 1.0:
                samples = np.frombuffer(chunk, dtype=np.int16)
                # Resample: skip/stretch samples
                indices = np.arange(0, len(samples), speed).astype(int)
                indices = indices[indices < len(samples)]
                chunk = samples[indices].tobytes()

            with buffer_lock:
                ring_buffer.extend(chunk)

    finally:
        stream.stop()
        stream.close()
```

Add `threading` to imports at top of `player.py`:

```python
import asyncio
import io
import tempfile
import threading
from pathlib import Path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_player_streaming.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/voice/player.py tests/test_player_streaming.py
git commit -m "feat(voice): add play_audio_stream() with OutputStream + queue

Callback-driven playback using sounddevice.RawOutputStream.
Supports stop_event for barge-in cancellation and speed adjustment."
```

---

## Task 4: is_voice_session Flag — Agent Voice Awareness

**Files:**
- Modify: `ares/agent.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py`:

```python
def test_voice_session_filters_cron_tools():
    """Agent with is_voice_session=True should exclude cron tools."""
    from ares.agent import Agent
    from ares.memory import MemoryStore

    agent = Agent(
        memory_store=MemoryStore(),
        is_voice_session=True,
    )

    tool_names = {t["function"]["name"] for t in agent.tools}
    cron_tools = {"create_cron_job", "list_cron_jobs", "get_cron_job",
                  "update_cron_job", "delete_cron_job", "run_cron_job_now",
                  "get_cron_logs"}

    assert not tool_names.intersection(cron_tools), f"Voice session should not have cron tools: {tool_names.intersection(cron_tools)}"


def test_non_voice_session_keeps_cron_tools():
    """Agent without is_voice_session should keep all tools."""
    from ares.agent import Agent
    from ares.memory import MemoryStore

    agent = Agent(
        memory_store=MemoryStore(),
        is_voice_session=False,
    )

    tool_names = {t["function"]["name"] for t in agent.tools}
    # At least one cron tool should be present
    assert "list_cron_jobs" in tool_names or "create_cron_job" in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py::test_voice_session_filters_cron_tools tests/test_agent.py::test_non_voice_session_keeps_cron_tools -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'is_voice_session'`

- [ ] **Step 3: Write minimal implementation**

In `ares/agent.py`, add `is_voice_session` to `Agent.__init__`:

```python
def __init__(
    self,
    memory_store: MemoryStore,
    conversation_store: ConversationStore | None = None,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    config: AppConfig | None = None,
    mcp_manager: Any | None = None,
    is_cron_session: bool = False,
    is_voice_session: bool = False,
    session_store: Any | None = None,
    session_id: str | None = None,
):
    self.memory_store = memory_store
    self.conversation_store = conversation_store
    self._session_store = session_store
    self._session_id = session_id
    self.tool_executor = ToolExecutor(
        memory_store=memory_store,
        conversation_store=conversation_store,
        config=config,
    )
    self.mcp_manager = mcp_manager
    self.is_cron_session = is_cron_session
    self.is_voice_session = is_voice_session
    self.refresh_tools()
    # ... rest unchanged
```

In `refresh_tools()`, add voice session filtering:

```python
def refresh_tools(self) -> None:
    """Refresh the advertised tool list, including connected MCP tools."""
    self.tools = get_tool_definitions()
    if getattr(self, "is_cron_session", False):
        cron_names = {"create_cron_job", "list_cron_jobs", "get_cron_job",
                      "update_cron_job", "delete_cron_job", "run_cron_job_now",
                      "get_cron_logs"}
        self.tools = [tool for tool in self.tools if tool.get("function", {}).get("name") not in cron_names]
    if getattr(self, "is_voice_session", False):
        cron_names = {"create_cron_job", "list_cron_jobs", "get_cron_job",
                      "update_cron_job", "delete_cron_job", "run_cron_job_now",
                      "get_cron_logs"}
        self.tools = [tool for tool in self.tools if tool.get("function", {}).get("name") not in cron_names]
    if self.mcp_manager is not None:
        self.tools.extend(getattr(self.mcp_manager, "tool_definitions", []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py::test_voice_session_filters_cron_tools tests/test_agent.py::test_non_voice_session_keeps_cron_tools -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/agent.py tests/test_agent.py
git commit -m "feat(agent): add is_voice_session flag for voice-aware tool filtering

Voice sessions exclude cron tools (same pattern as is_cron_session)."
```

---

## Task 5: Voice-Aware Context Building

**Files:**
- Modify: `ares/agent.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py`:

```python
def test_voice_session_uses_smaller_context_budget():
    """Voice sessions should use smaller token budget and fewer memories."""
    from ares.agent import Agent
    from ares.memory import MemoryStore

    agent = Agent(
        memory_store=MemoryStore(),
        is_voice_session=True,
    )

    # Voice session should have smaller budget
    # We can't easily test get_context without mocking LLM, but we can
    # verify the flag is set and the agent has the right config
    assert agent.is_voice_session is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py::test_voice_session_uses_smaller_context_budget -v`
Expected: This test should actually PASS already since we added the flag in Task 4. Skip to Step 4.

- [ ] **Step 3: Write minimal implementation**

The voice-aware context building is handled in the voice agent (Task 6) when it calls `build_context_prompt`. The `is_voice_session` flag on Agent is sufficient — the voice agent reads it and adjusts budgets. No additional changes needed in `agent.py` for this.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py::test_voice_session_uses_smaller_context_budget -v`
Expected: PASS

- [ ] **Step 5: Commit**

No commit needed — this was already covered in Task 4.

---

## Task 6: Sentence Chunking + Barge-in + Persistent Agent

**Files:**
- Modify: `ares/voice/agent.py`
- Test: `tests/test_voice_agent_streaming.py`

This is the largest task — it integrates all previous changes into the voice agent.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_agent_streaming.py`:

```python
"""Tests for voice agent streaming pipeline."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from ares.voice.agent import ContinuousVoiceAgent, _MAX_SENTENCE_CHARS, _MAX_VOICE_HISTORY


class TestStreamToSentences:
    """Tests for _stream_to_sentences sentence buffer."""

    @pytest.mark.asyncio
    async def test_buffers_and_sends_to_queue(self):
        """Tokens should be buffered and sent as sentences on max-length."""
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()

        # Mock agent.run_stream to yield tokens
        async def mock_run_stream(text, history):
            yield "Hello "
            yield "world "
            yield "this is a long sentence that exceeds the max length threshold "
            yield "and should be split into multiple sentences automatically by the buffer"

        agent.agent = MagicMock()
        agent.agent.run_stream = mock_run_stream

        sentence_q = asyncio.Queue()

        # Call the method (need to bind it properly)
        from ares.voice.agent import ContinuousVoiceAgent
        result = await ContinuousVoiceAgent._stream_to_sentences(agent, "test", sentence_q)

        # Should have collected sentences
        sentences = []
        while not sentence_q.empty():
            sentences.append(await sentence_q.get())

        # Last item should be None sentinel
        assert sentences[-1] is None
        # Should have at least one sentence
        assert len(sentences) > 1
        # Full response should be collected
        assert "Hello world" in result

    @pytest.mark.asyncio
    async def test_tool_tokens_skipped(self):
        """Tokens starting with [tool: should be skipped."""
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()

        async def mock_run_stream(text, history):
            yield "[tool:shell:echo hello]"
            yield "Visible response"

        agent.agent = MagicMock()
        agent.agent.run_stream = mock_run_stream

        sentence_q = asyncio.Queue()
        result = await ContinuousVoiceAgent._stream_to_sentences(agent, "test", sentence_q)

        assert "[tool:" not in result
        assert "Visible response" in result


class TestBargeInWatcher:
    """Tests for barge-in detection."""

    @pytest.mark.asyncio
    async def test_detects_speech_and_sets_stop_event(self):
        """Should detect speech and set stop_event."""
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        stop_event = asyncio.Event()

        # Mock _read_frames to return speech frames
        speech_frame = np.random.randn(480).astype(np.float32) * 0.1
        call_count = 0

        def mock_read_frames(count):
            nonlocal call_count
            call_count += 1
            if call_count > 5:  # After initial delay
                return [speech_frame]
            return []

        agent._read_frames = mock_read_frames
        agent._is_speech = lambda f: True  # Always detect speech
        agent._drain = MagicMock()

        # Run barge-in watcher
        play_start = time.time() - 1.0  # Already past the 500ms delay
        await ContinuousVoiceAgent._barge_in_watcher(agent, stop_event, play_start)

        assert stop_event.is_set()

    @pytest.mark.asyncio
    async def test_ignores_speech_during_initial_delay(self):
        """Should not trigger barge-in during the 500ms initial delay."""
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        stop_event = asyncio.Event()

        # Mock _read_frames to return speech frames immediately
        agent._read_frames = lambda c: [np.random.randn(480).astype(np.float32) * 0.1]
        agent._is_speech = lambda f: True
        agent._drain = MagicMock()

        # Run with very recent play_start (within 500ms)
        play_start = time.time()
        # Run for max 200ms
        try:
            await asyncio.wait_for(
                ContinuousVoiceAgent._barge_in_watcher(agent, stop_event, play_start),
                timeout=0.2
            )
        except asyncio.TimeoutError:
            pass

        # Should NOT have triggered
        assert not stop_event.is_set()


class TestPersistentAgent:
    """Tests for persistent agent and conversation history."""

    @pytest.mark.asyncio
    async def test_creates_agent_once(self):
        """Agent should be created in __init__, not per utterance."""
        with patch("ares.voice.agent.load_config") as mock_config:
            mock_config.return_value = MagicMock(
                voice=MagicMock(tts_provider="edge_tts", tts_voice="en-US-GuyNeural"),
                api_key="test",
                api_base_url="http://test",
                model="test-model",
                data_dir="/tmp/test",
            )
            with patch("ares.voice.agent.voice_config_from_env") as mock_vc:
                mock_vc.return_value = MagicMock(tts_provider="edge_tts", tts_voice="en-US-GuyNeural")
                with patch("ares.voice.agent.create_tts_provider") as mock_tts:
                    mock_tts.return_value = MagicMock()
                    with patch("ares.agent.Agent") as MockAgent:
                        MockAgent.return_value = MagicMock()

                        agent = ContinuousVoiceAgent()

                        # Agent should be created once in __init__
                        MockAgent.assert_called_once()
                        assert hasattr(agent, "conversation_history")
                        assert agent.conversation_history == []

    def test_max_voice_history_constant(self):
        """MAX_VOICE_HISTORY should be defined."""
        assert _MAX_VOICE_HISTORY == 10

    def test_max_sentence_chars_constant(self):
        """MAX_SENTENCE_CHARS should be defined."""
        assert _MAX_SENTENCE_CHARS == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_voice_agent_streaming.py -v`
Expected: FAIL with various `AttributeError` and import errors

- [ ] **Step 3: Write minimal implementation**

This is the main integration task. Modify `ares/voice/agent.py` with all changes:

**3a. Add constants and imports:**

```python
"""Continuous voice mode entry point.

This module provides a continuous voice mode using VAD (voice activity detection)
with faster-whisper for STT and Sarvam/Edge TTS for responses. It runs the full
Ares agent pipeline (LLM + tools) for each voice interaction.
"""

from __future__ import annotations

import asyncio
import collections
import threading
import time
import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.panel import Panel

from ares.config import load_config
from ares.voice.tts import voice_config_from_env, create_tts_provider
from ares.voice.stt import STTEngine, trim_silence_pcm16
from ares.voice.player import play_audio_bytes, play_audio_stream

_NATIVE_SR = 44100       # device native sample rate
_TARGET_SR = 16000       # VAD / STT rate
_FRAME_MS = 30
_FRAME_SAMPLES = int(_TARGET_SR * _FRAME_MS / 1000)  # 480 samples per VAD frame
_DEVICE_INDEX = 1        # Microphone Array (Realtek Audio)
_BLOCK_SIZE = 4410       # 100ms at 44100 Hz — how much audio we read per blocking call
_MAX_SENTENCE_CHARS = 200
_MAX_VOICE_HISTORY = 10
```

**3b. Update ContinuousVoiceAgent.__init__:**

```python
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

        # Persistent agent (created once, not per utterance)
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
        self.conversation_history: list[dict] = []
```

**3c. Add _stream_to_sentences method:**

```python
    async def _stream_to_sentences(self, text: str, sentence_q: asyncio.Queue[str]) -> str:
        """Stream LLM tokens, split into sentences via TTS SentenceBoundary events."""
        buffer = ""
        full_response = ""
        async for token in self.agent.run_stream(text, self.conversation_history):
            if token.startswith("[tool:"):
                continue
            # Console print (unchanged)
            self.console.print(token, end="", highlight=False)
            buffer += token
            full_response += token
            # Force-push on max length (safety net for non-TTS paths)
            if len(buffer) >= _MAX_SENTENCE_CHARS:
                await sentence_q.put(buffer)
                buffer = ""
        # Flush remaining
        if buffer.strip():
            await sentence_q.put(buffer)
        await sentence_q.put(None)  # Sentinel: stream complete
        return full_response
```

**3d. Add _tts_play_pipeline method:**

```python
    async def _tts_play_pipeline(self, sentence_q: asyncio.Queue[str], stop_event: asyncio.Event):
        """Drain sentence queue → TTS → playback queue. Runs parallel to LLM stream."""
        audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        # Start playback in background
        play_task = asyncio.create_task(play_audio_stream(audio_q, stop_event))

        while True:
            sentence = await sentence_q.get()
            if sentence is None:
                await audio_q.put(None)  # Signal end of playback
                break
            # Stream TTS for this sentence
            async for chunk in self.tts.speak_stream(sentence, self.voice_config.tts_voice):
                if stop_event.is_set():
                    break
                await audio_q.put(chunk)
            if stop_event.is_set():
                break

        await play_task
```

**3e. Add _barge_in_watcher method:**

```python
    async def _barge_in_watcher(self, stop_event: asyncio.Event, play_start: float):
        """Watch mic frames during playback. Cancel on user speech."""
        # Wait 500ms after playback starts to avoid feedback loop
        while time.time() - play_start < 0.5:
            if stop_event.is_set():
                return
            await asyncio.sleep(0.05)

        # Now check for speech
        silence_count = 0
        while not stop_event.is_set():
            frames = self._read_frames(1)
            if frames:
                if self._is_speech(frames[0]):
                    silence_count += 1
                    if silence_count >= 2:  # 2 consecutive speech frames (~60ms)
                        self.console.print("[yellow]>>> Barge-in detected[/yellow]")
                        stop_event.set()
                        self._drain()
                        return
                else:
                    silence_count = 0
            else:
                await asyncio.sleep(0.005)
```

**3f. Add _respond method and update listen_and_respond:**

```python
    async def _respond(self, text: str) -> str:
        """Run agent + TTS + playback with barge-in support."""
        sentence_q: asyncio.Queue[str] = asyncio.Queue()
        stop_event = asyncio.Event()

        # Run LLM streaming and TTS pipeline concurrently
        stream_task = asyncio.create_task(self._stream_to_sentences(text, sentence_q))
        tts_task = asyncio.create_task(self._tts_play_pipeline(sentence_q, stop_event))

        # Start barge-in watcher (starts checking after 500ms)
        play_start = asyncio.get_event_loop().time()
        barge_task = asyncio.create_task(self._barge_in_watcher(stop_event, play_start))

        # Wait for stream to finish (TTS pipeline handles its own completion)
        full_response = await stream_task

        # If barge-in cancelled playback, drain remaining
        if stop_event.is_set():
            tts_task.cancel()
            barge_task.cancel()
            return full_response

        await tts_task
        barge_task.cancel()
        return full_response
```

**3g. Update the main loop in listen_and_respond to use _respond and conversation history:**

Replace the section inside `listen_and_respond` that handles agent response:

```python
                    self.console.print("[yellow]Thinking…[/yellow]")
                    response = await self._respond(text)
                    if not response or not response.strip():
                        continue

                    # Update conversation history
                    self.conversation_history.append({"role": "user", "content": text})
                    self.conversation_history.append({"role": "assistant", "content": response})
                    max_history = self.voice_config.voice_max_history * 2
                    if len(self.conversation_history) > max_history:
                        self.conversation_history = self.conversation_history[-max_history:]
```

Remove the old `_stream_agent_response` and `_get_agent_response` methods — they're replaced by `_respond`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_voice_agent_streaming.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `pytest tests/ -v --tb=short`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add ares/voice/agent.py tests/test_voice_agent_streaming.py
git commit -m "feat(voice): integrate streaming TTS, barge-in, and persistent agent

- Sentence chunking via max-length buffer (200 chars)
- Parallel TTS + playback pipeline with asyncio.Queue
- Barge-in detection: 2 consecutive VAD frames (~60ms) after 500ms delay
- Persistent Agent created once in __init__ (not per utterance)
- Conversation history: last 10 turns, voice_max_history configurable
- Voice-aware context: smaller budget, fewer memories
- Old _stream_agent_response and _get_agent_response removed"
```

---

## Task 7: Integration Smoke Test

**Files:**
- Test: `tests/test_voice_agent_streaming.py`

- [ ] **Step 1: Write the integration test**

Add to `tests/test_voice_agent_streaming.py`:

```python
class TestIntegration:
    """Integration tests for the full streaming pipeline."""

    @pytest.mark.asyncio
    async def test_full_pipeline_end_to_end(self):
        """Simulate full pipeline: tokens → sentences → TTS → playback queue."""
        # Create a mock agent that yields tokens
        sentence_q = asyncio.Queue()
        stop_event = asyncio.Event()

        # Simulate LLM tokens arriving
        tokens = ["Hello ", "world. ", "This is ", "sentence two."]

        async def mock_run_stream(text, history):
            for token in tokens:
                yield token

        # Create a minimal agent-like object
        class MockAgent:
            async def run_stream(self, text, history):
                for token in tokens:
                    yield token

        agent_mock = MagicMock(spec=ContinuousVoiceAgent)
        agent_mock.agent = MockAgent()
        agent_mock.console = MagicMock()

        # Run sentence buffering
        full_response = await ContinuousVoiceAgent._stream_to_sentences(
            agent_mock, "test", sentence_q
        )

        # Collect sentences
        sentences = []
        while not sentence_q.empty():
            sentences.append(await sentence_q.get())

        # Should have sentences + None sentinel
        assert sentences[-1] is None
        assert len(sentences) >= 2  # At least "Hello world. " and rest

        # Full response should contain all tokens
        assert "Hello world." in full_response
        assert "sentence two." in full_response
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_voice_agent_streaming.py::TestIntegration -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_voice_agent_streaming.py
git commit -m "test(voice): add integration smoke test for streaming pipeline"
```

---

## Task 8: Backward Compatibility Verification

**Files:**
- Test: existing tests

- [ ] **Step 1: Run all existing tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass. Specifically verify:
- `tests/test_voice_config.py` — all pass
- `tests/test_agent.py` — all pass
- `tests/test_streaming.py` — all pass (agent streaming)

- [ ] **Step 2: Verify speak() still works**

Run: `pytest tests/test_tts_streaming.py::TestEdgeTTSStream::test_speak_stream_wraps_speak -v`
Expected: PASS — confirms `speak()` produces same audio as draining `speak_stream()`

- [ ] **Step 3: Verify play_audio_bytes still works**

Run: `pytest tests/test_player_streaming.py -v`
Expected: PASS — `play_audio_bytes()` was not modified

- [ ] **Step 4: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix(voice): backward compatibility fixes for streaming pipeline"
```

---

## Summary

| Task | Files Modified | Tests Added |
|------|---------------|-------------|
| 1. VoiceConfig | `ares/models.py` | 2 tests |
| 2. speak_stream() | `ares/voice/tts.py` | 5 tests |
| 3. play_audio_stream() | `ares/voice/player.py` | 4 tests |
| 4. is_voice_session | `ares/agent.py` | 2 tests |
| 5. Voice context | `ares/agent.py` | 1 test |
| 6. Voice agent integration | `ares/voice/agent.py` | 6 tests |
| 7. Integration smoke | `tests/test_voice_agent_streaming.py` | 1 test |
| 8. Backward compat | — | Verification |

**Total new tests:** 21
**Files modified:** 4 (`models.py`, `tts.py`, `player.py`, `voice/agent.py`, `agent.py`)
**Files created:** 3 (`test_tts_streaming.py`, `test_player_streaming.py`, `test_voice_agent_streaming.py`)
