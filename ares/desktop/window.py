"""Floating status window for the desktop voice assistant.

Uses customtkinter for a modern dark-themed always-on-top status indicator.
"""

from __future__ import annotations

import enum
import logging

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
