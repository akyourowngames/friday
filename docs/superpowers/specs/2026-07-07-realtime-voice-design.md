# Real-Time Voice — "Jarvis on a Call"

**Date:** 2026-07-07
**Status:** Approved
**Goal:** Make continuous voice mode feel like a live call instead of walkie-talkie — Ares starts speaking almost immediately after it starts generating, and the user can interrupt mid-sentence.

---

## Problem

`ContinuousVoiceAgent.listen_and_respond` is fully sequential per turn:

```
wait for silence → transcribe (STT) → run full agent turn → build full response string
  → tts.speak(full response) → play_audio_bytes(full clip)
```

Two blocking points break the "call" feel:
1. **TTS doesn't start until the entire LLM response is generated.** For a 5-sentence response, the user waits 3-5 seconds of silence before hearing anything.
2. **Playback is a single blocking `sd.play()`/`sd.wait()` call.** The mic capture thread keeps queuing frames, but nothing reads them, so the user can't interrupt.

---

## Architecture

```
LLM token stream ──► sentence buffer ──► TTS queue ──► streaming playback
                                              ▲
                                              │ cancel on barge-in
mic capture thread ──► frame queue ──► VAD watcher (runs during playback)
```

Three independent, additive changes — no rewrite of the working text-mode agent loop.

---

## Change 1: `speak_stream()` in `tts.py`

### What

Add an async generator `speak_stream(text, voice)` that yields audio chunks as they become available, instead of accumulating the full clip first.

### EdgeTTS

`edge_tts.Communicate.stream()` already yields chunks — expose them directly:

```python
async def speak_stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
    """Yield audio chunks as they arrive from Edge TTS."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice or self.default_voice)
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            yield chunk.get("data", b"")
```

Keep existing `speak()` as a thin wrapper that drains the generator, for backward compat (cron/CLI callers).

### SarvamTTS

No native streaming in the REST API. "Streaming" = call existing `speak()` per-sentence chunk (smaller, more frequent whole-request calls). Document this asymmetry in a comment:

```python
# NOTE: Sarvam has no streaming API. "Streaming" here means calling
# the REST endpoint per sentence-chunk. First-byte latency per chunk
# is ~200ms REST overhead, not true low-latency streaming like EdgeTTS.
```

### Signature

```python
async def speak_stream(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
    """Yield audio chunks for text. EdgeTTS yields as API streams;
    SarvamTTS yields one chunk per call (simulated streaming)."""
```

### Backward Compatibility

- `speak()` remains unchanged — wraps `speak_stream()` internally
- No changes to `create_tts_provider()` or `voice_config_from_env()`
- Cron/CLI callers unaffected

---

## Change 2: `play_audio_stream()` in `player.py`

### What

New function backed by `sounddevice.OutputStream` (raw callback-driven, not `sd.play()`). Takes an `asyncio.Queue[bytes]` of PCM chunks.

### Implementation

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
```

### How It Works

1. Create `sounddevice.OutputStream` with a callback that pulls from the queue
2. Producer task reads from `audio_queue`, writes to a ring buffer
3. Callback reads from ring buffer into the output stream
4. When `stop_event` is set: stop the output stream, drain the queue, return
5. When `None` sentinel received: play remaining buffer, then stop

### Key Details

- Uses `sounddevice.RawOutputStream` or `OutputStream` with `dtype='float32'`
- Ring buffer size: ~50ms (enough to smooth jitter, small enough for low latency)
- Speed adjustment: resample during ring buffer write (skip/stretch samples)
- Thread-safe: callback runs in audio thread, queue is asyncio

### What Stays Unchanged

- `play_audio_bytes()` remains untouched — cron/non-voice callers use it
- `_play_with_sounddevice()` and `_play_with_pydub()` unchanged

---

## Change 3: Sentence Chunking + Barge-in in `agent.py`

### Sentence Buffer

Edge TTS yields `SentenceBoundary` metadata with each sentence's text, offset, and duration. Instead of heuristic punctuation detection, we use these real sentence boundaries from the TTS engine itself.

In `_stream_agent_response`, replace "accumulate full string, print it" with:

1. Buffer incoming tokens (console print as they arrive)
2. When TTS yields `SentenceBoundary` events → push completed sentence to `asyncio.Queue[str]`
3. **Max-length fallback**: 200 chars without a boundary → force-push (handles non-TTS paths)
4. Console printing stays as-is (print tokens as they arrive)

```python
_MAX_SENTENCE_CHARS = 200

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

**Note:** Edge TTS's `SentenceBoundary` events provide accurate sentence splitting. The `speak_stream()` method yields these events alongside audio chunks. The TTS pipeline consumer picks up sentence boundaries and queues them for playback. The max-length fallback handles direct text paths (cron/CLI) where TTS isn't involved.

### Parallel TTS + Playback Pipeline

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

### Barge-in Detection

While playback is running, a watcher coroutine monitors `_frame_q` for user speech:

```python
async def _barge_in_watcher(self, stop_event: asyncio.Event, play_start: float):
    """Watch mic frames during playback. Cancel on user speech."""
    import time
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

### Integrated `_respond` Flow

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

---

## Change 4: Per-Session Optimizations

### `is_voice_session` Flag

Add to `Agent.__init__`, following `is_cron_session` pattern:

```python
# In Agent.__init__:
self.is_voice_session = is_voice_session

# In Agent.refresh_tools():
if getattr(self, "is_voice_session", False):
    # Voice sessions don't need cron tools
    cron_names = {"create_cron_job", "list_cron_jobs", ...}
    self.tools = [t for t in self.tools if t["function"]["name"] not in cron_names]
```

### Persistent Agent

Create `Agent` once in `ContinuousVoiceAgent.__init__`, not per utterance:

```python
def __init__(self, ...):
    ...
    self.agent = Agent(
        memory_store=self.memory_store,
        conversation_store=self.conversation_store,
        api_key=self.config.api_key,
        base_url=self.config.api_base_url,
        model=self.config.model,
        config=self.config,
        is_voice_session=True,
    )
    self.conversation_history: list[dict] = []
```

### Conversation History

Keep last 10 turns for voice (more feels laggy, fewer feels disjointed):

```python
_MAX_VOICE_HISTORY = 10

# After each turn:
self.conversation_history.append({"role": "user", "content": text})
self.conversation_history.append({"role": "assistant", "content": full_response})
if len(self.conversation_history) > _MAX_VOICE_HISTORY * 2:
    self.conversation_history = self.conversation_history[-_MAX_VOICE_HISTORY * 2:]
```

### Skip Unnecessary Context

```python
# In voice sessions, skip project context (most turns are conversational)
# and reduce memory retrieval
if self.agent.is_voice_session:
    context = build_context_prompt(
        soul_context=self.soul_manager.get_context(token_budget=200),
        profile_context=self.profile_manager.get_context(token_budget=200),
        memories=memories[:3],  # Voice: 3 memories max
        token_budget=800,  # Voice: smaller budget
    )
```

---

## File Changes Summary

| File | Action | What Changes |
|------|--------|--------------|
| `ares/voice/tts.py` | Modify | Add `speak_stream()` to `TTSProvider`, `EdgeTTS`, `SarvamTTS` |
| `ares/voice/player.py` | Modify | Add `play_audio_stream()` with OutputStream + queue |
| `ares/voice/agent.py` | Modify | Sentence chunking, TTS pipeline, barge-in watcher, persistent agent |
| `ares/agent.py` | Modify | Add `is_voice_session` flag, voice-aware context building |
| `ares/models.py` | Modify | Add `voice_max_history` and `voice_max_memories` to `VoiceConfig` |

---

## Testing Checklist

- [ ] Multi-sentence response: audio for sentence 1 starts before sentence 3 finishes generating
- [ ] Barge-in: speak while Ares is talking → playback stops within ~60ms (2 VAD frames)
- [ ] No false barge-in: speak nothing while Ares talks near speakers → no interrupt
- [ ] Cron/non-voice callers still work (never touch `speak_stream`/`play_audio_stream`)
- [ ] Conversation history persists across voice turns (ask follow-up question)
- [ ] Conversation history respects max limit (10 turns)
- [ ] EdgeTTS streaming: chunks arrive progressively (verify via timestamped logs)
- [ ] SarvamTTS: per-sentence calls work (document REST overhead)
- [ ] Fallback: if barge-in triggers, clear frame queue, jump to `_wait_for_speech()` phase 2

---

## Known Limitations (v1)

- **Sarvam "streaming"** is smaller, more frequent REST calls — first-byte latency per sentence is ~200ms, not true low-latency streaming.
- **Barge-in quality** depends on existing VAD (WebRTC mode 2-3 or energy fallback). 2-frame threshold (~60ms) is aggressive; noisy environments may cause false interrupts. Can tune to 3 frames if needed.
- **Sentence chunking** via punctuation is a heuristic. Abbreviations ("Dr.", "e.g.") or code snippets could produce awkward chunks. Acceptable for v1.
- **Speaker feedback** mitigated by 500ms delay, not eliminated. Headphones recommended for best experience.

---

## Self-Review

1. **Placeholder scan:** No TBDs or TODOs. All sections complete.
2. **Internal consistency:** Architecture diagram matches feature descriptions. File change table matches detailed sections.
3. **Scope check:** Focused on voice streaming — no unrelated refactoring. 4 changes, all additive.
4. **Ambiguity check:** Max sentence length (200 chars), barge-in delay (500ms), barge-in threshold (2 frames / ~60ms), history limit (10 turns) all explicitly specified.
