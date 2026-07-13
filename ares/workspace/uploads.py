"""Safe persistent file library used by the local Ares workspace."""

from __future__ import annotations

import base64
import binascii
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ares.attachments import MAX_ATTACHMENT_BYTES, inspect_attachment


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class WorkspaceUploadStore:
    """Persist uploaded files beneath the configured Ares data directory."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir).expanduser().resolve() / "uploads"
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict[str, Any]]:
        values = [self._payload(path) for path in self.root.iterdir() if path.is_file()]
        return sorted(values, key=lambda item: item["modified_at"], reverse=True)

    def save(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("Upload must be an object")
        original = Path(str(raw.get("name") or "attachment")).name or "attachment"
        encoded = raw.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Upload data is required")
        payload = encoded.split(",", 1)[1] if encoded.startswith("data:") and "," in encoded else encoded
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Upload data is not valid base64") from exc
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{original} is larger than the 25 MB workspace limit")
        safe_name = _SAFE_NAME.sub("_", original).strip(" .") or "attachment"
        file_id = f"{uuid4().hex[:12]}--{safe_name}"
        path = (self.root / file_id).resolve()
        self._assert_inside(path)
        path.write_bytes(data)
        # Decode once at upload time so corrupt images/documents cannot enter
        # the reusable library and fail later in an unrelated chat turn.
        try:
            inspection = inspect_attachment({
                "name": original,
                "type": str(raw.get("type") or ""),
                "path": str(path),
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise
        result = self._payload(path)
        result.update({"name": original, "kind": inspection.kind})
        return result

    def delete(self, file_id: str) -> bool:
        path = (self.root / Path(str(file_id)).name).resolve()
        self._assert_inside(path)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def attachment(self, file_id: str) -> dict[str, Any]:
        path = (self.root / Path(str(file_id)).name).resolve()
        self._assert_inside(path)
        if not path.is_file():
            raise ValueError("Workspace file not found")
        payload = self._payload(path)
        return {"name": payload["name"], "type": payload["type"], "path": str(path)}

    def _payload(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        stored_name = path.name
        display_name = stored_name.split("--", 1)[1] if "--" in stored_name else stored_name
        return {
            "id": stored_name,
            "name": display_name,
            "type": mimetypes.guess_type(display_name)[0] or "application/octet-stream",
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "path": str(path),
        }

    def _assert_inside(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Workspace file path escaped the upload directory") from exc


__all__ = ["WorkspaceUploadStore"]
