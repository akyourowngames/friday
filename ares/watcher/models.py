"""Typed domain models for the Ares watcher service.

The watcher deliberately uses small dataclasses at its storage boundary.  This
keeps the core usable by the CLI, Telegram channel, dashboard, and tests
without making SQLAlchemy an import-time requirement for every Ares process.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    return {key: (_iso(value) if isinstance(value, datetime) else value) for key, value in data.items()}


_SECRET_PARTS = ("token", "secret", "password", "passwd", "api_key", "apikey", "authorization", "cookie", "signature")


def redact_secrets(value: Any, key: str = "") -> Any:
    """Redact credentials before monitor state leaves the local control plane."""
    if any(part in key.lower() for part in _SECRET_PARTS):
        return "***REDACTED***" if value is not None and value != "" else value
    if isinstance(value, dict):
        return {str(k): redact_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def redact_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parts = urlsplit(url)
        query = [(name, "***REDACTED***" if any(part in name.lower() for part in _SECRET_PARTS) else val) for name, val in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except ValueError:
        return url


@dataclass(slots=True)
class Monitor:
    id: str
    name: str
    type: str
    url: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    interval_seconds: int = 900
    ai_action: str = "notify"
    ai_prompt: str | None = None
    enabled: bool = True
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    last_status: str | None = None
    error_count: int = 0
    total_checks: int = 0
    total_changes: int = 0
    last_duration_ms: int | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        self.type = self.type.strip().lower()
        if not self.name:
            raise ValueError("Monitor name is required")
        if self.type not in {"website", "custom", "instagram", "tool", "browser"}:
            raise ValueError(f"Unsupported monitor type: {self.type}")
        if self.interval_seconds < 20:
            raise ValueError("Monitor interval must be at least 20 seconds")
        if self.ai_action not in {"notify", "suggest", "auto"}:
            raise ValueError("ai_action must be notify, suggest, or auto")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def public_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data["config"] = redact_secrets(data["config"])
        data["url"] = redact_url(data["url"])
        return data


@dataclass(slots=True)
class Snapshot:
    id: str
    monitor_id: str
    content_hash: str | None = None
    content: str | None = None
    price_value: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(slots=True)
class Event:
    id: str
    monitor_id: str
    event_type: str
    old_value: str | None = None
    new_value: str | None = None
    change_summary: str | None = None
    severity: str = "info"
    notified: bool = False
    acknowledged: bool = False
    ai_analyzed: bool = False
    ai_summary: str | None = None
    confidence: float = 1.0
    change_percent: float | None = None
    suppressed: bool = False
    suppression_reason: str | None = None
    feedback: str | None = None
    feedback_note: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(slots=True)
class Notification:
    id: str
    event_id: str
    channel: str
    status: str = "pending"
    attempts: int = 0
    next_retry_at: datetime | None = None
    sent_at: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(slots=True)
class InstagramState:
    id: str
    monitor_id: str
    last_dm_id: str | None = None
    last_mention_id: str | None = None
    last_check_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(slots=True)
class CheckRun:
    id: str
    monitor_id: str
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    changed: bool = False
    http_status: int | None = None
    bytes_received: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))
