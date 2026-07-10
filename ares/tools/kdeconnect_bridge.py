"""KDE Connect bridge with live capability checks and bounded parsing."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from ares.config import load_config


MAX_PHONE_RESULTS = 100


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _bounded_limit(value: int | str | None, default: int = 20) -> int:
    try:
        numeric = int(default if value is None else value)
    except (TypeError, ValueError):
        numeric = default
    return max(1, min(numeric, MAX_PHONE_RESULTS))


def _run(args: list[str], timeout: int = 12) -> subprocess.CompletedProcess[str]:
    # Explicit UTF-8/replace prevents a malformed notification or a local code
    # page from turning a read-only phone operation into an uncaught exception.
    return subprocess.run(
        args,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_result(args: list[str], timeout: int = 12) -> tuple[subprocess.CompletedProcess[str] | None, str]:
    try:
        return _run(args, timeout=timeout), ""
    except subprocess.TimeoutExpired:
        return None, f"KDE Connect command timed out after {timeout}s."
    except (OSError, UnicodeError) as exc:
        return None, f"KDE Connect command failed: {exc}"


def _cli_path() -> str:
    try:
        configured = load_config().phone.kdeconnect_cli_path.strip()
        if configured:
            return configured
    except Exception:
        pass
    return shutil.which("kdeconnect-cli") or "kdeconnect-cli"


def _missing_cli() -> str | None:
    path = _cli_path()
    if path and shutil.which(path):
        return None
    return "kdeconnect-cli not found. Install KDE Connect desktop tools and pair your phone."


def _device_config_id() -> str:
    try:
        return load_config().phone.kdeconnect_device_id.strip()
    except Exception:
        return ""


def _parse_devices(output: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = re.search(r":\s*([A-Za-z0-9_-]{6,})\s*(?:\(([^)]*)\))?", line)
        if not match:
            continue
        state = (match.group(2) or "").casefold()
        devices.append(
            {
                "id": match.group(1),
                "paired": "paired" in state or "reachable" in state,
                "reachable": "reachable" in state and "unreachable" not in state,
                "raw": line,
            }
        )
    return devices


def _parse_device_id(output: str) -> str:
    devices = _parse_devices(output)
    reachable = next((device for device in devices if device["paired"] and device["reachable"]), None)
    selected = reachable or next((device for device in devices if device["paired"]), None)
    return str(selected["id"]) if selected else ""


def status() -> dict[str, Any]:
    """Return current, not cached, pairing and reachability state."""
    error = _missing_cli()
    if error:
        return {"ok": False, "paired": False, "reachable": False, "device_id": "", "devices": [], "error": error}
    process, command_error = _run_result([_cli_path(), "-l"])
    if process is None:
        return {"ok": False, "paired": False, "reachable": False, "device_id": "", "devices": [], "error": command_error}
    output = f"{process.stdout or ''}{process.stderr or ''}".strip()
    devices = _parse_devices(process.stdout or "")
    configured = _device_config_id()
    if configured:
        selected = next((device for device in devices if device["id"] == configured), None)
    else:
        selected = next((device for device in devices if device["paired"] and device["reachable"]), None)
        selected = selected or next((device for device in devices if device["paired"]), None)
    paired = bool(selected and selected["paired"])
    reachable = bool(selected and selected["reachable"])
    if process.returncode != 0:
        error = output or "KDE Connect device listing failed."
    elif configured and selected is None:
        error = f"Configured KDE Connect device '{configured}' was not found."
    elif not paired:
        error = output or "No KDE Connect device paired."
    elif not reachable:
        error = "KDE Connect device is paired but unreachable."
    else:
        error = ""
    return {
        "ok": process.returncode == 0 and paired and reachable,
        "paired": paired,
        "reachable": reachable,
        "device_id": str(selected["id"]) if selected else (configured if configured else ""),
        "devices": devices,
        "raw": output,
        "error": error,
    }


def get_device_id() -> str:
    """Resolve a usable device every time; config/re-pair changes are immediate."""
    live = status()
    return str(live.get("device_id", "")) if live.get("ok") else ""


def _device_args() -> list[str] | None:
    device_id = get_device_id()
    return ["--device", device_id] if device_id else None


def _unavailable_payload(kind: str) -> str:
    live = status()
    return _json({"ok": False, kind: [], "error": live.get("error") or "KDE Connect device not paired or not reachable."})


def _notification_records(output: str, limit: int) -> list[dict[str, str]]:
    """Parse common KDE CLI versions without losing indented continuation text."""
    groups: list[list[str]] = []
    current: list[str] = []
    for raw in output.splitlines():
        if not raw.strip():
            if current:
                groups.append(current)
                current = []
            continue
        if raw[:1].isspace() and current:
            current.append(raw.strip())
            continue
        # A new top-level ``app: title - message`` line begins a new record;
        # non-indented lines without an app separator remain part of the
        # preceding record for version-specific multiline formats.
        if current and ":" in raw:
            groups.append(current)
            current = [raw.strip()]
        elif current:
            current.append(raw.strip())
        else:
            current = [raw.strip()]
    if current:
        groups.append(current)
    records: list[dict[str, str]] = []
    for group in groups[:limit]:
        headline = group[0]
        app, separator, remainder = headline.partition(":")
        if not separator:
            app, remainder = "unknown", headline
        title, divider, text = remainder.strip().partition(" - ")
        continuation = "\n".join(group[1:]).strip()
        body = text.strip() if divider else remainder.strip()
        if continuation:
            body = f"{body}\n{continuation}".strip()
        records.append(
            {
                "package": "",
                "app": app.strip() or "unknown",
                "title": title.strip() if divider else "",
                "text": body,
                "timestamp": "",
            }
        )
    return records


def get_recent_notifications(limit: int = 20) -> str:
    bounded = _bounded_limit(limit)
    error = _missing_cli()
    if error:
        return _json({"ok": False, "notifications": [], "limit": bounded, "error": error})
    device = _device_args()
    if not device:
        return _unavailable_payload("notifications")
    process, command_error = _run_result([_cli_path(), *device, "--list-notifications"])
    if process is None:
        return _json({"ok": False, "notifications": [], "limit": bounded, "error": command_error})
    if process.returncode != 0:
        return _json({"ok": False, "notifications": [], "limit": bounded, "error": (process.stderr or process.stdout or "").strip()})
    return _json({"ok": True, "snapshot": True, "limit": bounded, "notifications": _notification_records(process.stdout or "", bounded)})


def _parse_contact_line(line: str) -> dict[str, Any]:
    numbers = re.findall(r"\+?[0-9][0-9 ()-]{5,}[0-9]", line)
    name = re.sub(r"\s*<?\+?[0-9][0-9 ()-]{5,}[0-9]>?", "", line).strip(" -:,")
    return {"name": name or line.strip(), "numbers": [number.strip() for number in numbers]}


def search_contacts(query: str, limit: int = 20) -> str:
    bounded = _bounded_limit(limit)
    query = str(query or "").strip()
    if not query:
        return _json({"ok": False, "contacts": [], "limit": bounded, "error": "Contact query is required."})
    error = _missing_cli()
    if error:
        return _json({"ok": False, "contacts": [], "limit": bounded, "error": error})
    device = _device_args()
    if not device:
        return _unavailable_payload("contacts")
    attempts = [[_cli_path(), *device, "--search-contacts", query], [_cli_path(), *device, "--list-contacts"]]
    last_error = ""
    needle = query.casefold()
    for command in attempts:
        process, command_error = _run_result(command)
        if process is None:
            last_error = command_error
            continue
        last_error = (process.stderr or process.stdout or "").strip()
        if process.returncode != 0:
            continue
        contacts = [
            _parse_contact_line(line)
            for line in (process.stdout or "").splitlines()
            if needle in line.casefold()
        ][:bounded]
        return _json({"ok": True, "contacts": contacts, "limit": bounded})
    return _json({"ok": False, "contacts": [], "limit": bounded, "error": last_error or "Contacts plugin unavailable."})


def send_sms(number: str, message: str) -> str:
    error = _missing_cli()
    if error:
        return _json({"ok": False, "sent": False, "error": error})
    device = _device_args()
    if not device:
        live = status()
        return _json({"ok": False, "sent": False, "error": live.get("error") or "KDE Connect device not paired or not reachable."})
    process, command_error = _run_result([_cli_path(), *device, "--send-sms", message, "--destination", number], timeout=20)
    if process is None:
        return _json({"ok": False, "sent": False, "number": number, "error": command_error})
    return _json({"ok": process.returncode == 0, "sent": process.returncode == 0, "number": number, "error": "" if process.returncode == 0 else (process.stderr or process.stdout or "").strip()})


def get_recent_sms(limit: int = 10) -> str:
    return _json({"ok": False, "messages": [], "limit": _bounded_limit(limit, 10), "error": "KDE Connect CLI does not expose a stable SMS read API on all platforms yet."})
