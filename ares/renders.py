"""Backward-compatibility shim — moved to ares.tools.renders."""

from ares.tools.renders import *  # noqa: F401,F403
from ares.tools.renders import (
    get_renderer,
    render_directory,
    render_file_content,
    render_generic_tool,
    render_search_results,
    render_web_search,
)
