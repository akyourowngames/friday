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


def install_windows_mcp_compat() -> None:
    """Install the Snapshot response sanitizer once, if windows-mcp is present."""
    from windows_mcp.tools import _snapshot_helpers as helpers

    if getattr(helpers, "_ares_snapshot_text_sanitized", False):
        return

    original = helpers.build_snapshot_response

    def safe_build_snapshot_response(*args: Any, **kwargs: Any) -> list[Any]:
        response = original(*args, **kwargs)
        return [_utf8_safe(item) if isinstance(item, str) else item for item in response]

    helpers.build_snapshot_response = safe_build_snapshot_response
    helpers._ares_snapshot_text_sanitized = True
