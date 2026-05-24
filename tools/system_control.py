import ctypes
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_SYSTEM_VERSION = "1.1.0"
_MEDIA_KEYS = {
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "volume_mute": 0xAD,
    "media_play_pause": 0xB3,
    "media_next": 0xB0,
    "media_previous": 0xB1,
    "brightness_up": 0xD6,
    "brightness_down": 0xD5,
}
_ACTION_ALIASES_PATH = Path(__file__).with_name("SYSTEM_CONTROL_ALIASES.md")
_BUNDLED_CONTROLS_PATH = Path(__file__).with_name("SYSTEM_CONTROLS.md")


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


def _controls_config_candidates(requested: str) -> list[Path]:
    seen: set[str] = set()
    candidates: list[Path] = []
    for raw in (requested, settings.system_controls_file, str(_BUNDLED_CONTROLS_PATH)):
        text = str(raw or "").strip()
        if not text:
            continue
        path = _resolve_path(text)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)
    return candidates


def _resolve_controls_path(requested: str) -> tuple[Path | None, list[Path]]:
    candidates = _controls_config_candidates(requested)
    for path in candidates:
        if path.exists():
            return path, candidates
    return None, candidates


def _load_controls(config_path: str) -> tuple[dict[str, dict], dict | None, Path | None]:
    path, candidates = _resolve_controls_path(config_path)
    if path is None:
        tried = ", ".join(str(item) for item in candidates) or "(none)"
        return {}, error_payload(
            "CONFIG_NOT_FOUND",
            "The system controls markdown file does not exist.",
            "config_path",
            tried,
            "existing markdown file",
            False,
            "Omit config_path to use the bundled catalog, or set KING_SYSTEM_CONTROLS_FILE.",
        ), None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}, error_payload(
            "CONFIG_READ_FAILED",
            "The system controls markdown file could not be read.",
            "config_path",
            str(path),
            "UTF-8 markdown file",
            True,
            "Verify file permissions and encoding.",
        ), path

    actions = {}
    current = None
    default_platform = "windows"
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("# ") and not line.startswith("### "):
            continue
        if line.startswith("### "):
            name = line[4:].strip()
            current = {"name": name}
            actions[name] = current
            continue
        key, value = _line_key_value(line)
        if not key:
            continue
        if key == "default_platform":
            default_platform = value.lower()
            continue
        if current is not None:
            current[key] = value
    return {"actions": actions, "default_platform": default_platform}, None, path


@lru_cache(maxsize=1)
def _load_action_aliases() -> dict:
    aliases = {}
    path = _ACTION_ALIASES_PATH
    if not path.exists():
        return aliases
    in_aliases = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.lower() == "## aliases":
            in_aliases = True
            continue
        if in_aliases and line.startswith("## "):
            break
        if not in_aliases or not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        alias = left.strip().lower().replace(" ", "_")
        target = right.strip().lower()
        if alias and target:
            aliases[alias] = target
    return aliases


def _normalize_action_name(action: str) -> str:
    normalized = str(action or "").strip().lower().replace(" ", "_")
    return _load_action_aliases().get(normalized, normalized)


def _coerce_int(value, default: int = 0) -> int:
    if value is None or value is False:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _press_media_key(vk: int, repeats: int = 1, pause_seconds: float = 0.04) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "media_key requires win32"
    try:
        user32 = ctypes.windll.user32
        count = max(1, int(repeats))
        for _ in range(count):
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 0, 2)
            if pause_seconds > 0:
                time.sleep(pause_seconds)
        return True, ""
    except Exception as exc:
        return False, exc.__class__.__name__


_WMI_BRIGHTNESS_PROBE: dict[str, bool | None] = {"checked": False, "effective": None}


def _wmi_brightness_is_effective() -> bool:
    cached = _WMI_BRIGHTNESS_PROBE.get("effective")
    if _WMI_BRIGHTNESS_PROBE.get("checked"):
        return bool(cached)
    _WMI_BRIGHTNESS_PROBE["checked"] = True
    current, _ = _brightness_get()
    if current is None:
        _WMI_BRIGHTNESS_PROBE["effective"] = False
        return False
    probe = 60 if current != 60 else 61
    probe = max(1, min(100, int(probe)))
    ok, _ = _brightness_set(probe)
    if not ok:
        _WMI_BRIGHTNESS_PROBE["effective"] = False
        return False
    time.sleep(0.35)
    after, _ = _brightness_get()
    effective = after is not None and int(after) == probe
    if effective:
        _brightness_set(current)
        time.sleep(0.15)
    _WMI_BRIGHTNESS_PROBE["effective"] = effective
    return effective


def _brightness_key_repeats(delta: int) -> int:
    return max(2, min(8, abs(int(delta)) // 2 or 2))


def _run_powershell(script: str, timeout_seconds: float = 15.0) -> tuple[bool, str, str]:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit {completed.returncode}"
            return False, detail, stdout
        return True, "", stdout
    except subprocess.TimeoutExpired:
        return False, "timeout", ""
    except OSError as exc:
        return False, exc.__class__.__name__, ""


def _brightness_get() -> tuple[int | None, str]:
    script = (
        "$b = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty CurrentBrightness; "
        "if ($null -eq $b) { exit 2 }; Write-Output $b"
    )
    ok, err, out = _run_powershell(script)
    if not ok:
        return None, err
    try:
        return int(str(out).strip()), ""
    except ValueError:
        return None, "invalid brightness value"


def _brightness_set(level: int) -> tuple[bool, str]:
    level = max(0, min(100, int(level)))
    script = (
        f"$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction SilentlyContinue | "
        f"Select-Object -First 1; if ($null -eq $m) {{ exit 2 }}; "
        f"$m.WmiSetBrightness(1, {level}); Write-Output {level}"
    )
    ok, err, _out = _run_powershell(script)
    return ok, err


def _brightness_delta_with_fallback(action: dict) -> tuple[dict, dict | None]:
    delta = _coerce_int(action.get("delta", 0), 0)
    current, read_error = _brightness_get()
    key_name = "brightness_up" if delta > 0 else "brightness_down"
    vk = _MEDIA_KEYS.get(key_name)
    if vk is None:
        return {}, error_payload(
            "BRIGHTNESS_UNAVAILABLE",
            "Brightness could not be changed via WMI or hardware keys.",
            "action",
            action.get("name", ""),
            "brightness change",
            True,
            read_error or "brightness key map missing",
        )

    if current is not None and _wmi_brightness_is_effective():
        target = max(0, min(100, current + delta))
        ok, err = _brightness_set(target)
        if ok:
            time.sleep(0.35)
            after, _ = _brightness_get()
            if after is not None and int(after) == target:
                return {
                    "method": "brightness_delta",
                    "path": "wmi",
                    "previous": current,
                    "level": target,
                    "delta": delta,
                    "verified_level": after,
                    "verified": True,
                }, None
        read_error = err or read_error

    repeats = _brightness_key_repeats(delta)
    ok, detail = _press_media_key(vk, repeats, pause_seconds=0.06)
    if not ok:
        return {}, error_payload(
            "ACTION_FAILED",
            "Brightness hardware key press failed.",
            "action",
            action.get("name", ""),
            "brightness change",
            True,
            detail,
        )
    time.sleep(0.2)
    after, _ = _brightness_get()
    verified = after is not None and current is not None and int(after) != int(current)
    outcome = {
        "method": "brightness_delta",
        "path": "hardware_key",
        "key": key_name,
        "delta": delta,
        "repeats": repeats,
        "previous": current,
        "verified_level": after,
        "verified": verified,
        "note": (
            "WMI cannot change this display; sent brightness hardware keys. "
            "Watch for the on-screen brightness indicator."
        ),
    }
    if current is not None and after is not None and verified:
        outcome["level"] = after
    return outcome, None


def _execute_action(action: dict, level: int = 0) -> tuple[dict, dict | None]:
    method = str(action.get("method", "")).strip().lower()
    if method == "media_key":
        key_name = str(action.get("key", "")).strip().lower()
        vk = _MEDIA_KEYS.get(key_name)
        if vk is None:
            return {}, error_payload(
                "UNKNOWN_MEDIA_KEY",
                "The media key is not defined in the runtime map.",
                "key",
                key_name,
                "supported media key name",
                False,
                "Use an action from SYSTEM_CONTROLS.md.",
            )
        step = max(1, _coerce_int(action.get("step", 1), 1))
        ok, detail = _press_media_key(vk, step)
        if not ok:
            return {}, error_payload(
                "ACTION_FAILED",
                "The media key action did not complete.",
                "action",
                action.get("name", ""),
                "successful key press",
                True,
                detail,
            )
        return {"method": method, "key": key_name, "repeats": step, "platform": sys.platform}, None

    if method == "brightness_delta":
        if sys.platform != "win32":
            return {}, error_payload(
                "PLATFORM_UNSUPPORTED",
                "Brightness control is only available on Windows in this build.",
                "platform",
                sys.platform,
                "win32",
                False,
                "Use volume or media keys, or run on Windows.",
            )
        return _brightness_delta_with_fallback(action)

    if method == "brightness_set":
        if sys.platform != "win32":
            return {}, error_payload(
                "PLATFORM_UNSUPPORTED",
                "Brightness control is only available on Windows in this build.",
                "platform",
                sys.platform,
                "win32",
                False,
                "Run on Windows or use brightness_up/down.",
            )
        if level <= 0:
            return {}, error_payload(
                "MISSING_LEVEL",
                "brightness_set requires level between 1 and 100.",
                "level",
                level,
                "integer 1..100",
                False,
                "Pass level with the action.",
            )
        current, _ = _brightness_get()
        ok, err = _brightness_set(level)
        if not ok:
            return {}, error_payload("ACTION_FAILED", "Brightness set failed.", "action", action.get("name", ""), "brightness set", True, err)
        return {"method": method, "previous": current, "level": level}, None

    return {}, error_payload(
        "UNKNOWN_METHOD",
        "The action method is not supported.",
        "method",
        method,
        "media_key, brightness_delta, or brightness_set",
        False,
        "Define the action in SYSTEM_CONTROLS.md.",
    )


def _control_trace(started_at, started, inputs_received, path, status, fields, error_code=None):
    return make_trace(
        "system_control",
        _SYSTEM_VERSION,
        started_at,
        started,
        inputs_received,
        True,
        path,
        status,
        fields,
        {"count": 1, "systems": ["os"]},
        error_code,
    )


@tool(
    name="system_control",
    description=(
        "Change this PC's volume, screen brightness, or media keys. "
        "Use action names volume_up, volume_down, brightness_down, brightness_up, brightness_set. "
        "Omit config_path; the bundled tools/SYSTEM_CONTROLS.md catalog is used automatically."
    ),
    examples=[
        "increase volume",
        "decrease brightness",
        "mute volume",
        "media play pause",
        "turn brightness up",
    ],
    param_descriptions={
        "action": "Catalog action: volume_up, volume_down, volume_mute, brightness_up, brightness_down, brightness_set",
        "level": "Target brightness 1-100 for brightness_set only",
        "config_path": "Optional override; omit unless the user named a specific markdown path",
        "response_format": "legacy or structured",
        "trace_enabled": "Emit machine-readable trace when true",
    },
)
def system_control(
    action: str,
    level: int = 0,
    config_path: str = "",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 5
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    action = _normalize_action_name(action)
    config_path = config_path or settings.system_controls_file
    parsed_level = 0

    if not action:
        error = error_payload("EMPTY_ACTION", "action must not be empty.", "action", action, "named system action", False, "Pass an action from SYSTEM_CONTROLS.md.")
        trace = _control_trace(started_at, started, inputs_received, "validate", "FAILED", 1, error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("system_control", _SYSTEM_VERSION, error, started, trace)
        return "Error: action is required"

    if action == "brightness_set":
        parsed_level, level_error = normalize_int(level, "level", 0, 1, 100, "Use level between 1 and 100.", "INVALID_LEVEL")
        if level_error is not None:
            trace = _control_trace(started_at, started, inputs_received, "validate", "FAILED", 1, level_error["code"])
            emit_trace(trace, trace_enabled)
            if response_format == "structured":
                return structured_error("system_control", _SYSTEM_VERSION, level_error, started, trace)
            return "Error: brightness_set requires level between 1 and 100"

    catalog, config_error, resolved_config_path = _load_controls(config_path)
    if config_error is not None:
        trace = _control_trace(started_at, started, inputs_received, "config", "FAILED", 1, config_error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("system_control", _SYSTEM_VERSION, config_error, started, trace)
        return "Error: system controls config unavailable"

    action_def = catalog.get("actions", {}).get(action)
    if not action_def:
        error = error_payload(
            "ACTION_NOT_FOUND",
            "The requested action is not defined in the markdown catalog.",
            "action",
            action,
            "action section in SYSTEM_CONTROLS.md",
            False,
            "Add the action or pick a listed action name.",
        )
        trace = _control_trace(started_at, started, inputs_received, "resolve", "FAILED", 1, error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("system_control", _SYSTEM_VERSION, error, started, trace)
        return f"Unknown action '{action}'"

    outcome, run_error = _execute_action(action_def, level=parsed_level)
    if run_error is not None:
        trace = _control_trace(started_at, started, inputs_received, "execute", "FAILED", 1, run_error["code"])
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_error("system_control", _SYSTEM_VERSION, run_error, started, trace)
        return f"Action failed: {run_error.get('message', '')}"

    result = {
        "action": action,
        "platform": sys.platform,
        "outcome": outcome,
        "config_path": str(resolved_config_path or _BUNDLED_CONTROLS_PATH),
    }
    legacy = f"System action '{action}' completed on {sys.platform}"
    if outcome.get("path") == "hardware_key" and "brightness" in str(outcome.get("key", "")):
        legacy = (
            f"Pressed {outcome.get('key')} {outcome.get('repeats', 1)} time(s) on {sys.platform}. "
            f"{outcome.get('note', 'Check the on-screen brightness indicator.')}"
        )
        if outcome.get("verified") and outcome.get("verified_level") is not None:
            legacy += f" WMI now reads {outcome.get('verified_level')}%."
    elif outcome.get("previous") is not None and outcome.get("level") is not None and outcome.get("verified"):
        legacy = (
            f"Brightness changed from {outcome.get('previous')}% to {outcome.get('level')}% "
            f"via {outcome.get('path', outcome.get('method', 'system'))}"
        )
    elif outcome.get("key"):
        legacy = f"Sent {outcome.get('key')} key ({outcome.get('repeats', 1)}x) on {sys.platform}"
    trace = _control_trace(started_at, started, inputs_received, "execute", "SUCCESS", len(result))
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("system_control", _SYSTEM_VERSION, result, started, trace)
    return legacy
