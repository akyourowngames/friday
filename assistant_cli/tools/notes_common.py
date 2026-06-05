from __future__ import annotations

from pathlib import Path

from .core import ToolContext


def notes_path(ctx: ToolContext) -> Path:
    return ctx.workspace_root / "storage" / "tool_notes.jsonl"
