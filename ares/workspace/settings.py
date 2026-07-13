"""Friendly workspace settings projected onto Ares' shared files and config."""

from __future__ import annotations

import re
from typing import Any

from ares.models import AppConfig


def _section(markdown: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n?(.*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _field(markdown: str, label: str) -> str:
    match = re.search(
        rf"^[ \t]*-[ \t]*{re.escape(label)}[ \t]*:[ \t]*(.*?)[ \t]*$",
        markdown,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def _list(section: str) -> list[str]:
    values: list[str] = []
    for line in section.splitlines():
        value = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if value:
            values.append(value)
    return values


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _lines(value: Any) -> list[str]:
    return [line.strip(" -*\t") for line in _text(value).splitlines() if line.strip(" -*\t")]


def workspace_settings(config: AppConfig, profile: str, soul: str) -> dict[str, Any]:
    """Return a form-friendly, secret-safe view of the shared Ares configuration."""
    title = re.search(r"^#\s+(.+?)(?:\s+-\s+My AI Assistant)?\s*$", soul, re.MULTILINE)
    personality = _list(_section(soul, "Personality"))
    communication = _list(_section(soul, "Communication Style"))
    values = _list(_section(soul, "Values"))
    custom = _section(soul, "Custom Instructions")
    telegram = config.telegram
    watcher = config.watcher
    return {
        "identity": {
            "user_name": _field(profile, "Name"),
            "pronouns": _field(profile, "Pronouns"),
            "coding_style": _field(profile, "Coding style"),
            "assistant_style": _field(profile, "Assistant style"),
            "terminal": _field(profile, "Terminal/OS"),
            "projects": "\n".join(_list(_section(profile, "Current Projects"))),
            "goals": "\n".join(_list(_section(profile, "Goals"))),
            "notes": _section(profile, "Notes"),
        },
        "personalization": {
            "assistant_name": title.group(1).strip() if title else "Ares",
            "personality": "\n".join(personality),
            "communication_style": "\n".join(communication),
            "values": "\n".join(values),
            "custom_instructions": custom,
        },
        "model": {
            "name": config.model,
            "api_base_url": config.api_base_url,
            "api_key_configured": bool(config.api_key),
            "max_context_messages": config.max_context_messages,
            "agent_max_iterations": config.agent_max_iterations,
        },
        "telegram": {
            "enabled": telegram.enabled,
            "bot_token_configured": bool(telegram.bot_token),
            "allowed_chat_ids": telegram.allowed_chat_ids,
            "allow_group_chats": telegram.allow_group_chats,
            "show_tool_progress": telegram.show_tool_progress,
            "audio_transcription_enabled": telegram.audio_transcription_enabled,
        },
        "browser": {
            "mode": config.browser_mode,
            "cdp_port": config.browser_cdp_port,
            "chrome_path": config.browser_chrome_path,
            "extension_token_configured": bool(config.browser_extension_token),
        },
        "monitoring": {
            "enabled": watcher.enabled,
            "tool_monitors_enabled": watcher.tool_monitors_enabled,
            "allow_mutating_tool_steps": watcher.allow_mutating_tool_steps,
            "poll_seconds": watcher.poll_seconds,
            "max_concurrency": watcher.max_concurrency,
            "default_interval_seconds": watcher.defaults.interval_seconds,
            "default_ai_action": watcher.defaults.ai_action,
            "dashboard_enabled": watcher.dashboard.enabled,
            "dashboard_host": watcher.dashboard.host,
            "dashboard_port": watcher.dashboard.port,
        },
        "workspace": config.workspace.model_dump(),
        "advanced": {"profile": profile, "soul": soul},
    }


def render_profile(data: dict[str, Any]) -> str:
    projects = _lines(data.get("projects"))
    goals = _lines(data.get("goals"))
    notes = _text(data.get("notes"))
    body = [
        "# About Me",
        "",
        "## Identity",
        f"- Name: {_text(data.get('user_name'))}",
        f"- Pronouns: {_text(data.get('pronouns'))}",
        "",
        "## Preferences",
        f"- Coding style: {_text(data.get('coding_style'))}",
        f"- Assistant style: {_text(data.get('assistant_style'))}",
        f"- Terminal/OS: {_text(data.get('terminal'))}",
        "",
        "## Current Projects",
        *[f"- {value}" for value in projects],
        "",
        "## Goals",
        *[f"- {value}" for value in goals],
        "",
        "## Notes",
        notes,
    ]
    return "\n".join(body).rstrip() + "\n"


def render_soul(data: dict[str, Any]) -> str:
    name = _text(data.get("assistant_name")) or "Ares"
    personality = _lines(data.get("personality"))
    communication = _lines(data.get("communication_style"))
    values = _lines(data.get("values"))
    custom = _text(data.get("custom_instructions"))
    body = [
        f"# {name} - My AI Assistant",
        "",
        "## Personality",
        *[f"- {value}" for value in personality],
        "",
        "## Communication Style",
        *[f"- {value}" for value in communication],
        "",
        "## Values",
        *[f"- {value}" for value in values],
        "",
        "## Custom Instructions",
        custom,
    ]
    return "\n".join(body).rstrip() + "\n"


__all__ = ["render_profile", "render_soul", "workspace_settings"]
