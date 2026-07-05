"""KDE Connect bridge for Android notifications, contacts, and SMS."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from typing import Any

from ares.config import load_config


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _run(args: list[str], timeout: int = 12) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def _missing_cli() -> str | None:
    return None if shutil.which("kdeconnect-cli") else "kdeconnect-cli not found. Install KDE Connect desktop tools and pair your phone."


def _device_config_id() -> str:
    try:
        return load_config().phone.kdeconnect_device_id.strip()
    except Exception:
        return ""


@lru_cache(maxsize=1)
def get_device_id() -> str:
    """Resolve the paired KDE Connect device id from config or kdeconnect-cli."""
    configured = _device_config_id()
    if configured:
        return configured
    if _missing_cli():
        return ""
    proc = _run(["kdeconnect-cli", "-l"])
    if proc.returncode != 0:
        return ""
    for line in proc.stdout.splitlines():
        # Typical: "- Pixel 8: abcdef... (paired and reachable)"
        match = re.search(r":\s*([A-Za-z0-9_-]{6,})\s*\(", line)
        if match:
            return match.group(1)
    return ""


def status() -> dict[str, Any]:
    err = _missing_cli()
    if err:
        return {"ok": False, "paired": False, "reachable": False, "error": err}
    proc = _run(["kdeconnect-cli", "-l"])
    output = (proc.stdout + proc.stderr).strip()

    device_id = _device_config_id()
    if not device_id and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if "paired" not in line.lower():
                continue
            match = re.search(r":\s*([A-Za-z0-9_-]{6,})\s*\(", line)
            if match:
                device_id = match.group(1)
                break

    paired = bool(device_id)
    reachable = paired and ("reachable" in output.lower())
    return {
        "ok": proc.returncode == 0 and paired,
        "paired": paired,
        "reachable": reachable,
        "device_id": device_id,
        "raw": output,
        "error": "" if proc.returncode == 0 and paired else (output or "No KDE Connect device paired."),
    }


def _device_args() -> list[str] | None:
    device_id = get_device_id()
    return ["--device", device_id] if device_id else None


def get_recent_notifications(limit: int = 20) -> str:
    err = _missing_cli()
    if err:
        return _json({"ok": False, "notifications": [], "error": err})
    dev = _device_args()
    if not dev:
        return _json({"ok": False, "notifications": [], "error": "KDE Connect device not paired or not reachable."})
    proc = _run(["kdeconnect-cli", *dev, "--list-notifications"])
    if proc.returncode != 0:
        return _json({"ok": False, "notifications": [], "error": (proc.stderr or proc.stdout).strip()})
    notifications = []
    for line in proc.stdout.splitlines()[: max(0, int(limit))]:
        text = line.strip()
        if not text:
            continue
        app, _, body = text.partition(":")
        title, _, msg = body.strip().partition(" - ")
        notifications.append({"package": "", "app": app.strip() or "unknown", "title": title.strip(), "text": msg.strip() or body.strip(), "timestamp": ""})
    return _json({"ok": True, "snapshot": True, "notifications": notifications})


def search_contacts(query: str) -> str:
    err = _missing_cli()
    if err:
        return _json({"ok": False, "contacts": [], "error": err})
    dev = _device_args()
    if not dev:
        return _json({"ok": False, "contacts": [], "error": "KDE Connect device not paired or not reachable."})
    attempts = [["kdeconnect-cli", *dev, "--search-contacts", query], ["kdeconnect-cli", *dev, "--list-contacts"]]
    last = ""
    for args in attempts:
        proc = _run(args)
        last = (proc.stderr or proc.stdout).strip()
        if proc.returncode == 0:
            contacts = []
            for line in proc.stdout.splitlines():
                if query.lower() not in line.lower():
                    continue
                numbers = re.findall(r"\+?[0-9][0-9 ()-]{5,}[0-9]", line)
                name = re.sub(r"\s*<?\+?[0-9][0-9 ()-]{5,}[0-9]>?", "", line).strip(" -:,")
                contacts.append({"name": name or line.strip(), "numbers": [n.strip() for n in numbers]})
            return _json({"ok": True, "contacts": contacts})
    return _json({"ok": False, "contacts": [], "error": last or "Contacts plugin unavailable."})


def send_sms(number: str, message: str) -> str:
    err = _missing_cli()
    if err:
        return _json({"ok": False, "sent": False, "error": err})
    dev = _device_args()
    if not dev:
        return _json({"ok": False, "sent": False, "error": "KDE Connect device not paired or not reachable."})
    proc = _run(["kdeconnect-cli", *dev, "--send-sms", message, "--destination", number], timeout=20)
    return _json({"ok": proc.returncode == 0, "sent": proc.returncode == 0, "number": number, "error": "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()})


def get_recent_sms(limit: int = 10) -> str:
    return _json({"ok": False, "messages": [], "limit": limit, "error": "KDE Connect CLI does not expose a stable SMS read API on all platforms yet."})
