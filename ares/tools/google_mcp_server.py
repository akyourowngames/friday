"""Standalone MCP server for Google Workspace (stdio transport).

Launched by Ares as a subprocess. Uses existing OAuth tokens from
~/.ares/data/mcp_tokens/ — no Developer Preview required.

Run directly:
    python -m ares.tools.google_mcp_server
"""

from mcp.server.fastmcp import FastMCP

from ares.tools.google_mcp_bridge import (
    calendar_create_event,
    calendar_get_event,
    calendar_list,
    calendar_upcoming,
    gmail_get_message,
    gmail_get_unread_count,
    gmail_list_labels,
    gmail_reply,
    gmail_search,
    gmail_send,
)

mcp = FastMCP("google-workspace")

mcp.tool(name="gmail_search")(gmail_search)
mcp.tool(name="gmail_get_message")(gmail_get_message)
mcp.tool(name="gmail_get_unread_count")(gmail_get_unread_count)
mcp.tool(name="gmail_list_labels")(gmail_list_labels)
mcp.tool(name="gmail_send")(gmail_send)
mcp.tool(name="gmail_reply")(gmail_reply)

mcp.tool(name="calendar_list")(calendar_list)
mcp.tool(name="calendar_upcoming")(calendar_upcoming)
mcp.tool(name="calendar_create_event")(calendar_create_event)
mcp.tool(name="calendar_get_event")(calendar_get_event)

if __name__ == "__main__":
    mcp.run(transport="stdio")
