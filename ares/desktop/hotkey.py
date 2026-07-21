"""Global hotkey listener for the desktop voice assistant.

Uses pynput to register system-wide hotkeys for push-to-talk,
mute toggle, and window visibility toggle.

Push-to-talk uses a low-level keyboard listener to detect both
key-down (start recording) and key-up (stop recording + process).
Toggle hotkeys (mute, window) use GlobalHotKeys for simplicity.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# pynput key name mapping for the space key
_SPACE_NAMES = {"space", "Key.space", "Key.space_l", "Key.space_r"}


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
        self.ptt_release_callback: Callable[[], None] | None = None
        self.mute_callback: Callable[[], None] | None = None
        self.window_callback: Callable[[], None] | None = None
        self._ptt_listener = None
        self._toggle_listener = None
        self._ptt_thread: threading.Thread | None = None
        self._toggle_thread: threading.Thread | None = None
        self._ctrl_held = False
        self._space_held = False

    def set_callbacks(
        self,
        ptt: Callable[[], None] | None = None,
        ptt_release: Callable[[], None] | None = None,
        mute: Callable[[], None] | None = None,
        window: Callable[[], None] | None = None,
    ) -> None:
        if ptt is not None:
            self.ptt_callback = ptt
        if ptt_release is not None:
            self.ptt_release_callback = ptt_release
        if mute is not None:
            self.mute_callback = mute
        if window is not None:
            self.window_callback = window

    def start(self) -> None:
        from pynput import keyboard

        # Push-to-talk: low-level listener for press/release detection
        if self.ptt_callback:
            self._ptt_listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            self._ptt_thread = threading.Thread(
                target=self._ptt_listener.start, daemon=True, name="ares-ptt"
            )
            self._ptt_thread.start()

        # Toggle hotkeys: GlobalHotKeys for mute and window
        toggle_hotkeys = {}
        if self.mute_callback:
            toggle_hotkeys[self._hotkey_mute] = self.mute_callback
        if self.window_callback:
            toggle_hotkeys[self._hotkey_window] = self.window_callback

        if toggle_hotkeys:
            self._toggle_listener = keyboard.GlobalHotKeys(toggle_hotkeys)
            self._toggle_thread = threading.Thread(
                target=self._toggle_listener.start, daemon=True, name="ares-toggle"
            )
            self._toggle_thread.start()

        logger.info(
            "Hotkey listener started: ptt=%s, toggles=%s",
            self._hotkey_ptt,
            list(toggle_hotkeys.keys()),
        )

    def stop(self) -> None:
        if self._ptt_listener is not None:
            self._ptt_listener.stop()
            self._ptt_listener = None
        if self._toggle_listener is not None:
            self._toggle_listener.stop()
            self._toggle_listener = None
        if self._ptt_thread is not None:
            self._ptt_thread.join(timeout=2.0)
            self._ptt_thread = None
        if self._toggle_thread is not None:
            self._toggle_thread.join(timeout=2.0)
            self._toggle_thread = None

    def _on_press(self, key) -> None:
        try:
            name = str(key)
            if "ctrl" in name.lower() or "Key.ctrl" in name:
                self._ctrl_held = True
            if name in _SPACE_NAMES:
                self._space_held = True
        except Exception:
            return

        if self._ctrl_held and self._space_held and self.ptt_callback:
            logger.debug("PTT hotkey pressed")
            self.ptt_callback()

    def _on_release(self, key) -> None:
        try:
            name = str(key)
            if "ctrl" in name.lower() or "Key.ctrl" in name:
                self._ctrl_held = False
            if name in _SPACE_NAMES:
                self._space_held = False
        except Exception:
            return

        if not self._ctrl_held and not self._space_held:
            if self.ptt_release_callback:
                logger.debug("PTT hotkey released")
                self.ptt_release_callback()

    @staticmethod
    def _parse_hotkey(hotkey: str) -> list[str]:
        return [k.strip().lower() for k in hotkey.split("+")]
