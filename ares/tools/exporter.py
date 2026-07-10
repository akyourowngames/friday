"""JSON export/import helpers for Ares local data."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ares.conversations import ConversationStore

from ares.config import CONFIG_PATH, load_config, save_config
from ares.tools.dates import now_local, now_local_iso
from ares.memory import MemoryStore
from ares.models import AppConfig

EXPORT_PROFILES: dict[str, dict[str, bool]] = {
    "full": {"config": True, "memories": True, "conversations": True},
    "memories": {"config": False, "memories": True, "conversations": False},
    "conversations": {"config": False, "memories": False, "conversations": True},
    "config": {"config": True, "memories": False, "conversations": False},
}


def default_export_path() -> Path:
    """Return a collision-safe export path under ~/.ares."""
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    return Path("~/.ares").expanduser() / f"ares-export-{stamp}-{uuid.uuid4().hex[:8]}.json"


_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|authorization|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)


def _redact_credentials(value: Any, *, path: str = "") -> tuple[Any, dict[str, str]]:
    """Redact nested secrets while retaining every non-secret config field."""
    preview: dict[str, str] = {}
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)):
                preview[child_path] = "redacted" if item not in (None, "", [], {}) else "empty"
                cleaned[key] = None
            else:
                cleaned_item, child_preview = _redact_credentials(item, path=child_path)
                cleaned[key] = cleaned_item
                preview.update(child_preview)
        return cleaned, preview
    if isinstance(value, list):
        cleaned_list = []
        for index, item in enumerate(value):
            cleaned_item, child_preview = _redact_credentials(item, path=f"{path}[{index}]")
            cleaned_list.append(cleaned_item)
            preview.update(child_preview)
        return cleaned_list, preview
    return value, preview


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """Validate serialized JSON before atomically making an export visible."""
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    # Catch serialization / malformed output before a destination is touched.
    json.loads(encoded.decode("utf-8"))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Verify precisely what was written, not only the pre-write bytes.
        json.loads(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def export_data(
    *,
    memory_store: MemoryStore,
    conversation_store: ConversationStore | None = None,
    config: AppConfig | None = None,
    path: str | Path | None = None,
    profile: str = "full",
) -> Path:
    """Export local Ares data to JSON."""
    output_path = Path(path).expanduser() if path else default_export_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    app_config = config or load_config()
    normalized_profile = str(profile or "full").casefold()
    if normalized_profile not in EXPORT_PROFILES:
        normalized_profile = "full"
    flags = EXPORT_PROFILES[normalized_profile]
    config_data, redaction_preview = _redact_credentials(app_config.model_dump()) if flags["config"] else ({}, {})
    payload: dict[str, Any] = {
        "version": 1,
        "exported_at": now_local_iso(),
        "export_profile": normalized_profile,
        "config": config_data,
        "secrets_redacted": sorted(redaction_preview),
        "redaction_preview": redaction_preview,
        "memories": memory_store.list_all() if flags["memories"] else [],
        "conversations": [],
        "conversation_messages": [],
    }
    if conversation_store is not None and flags["conversations"]:
        payload["conversations"] = conversation_store.list_conversations()
        payload["conversation_messages"] = conversation_store.list_messages()

    _atomic_json_write(output_path, payload)
    return output_path


def _redaction_preview(config: AppConfig) -> dict[str, str]:
    """Backward-compatible public helper for redaction diagnostics."""
    _data, preview = _redact_credentials(config.model_dump())
    return preview


def import_data(
    path: str | Path,
    *,
    memory_store: MemoryStore,
    conversation_store: ConversationStore | None = None,
    import_config: bool = False,
) -> dict[str, int]:
    """Import data from an Ares JSON export."""
    input_path = Path(path).expanduser()
    with open(input_path, encoding="utf-8") as f:
        payload = json.load(f)

    counts = {
        "memories": memory_store.import_memories(payload.get("memories", [])),
        "conversations": 0,
        "config": 0,
    }

    if conversation_store is not None:
        counts["conversations"] = conversation_store.import_conversations(
            payload.get("conversations", []),
            payload.get("conversation_messages", []),
        )

    if import_config and payload.get("config"):
        current = load_config()
        config_data = current.model_dump()
        config_data.update(payload["config"])
        config_data["api_key"] = current.api_key
        config_data["tavily_api_key"] = current.tavily_api_key
        save_config(AppConfig(**config_data))
        counts["config"] = 1

    return counts
