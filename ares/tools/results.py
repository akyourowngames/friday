"""Backward-compatible structured result helpers for upgraded tools."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolResultEnvelope(BaseModel):
    """Stable machine-readable result contract shared by upgraded tools."""

    ok: bool = True
    status: Literal["completed", "partial", "failed", "conflict", "preview", "not_found"] = "completed"
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    next_actions: list[dict[str, Any]] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    undo_id: str | None = None


def wants_structured(args: dict[str, Any] | None) -> bool:
    """Return whether a caller explicitly selected the new response contract."""
    value = str((args or {}).get("response_format") or "legacy").strip().casefold()
    if value not in {"legacy", "structured"}:
        raise ValueError("response_format must be legacy or structured")
    return value == "structured"


def structured_result(
    summary: str,
    *,
    ok: bool = True,
    status: str = "completed",
    data: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    next_actions: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    undo_id: str | None = None,
) -> str:
    """Validate and serialize one structured tool result."""
    payload = ToolResultEnvelope(
        ok=ok,
        status=status,
        summary=str(summary),
        data=data or {},
        artifacts=artifacts or [],
        warnings=warnings or [],
        errors=errors or [],
        next_actions=next_actions or [],
        provenance=provenance or {},
        metrics=metrics or {},
        undo_id=undo_id,
    )
    return json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2)


def error_result(summary: str, *, code: str = "tool_error", status: str = "failed") -> str:
    return structured_result(
        summary,
        ok=False,
        status=status,
        errors=[{"code": code, "message": summary}],
    )
