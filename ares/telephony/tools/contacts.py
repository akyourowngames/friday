"""Contact tool helpers."""

from __future__ import annotations

from typing import Any


def lookup_contact(manager: Any, name: str) -> dict[str, Any] | None:
    contact = manager.store.find_contact(name)
    return contact.to_dict() if contact else None


def save_contact(manager: Any, name: str, phone_number: str, *, nickname: str = "", notes: str = "") -> dict[str, Any]:
    return manager.add_contact(name, phone_number, nickname=nickname, notes=notes).to_dict()
