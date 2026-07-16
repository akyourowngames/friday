"""Backward-compatibility shim — moved to ares.tools.filesystem_write."""

# ruff: noqa: F401

from ares.tools.filesystem_write import *  # noqa: F401,F403
from ares.tools.filesystem_write import resolve_write_path, atomic_write
