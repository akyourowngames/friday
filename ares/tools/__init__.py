"""Ares tools package — tool definitions and implementations.

Re-exports get_tool_definitions and ToolExecutor for backward compatibility:
    from ares.tools import get_tool_definitions, ToolExecutor
"""

from ares.tools.definitions import get_tool_definitions
from ares.tools.executor import ToolExecutor

__all__ = ["get_tool_definitions", "ToolExecutor"]