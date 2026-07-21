# Ares Desktop Voice Assistant — Design Spec

**Date:** 2026-07-21
**Status:** Draft

## Overview

Add a background desktop voice assistant to Ares via `python -m ares --desktop`. The assistant lives in the Windows system tray with a floating status window, activated by a global push-to-talk hotkey (Ctrl+Space). It reuses the existing Agent, STT (faster-whisper), TTS (Edge TTS), and audio playback infrastructure — the only new code is the tray/window/hotkey orchestration layer.

---

## Goals

1. Ares runs as a background process with a system tray icon
2. Global hotkey (Ctrl+Space) activates push-to-talk — hold to speak, release to process
3. Multi-language speech (Hindi, English, Hinglish) transcribed to English automatically
4. Ares always speaks its response back via Edge TTS (voice assistant style)
5. Floating status window shows current state (Listening / Thinking / Speaking / Idle / Muted)
6. Desktop toast notifications for proactive messages and errors
7. Tray menu with recent history, mute toggle, session control
8. Shares Agent, config, memory, and conversation store with the existing CLI

---

## Architecture

```
python -m ares --desktop
         |
         v
+-----------------------------------------+
|           DesktopVoiceAgent             |
|  (ares/desktop/agent.py)                |
|                                         |
|  +----------+  +----------+  +-------+ |
|  | TrayIcon |  | FloatWin |  | Hotkey| |
|  | pystray  |  |customtkin|  |pynput | |
|  +----+-----+  +----+-----+  +---+---+ |
|       |              |            |      |
|       +--------------+------------+      |
|                      v                   |
|              +--------------+            |
|              |  VoiceEngine |            |
|              | (reuses existing)         |
|              |  - MicrophoneFrames       |
|              |  - WhisperTranscriber     |
|              |  - EdgeTTS                |
|              |  - play_audio_stream      |
|              +------+-------+            |
|                     v                    |
|              +--------------+            |
|              |  Agent       |            |
|              | (existing)   |            |
|              |  - run_stream|            |
|              |  - tools     |            |
|              |  - memory    |            |
|              +--------------+            |
+-----------------------------------------+
```

This is a new frontend for the existing Ares runtime. It reuses `Agent`, `EdgeTTS`, `WhisperTranscriber`, `MicrophoneFrames`, `play_audio_stream`, `ConversationStore`, `MemoryStore` — all existing code. The only new code is the tray/window/hotkey orchestration layer.

### Data Flow

```
User holds Ctrl+Space
  -> pynput detects keydown
  -> MicrophoneFrames starts capturing
  -> User releases Ctrl+Space
  -> Captured audio -> trim silence -> WhisperTranscriber (Hindi/Eng/Hinglish -> English)
  -> Transcribed text -> Agent.run_stream()
  -> Response text -> EdgeTTS.stream()
  -> Audio -> play_audio_stream (speaks back)
  -> Status window: Listening -> Thinking -> Speaking -> Idle
```

---

## Floating Status Window

A small, always-on-top, semi-transparent customtkinter window.

### Visual Style

Modern minimal — like a macOS Touch Bar widget or Spotify mini-player. Dark theme, no emojis, typography-driven.

```
+-----------------------------------+
|  *  Listening...            ===   |
|  /////////////////////////////    |
+-----------------------------------+
```

### States

| State | Indicator | Text | Color |
|-------|-----------|------|-------|
| Listening | Bright dot + animated progress bar (audio waveform) | "Listening..." | Cyan |
| Thinking | Pulsing dot + indeterminate barber-pole bar | "Thinking..." | Amber |
| Speaking | Animated equalizer bars (3-4 bouncing bars) | "Speaking..." | Blue |
| Idle | Dim dot | "Ready" | Gray |
| Muted | Red dot | "Muted" | Red |

### Behavior

- `CTkToplevel` with `overrideredirect(True)` (no title bar)
- `attributes('-alpha', 0.85)` for slight transparency
- `attributes('-topmost', True)` always on top
- Dark theme (`customtkinter.set_appearance_mode("dark")`)
- Subtle shadow effect via a second transparent window behind it
- ~200x60px — compact, doesn't obstruct work
- Dragable by mouse
- Appears centered at bottom-right of screen (above taskbar)
- Auto-hides to tray after 3 seconds of idle
- Clicking the tray icon brings it back
- Smooth state transitions with 200ms fade animations

---

## System Tray Icon

### Left-click

Shows/hides the floating status window.

### Right-click Menu

Clean, modern dark theme. No emojis. Typography-driven with subtle separators.

```
  Ares Desktop
  ----------------------------
  New Session
  Status
  ----------------------------
  Recent
    "What time is it in Tokyo"
    "Summarize my last email"
    "Open the project folder"
  ----------------------------
  Mute TTS          [ON]
  ----------------------------
  Quit
```

### Design Principles

- Dark background (`#1a1a2e` or similar deep navy/charcoal)
- Thin border, no heavy separators — subtle horizontal lines
- Clean sans-serif font (Segoe UI on Windows)
- Mute toggle uses `[ON]`/`[OFF]` text indicator
- Recent history shows actual recent voice exchanges (last 5, truncated to ~40 chars)
- Hover highlight on each item (`#2a2a4a`)
- No icons anywhere — pure clean text

### Tray Icon

Custom icon — the Ares "A" logo or a simple colored circle that changes based on state:
- Gray = idle
- Cyan = listening
- Blue = speaking

---

## Hotkeys & Interaction

### Global Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+Space` | Push-to-talk: hold to listen, release to process |
| `Ctrl+Shift+M` | Toggle mute/unmute TTS |
| `Ctrl+Shift+H` | Show/hide status window |

### Push-to-Talk Flow

1. User presses Ctrl+Space
2. Status: "Listening..." (cyan dot, progress bar)
3. MicrophoneFrames captures audio continuously
4. User releases Ctrl+Space
5. Audio -> trim silence -> WhisperTranscriber (any language -> English)
6. Status: "Thinking..." (amber dot, barber-pole)
7. Transcribed text -> Agent.run_stream()
8. Response text -> EdgeTTS.stream() -> play_audio_stream
9. Status: "Speaking..." (blue equalizer bars)
10. Audio finishes -> Status: "Ready" -> auto-hide after 3s

### Edge Cases

- **User presses Ctrl+Space while Ares is speaking** -> barge-in: stop TTS, start listening
- **No speech detected after 5 seconds** -> status: "No speech detected" -> return to idle
- **Transcription returns empty** -> status: "Could not understand" -> return to idle
- **Agent errors** -> status: "Error" + desktop notification with error summary
- **User presses Ctrl+Space while in "Thinking" state** -> ignored (status: "Please wait...") — only one turn at a time to prevent context pollution

---

## File Structure

### New Files

| File | Purpose |
|------|---------|
| `ares/desktop/__init__.py` | Module init |
| `ares/desktop/agent.py` | `DesktopVoiceAgent` — orchestrates tray, window, hotkey, voice pipeline |
| `ares/desktop/tray.py` | `TrayIcon` — pystray setup, menu, icon state management |
| `ares/desktop/window.py` | `StatusWindow` — customtkinter floating status window |
| `ares/desktop/hotkey.py` | `HotkeyListener` — pynput global hotkey wrapper |
| `ares/desktop/history.py` | `HistoryStore` — last N exchanges for tray menu |

### Modified Files

| File | Change |
|------|--------|
| `ares/__main__.py` | Add `--desktop` flag, `_run_desktop()` entry point |
| `ares/models.py` | Add `DesktopConfig` to `AppConfig` (hotkey bindings, window position, etc.) |
| `pyproject.toml` | Add `desktop` optional dependency group |

---

## Dependencies

```toml
[project.optional-dependencies]
desktop = [
    "pystray>=0.19",
    "customtkinter>=5.2",
    "pynput>=1.7",
]
```

`Pillow` is already a project dependency (needed by pystray for tray icon images). The desktop extra adds `pystray`, `customtkinter`, and `pynput`.

---

## Configuration

New `DesktopConfig` model added to `AppConfig`:

```python
class DesktopConfig(BaseModel):
    enabled: bool = False
    hotkey_ptt: str = "ctrl+space"       # push-to-talk
    hotkey_mute: str = "ctrl+shift+m"    # toggle mute
    hotkey_window: str = "ctrl+shift+h"  # show/hide window
    window_x: int = -1                    # -1 = auto (bottom-right)
    window_y: int = -1
    window_opacity: float = 0.85
    auto_hide_seconds: int = 3
    history_size: int = 5
```

---

## Error Handling

### Graceful Degradation

- `pynput` not installed -> print clear error: `Install desktop extras: pip install ares[desktop]`
- No microphone -> tray icon shows "No mic" state, push-to-talk disabled but tray menu still works
- Edge TTS network failure -> status: "TTS unavailable" + desktop notification with text response instead
- Whisper model load failure -> status: "Speech recognition unavailable" + fallback to text-only input via tray menu
- Hotkey registration fails (another app owns Ctrl+Space) -> print warning, suggest alternative hotkey via config

### Desktop Notifications

- Uses `plyer` (already a dependency) for Windows toast notifications
- Fires when: proactive service has something to say, watcher alerts, errors during voice turns
- Notification click -> shows status window

### Thread Safety

- Tray runs in its own thread (pystray requirement)
- Voice pipeline runs in asyncio event loop (main thread)
- Communication via `threading.Event` and `asyncio.Queue`
- Status window updates via `root.after()` (tkinter thread-safe scheduling)

---

## Startup & Shutdown

### Startup

```
python -m ares --desktop
  -> Load config
  -> Initialize Agent (reuse existing)
  -> Start tray icon thread
  -> Create status window (hidden)
  -> Register global hotkeys
  -> Status: "Ready"
  -> Wait for Ctrl+Space
```

### Shutdown

- Tray menu "Quit" -> stops hotkey listener, closes window, stops agent, exits
- Window close (X) -> minimizes to tray (doesn't quit)
- `Ctrl+C` in terminal -> graceful shutdown

---

## What This Does NOT Include (Future Considerations)

- Always-on mode with wake word ("Hey Ares")
- Multiple hotkey modes (push-to-ask vs push-to-command)
- Voice activity visualization beyond the status window
- Speaker diarization
- Custom voice cloning
- Multi-monitor window positioning logic
- Keyboard text input as alternative to voice (text-only mode via window)
