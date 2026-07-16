"""Optional detector-provider implementations for Ares Vision.

Importing this package does not import OpenCV, Ultralytics, Supervision, or
PaddleOCR.  Instantiate a provider only when the corresponding local feature
is requested.
"""

from .base import (
    VisionDependencyError,
    VisionDependencyUnavailableError,
    VisionProviderError,
    optional_dependency_available,
    require_optional_dependency,
)

__all__ = [
    "VisionDependencyError",
    "VisionDependencyUnavailableError",
    "VisionProviderError",
    "optional_dependency_available",
    "require_optional_dependency",
]
