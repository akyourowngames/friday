# Desktop Voice Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a background desktop voice assistant to Ares via `python -m ares --desktop` with system tray, floating status window, push-to-talk hotkey, and Edge TTS speech output.

**Architecture:** New `ares/desktop/` module orchestrates pystray (tray icon), customtkinter (floating status window), and pynput (global hotkey). Reuses existing `Agent`, `EdgeTTS`, `WhisperTranscriber`, `MicrophoneFrames`, and `play_audio_stream` from `ares/voice/`. The desktop module is a new frontend — no changes to the existing voice or agent internals.

**Tech Stack:** pystray, customtkinter, pynput, Pillow (existing), plyer (existing), edge-tts (existing), faster-whisper (existing), sounddevice (existing)

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `ares/desktop/__init__.py` | Create | Module init, exports `run_desktop` |
| `ares/desktop/agent.py` | Create | `DesktopVoiceAgent` — main orchestrator |
| `ares/desktop/tray.py` | Create | `TrayIcon` — pystray tray icon + menu |
| `ares/desktop/window.py` | Create | `StatusWindow` — customtkinter floating window |
| `ares/desktop/hotkey.py` | Create | `HotkeyListener` — pynput global hotkey wrapper |
| `ares/desktop/history.py` | Create | `HistoryStore` — last N exchanges for tray menu |
| `ares/models.py` | Modify | Add `DesktopConfig` to `AppConfig` |
| `ares/__main__.py` | Modify | Add `--desktop` flag + `_run_desktop()` |
| `pyproject.toml` | Modify | Add `desktop` optional dependency group |
| `tests/test_desktop_history.py` | Create | Tests for HistoryStore |
| `tests/test_desktop_window.py` | Create | Tests for StatusWindow state logic |
| `tests/test_desktop_hotkey.py` | Create | Tests for HotkeyListener |
| `tests/test_desktop_agent.py` | Create | Tests for DesktopVoiceAgent orchestration |

---

### Task 1: Add DesktopConfig to models.py

**Files:**
- Modify: `ares/models.py`
- Test: none (config model, validated by existing pydantic tests)

- [ ] **Step 1: Add DesktopConfig model**

Add after the existing `VoiceConfig` class in `ares/models.py`:

```python
class DesktopConfig(BaseModel):
    """Configuration for the desktop voice assistant mode."""

    enabled: bool = False
    hotkey_ptt: str = "ctrl+space"
    hotkey_mute: str = "ctrl+shift+m"
    hotkey_window: str = "ctrl+shift+h"
    window_x: int = -1
    window_y: int = -1
    window_opacity: float = 0.85
    auto_hide_seconds: int = 3
    history_size: int = 5
```

- [ ] **Step 2: Add `desktop` field to AppConfig**

In the `AppConfig` class, add:

```python
desktop: DesktopConfig = Field(default_factory=DesktopConfig)
```

- [ ] **Step 3: Commit**

```bash
git add ares/models.py
git commit -m "feat(desktop): add DesktopConfig model"
```

---

### Task 2: Add desktop optional dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add desktop dependency group**

Add after the `voice` optional dependencies section:

```toml
desktop = [
    "pystray>=0.19",
    "customtkinter>=5.2",
    "pynput>=1.7",
]
```

- [ ] **Step 2: Commit**

```bash
git add pyproject.toml
git commit -m "feat(desktop): add desktop optional dependencies"
```

---

### Task 3: Create HistoryStore

**Files:**
- Create: `ares/desktop/__init__.py`
- Create: `ares/desktop/history.py`
- Create: `tests/test_desktop_history.py`

- [ ] **Step 1: Create module init**

Create `ares/desktop/__init__.py`:

```python
"""Desktop voice assistant mode for Ares."""

from ares.desktop.agent import DesktopVoiceAgent, run_desktop

__all__ = ["DesktopVoiceAgent", "run_desktop"]
```

- [ ] **Step 2: Write failing test for HistoryStore**

Create `tests/test_desktop_history.py`:

```python
from ares.desktop.history import HistoryStore


def test_history_store_add_and_get():
    store = HistoryStore(max_size=3)
    store.add("Hello Ares", "Hi there!")
    store.add("What time is it?", "It's 3 PM.")
    entries = store.recent()
    assert len(entries) == 2
    assert entries[0]["user"] == "Hello Ares"
    assert entries[0]["assistant"] == "Hi there!"


def test_history_store_respects_max_size():
    store = HistoryStore(max_size=2)
    store.add("q1", "a1")
    store.add("q2", "a2")
    store.add("q3", "a3")
    entries = store.recent()
    assert len(entries) == 2
    assert entries[0]["user"] == "q2"
    assert entries[1]["user"] == "q3"


def test_history_store_empty():
    store = HistoryStore(max_size=5)
    assert store.recent() == []


def test_history_store_recent_limit():
    store = HistoryStore(max_size=10)
    for i in range(5):
        store.add(f"q{i}", f"a{i}")
    entries = store.recent(limit=3)
    assert len(entries) == 3
    assert entries[0]["user"] == "q2"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_desktop_history.py -v`
Expected: FAIL (module not found)

- [ ] **Step 4: Implement HistoryStore**

Create `ares/desktop/history.py`:

```python
"""In-memory store for recent voice exchanges shown in the tray menu."""

from __future__ import annotations

from collections import deque
from typing import Any


class HistoryStore:
    """Fixed-size ring buffer of recent user/assistant exchanges."""

    def __init__(self, max_size: int = 5) -> None:
        self._max_size = max(1, max_size)
        self._entries: deque[dict[str, str]] = deque(maxlen=self._max_size)

    def add(self, user_text: str, assistant_text: str) -> None:
        self._entries.append({"user": user_text, "assistant": assistant_text})

    def recent(self, limit: int | None = None) -> list[dict[str, str]]:
        entries = list(self._entries)
        if limit is not None:
            entries = entries[-limit:]
        return entries

    def clear(self) -> None:
        self._entries.clear()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_desktop_history.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ares/desktop/__init__.py ares/desktop/history.py tests/test_desktop_history.py
git commit -m "feat(desktop): add HistoryStore for tray menu"
```

---

### Task 4: Create StatusWindow

**Files:**
- Create: `ares/desktop/window.py`
- Create: `tests/test_desktop_window.py`

- [ ] **Step 1: Write failing test for StatusWindow state logic**

Create `tests/test_desktop_window.py`:

```python
from unittest.mock import MagicMock, patch
from ares.desktop.window import StatusWindow, StatusState


def test_status_state_values():
    assert StatusState.IDLE == "idle"
    assert StatusState.LISTENING == "listening"
    assert StatusState.THINKING == "thinking"
    assert StatusState.SPEAKING == "speaking"
    assert StatusState.MUTED == "muted"
    assert StatusState.ERROR == "error"


def test_status_window_creation():
    """StatusWindow can be instantiated without starting the GUI loop."""
    with patch("ares.desktop.window.customtkinter") as mock_ctk:
        mock_root = MagicMock()
        mock_ctk.CTkToplevel.return_value = mock_root
        mock_ctk.CTkLabel.return_value = MagicMock()
        mock_ctk.CTkFrame.return_value = MagicMock()
        win = StatusWindow(opacity=0.85)
        assert win.state == StatusState.IDLE
        win.destroy()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_desktop_window.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement StatusWindow**

Create `ares/desktop/window.py`:

```python
"""Floating status window for the desktop voice assistant.

Uses customtkinter for a modern dark-themed always-on-top status indicator.
"""

from __future__ import annotations

import enum
import logging
import tkinter as tk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StatusState(str, enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    MUTED = "muted"
    ERROR = "error"


_STATE_LABELS = {
    StatusState.IDLE: "Ready",
    StatusState.LISTENING: "Listening...",
    StatusState.THINKING: "Thinking...",
    StatusState.SPEAKING: "Speaking...",
    StatusState.MUTED: "Muted",
    StatusState.ERROR: "Error",
}

_STATE_COLORS = {
    StatusState.IDLE: "#666666",
    StatusState.LISTENING: "#00d4ff",
    StatusState.THINKING: "#ffaa00",
    StatusState.SPEAKING: "#4488ff",
    StatusState.MUTED: "#ff4444",
    StatusState.ERROR: "#ff4444",
}


class StatusWindow:
    """Compact floating status indicator that shows Ares voice state."""

    def __init__(self, opacity: float = 0.85) -> None:
        import customtkinter

        self._ctk = customtkinter
        self._ctk.set_appearance_mode("dark")
        self._ctk.set_default_color_theme("blue")

        self._root = customtkinter.CTkToplevel()
        self._root.title("Ares")
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", opacity)
        self._root.configure(fg_color="#1a1a2e")

        width, height = 220, 60
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = screen_w - width - 20
        y = screen_h - height - 60
        self._root.geometry(f"{width}x{height}+{x}+{y}")

        self._state = StatusState.IDLE

        self._dot = customtkinter.CTkLabel(
            self._root, text="O", width=20, height=20,
            text_color=_STATE_COLORS[StatusState.IDLE],
            font=("Segoe UI", 14, "bold"),
        )
        self._dot.pack(side="left", padx=(12, 6), pady=10)

        self._label = customtkinter.CTkLabel(
            self._root, text=_STATE_LABELS[StatusState.IDLE],
            text_color="#cccccc", font=("Segoe UI", 11),
        )
        self._label.pack(side="left", fill="x", expand=True, pady=10)

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._visible = False
        self._hide_job = None

    @property
    def state(self) -> StatusState:
        return self._state

    def set_state(self, state: StatusState, text: str = "") -> None:
        self._state = state
        self._label.configure(text=text or _STATE_LABELS.get(state, ""))
        self._dot.configure(text_color=_STATE_COLORS.get(state, "#666666"))
        if state == StatusState.IDLE:
            self._schedule_hide()
        else:
            self._cancel_hide()
            self.show()

    def show(self) -> None:
        if not self._visible:
            self._root.deiconify()
            self._visible = True
        self._cancel_hide()

    def hide(self) -> None:
        if self._visible:
            self._root.withdraw()
            self._visible = False

    def destroy(self) -> None:
        self._cancel_hide()
        try:
            self._root.destroy()
        except Exception:
            pass

    def _schedule_hide(self) -> None:
        self._cancel_hide()
        self._hide_job = self._root.after(3000, self.hide)

    def _cancel_hide(self) -> None:
        if self._hide_job is not None:
            try:
                self._root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

    def _on_close(self) -> None:
        self.hide()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_desktop_window.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/desktop/window.py tests/test_desktop_window.py
git commit -m "feat(desktop): add StatusWindow with state management"
```

---

### Task 5: Create HotkeyListener

**Files:**
- Create: `ares/desktop/hotkey.py`
- Create: `tests/test_desktop_hotkey.py`

- [ ] **Step 1: Write failing test for HotkeyListener**

Create `tests/test_desktop_hotkey.py`:

```python
from unittest.mock import MagicMock, patch
from ares.desktop.hotkey import HotkeyListener


def test_hotkey_listener_creation():
    listener = HotkeyListener(
        hotkey_ptt="ctrl+space",
        hotkey_mute="ctrl+shift+m",
        hotkey_window="ctrl+shift+h",
    )
    assert listener.ptt_callback is None
    assert listener.mute_callback is None
    assert listener.window_callback is None


def test_hotkey_listener_set_callbacks():
    listener = HotkeyListener(
        hotkey_ptt="ctrl+space",
        hotkey_mute="ctrl+shift+m",
        hotkey_window="ctrl+shift+h",
    )
    ptt_fn = MagicMock()
    mute_fn = MagicMock()
    window_fn = MagicMock()
    listener.set_callbacks(ptt=ptt_fn, mute=mute_fn, window=window_fn)
    assert listener.ptt_callback is ptt_fn
    assert listener.mute_callback is mute_fn
    assert listener.window_callback is window_fn


def test_hotkey_listener_parse_hotkey():
    listener = HotkeyListener(
        hotkey_ptt="ctrl+space",
        hotkey_mute="ctrl+shift+m",
        hotkey_window="ctrl+shift+h",
    )
    keys = listener._parse_hotkey("ctrl+shift+m")
    assert "ctrl" in keys
    assert "shift" in keys
    assert "m" in keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_desktop_hotkey.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement HotkeyListener**

Create `ares/desktop/hotkey.py`:

```python
"""Global hotkey listener for the desktop voice assistant.

Uses pynput to register system-wide hotkeys for push-to-talk,
mute toggle, and window visibility toggle.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class HotkeyListener:
    """Register and listen for global hotkeys via pynput."""

    def __init__(
        self,
        *,
        hotkey_ptt: str = "ctrl+space",
        hotkey_mute: str = "ctrl+shift+m",
        hotkey_window: str = "ctrl+shift+h",
    ) -> None:
        self._hotkey_ptt = hotkey_ptt
        self._hotkey_mute = hotkey_mute
        self._hotkey_window = hotkey_window
        self.ptt_callback: Callable[[], None] | None = None
        self.mute_callback: Callable[[], None] | None = None
        self.window_callback: Callable[[], None] | None = None
        self._listener = None
        self._thread: threading.Thread | None = None

    def set_callbacks(
        self,
        ptt: Callable[[], None] | None = None,
        mute: Callable[[], None] | None = None,
        window: Callable[[], None] | None = None,
    ) -> None:
        if ptt is not None:
            self.ptt_callback = ptt
        if mute is not None:
            self.mute_callback = mute
        if window is not None:
            self.window_callback = window

    def start(self) -> None:
        from pynput import keyboard

        hotkeys = {}
        if self.ptt_callback:
            hotkeys[self._hotkey_ptt] = self.ptt_callback
        if self.mute_callback:
            hotkeys[self._hotkey_mute] = self.mute_callback
        if self.window_callback:
            hotkeys[self._hotkey_window] = self.window_callback

        if not hotkeys:
            logger.warning("No hotkeys registered — all callbacks are None")
            return

        self._listener = keyboard.GlobalHotKeys(hotkeys)
        self._thread = threading.Thread(
            target=self._listener.start, daemon=True, name="ares-hotkey"
        )
        self._thread.start()
        logger.info("Hotkey listener started: %s", list(hotkeys.keys()))

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @staticmethod
    def _parse_hotkey(hotkey: str) -> list[str]:
        return [k.strip().lower() for k in hotkey.split("+")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_desktop_hotkey.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ares/desktop/hotkey.py tests/test_desktop_hotkey.py
git commit -m "feat(desktop): add HotkeyListener with pynput"
```

---

### Task 6: Create TrayIcon

**Files:**
- Create: `ares/desktop/tray.py`

- [ ] **Step 1: Implement TrayIcon**

Create `ares/desktop/tray.py`:

```python
"""System tray icon for the desktop voice assistant.

Uses pystray to create a tray icon with a context menu for
session control, mute toggle, recent history, and quit.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)


def _create_icon_image(color: str = "#666666") -> Image.Image:
    """Create a simple colored circle icon for the tray."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = size // 2 - 4
    draw.ellipse([4, 4, 4 + r * 2, 4 + r * 2], fill=color)
    return img


_ICON_COLORS = {
    "idle": "#666666",
    "listening": "#00d4ff",
    "thinking": "#ffaa00",
    "speaking": "#4488ff",
    "muted": "#ff4444",
}


class TrayIcon:
    """System tray icon with context menu."""

    def __init__(
        self,
        *,
        on_new_session: Callable[[], None] | None = None,
        on_status: Callable[[], None] | None = None,
        on_mute_toggle: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        history_provider: Callable[[], list[dict[str, str]]] | None = None,
        mute_state_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._on_new_session = on_new_session
        self._on_status = on_status
        self._on_mute_toggle = on_mute_toggle
        self._on_quit = on_quit
        self._history_provider = history_provider
        self._mute_state_provider = mute_state_provider
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import pystray

        self._icon = pystray.Icon(
            "ares",
            icon=_create_icon_image(),
            title="Ares Desktop",
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(
            target=self._icon.run, daemon=True, name="ares-tray"
        )
        self._thread.start()
        logger.info("Tray icon started")

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
            self._icon = None

    def set_state(self, state: str) -> None:
        if self._icon is not None:
            color = _ICON_COLORS.get(state, "#666666")
            self._icon.icon = _create_icon_image(color)

    def _build_menu(self) -> Any:
        import pystray

        items = [
            pystray.MenuItem("New Session", self._handle_new_session),
            pystray.MenuItem("Status", self._handle_status),
            pystray.Menu.SEPARATOR,
        ]

        history = self._get_history()
        if history:
            for entry in history[-3:]:
                text = entry.get("user", "")[:40]
                items.append(pystray.MenuItem(f'"{text}"', None, enabled=False))
            items.append(pystray.Menu.SEPARATOR)

        muted = self._is_muted()
        mute_label = "Unmute TTS" if muted else "Mute TTS"
        items.append(pystray.MenuItem(mute_label, self._handle_mute_toggle))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Quit", self._handle_quit))

        return pystray.Menu(*items)

    def _handle_new_session(self, icon: Any, item: Any) -> None:
        if self._on_new_session:
            self._on_new_session()

    def _handle_status(self, icon: Any, item: Any) -> None:
        if self._on_status:
            self._on_status()

    def _handle_mute_toggle(self, icon: Any, item: Any) -> None:
        if self._on_mute_toggle:
            self._on_mute_toggle()

    def _handle_quit(self, icon: Any, item: Any) -> None:
        if self._on_quit:
            self._on_quit()

    def _get_history(self) -> list[dict[str, str]]:
        if self._history_provider:
            try:
                return self._history_provider()
            except Exception:
                return []
        return []

    def _is_muted(self) -> bool:
        if self._mute_state_provider:
            try:
                return self._mute_state_provider()
            except Exception:
                return False
        return False
```

- [ ] **Step 2: Commit**

```bash
git add ares/desktop/tray.py
git commit -m "feat(desktop): add TrayIcon with pystray"
```

---

### Task 7: Create DesktopVoiceAgent (main orchestrator)

**Files:**
- Create: `ares/desktop/agent.py`

- [ ] **Step 1: Implement DesktopVoiceAgent**

Create `ares/desktop/agent.py`:

```python
"""Desktop voice agent — orchestrates tray, window, hotkey, and voice pipeline.

This is the main entry point for `python -m ares --desktop`. It wires together
the system tray icon, floating status window, global hotkey listener, and the
existing voice pipeline (MicrophoneFrames, WhisperTranscriber, EdgeTTS,
play_audio_stream) with the existing Agent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Coroutine
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
        self._speaking = False
        self._processing = False
        self._agent = None
        self._conversation_history: list[dict[str, str]] = []

        self._window: StatusWindow | None = None
        self._tray: TrayIcon | None = None
        self._hotkey: HotkeyListener | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()

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
            ptt=self._handle_ptt_hotkey,
            mute=self._handle_mute_toggle,
            window=self._handle_window_toggle,
        )
        self._hotkey.start()

        logger.info("Desktop voice agent started")
        await self._stop_event.wait()
        self._cleanup()

    def _cleanup(self) -> None:
        if self._hotkey:
            self._hotkey.stop()
        if self._tray:
            self._tray.stop()
        if self._window:
            self._window.destroy()

    def _handle_ptt_hotkey(self) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._push_to_talk(), self._loop)

    def _handle_mute_toggle(self) -> None:
        self._muted = not self._muted
        state = StatusState.MUTED if self._muted else StatusState.IDLE
        if self._window:
            self._window.set_state(state)
        if self._tray:
            self._tray.set_state("muted" if self._muted else "idle")
        logger.info("TTS %s", "muted" if self._muted else "unmuted")

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
        if self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    async def _push_to_talk(self) -> None:
        if self._processing:
            if self._window:
                self._window.set_state(StatusState.THINKING, "Please wait...")
            return

        from ares.voice.agent import MicrophoneFrames

        capture = MicrophoneFrames(
            sample_rate=_SAMPLE_RATE,
            frame_samples=_FRAME_SAMPLES,
            device=self.voice_config.mic_device,
            max_seconds=30,
        )

        try:
            if self._window:
                self._window.set_state(StatusState.LISTENING)
            if self._tray:
                self._tray.set_state("listening")

            capture.start()
            await asyncio.sleep(0.1)

            audio = await self._capture_until_release(capture)
            if audio is None:
                if self._window:
                    self._window.set_state(StatusState.IDLE)
                if self._tray:
                    self._tray.set_state("idle")
                return

            text = await asyncio.to_thread(
                self._transcriber.transcribe_samples, audio, _SAMPLE_RATE
            )
            text = text.strip()
            if not text:
                if self._window:
                    self._window.set_state(StatusState.ERROR, "Could not understand")
                    self._window.set_state(StatusState.IDLE)
                if self._tray:
                    self._tray.set_state("idle")
                return

            logger.info("User said: %s", text)

            if self._window:
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
            logger.exception("Push-to-talk failed")
            if self._window:
                self._window.set_state(StatusState.ERROR, "Error occurred")
            if self._tray:
                self._tray.set_state("idle")
        finally:
            capture.close()

    async def _capture_until_release(
        self, capture: Any
    ) -> np.ndarray | None:
        """Capture audio while Ctrl is held, return trimmed audio or None."""
        frames: list[np.ndarray] = []
        import keyboard

        while keyboard.is_pressed("ctrl"):
            raw = capture.read(1)
            if raw:
                frames.extend(raw)
            await asyncio.sleep(0.01)

        if not frames:
            return None

        audio = np.concatenate(frames).astype(np.float32)
        audio = trim_silence(audio, _SAMPLE_RATE)

        duration_ms = len(audio) * 1000 / _SAMPLE_RATE
        if duration_ms < self.voice_config.min_utterance_ms:
            return None

        return audio

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

        encoded = await self._tts.synthesize(text, self.voice_config.tts_voice or DEFAULT_EDGE_VOICE)
        if not encoded:
            return

        pcm = audio_bytes_to_pcm16(encoded, sample_rate=self._tts_sample_rate, speed=1.08)
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
        if not re.search(r"[A-Za-z0-9ऀ-ॿ]", text):
            return ""
        return text


async def run_desktop() -> None:
    """Entry point for `python -m ares --desktop`."""
    agent = DesktopVoiceAgent()
    await agent.run()
```

- [ ] **Step 2: Commit**

```bash
git add ares/desktop/agent.py
git commit -m "feat(desktop): add DesktopVoiceAgent orchestrator"
```

---

### Task 8: Add --desktop flag to __main__.py

**Files:**
- Modify: `ares/__main__.py`

- [ ] **Step 1: Add --desktop argument**

In `ares/__main__.py`, add after the `--voice` argument:

```python
parser.add_argument(
    "--desktop",
    action="store_true",
    help="Run the background desktop voice assistant with system tray and push-to-talk",
)
```

- [ ] **Step 2: Add _run_desktop function**

Add after the existing `_run_voice` function:

```python
async def _run_desktop() -> None:
    from ares.desktop.agent import run_desktop

    await run_desktop()
```

- [ ] **Step 3: Wire --desktop in main()**

In the `main()` function, add a new elif branch after the `args.voice` block:

```python
elif args.desktop:
    _run_coro(_run_desktop())
```

- [ ] **Step 4: Commit**

```bash
git add ares/__main__.py
git commit -m "feat(desktop): add --desktop CLI flag"
```

---

### Task 9: Integration smoke test

**Files:**
- Test: manual

- [ ] **Step 1: Install desktop dependencies**

Run: `pip install -e ".[desktop]"`

- [ ] **Step 2: Run with --desktop flag**

Run: `python -m ares --desktop`
Expected: Tray icon appears, status window shows "Ready", Ctrl+Space activates mic

- [ ] **Step 3: Test push-to-talk**

Hold Ctrl+Space, speak a short phrase, release. Expected: status changes Listening -> Thinking -> Speaking, Ares responds verbally.

- [ ] **Step 4: Test mute toggle**

Press Ctrl+Shift+M. Expected: status shows "Muted", Ctrl+Space still transcribes but doesn't speak. Press again to unmute.

- [ ] **Step 5: Test window toggle**

Press Ctrl+Shift+H. Expected: status window hides/shows.

- [ ] **Step 6: Test tray menu**

Right-click tray icon. Expected: clean dark menu with New Session, Status, Recent history, Mute TTS, Quit.

- [ ] **Step 7: Test quit**

Click Quit in tray menu. Expected: clean shutdown.

---

### Task 10: Final commit

- [ ] **Step 1: Run all tests**

Run: `pytest tests/ -v`

- [ ] **Step 2: Commit everything**

```bash
git add -A
git commit -m "feat: desktop voice assistant with tray, push-to-talk, and Edge TTS

- System tray icon with dark-themed context menu
- Floating customtkinter status window (always-on-top, semi-transparent)
- Global push-to-talk via Ctrl+Space (pynput)
- Multi-language STT (faster-whisper: Hindi/English/Hinglish -> English)
- Edge TTS speech output with barge-in support
- Mute/unmute hotkey (Ctrl+Shift+M)
- Window toggle hotkey (Ctrl+Shift+H)
- Recent history in tray menu
- Desktop toast notifications for proactive messages

Co-Authored-By: Claude <noreply@anthropic.com>"
```
