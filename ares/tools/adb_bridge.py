"""Small ADB bridge for Android phone status and call placement."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any


from ares.config import load_config
from ares.tools import kdeconnect_bridge


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _run(args: list[str], timeout: int = 12) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _adb() -> str | None:
    """Return the path to adb from config or PATH."""
    try:
        cfg_path = load_config().phone.adb_path.strip()
        if cfg_path:
            return cfg_path
    except Exception:
        pass
    return shutil.which("adb")


def _configured_device() -> str:
    try:
        return load_config().phone.adb_device_address.strip()
    except Exception:
        return ""


def _base_args() -> list[str]:
    adb = _adb() or "adb"
    device = _configured_device()
    return [adb, "-s", device] if device else [adb]


def connected_devices() -> list[str]:
    adb = _adb()
    if not adb:
        return []
    try:
        proc = _run([adb, "devices"])
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return []
    devices = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    configured = _configured_device()
    return [d for d in devices if d == configured] if configured else devices


def is_device_connected() -> bool:
    return bool(connected_devices())


def get_battery_status() -> str:
    if not _adb():
        return _json({"ok": False, "error": "adb not found. Install Android platform-tools."})
    if not is_device_connected():
        return _json({"ok": False, "error": "No authorized ADB device connected."})
    try:
        proc = _run([*_base_args(), "shell", "dumpsys", "battery"])
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return _json({"ok": False, "error": f"ADB battery query failed: {exc}"})
    if proc.returncode != 0:
        return _json({"ok": False, "error": (proc.stderr or proc.stdout).strip()})
    fields: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            key, value = line.strip().split(":", 1)
            fields[key.strip()] = value.strip()
    return _json({"ok": True, "battery": fields})


def call_number(number: str, confirm: bool = False) -> str:
    if not re.fullmatch(r"[+0-9 ()-]{3,30}", number):
        return _json({"ok": False, "dialed": False, "error": "Invalid phone number format."})
    if not _adb():
        return _json({"ok": False, "dialed": False, "error": "adb not found. Install Android platform-tools."})
    if not is_device_connected():
        return _json({"ok": False, "dialed": False, "error": "No authorized ADB device connected."})

    normalized = re.sub(r"[ ()-]", "", number)
    uri = "tel:" + normalized
    try:
        proc = _run([*_base_args(), "shell", "am", "start", "-a", "android.intent.action.CALL", "-d", uri], timeout=20)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return _json({"ok": False, "dialed": False, "number": number, "error": f"ADB call command failed: {exc}"})
    return _json({"ok": proc.returncode == 0, "dialed": proc.returncode == 0, "manual_phone_confirmation_may_be_required": True, "number": number, "error": "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()})


def phone_status() -> str:
    kde = kdeconnect_bridge.status()

    adb_present = bool(_adb())
    devices = connected_devices()
    adb_ok = bool(devices)

    battery_fields: dict[str, Any] = {}
    if adb_ok:
        try:
            proc = _run([*_base_args(), "shell", "dumpsys", "battery"])
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if ":" in line:
                        key, value = line.strip().split(":", 1)
                        battery_fields[key.strip()] = value.strip()
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass

    adb_error = ""
    if not adb_ok:
        adb_error = (
            "adb not found. Install Android platform-tools."
            if not adb_present
            else "No authorized ADB device connected."
        )
    kde_ready = bool(kde.get("ok")) and bool(kde.get("reachable"))
    capability_matrix = {
        "notifications": kde_ready,
        "contacts": kde_ready,
        "sms": kde_ready,
        "battery": adb_ok,
        "calls": adb_ok,
        "launch_app": adb_ok,
        "open_url": adb_ok,
    }
    permission_preflight = {
        "kdeconnect_cli": {
            "ok": bool(kde.get("ok")),
            "paired": bool(kde.get("paired")),
            "reachable": bool(kde.get("reachable")),
            "error": kde.get("error", ""),
        },
        "adb": {
            "installed": adb_present,
            "authorized_device": adb_ok,
            "configured_device": _configured_device(),
            "error": adb_error,
        },
    }

    return _json(
        {
            # ``ok`` means at least one useful capability family is available;
            # callers that need both can use fully_ready.  This prevents an
            # ADB-only phone from looking wholly unavailable.
            "ok": kde_ready or adb_ok,
            "any_ready": kde_ready or adb_ok,
            "fully_ready": kde_ready and adb_ok,
            "permission_preflight": permission_preflight,
            "capability_matrix": capability_matrix,
            "kdeconnect": kde,
            "adb": {
                "ok": adb_ok,
                "installed": adb_present,
                "connected": adb_ok,
                "devices": devices,
                "configured_device": _configured_device(),
                "battery": battery_fields,
                "error": adb_error,
            },
        }
    )


def launch_app(package: str) -> str:
    """Launch an app on the phone by package name via ADB monkey command."""
    if not _adb():
        return _json({"ok": False, "error": "adb not found. Install Android platform-tools."})
    if not is_device_connected():
        return _json({"ok": False, "error": "No authorized ADB device connected."})
    if not package or not package.strip():
        return _json({"ok": False, "error": "Package name is required."})
    package = package.strip()
    try:
        proc = _run(
            [*_base_args(), "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return _json({"ok": False, "launched": False, "package": package, "error": f"ADB launch command failed: {exc}"})
    output = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode == 0 and "Events injected" in output
    return _json({"ok": ok, "launched": ok, "package": package, "error": "" if ok else (output or "Failed to launch app.")})


def launch_url(url: str) -> str:
    """Open a URL on the phone via ADB intent."""
    if not _adb():
        return _json({"ok": False, "error": "adb not found. Install Android platform-tools."})
    if not is_device_connected():
        return _json({"ok": False, "error": "No authorized ADB device connected."})
    if not url or not url.strip():
        return _json({"ok": False, "error": "URL is required."})
    url = url.strip()
    try:
        proc = _run(
            [*_base_args(), "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return _json({"ok": False, "launched": False, "url": url, "error": f"ADB URL command failed: {exc}"})
    output = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode == 0
    return _json({"ok": ok, "launched": ok, "url": url, "error": "" if ok else (output or "Failed to open URL.")})
