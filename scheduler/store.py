from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from config import settings


class SchedulerStore:
    def __init__(self, store_path: str | Path | None = None, log_path: str | Path | None = None):
        self.store_path = Path(store_path or settings.scheduler_store_path).expanduser().resolve()
        self.log_path = Path(log_path or settings.scheduler_log_path).expanduser().resolve()
        self._lock = threading.Lock()

    def load(self) -> dict:
        if not self.store_path.exists():
            return {"items": [], "next_id": 1}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"items": [], "next_id": 1}
        if not isinstance(data, dict):
            return {"items": [], "next_id": 1}
        if not isinstance(data.get("items"), list):
            data["items"] = []
        if not isinstance(data.get("next_id"), int):
            data["next_id"] = 1
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

    def append_log(self, entry: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")

    def all_items(self) -> list[dict]:
        return list(self.load().get("items", []))

    def upsert_item(self, item: dict) -> dict:
        with self._lock:
            data = self.load()
            items: list[dict] = list(data.get("items", []))
            item_id = item.get("id")
            if not item_id:
                item_id = int(data.get("next_id", 1))
                item["id"] = item_id
                data["next_id"] = item_id + 1
                items.append(item)
            else:
                replaced = False
                for index, existing in enumerate(items):
                    if existing.get("id") == item_id:
                        items[index] = item
                        replaced = True
                        break
                if not replaced:
                    items.append(item)
            data["items"] = items
            self.save(data)
            return dict(item)

    def remove_item(self, item_id: int) -> bool:
        with self._lock:
            data = self.load()
            items = list(data.get("items", []))
            new_items = [entry for entry in items if entry.get("id") != item_id]
            if len(new_items) == len(items):
                return False
            data["items"] = new_items
            self.save(data)
            return True

    def get_item(self, item_id: int) -> dict | None:
        for entry in self.all_items():
            if entry.get("id") == item_id:
                return dict(entry)
        return None

    def now_iso(self) -> str:
        return datetime.now().isoformat(timespec="seconds")
