from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Webhook:
    id: str
    url: str
    events: list[str] = field(default_factory=list)
    filter: dict[str, Any] = field(default_factory=dict)


class WebhookRegistry:
    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self._hooks: dict[str, Webhook] = {}
        self._lock = threading.Lock()

    def register(self, url: str, events: list[str] | None = None, filter: dict[str, Any] | None = None) -> dict:
        clean_url = str(url or "").strip()
        if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
            raise ValueError("webhook url must start with http:// or https://")
        hook = Webhook(
            id=uuid.uuid4().hex,
            url=clean_url,
            events=[str(item).strip().upper() for item in (events or []) if str(item).strip()],
            filter=filter or {},
        )
        with self._lock:
            self._hooks[hook.id] = hook
        return self._public(hook)

    def list_hooks(self) -> list[dict]:
        with self._lock:
            return [self._public(hook) for hook in self._hooks.values()]

    def dispatch(self, event: dict):
        with self._lock:
            hooks = list(self._hooks.values())
        for hook in hooks:
            if not _matches(hook, event):
                continue
            thread = threading.Thread(target=self._post, args=(hook, event), daemon=True)
            thread.start()

    def _post(self, hook: Webhook, event: dict):
        try:
            httpx.post(hook.url, json=event, timeout=self.timeout)
        except httpx.HTTPError:
            pass

    def _public(self, hook: Webhook) -> dict:
        return {"id": hook.id, "url": hook.url, "events": list(hook.events), "filter": dict(hook.filter)}


def _matches(hook: Webhook, event: dict) -> bool:
    event_type = str(event.get("event_type", "")).upper()
    if hook.events and event_type not in hook.events:
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    file_info = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    filters = hook.filter or {}
    ext_values = _as_list(filters.get("ext") or filters.get("extension"))
    if ext_values:
        extension = str(file_info.get("extension") or "").lower()
        wanted = {_normalize_extension(item) for item in ext_values}
        if extension not in wanted:
            return False
    mime_values = _as_list(filters.get("mime") or filters.get("mime_prefix"))
    if mime_values:
        mime_type = str(file_info.get("mime_type") or "")
        if not any(mime_type.startswith(str(value)) for value in mime_values):
            return False
    return True


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_extension(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return text
    if text.startswith("."):
        return text
    return "." + text
