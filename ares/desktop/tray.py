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
    "awake": "#69e6a6",
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
        on_wake_toggle: Callable[[], None] | None = None,
        on_barge_toggle: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        history_provider: Callable[[], list[dict[str, str]]] | None = None,
        mute_state_provider: Callable[[], bool] | None = None,
        wake_state_provider: Callable[[], bool] | None = None,
        barge_state_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._on_new_session = on_new_session
        self._on_status = on_status
        self._on_mute_toggle = on_mute_toggle
        self._on_wake_toggle = on_wake_toggle
        self._on_barge_toggle = on_barge_toggle
        self._on_quit = on_quit
        self._history_provider = history_provider
        self._mute_state_provider = mute_state_provider
        self._wake_state_provider = wake_state_provider
        self._barge_state_provider = barge_state_provider
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

    def refresh_menu(self) -> None:
        if self._icon is not None:
            try:
                self._icon.menu = self._build_menu()
                self._icon.update_menu()
            except Exception:
                logger.debug("Tray menu refresh failed", exc_info=True)

    def _build_menu(self) -> Any:
        import pystray

        items = [
            pystray.MenuItem("Open Ares", self._handle_status, default=True),
            pystray.MenuItem("New Session", self._handle_new_session),
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
        wake_label = "Disable Wake Word" if self._is_wake_enabled() else "Enable Wake Word"
        items.append(pystray.MenuItem(wake_label, self._handle_wake_toggle))
        barge_label = (
            "Disable Interruption" if self._is_barge_enabled() else "Enable Interruption"
        )
        items.append(pystray.MenuItem(barge_label, self._handle_barge_toggle))
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

    def _handle_wake_toggle(self, icon: Any, item: Any) -> None:
        if self._on_wake_toggle:
            self._on_wake_toggle()

    def _handle_barge_toggle(self, icon: Any, item: Any) -> None:
        if self._on_barge_toggle:
            self._on_barge_toggle()

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

    def _is_wake_enabled(self) -> bool:
        if self._wake_state_provider:
            try:
                return self._wake_state_provider()
            except Exception:
                return False
        return False

    def _is_barge_enabled(self) -> bool:
        if self._barge_state_provider:
            try:
                return self._barge_state_provider()
            except Exception:
                return False
        return False
