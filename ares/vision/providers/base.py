"""Dependency-safe provider primitives shared by vision integrations."""

from __future__ import annotations

import importlib
from typing import Any


class VisionDependencyError(RuntimeError):
    """An optional local vision dependency is not installed or usable."""

    def __init__(self, dependency: str, *, detail: str | None = None) -> None:
        message = (
            f"Vision support requires optional dependency '{dependency}'. "
            "Install it with `pip install 'ares[vision]'`."
        )
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)
        self.dependency = dependency


class VisionProviderError(RuntimeError):
    """A configured local model provider could not process a frame."""


def require_optional_dependency(module_name: str, *, package_name: str | None = None) -> Any:
    """Import an optional package only when a provider is actually invoked."""

    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # If the imported module itself is missing we can give a clear install
        # instruction.  If a nested dependency is missing, surface it too with
        # enough detail to diagnose a broken optional installation.
        missing = exc.name or module_name
        dependency = package_name or module_name
        detail = None if missing == module_name else f"Missing nested module: {missing}."
        raise VisionDependencyError(dependency, detail=detail) from exc


def optional_dependency_available(module_name: str) -> bool:
    """Return availability without importing heavyweight model code eagerly."""

    return importlib.util.find_spec(module_name) is not None


# A few integrations used this longer spelling during planning; retain it as a
# public alias rather than making callers know about provider internals.
VisionDependencyUnavailableError = VisionDependencyError


__all__ = [
    "VisionDependencyError",
    "VisionDependencyUnavailableError",
    "VisionProviderError",
    "optional_dependency_available",
    "require_optional_dependency",
]
