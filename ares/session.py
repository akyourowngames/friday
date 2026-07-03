"""Session identity management."""
from __future__ import annotations

from uuid import uuid4

from ares.tools.dates import now_local_iso


class SessionManager:
    """Generates and tracks a unique session ID."""

    def __init__(self) -> None:
        self.session_id: str = f"sess-{uuid4().hex[:12]}"
        self.started_at: str = now_local_iso()

    def get_id(self) -> str:
        return self.session_id
