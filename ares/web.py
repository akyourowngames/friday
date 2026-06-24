"""Backward-compatibility shim — moved to ares.tools.web."""

from ares.tools.web import *  # noqa: F401,F403
from ares.tools.web import (
    fetch_url_tool,
    payload_to_json,
    web_search_payload,
    format_results,
    summarize_results,
    tavily_search,
    web_search,
)
