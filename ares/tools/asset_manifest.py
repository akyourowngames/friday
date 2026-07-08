"""Asset manifest helpers for generated and edited image files."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_manifest_path() -> Path:
    configured = os.environ.get("ARES_ASSET_MANIFEST", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("~/.ares/images/asset_manifest.jsonl").expanduser()


def asset_metadata(path: str | Path) -> dict[str, Any]:
    """Return file and image metadata for a saved asset."""
    asset_path = Path(path).expanduser()
    row: dict[str, Any] = {
        "path": str(asset_path),
        "exists": asset_path.exists(),
    }
    if not asset_path.exists() or not asset_path.is_file():
        return row
    row.update({
        "bytes": asset_path.stat().st_size,
        "checksum_sha256": _checksum(asset_path),
    })
    try:
        with Image.open(asset_path) as image:
            row.update({
                "width": image.size[0],
                "height": image.size[1],
                "format": image.format,
                "mode": image.mode,
            })
    except Exception as exc:
        row["image_error"] = str(exc)
    return row


def record_asset(
    path: str | Path,
    *,
    action: str,
    history: dict[str, Any] | None = None,
    manifest_path: str | Path | None = None,
) -> Path:
    """Append one JSONL row describing an image generation/edit operation."""
    manifest = Path(manifest_path).expanduser() if manifest_path else default_manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    row = asset_metadata(path)
    row.update({
        "action": action,
        "history": history or {},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return manifest
