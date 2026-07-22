"""Process-isolated floating status window for the desktop voice assistant.

On Windows, Tcl/Tk must be created and destroyed by the main thread of the
process that owns it.  Ares therefore runs the status card in a tiny child
process and communicates with it through a queue.  Tray, hotkey, asyncio, and
Tk event loops cannot block or call into one another.
"""

from __future__ import annotations

import enum
import importlib.util
import json
import logging
import multiprocessing
import os
import queue
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StatusState(str, enum.Enum):
    IDLE = "idle"
    AWAKE = "awake"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    MUTED = "muted"
    ERROR = "error"


_STATE_LABELS = {
    StatusState.IDLE: "Ready to help",
    StatusState.AWAKE: "Wake word heard",
    StatusState.LISTENING: "Listening…",
    StatusState.THINKING: "Working on it…",
    StatusState.SPEAKING: "Speaking…",
    StatusState.MUTED: "Voice replies muted",
    StatusState.ERROR: "Something needs attention",
}

_STATE_COLORS = {
    StatusState.IDLE: "#8b95a7",
    StatusState.AWAKE: "#69e6a6",
    StatusState.LISTENING: "#31d0ff",
    StatusState.THINKING: "#ffb454",
    StatusState.SPEAKING: "#7c9cff",
    StatusState.MUTED: "#ff6b81",
    StatusState.ERROR: "#ff6b81",
}


class StatusWindow:
    """Non-blocking controller for the process that owns the Tk status card."""

    def __init__(
        self,
        opacity: float = 0.85,
        *,
        window_x: int = -1,
        window_y: int = -1,
        auto_hide_seconds: int = 3,
        hotkey_label: str = "Ctrl + Space",
        wake_word_hint: str = "Hey Jarvis",
        _mp_context: Any | None = None,
    ) -> None:
        if importlib.util.find_spec("customtkinter") is None:
            raise RuntimeError(
                "Desktop mode requires customtkinter. Install it with `pip install -e .[desktop]`."
            )

        self._state = StatusState.IDLE
        self._visible = False
        self._state_lock = threading.Lock()
        self._destroyed = False
        context = _mp_context or multiprocessing.get_context("spawn")
        self._commands = context.Queue()
        self._startup_events = context.Queue()
        self._ready = context.Event()
        options = {
            "opacity": max(0.2, min(float(opacity), 1.0)),
            "window_x": int(window_x),
            "window_y": int(window_y),
            "auto_hide_ms": max(0, int(auto_hide_seconds * 1000)),
            "hotkey_label": str(hotkey_label),
            "wake_word_hint": str(wake_word_hint).strip(),
        }
        self._process = context.Process(
            target=_status_window_process,
            args=(self._commands, self._startup_events, self._ready, options),
            daemon=True,
            name="ares-status-ui",
        )
        self._process.start()
        if not self._ready.wait(timeout=15):
            exit_code = self._process.exitcode
            startup_error, progress = self._read_startup_events()
            self.destroy()
            detail = startup_error or progress or f"child exit code: {exit_code}"
            raise RuntimeError(f"Ares desktop window did not start within 15 seconds ({detail}).")
        startup_error, _progress = self._read_startup_events()
        if startup_error:
            self.destroy()
            raise RuntimeError(f"Failed to start the Ares desktop window: {startup_error}")
        if not self._process.is_alive():
            self.destroy()
            raise RuntimeError("Ares desktop window exited during startup.")

    @property
    def state(self) -> StatusState:
        with self._state_lock:
            return self._state

    @property
    def is_visible(self) -> bool:
        with self._state_lock:
            return self._visible

    def set_state(self, state: StatusState, text: str = "") -> None:
        with self._state_lock:
            self._state = state
            if state != StatusState.IDLE:
                self._visible = True
        self._post("state", state.value, text)

    def show(self) -> None:
        with self._state_lock:
            self._visible = True
        self._post("show")

    def hide(self) -> None:
        with self._state_lock:
            self._visible = False
        self._post("hide")

    def toggle(self) -> None:
        self._post("toggle")

    def set_transcript(self, text: str) -> None:
        self._post("transcript", str(text or "")[:2_000])

    def append_response(self, text: str) -> None:
        self._post("response", str(text or "")[:2_000])

    def tool_started(self, name: str) -> None:
        self._post("tool_start", str(name or "tool")[:120])

    def tool_progress(self, name: str, detail: str) -> None:
        self._post("tool_progress", str(name or "tool")[:120], str(detail or "")[:500])

    def tool_result(self, name: str, payload: str) -> None:
        self._post("tool_result", str(name or "tool")[:120], str(payload or "")[:20_000])

    def set_wake_word_hint(self, hint: str) -> None:
        self._post("wake_hint", str(hint or "")[:120])

    def set_microphone_name(self, name: str) -> None:
        self._post("microphone", str(name or "")[:120])

    def clear_session(self) -> None:
        self._post("clear")

    def destroy(self) -> None:
        """Ask the UI process to exit, then force only that child if necessary."""
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self._commands.put(("destroy", ()))
        except (BrokenPipeError, OSError, ValueError):
            pass
        self._process.join(timeout=3.0)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2.0)
        try:
            self._process.close()
        except (OSError, ValueError):
            pass
        for channel in (self._commands, self._startup_events):
            try:
                channel.cancel_join_thread()
                channel.close()
            except (AttributeError, OSError, ValueError):
                pass
        with self._state_lock:
            self._visible = False

    def _post(self, command: str, *args: Any) -> None:
        if self._destroyed:
            return
        if not self._process.is_alive():
            logger.warning("Ares status UI is not running; dropping %s update", command)
            return
        try:
            self._commands.put((command, args))
        except (BrokenPipeError, OSError, ValueError):
            logger.warning("Ares status UI stopped while posting %s", command)

    def _read_startup_events(self) -> tuple[str, str]:
        error = ""
        progress = ""
        while True:
            try:
                kind, message = self._startup_events.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                error = str(message)
            elif kind == "progress":
                progress = str(message)
        return error, progress


def _status_window_process(
    commands: Any,
    startup_events: Any,
    ready: Any,
    options: dict[str, Any],
) -> None:
    """Child-process entry point; Tk lives on this process's main thread."""
    try:
        startup_events.put(("progress", "UI child started"))
        import customtkinter

        startup_events.put(("progress", "customtkinter imported"))
        ui = _StatusWindowUI(customtkinter, commands, options)
        startup_events.put(("progress", "Tk window constructed"))
        ready.set()
        ui.run()
    except BaseException as exc:
        try:
            startup_events.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            ready.set()


class _StatusWindowUI:
    """Tk implementation used only inside the isolated UI process."""

    _POLL_INTERVAL_MS = 30

    def __init__(self, ctk: Any, commands: Any, options: dict[str, Any]) -> None:
        self._ctk = ctk
        self._commands = commands
        self._options = options
        self._visible = False
        self._expanded = False
        self._hide_job: Any = None
        self._destroying = False
        self._response_text = ""
        self._tool_rows: dict[str, tuple[Any, Any, Any]] = {}
        self._drag_offset = (0, 0)
        self._microphone_name = ""
        self._idle_hint = ""
        self._rebuild_idle_hint()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self._root = ctk.CTk()
        self._root.title("Ares Voice")
        self._root.resizable(False, False)
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", options["opacity"])
        self._root.configure(fg_color="#101827")
        self._place_window(expanded=False)
        self._build_content()
        self._root.protocol("WM_DELETE_WINDOW", self._hide)
        self._root.withdraw()

    def run(self) -> None:
        self._root.after(self._POLL_INTERVAL_MS, self._drain_commands)
        try:
            self._root.mainloop()
        finally:
            self._cancel_hide()
            try:
                self._root.destroy()
            except Exception:
                pass

    def _place_window(self, *, expanded: bool) -> None:
        width, height = ((540, 510) if expanded else (350, 116))
        x, y = self._options["window_x"], self._options["window_y"]
        if x < 0 or y < 0:
            x = self._root.winfo_screenwidth() - width - 24
            y = self._root.winfo_screenheight() - height - 78
        self._root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_content(self) -> None:
        ctk = self._ctk
        self._card = ctk.CTkFrame(self._root, fg_color="#172033", corner_radius=14)
        self._card.pack(fill="both", expand=True, padx=1, pady=1)
        header = ctk.CTkFrame(self._card, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(13, 4))
        header.bind("<ButtonPress-1>", self._drag_start)
        header.bind("<B1-Motion>", self._drag_move)
        ctk.CTkLabel(
            header, text="ARES  /  VOICE", text_color="#9eafc9",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        ctk.CTkButton(
            header, text="Hide", width=40, height=20, fg_color="transparent",
            hover_color="#263650", text_color="#9eafc9", font=("Segoe UI", 10),
            command=self._hide,
        ).pack(side="right")
        status = ctk.CTkFrame(self._card, fg_color="transparent")
        status.pack(fill="x", padx=16, pady=(0, 2))
        self._dot = ctk.CTkLabel(
            status, text="●", width=18, text_color=_STATE_COLORS[StatusState.IDLE],
            font=("Segoe UI", 17),
        )
        self._dot.pack(side="left", padx=(0, 7))
        self._label = ctk.CTkLabel(
            status, text=_STATE_LABELS[StatusState.IDLE], anchor="w",
            text_color="#f3f6fb", font=("Segoe UI", 17, "bold"),
        )
        self._label.pack(side="left", fill="x", expand=True)
        self._detail = ctk.CTkLabel(
            self._card, text=self._idle_hint, anchor="w",
            text_color="#8f9db3", font=("Segoe UI", 11),
        )
        self._detail.pack(fill="x", padx=17, pady=(1, 13))

        self._content = ctk.CTkFrame(self._card, fg_color="#111a2a", corner_radius=12)
        self._conversation = ctk.CTkFrame(self._content, fg_color="transparent")
        self._conversation.pack(fill="x", padx=14, pady=(12, 5))
        ctk.CTkLabel(
            self._conversation, text="YOU", width=44, anchor="nw",
            text_color="#69e6a6", font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="nw", padx=(0, 9))
        self._transcript = ctk.CTkLabel(
            self._conversation, text="", anchor="w", justify="left", wraplength=430,
            text_color="#dce5f3", font=("Segoe UI", 12),
        )
        self._transcript.grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(
            self._conversation, text="ARES", width=44, anchor="nw",
            text_color="#7c9cff", font=("Segoe UI", 9, "bold"),
        ).grid(row=1, column=0, sticky="nw", padx=(0, 9), pady=(9, 0))
        self._response = ctk.CTkLabel(
            self._conversation, text="", anchor="w", justify="left", wraplength=430,
            text_color="#f3f6fb", font=("Segoe UI", 12),
        )
        self._response.grid(row=1, column=1, sticky="ew", pady=(9, 0))
        self._conversation.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self._content, text="ACTIVITY", anchor="w", text_color="#8f9db3",
            font=("Segoe UI", 9, "bold"),
        ).pack(fill="x", padx=14, pady=(8, 4))
        self._activity = ctk.CTkScrollableFrame(
            self._content, height=220, fg_color="#0d1523", corner_radius=9,
        )
        self._activity.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _drag_start(self, event: Any) -> None:
        self._drag_offset = (int(event.x_root) - self._root.winfo_x(), int(event.y_root) - self._root.winfo_y())

    def _drag_move(self, event: Any) -> None:
        x = int(event.x_root) - self._drag_offset[0]
        y = int(event.y_root) - self._drag_offset[1]
        self._root.geometry(f"+{x}+{y}")

    def _drain_commands(self) -> None:
        for _ in range(100):
            try:
                command, args = self._commands.get_nowait()
            except queue.Empty:
                break
            if command == "state":
                self._apply_state(StatusState(args[0]), args[1])
            elif command == "show":
                self._show()
            elif command == "hide":
                self._hide()
            elif command == "toggle":
                self._hide() if self._visible else self._show()
            elif command == "transcript":
                self._set_transcript(*args)
            elif command == "response":
                self._append_response(*args)
            elif command == "tool_start":
                self._tool_started(*args)
            elif command == "tool_progress":
                self._tool_progress(*args)
            elif command == "tool_result":
                self._tool_result(*args)
            elif command == "wake_hint":
                self._set_wake_hint(*args)
            elif command == "microphone":
                self._set_microphone(*args)
            elif command == "clear":
                self._clear_session()
            elif command == "destroy":
                self._destroying = True
                self._root.quit()
                return
        if not self._destroying:
            self._root.after(self._POLL_INTERVAL_MS, self._drain_commands)

    def _apply_state(self, state: StatusState, text: str) -> None:
        self._label.configure(text=text or _STATE_LABELS.get(state, ""))
        self._dot.configure(text_color=_STATE_COLORS.get(state, "#8b95a7"))
        if state == StatusState.IDLE:
            self._detail.configure(text=self._idle_hint)
            self._schedule_hide()
        elif state == StatusState.AWAKE:
            self._detail.configure(text="Say your request — wake window is active")
            self._show()
        elif state == StatusState.MUTED:
            self._detail.configure(text="Use the tray menu or hotkey to unmute")
            self._show()
        elif state == StatusState.ERROR:
            self._detail.configure(text="Check the Ares terminal for details")
            self._show()
        else:
            detail = (
                "Listening for your next instruction"
                if state == StatusState.LISTENING
                else "Ares is processing your request"
            )
            self._detail.configure(text=detail)
            self._show()

    def _expand(self) -> None:
        if self._expanded:
            return
        self._expanded = True
        self._place_window(expanded=True)
        self._content.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._show()

    def _set_wake_hint(self, hint: str) -> None:
        self._options["wake_word_hint"] = hint
        self._rebuild_idle_hint()
        if not self._visible:
            self._detail.configure(text=self._idle_hint)

    def _set_microphone(self, name: str) -> None:
        self._microphone_name = self._clean_display_text(name).strip()
        self._rebuild_idle_hint()
        self._detail.configure(text=self._idle_hint)

    def _rebuild_idle_hint(self) -> None:
        hint = f"Hold {self._options['hotkey_label']}"
        wake_hint = str(self._options.get("wake_word_hint") or "").strip()
        if wake_hint:
            hint += f" or say ‘{wake_hint}’"
        if self._microphone_name:
            hint += f"  ·  Mic: {self._microphone_name}"
        self._idle_hint = hint

    def _clear_session(self) -> None:
        self._transcript.configure(text="")
        self._response.configure(text="")
        self._response_text = ""
        for child in self._activity.winfo_children():
            child.destroy()
        self._tool_rows.clear()
        if self._expanded:
            self._content.pack_forget()
            self._expanded = False
            self._place_window(expanded=False)

    def _set_transcript(self, text: str) -> None:
        self._expand()
        self._transcript.configure(text=self._clean_display_text(text))
        self._response_text = ""
        self._response.configure(text="")
        for child in self._activity.winfo_children():
            child.destroy()
        self._tool_rows.clear()

    def _append_response(self, text: str) -> None:
        self._expand()
        self._response_text = (
            self._response_text + self._clean_display_text(text)
        )[-4_000:]
        self._response.configure(text=self._response_text)

    @staticmethod
    def _clean_display_text(text: str) -> str:
        cleaned: list[str] = []
        for char in str(text or ""):
            if char in {"\u200d", "\ufe0e", "\ufe0f", "\u20e3"}:
                continue
            if unicodedata.category(char) in {"So", "Sk", "Cs", "Co"}:
                continue
            cleaned.append(char)
        return "".join(cleaned)

    @staticmethod
    def _tool_title(name: str) -> str:
        clean = str(name or "tool").split("__")[-1].replace("_", " ").strip()
        return clean.title() or "Tool"

    def _ensure_tool_row(self, name: str) -> tuple[Any, Any, Any]:
        row = self._tool_rows.get(name)
        if row is not None:
            return row
        ctk = self._ctk
        frame = ctk.CTkFrame(self._activity, fg_color="#17243a", corner_radius=8)
        frame.pack(fill="x", padx=2, pady=3)
        icon = ctk.CTkLabel(
            frame, text="●", width=22, text_color="#ffb454", font=("Segoe UI", 11),
        )
        icon.pack(side="left", padx=(9, 5), pady=8)
        labels = ctk.CTkFrame(frame, fg_color="transparent")
        labels.pack(side="left", fill="x", expand=True, pady=6)
        ctk.CTkLabel(
            labels, text=self._tool_title(name), anchor="w", text_color="#eef4ff",
            font=("Segoe UI", 11, "bold"),
        ).pack(fill="x")
        detail = ctk.CTkLabel(
            labels, text="Starting…", anchor="w", text_color="#8f9db3",
            font=("Segoe UI", 10),
        )
        detail.pack(fill="x")
        result = ctk.CTkFrame(self._activity, fg_color="transparent")
        self._tool_rows[name] = (icon, detail, result)
        return icon, detail, result

    def _tool_started(self, name: str) -> None:
        self._expand()
        icon, detail, _result = self._ensure_tool_row(name)
        icon.configure(text_color="#ffb454")
        detail.configure(text="Starting…")

    def _tool_progress(self, name: str, detail_text: str) -> None:
        self._expand()
        _icon, detail, _result = self._ensure_tool_row(name)
        detail.configure(text=self._clean_display_text(detail_text) or "Working…")

    def _tool_result(self, name: str, payload: str) -> None:
        self._expand()
        icon, detail, result = self._ensure_tool_row(name)
        icon.configure(text="✓", text_color="#69e6a6")
        detail.configure(text="Completed")
        for child in result.winfo_children():
            child.destroy()
        items = self._result_items(payload)
        if not items:
            preview = re.sub(
                r"\s+", " ", self._clean_display_text(payload)
            ).strip()[:240]
            if preview:
                items = [preview]
        if not items:
            return
        result.pack(fill="x", padx=31, pady=(0, 4))
        for item in items[:8]:
            item = self._clean_display_text(item)
            candidate = Path(item).expanduser()
            if candidate.exists():
                self._ctk.CTkButton(
                    result, text=f"↗  {item}", anchor="w", height=25,
                    fg_color="transparent", hover_color="#223653", text_color="#b9c7da",
                    font=("Consolas", 10), command=lambda path=candidate: self._open_path(path),
                ).pack(fill="x", pady=1)
            else:
                self._ctk.CTkLabel(
                    result, text=f"  {item}", anchor="w", justify="left", wraplength=440,
                    text_color="#b9c7da", font=("Consolas", 10),
                ).pack(fill="x", pady=1)

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError:
            pass

    @staticmethod
    def _result_items(payload: str) -> list[str]:
        try:
            value = json.loads(payload)
        except (TypeError, ValueError):
            value = None
        found: list[str] = []

        def visit(item: Any, key: str = "") -> None:
            if len(found) >= 12:
                return
            if isinstance(item, dict):
                for child_key, child in item.items():
                    visit(child, str(child_key))
            elif isinstance(item, list):
                for child in item:
                    visit(child, key)
            elif isinstance(item, str):
                clean = re.sub(r"\s+", " ", item).strip()
                path_key = any(part in key.casefold() for part in ("path", "file", "name", "title"))
                path_text = bool(re.search(r"(?:[A-Za-z]:\\|[/\\]).+", clean))
                if clean and (path_key or path_text) and clean not in found:
                    found.append(clean[:260])

        if value is not None:
            visit(value)
        if not found:
            for match in re.findall(
                r"(?:[A-Za-z]:\\[^\r\n,;]+|(?:[\w .-]+[/\\])+[\w .-]+)", payload
            ):
                clean = match.strip(' \t\"\'')
                if clean and clean not in found:
                    found.append(clean[:260])
        return found

    def _show(self) -> None:
        self._cancel_hide()
        if not self._visible:
            self._root.deiconify()
            self._root.lift()
            self._visible = True

    def _hide(self) -> None:
        self._cancel_hide()
        if self._visible:
            self._root.withdraw()
            self._visible = False

    def _schedule_hide(self) -> None:
        self._cancel_hide()
        if self._options["auto_hide_ms"]:
            self._hide_job = self._root.after(self._options["auto_hide_ms"], self._hide)

    def _cancel_hide(self) -> None:
        if self._hide_job is not None:
            try:
                self._root.after_cancel(self._hide_job)
            except Exception:
                pass
        self._hide_job = None
