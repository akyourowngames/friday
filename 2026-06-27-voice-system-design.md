# Ares Voice System — Design Spec

**Date:** 2026-06-27
**Status:** Draft

## Overview

Add speech-in/speech-out to Ares with two modes: push-to-talk (inline in the normal CLI) and continuous voice (`--voice` flag using LiveKit). Three switchable TTS providers: edge-tts, Sarvam AI, and LiveKit's full pipeline. STT is always local via faster-whisper.

---

## Modes

### Mode 1: Push-to-Talk (default CLI)

No extra flag needed. Inside the normal `ares` prompt, the user holds a hotkey (default: Space), speaks, releases. Ares transcribes the audio, processes normally, then speaks the response back.

**Flow:**
```
1. User presses Space → keyboard listener fires
2. sounddevice starts capturing from default mic
3. User releases Space → capture stops
4. WebRTC VAD trims silence from start/end
5. faster-whisper (tiny model, CPU) transcribes
6. Transcribed text injected as user input to Ares LLM
7. Ares generates response
8. Response text sent to selected TTS provider
9. Audio played back via sounddevice
10. Loop returns to prompt
```

**Hotkey config:** Configurable via `voice.hotkey` in config. Uses `pynput` or `keyboard` library for global hotkey listener.

**Overlap with typing:** The user can type or use voice interchangeably in the same session. Push-to-talk is additive — it doesn't replace the text prompt.

**VAD:** WebRTC VAD (via `webrtcvad` package) for trimming. Lighter than silero-vad, sufficient for push-to-talk where we know boundaries at release time.

### Mode 2: Continuous Voice (`--voice` flag)

**Flag:** `python -m ares --voice`

Runs Ares as a LiveKit voice agent. Full duplex conversation with VAD-driven turn detection, no key holding.

**LiveKit setup:**
- LiveKit server runs as background process (single Go binary, ~50MB RAM)
- User starts it via `livekit-server --dev` or Ares auto-starts it
- Agent connects via `livekit-agents` Python SDK as a room participant
- WebRTC handles audio transport between mic → agent → speakers

**Agent pipeline:**
```
LiveKit audio stream → VAD (built-in) → STT (faster-whisper via plugin)
  → Ares LLM (via tool-compatible adapter) → TTS via provider → audio out
```

**Interruptions:** LiveKit Agent framework handles turn detection naturally — if the user speaks while Ares is talking, Ares stops and listens.

When `--voice` is active, the CLI shows a visual indicator of the conversation state (listening, thinking, speaking) and also prints the transcript.

---

## TTS Provider System

### Common Interface

All TTS providers implement the same async interface:

```python
class TTSProvider:
    """Abstract TTS provider."""
    async def speak(self, text: str, voice: str = "") -> bytes:
        """Return WAV audio bytes for the given text."""
        ...

    def list_voices(self) -> list[dict]:
        """Return available voices with name, gender, language info."""
        ...
```

### Provider: edge-tts

| Property | Value |
|----------|-------|
| Package | `edge-tts` (already installed v7.2.8) |
| API key | None (free, uses Microsoft Edge's servers) |
| Best for | Natural English |
| Voices | Hundreds — Microsoft's neural TTS voices |
| Latency | ~200-500ms first byte (HTTP to Microsoft) |
| Network req'd | Yes (calls Microsoft's TTS API) |
| Quality | Excellent — natural, emotional range |

Usage:
```python
import edge_tts
communicate = edge_tts.Communicate(text, voice="en-US-JennyNeural")
audio = b""
async for chunk in communicate.stream():
    audio += chunk["audio"]
```

### Provider: Sarvam AI

| Property | Value |
|----------|-------|
| Package | `sarvamai` (PyPI) |
| API key | Required (free tier: 100 credits) |
| Best for | Indian languages (Hindi, Tamil, Bengali, Telugu, etc. — 11 languages) |
| Voices | 35+ voices with emotional styles |
| Latency | ~200ms first byte (WebSocket streaming) |
| Network req'd | Yes |
| Quality | Good — specifically optimized for Indian languages |

Configuration:
```json
{
  "voice": {
    "tts_provider": "sarvam",
    "sarvam_api_key": "..."
  }
}
```

Or via env var: `SARVAM_API_KEY=...`

### Provider: LiveKit (for `--voice` mode)

In `--voice` mode, the LiveKit Agent framework handles the full audio pipeline. TTS is provided via LiveKit plugins or can be routed through edge-tts/Sarvam within the agent.

LiveKit mode doesn't use the `TTSProvider` interface directly — instead it builds the full pipeline via LiveKit's `VoicePipelineAgent`:
- STT: LiveKit's faster-whisper plugin or custom
- LLM: Ares's existing LLM via LiveKit's LLM adapter
- TTS: LiveKit's TTS plugin or edge-tts/Sarvam wrapper

For push-to-talk mode, LiveKit-as-TTS-provider is not relevant — it's purely for the continuous duplex pipeline.

---

## STT: faster-whisper

| Property | Value |
|----------|-------|
| Package | `faster-whisper` (already installed v1.2.1) |
| Model | `tiny` (default) — ~500MB RAM, ~2-4x realtime on CPU |
| Alternatives | `base`, `small` models — more accurate but slower |
| Config | `voice.stt_model` in config or `ARES_STT_MODEL` env var |
| Compute | Local CPU via CTranslate2 |

The `tiny` model is the right default for this machine (Intel HD 520, no CUDA). It runs ~2-4x faster than realtime on CPU, meaning 5 seconds of speech transcribes in ~1-2 seconds.

faster-whisper is a drop-in replacement for OpenAI Whisper that runs 4x faster on CPU via CTranslate2 int8 quantization.

---

## Config Changes

New section in `~/.ares/config.json`:

```json
{
  "voice": {
    "enabled": true,
    "tts_provider": "edge_tts",
    "hotkey": "space",
    "stt_model": "tiny",
    "sarvam_api_key": "",
    "livekit_url": "",
    "livekit_api_key": "",
    "livekit_api_secret": ""
  }
}
```

Env var overrides:
- `ARES_TTS_PROVIDER` — override TTS provider
- `ARES_STT_MODEL` — override STT model size
- `SARVAM_API_KEY` — Sarvam API key
- `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` — LiveKit connection

---

## Files

| File | New/Existing | Purpose |
|------|-------------|---------|
| `ares/voice/__init__.py` | New | Module init, exports |
| `ares/voice/tts.py` | New | `TTSProvider` interface, `EdgeTTS`, `SarvamTTS` implementations |
| `ares/voice/stt.py` | New | `faster-whisper` transcription wrapper with VAD trim |
| `ares/voice/listener.py` | New | Hotkey push-to-talk mic capture via `sounddevice` |
| `ares/voice/agent.py` | New | LiveKit `--voice` agent pipeline |
| `ares/voice/player.py` | New | Audio playback via `sounddevice` |
| `ares/__main__.py` | Modify | Add `--voice` argument |
| `ares/cli.py` | Modify | Wire push-to-talk listener, voice response after LLM |
| `ares/models.py` | Modify | Add `VoiceConfig` to `AppConfig` |
| `pyproject.toml` | Modify | Add optional deps: `livekit-agents`, `pynput`, `webrtcvad` |

---

## Dependencies

| Package | Purpose | Required for |
|---------|---------|-------------|
| `sounddevice` | Mic capture + audio playback | Already installed |
| `faster-whisper` | Local STT | Already installed |
| `edge-tts` | TTS provider | Already installed |
| `numpy` | Audio buffer manipulation | Already installed |
| `webrtcvad` | Voice activity detection for push-to-talk | New |
| `pynput` or `keyboard` | Global hotkey listener | New |
| `sarvamai` | Sarvam AI TTS | New (optional) |
| `livekit-agents` | LiveKit voice pipeline | New (optional) |

---

## Error Handling

- **No mic found:** Graceful fallback — Ares logs "No microphone detected, voice disabled" and continues in text-only mode.
- **TTS provider down:** If edge-tts fails (network), fall back to no-TTS (text-only response). Log the error but don't crash.
- **STT fails to load:** If faster-whisper can't load the model (disk/memory), log and disable voice features.
- **LiveKit not available:** `--voice` flag requires `livekit-agents` installed. If missing, print clear install instructions and exit.
- **Hotkey conflict:** If global hotkey can't register, log warning and fall back to text-only.

---

## Future Considerations (Not In Scope)

- Multiple hotkeys for different modes (e.g. push-to-ask vs push-to-command)
- Voice activity visualization in the terminal
- Wake word ("Hey Ares")
- Speaker diarization
- Custom voice cloning via Orpheus-TTS or Qwen3-TTS (when GPU available)
