"""Compatibility import for the local Ares Vision router.

The implementation lives beside the Vision service to keep frame-privacy
rules co-located; this module provides the planned API package surface.
"""

from ares.vision.api import create_vision_router

__all__ = ["create_vision_router"]
