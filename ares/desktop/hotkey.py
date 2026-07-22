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
from typing import Callable, Set

logger = logging.getLogger(__name__)

# Map friendly key names to the angle-bracket tokens pynput GlobalHotKeys expects.
# Each modifier in a GlobalHotKeys combo must be wrapped in angle brackets.
_GLOBAL_KEY_ALIASES = {
    "ctrl": "<ctrl>",
    "ctrl_l": "<ctrl_l>",
    "ctrl_r": "<ctrl_r>",
    "alt": "<alt>",
    "alt_l": "<alt_l>",
    "alt_r": "<alt_r>",
    "shift": "<shift>",
    "shift_l": "<shift_l>",
    "shift_r": "<shift_r>",
    "win": "<cmd>",
    "super": "<cmd>",
    "cmd": "<cmd>",
    "cmd_l": "<cmd_l>",
    "cmd_r": "<cmd_r>",
}


class HotkeyListener:
    """Register and listen for global hotkeys via pynput."""

    def __init__(
        self,
        *,
        hotkey_ptt: str = "win+ctrl",
        hotkey_mute: str = "win+ctrl+m",
        hotkey_window: str = "win+ctrl+h",
    ) -> None:
        self._hotkey_ptt = hotkey_ptt
        self._hotkey_mute = hotkey_mute
        self._hotkey_window = hotkey_window

        # Friendly key names for the PTT combo, e.g. ["win", "ctrl"].
        self._ptt_key_names: list[str] = HotkeyListener._parse_hotkey(hotkey_ptt)

        # Callbacks
        self.ptt_callback: Callable[[], None] | None = None
        self.ptt_release_callback: Callable[[], None] | None = None
        self.mute_callback: Callable[[], None] | None = None
        self.window_callback: Callable[[], None] | None = None

        # Runtime state
        self._current_pressed: Set[str] = set()
        self._ptt_active = False
        self._ptt_listener = None
        self._toggle_listener = None
        self._ptt_thread: threading.Thread | None = None
        self._toggle_thread: threading.Thread | None = None

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
        import pynput.keyboard

        self._ptt_listener = pynput.keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False,
        )
        self._ptt_thread = threading.Thread(
            target=self._ptt_listener.run,
            daemon=True,
            name="ares-ptt-listen",
        )
        self._ptt_thread.start()

        toggle_hotkeys = {}
        if self.mute_callback:
            toggle_hotkeys[self._convert_to_pynput_hotkey(self._hotkey_mute)] = self.mute_callback
        if self.window_callback:
            toggle_hotkeys[self._convert_to_pynput_hotkey(self._hotkey_window)] = self.window_callback

        if toggle_hotkeys:
            self._toggle_listener = pynput.keyboard.GlobalHotKeys(toggle_hotkeys)
            self._toggle_thread = threading.Thread(
                target=self._toggle_listener.run,
                daemon=True,
                name="ares-toggle-listen",
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
            friendly = self._normalize_key_name(key)
            if not friendly:
                return

            self._current_pressed.add(friendly)

            if not self._ptt_active and self._is_ptt_complete():
                self._ptt_active = True
                logger.debug("PTT combo pressed: %s", "+".join(self._ptt_key_names))
                if self.ptt_callback:
                    self.ptt_callback()
        except Exception as exc:
            logger.debug("PTT press handler error: %s", exc)

    def _on_release(self, key) -> None:
        try:
            friendly = self._normalize_key_name(key)
            if not friendly:
                return

            released = friendly in self._current_pressed
            self._current_pressed.discard(friendly)

            if self._ptt_active and released:
                if not self._is_ptt_complete():
                    self._ptt_active = False
                    logger.debug("PTT combo released: %s", "+".join(self._ptt_key_names))
                    if self.ptt_release_callback:
                        self.ptt_release_callback()
        except Exception as exc:
            logger.debug("PTT release handler error: %s", exc)

    def _is_ptt_complete(self) -> bool:
        return all(name in self._current_pressed for name in self._ptt_key_names)

    @staticmethod
    def _normalize_key_name(key) -> str | None:
        """Convert a pynput key event into a friendly lowercase key name."""
        try:
            if hasattr(key, "name") and key.name:
                name = key.name
            elif hasattr(key, "char") and key.char:
                name = key.char
            else:
                return None

            name = name.lower()
            if not name:
                return None

            if name in ("ctrl_l", "ctrl_r"):
                return "ctrl"
            if name in ("alt_l", "alt_r", "alt_gr"):
                return "alt"
            if name in ("shift_l", "shift_r"):
                return "shift"
            if name in ("cmd", "cmd_l", "cmd_r", "super", "super_l", "super_r", "win"):
                return "win"
            if name in ("space", "space_l", "space_r"):
                return "space"

            if len(name) == 1 and name.isprintable():
                return name

            if name.startswith("key."):
                return name[4:]
            return name
        except Exception:
            return None

    @staticmethod
    def _convert_to_pynput_hotkey(hotkey: str) -> str:
        """Convert friendly hotkey (e.g. 'win+ctrl+m') to pynput GlobalHotKeys format."""
        parts = [k.strip().lower() for k in hotkey.split("+")]
        converted = []
        for p in parts:
            alias = _GLOBAL_KEY_ALIASES.get(p, p)
            converted.append(alias)
        return "+".join(converted)

    @staticmethod
    def _parse_hotkey(hotkey: str) -> list[str]:
        return [k.strip().lower() for k in hotkey.split("+")]
