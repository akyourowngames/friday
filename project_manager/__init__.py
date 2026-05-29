"""KING Autonomous Project Manager.

A living process that tracks projects from natural-language updates, recomputes
health and momentum, fires drift triggers, and packages briefs. It owns its own
durable store and never touches agent core or routing. All thresholds and
weights come from the markdown control surface (`tools/PROJECT_MANAGER_CONFIG.md`).
"""

from .store import ProjectStore
from .manager import ProjectManager

__all__ = ["ProjectStore", "ProjectManager"]
