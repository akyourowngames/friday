import ctypes
import sys
import time
from pathlib import Path

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_KEYBOARD_VERSION = "1.0.0"
_KEY_ALIASES = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "windows": 0x5B,
    "cmd": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}


def _resolve_path(path: str) -> Path:
    candidate = Path(path or ".").expanduser()
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parent.parent / candidate
    return candidate.resolve()


def _line_key_value(line: str) -> tuple[str, str]:
    cleaned = line.strip()
    if cleaned.startswith("- "):
        cleaned = cleaned[2:].strip()
    key, marker, value = cleaned.partition(":")
    if not marker:
        return "", ""
    return key.strip().lower(), value.strip()


def _load_shortcuts(config_path: str) -> tuple[dict[str, dict], dict | None]:
    path = _resolve_path(config_path)
    if not path.exists():
        return {}, error_payload(
            "CONFIG_NOT_FOUND",
            "The keyboard shortcuts markdown file does not exist.",
            "config_path",
            str(path),
            "existing markdown file",
            False,
            "Create KEYBOARD_SHORTCUTS.md or pass config_path.",
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}, error_payload(
            "CONFIG_READ_FAILED",
            "The keyboard shortcuts file could not be read.",
            "config_path",
            str(path),
            "UTF-8 markdown file",
            True,
            "Verify file permissions.",
        )

    shortcuts = {}
    current = None
    for raw in lines:
        line = raw.strip()
        if not line or (line.startswith("# ") and not line.startswith("### ")):
            continue
        if line.startswith("### "):
            name = line[4:].strip()
            current = {"name": name}
            shortcuts[name] = current
            continue
        key, value = _line_key_value(line)
        if current is not None and key:
            current[key] = value
    return shortcuts, None


def _vk_for_token(token: str) -> int | None:
    lowered = token.strip().lower()
    if not lowered:
        return None
    if lowered in _KEY_ALIASES:
        return _KEY_ALIASES[lowered]
    if len(lowered) == 1:
        return ord(lowered.upper())
    if lowered.startswith("f") and lowered[1:].isdigit():
        fn = int(lowered[1:])
        if 1 <= fn <= 24:
            return 0x6F + fn
    return None


def _parse_keys(keys: str) -> tuple[list[int], dict | None]:
    parts = [part.strip() for part in str(keys or "").split("+") if part.strip()]
    if not parts:
        return [], error_payload(
            "EMPTY_KEYS",
            "keys must not be empty.",
            "keys",
            keys,
            "key combination such as ctrl+c",
            False,
            "Pass keys with + between modifiers.",
        )
    vks = []
    for part in parts:
        vk = _vk_for_token(part)
        if vk is None:
            return [], error_payload(
                "UNKNOWN_KEY",
                "One of the key tokens is not supported.",
                "keys",
                part,
                "supported key name or single character",
                False,
                "Use aliases like ctrl, shift, alt, enter, or a single letter.",
            )
        vks.append(vk)
    return vks, None


def _send_keys(vks: list[int]) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "keyboard_send requires win32"
    try:
        user32 = ctypes.windll.user32
        for vk in vks[:-1]:
            user32.keybd_event(vk, 0, 0, 0)
        if vks:
            user32.keybd_event(vks[-1], 0, 0, 0)
            user32.keybd_event(vks[-1], 0, 0, 2)
        for vk in reversed(vks[:-1]):
            user32.keybd_event(vk, 0, 0, 2)
        return True, ""
    except Exception as exc:
        return False, exc.__class__.__name__


def _keyboard_trace(started_at, started, inputs_received, path, status, fields, error_code=None):
    return make_trace(
        "keyboard_press",
        _KEYBOARD_VERSION,
        started_at,
        started,
        inputs_received,
        True,
        path,
        status,
        fields,
        {"count": 0, "systems": ["keyboard"]},
        error_code,
    )


@tool(
    name="keyboard_press",
    description="Press an arbitrary keyboard shortcut or key combination using explicit key tokens joined by +",
    examples=[
        "press ctrl+s",
        "send alt+tab",
        "press enter",
    ],
    param_descriptions={
        "keys": "Key combination with + between parts, e.g. ctrl+shift+s",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def keyboard_press(keys: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 3
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    vks, parse_error = _parse_keys(keys)
    if parse_error is not None:
        trace = _keyboard_trace(started_at, started, inputs_received, "validate", "FAILED", 1, parse_error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("keyboard_press", _KEYBOARD_VERSION, parse_error, started, trace)
        return "Error: invalid keys"
    ok, detail = _send_keys(vks)
    if not ok:
        error = error_payload("KEYPRESS_FAILED", "The key press did not complete.", "keys", keys, "successful key event", True, detail)
        trace = _keyboard_trace(started_at, started, inputs_received, "send", "FAILED", 1, error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("keyboard_press", _KEYBOARD_VERSION, error, started, trace)
        return f"Key press failed: {detail}"
    result = {"keys": keys, "tokens": keys.split("+"), "platform": sys.platform, "sent": True}
    legacy = f"Pressed keys: {keys}"
    trace = _keyboard_trace(started_at, started, inputs_received, "send", "SUCCESS", len(result))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("keyboard_press", _KEYBOARD_VERSION, result, started, trace)
    return legacy


@tool(
    name="keyboard_shortcut",
    description="Press a named shortcut from KEYBOARD_SHORTCUTS.md",
    examples=[
        "use the copy shortcut",
        "run keyboard shortcut save",
        "press shortcut new_tab",
    ],
    param_descriptions={
        "name": "Shortcut name from KEYBOARD_SHORTCUTS.md",
        "config_path": "Markdown shortcuts file path",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def keyboard_shortcut(
    name: str,
    config_path: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 4
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    name = str(name or "").strip().lower()
    config_path = config_path or settings.keyboard_shortcuts_file
    if not name:
        error = error_payload("EMPTY_NAME", "name must not be empty.", "name", name, "shortcut name", False, "Pass a shortcut name from KEYBOARD_SHORTCUTS.md.")
        trace = make_trace("keyboard_shortcut", _KEYBOARD_VERSION, started_at, started, inputs_received, False, "validate", "FAILED", 1, error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("keyboard_shortcut", _KEYBOARD_VERSION, error, started, trace)
        return "Error: shortcut name is required"

    shortcuts, config_error = _load_shortcuts(config_path)
    if config_error is not None:
        trace = make_trace("keyboard_shortcut", _KEYBOARD_VERSION, started_at, started, inputs_received, False, "config", "FAILED", 1, config_error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("keyboard_shortcut", _KEYBOARD_VERSION, config_error, started, trace)
        return "Error: shortcuts config unavailable"

    entry = shortcuts.get(name)
    if not entry:
        error = error_payload(
            "SHORTCUT_NOT_FOUND",
            "The shortcut name was not found in the markdown file.",
            "name",
            name,
            "defined shortcut name",
            False,
            "Add the shortcut to KEYBOARD_SHORTCUTS.md.",
        )
        trace = make_trace("keyboard_shortcut", _KEYBOARD_VERSION, started_at, started, inputs_received, False, "resolve", "FAILED", 1, error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("keyboard_shortcut", _KEYBOARD_VERSION, error, started, trace)
        return f"Unknown shortcut '{name}'"

    keys = entry.get("keys", "")
    pressed = keyboard_press(keys, response_format=response_format, trace_enabled=trace_enabled)
    if response_format == "structured" and isinstance(pressed, dict):
        if "error" in pressed:
            return pressed
        pressed["result"]["shortcut"] = name
        pressed["meta"]["tool"] = "keyboard_shortcut"
        return pressed
    return f"Shortcut '{name}': {pressed}"
