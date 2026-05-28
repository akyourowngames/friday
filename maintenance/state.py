from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from config import settings


class MaintenanceState:
    def __init__(self, state_path: str | Path | None = None, log_path: str | Path | None = None):
        self.state_path = Path(state_path or settings.maintenance_state_path).expanduser().resolve()
        self.log_path = Path(log_path or settings.maintenance_log_path).expanduser().resolve()

    def load(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, payload: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.state_path.parent, prefix=f".{self.state_path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(temp, self.state_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def append_log(self, entry: dict, max_runs: int) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self._trim_log(max_runs)

    def recent_runs(self, limit: int = 10) -> list[dict]:
        if not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[dict] = []
        for line in lines[-max(1, limit):]:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    def _trim_log(self, max_runs: int) -> None:
        if max_runs <= 0:
            return
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= max_runs:
            return
        kept = lines[-max_runs:]
        self.log_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    def already_ran_today(self, today: date | None = None) -> bool:
        today = today or date.today()
        last = self.load().get("last_run_date")
        return str(last or "") == today.isoformat()

    def too_soon(self, min_minutes: int, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        last_iso = self.load().get("last_run_at")
        if not last_iso:
            return False
        try:
            last = datetime.fromisoformat(str(last_iso))
        except ValueError:
            return False
        return now - last < timedelta(minutes=max(0, int(min_minutes)))
