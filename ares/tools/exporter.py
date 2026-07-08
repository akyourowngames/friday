"""JSON export/import helpers for Ares local data."""

from __future__ import annotations

import json
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
    """Return a timestamped export path under ~/.ares."""
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    return Path("~/.ares").expanduser() / f"ares-export-{stamp}.json"


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
    flags = EXPORT_PROFILES.get((profile or "full").lower(), EXPORT_PROFILES["full"])
    redaction_preview = _redaction_preview(app_config) if flags["config"] else {}
    payload: dict[str, Any] = {
        "version": 1,
        "exported_at": now_local_iso(),
        "export_profile": profile if profile in EXPORT_PROFILES else "full",
        "config": app_config.model_dump(exclude={"api_key", "tavily_api_key"}) if flags["config"] else {},
        "secrets_redacted": list(redaction_preview),
        "redaction_preview": redaction_preview,
        "memories": memory_store.list_all() if flags["memories"] else [],
        "conversations": [],
        "conversation_messages": [],
    }
    if conversation_store is not None and flags["conversations"]:
        payload["conversations"] = conversation_store.list_conversations()
        payload["conversation_messages"] = conversation_store.list_messages()

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return output_path


def _redaction_preview(config: AppConfig) -> dict[str, str]:
    """Return which sensitive config fields were excluded from export."""
    data = config.model_dump()
    preview: dict[str, str] = {}
    for key in ("api_key", "tavily_api_key"):
        value = str(data.get(key) or "")
        preview[key] = "redacted" if value else "empty"
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
