"""Durable JSON store for the project manager.

One file holds every project keyed by slug, plus an archive list for completed
or cancelled projects. Writes are atomic (temp file + os.replace) and guarded by
a process lock, matching the pattern used by `scheduler/store.py` and
`maintenance/state.py`. A JSONL audit log records intake and trigger events.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from config import settings


def _resolve(path_value, default) -> Path:
    target = Path(path_value or default).expanduser()
    if not target.is_absolute():
        target = Path(__file__).resolve().parent.parent / target
    return target.resolve()


class ProjectStore:
    def __init__(self, store_path=None, log_path=None):
        self.store_path = _resolve(store_path, settings.project_store_path)
        self.log_path = _resolve(log_path, settings.project_log_path)
        self._lock = threading.Lock()

    # --- raw load/save ---

    def load(self) -> dict:
        if not self.store_path.exists():
            return {"projects": {}, "archive": []}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"projects": {}, "archive": []}
        if not isinstance(data, dict):
            return {"projects": {}, "archive": []}
        if not isinstance(data.get("projects"), dict):
            data["projects"] = {}
        if not isinstance(data.get("archive"), list):
            data["archive"] = []
        return data

    def save(self, payload: dict) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.store_path.parent, prefix=f".{self.store_path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(temp, self.store_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    # --- project access ---

    def all_projects(self) -> list[dict]:
        return list(self.load().get("projects", {}).values())

    def get(self, slug: str) -> dict | None:
        project = self.load().get("projects", {}).get(str(slug))
        return dict(project) if isinstance(project, dict) else None

    def upsert(self, project: dict) -> dict:
        with self._lock:
            data = self.load()
            projects = dict(data.get("projects", {}))
            projects[str(project["id"])] = project
            data["projects"] = projects
            self.save(data)
            return dict(project)

    def remove(self, slug: str) -> bool:
        with self._lock:
            data = self.load()
            projects = dict(data.get("projects", {}))
            if str(slug) not in projects:
                return False
            del projects[str(slug)]
            data["projects"] = projects
            self.save(data)
            return True

    def archive(self, project: dict, keep: int) -> dict:
        with self._lock:
            data = self.load()
            projects = dict(data.get("projects", {}))
            projects.pop(str(project["id"]), None)
            archive = list(data.get("archive", []))
            archive.append(project)
            if keep > 0 and len(archive) > keep:
                archive = archive[-keep:]
            data["projects"] = projects
            data["archive"] = archive
            self.save(data)
            return dict(project)

    def all_archived(self) -> list[dict]:
        return list(self.load().get("archive", []))

    # --- audit log ---

    def append_log(self, entry: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(entry)
        record.setdefault("at", datetime.now().isoformat(timespec="seconds"))
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")

    def now_iso(self) -> str:
        return datetime.now().isoformat(timespec="seconds")
