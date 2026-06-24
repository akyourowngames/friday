"""Backward-compatibility shim — moved to ares.tools.filesystem."""

from ares.tools.filesystem import *  # noqa: F401,F403
from ares.tools.filesystem import (
    _allowed_roots,
    _display_path,
    _format_size,
    _normalize_path,
    SKIP_DIRS,
    resolve_path,
)
