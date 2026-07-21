"""Compatibility fixes for the third-party ``windows-mcp`` subprocess.

The 0.8.2 Snapshot implementation passes raw Windows UI Automation text into
FastMCP. Some desktop apps can expose a lone UTF-16 surrogate in that text.
Pydantic then refuses to serialize the tool response and the server exits.
This hook replaces only invalid code points in Snapshot text immediately before
the response enters FastMCP; it does not alter normal Unicode or screenshots.
"""

from __future__ import annotations

from typing import Any


def _utf8_safe(value: str) -> str:
    """Replace lone Unicode surrogates while preserving valid text unchanged."""
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_result(value: Any) -> Any:
    """Sanitize textual tool output without mutating binary content objects."""
    if isinstance(value, str):
        return _utf8_safe(value)
    if isinstance(value, list):
        return [_sanitize_result(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_result(item) for item in value)
    if isinstance(value, dict):
        return {key: _sanitize_result(item) for key, item in value.items()}
    return value


def install_windows_mcp_compat() -> None:
    """Install final-result sanitization once, if windows-mcp is present."""
    from windows_mcp.tools import _snapshot_helpers as helpers
    from fastmcp.tools import base as tool_base

    if getattr(helpers, "_ares_snapshot_text_sanitized", False):
        return

    original = helpers.build_snapshot_response

    def safe_build_snapshot_response(*args: Any, **kwargs: Any) -> list[Any]:
        response = original(*args, **kwargs)
        return [_utf8_safe(item) if isinstance(item, str) else item for item in response]

    helpers.build_snapshot_response = safe_build_snapshot_response

    # Snapshot is not the only exposed surface: Type echoes the entered text,
    # and other desktop tools can return UI Automation text too. FastMCP turns
    # every tool's return value into a TextContent block in this one function,
    # so wrapping it protects the complete Windows-MCP result boundary before
    # Pydantic serializes the response to stdio.
    original_convert = tool_base._convert_to_single_content_block

    def safe_convert_to_single_content_block(
        item: Any, serializer: Any = None
    ) -> Any:
        return original_convert(_sanitize_result(item), serializer)

    tool_base._convert_to_single_content_block = safe_convert_to_single_content_block
    helpers._ares_snapshot_text_sanitized = True
