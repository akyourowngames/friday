"""Ares local web workspace."""

from ares.workspace.app import create_workspace_app
from ares.workspace.uploads import WorkspaceUploadStore

__all__ = ["WorkspaceUploadStore", "create_workspace_app"]
