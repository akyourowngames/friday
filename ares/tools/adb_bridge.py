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
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


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
    proc = _run([adb, "devices"])
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
    proc = _run([*_base_args(), "shell", "dumpsys", "battery"])
    if proc.returncode != 0:
        return _json({"ok": False, "error": (proc.stderr or proc.stdout).strip()})
    fields: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            key, value = line.strip().split(":", 1)
            fields[key.strip()] = value.strip()
    return _json({"ok": True, "battery": fields})


def call_number(number: str, confirm: bool = False) -> str:
    if not confirm:
        return _json({"ok": False, "dialed": False, "confirm_required": True, "error": "Real phone call blocked. Re-call with confirm=true only after explicit user approval for this exact number."})
    if not re.fullmatch(r"[+0-9 ()-]{3,30}", number):
        return _json({"ok": False, "dialed": False, "error": "Invalid phone number format."})
    if not _adb():
        return _json({"ok": False, "dialed": False, "error": "adb not found. Install Android platform-tools."})
    if not is_device_connected():
        return _json({"ok": False, "dialed": False, "error": "No authorized ADB device connected."})

    normalized = re.sub(r"[ ()-]", "", number)
    uri = "tel:" + normalized
    proc = _run([*_base_args(), "shell", "am", "start", "-a", "android.intent.action.CALL", "-d", uri], timeout=20)
    return _json({"ok": proc.returncode == 0, "dialed": proc.returncode == 0, "manual_phone_confirmation_may_be_required": True, "number": number, "error": "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()})


def phone_status() -> str:
    kde = kdeconnect_bridge.status()

    adb_present = bool(_adb())
    devices = connected_devices()
    adb_ok = bool(devices)

    battery_fields: dict[str, Any] = {}
    if adb_ok:
        proc = _run([*_base_args(), "shell", "dumpsys", "battery"])
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if ":" in line:
                    key, value = line.strip().split(":", 1)
                    battery_fields[key.strip()] = value.strip()

    adb_error = ""
    if not adb_ok:
        adb_error = (
            "adb not found. Install Android platform-tools."
            if not adb_present
            else "No authorized ADB device connected."
        )

    return _json(
        {
            "ok": bool(kde.get("ok")) and adb_ok,
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
