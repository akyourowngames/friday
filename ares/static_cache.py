"""Small modification-aware caches for static local context files."""

from __future__ import annotations

from pathlib import Path
from typing import TypeAlias


FileSignature: TypeAlias = tuple[str, int | None, int | None]


def file_signature(path: Path | str) -> FileSignature:
    """Return a portable cache key based on path, mtime nanoseconds, and size.

    A missing/unreadable path deliberately has a stable sentinel signature, so
    creating or deleting a file invalidates a previously cached value without a
    process restart.
    """
    resolved = Path(path).expanduser().resolve(strict=False)
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), None, None)
    return (str(resolved), int(stat.st_mtime_ns), int(stat.st_size))


class MtimeFileCache:
    """Cache text and bytes while validating the source signature on access."""

    def __init__(self) -> None:
        self._text: dict[str, tuple[FileSignature, str]] = {}
        self._bytes: dict[str, tuple[FileSignature, bytes | None]] = {}

    def invalidate(self, path: Path | str | None = None) -> None:
        """Forget one path or every cached value after an explicit write."""
        if path is None:
            self._text.clear()
            self._bytes.clear()
            return
        key = file_signature(path)[0]
        self._text.pop(key, None)
        self._bytes.pop(key, None)

    def read_text(self, path: Path | str) -> str:
        """Read UTF-8 text once per current file signature."""
        resolved = Path(path).expanduser().resolve(strict=False)
        signature = file_signature(resolved)
        key = signature[0]
        cached = self._text.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        if signature[1] is None:
            value = ""
        else:
            try:
                value = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                value = ""
        # Store the post-read signature when possible so a concurrent external
        # edit is observed on the next access rather than being hidden by the
        # signature captured immediately before the read.
        self._text[key] = (file_signature(resolved), value)
        return value

    def read_bytes(self, path: Path | str) -> bytes | None:
        """Read bytes once per current file signature, retaining read failures."""
        resolved = Path(path).expanduser().resolve(strict=False)
        signature = file_signature(resolved)
        key = signature[0]
        cached = self._bytes.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        if signature[1] is None:
            value = None
        else:
            try:
                value = resolved.read_bytes()
            except OSError:
                value = None
        self._bytes[key] = (file_signature(resolved), value)
        return value


__all__ = ["FileSignature", "MtimeFileCache", "file_signature"]
