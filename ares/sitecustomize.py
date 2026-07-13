"""Startup hook for the isolated ``windows-mcp`` subprocess only.

Python imports ``sitecustomize`` automatically when its directory is supplied
through ``PYTHONPATH``.  Ares sets that path exclusively for the configured
Windows MCP server, and the opt-in flag keeps this module inert everywhere
else.
"""

from __future__ import annotations

import os


if os.getenv("ARES_WINDOWS_MCP_COMPAT") == "1":
    try:
        from windows_mcp_compat import install_windows_mcp_compat

        install_windows_mcp_compat()
    except Exception:
        # A missing or changed third-party package must not prevent the MCP
        # server from starting. The regular Ares timeout/reconnect path still
        # reports a usable failure to the agent.
        pass
