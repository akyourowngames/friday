"""End-call helper."""

from __future__ import annotations

from typing import Any


def end_call(manager: Any, call_id: str) -> dict[str, Any]:
    return manager.hangup(call_id).to_dict()
