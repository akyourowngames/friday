"""Call-transfer helper."""

from __future__ import annotations

from typing import Any


def transfer_call(manager: Any, call_id: str, destination: str) -> dict[str, Any]:
    return manager.transfer(call_id, destination).to_dict()
