"""Shared watcher command operations for terminal and chat surfaces."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any
from uuid import uuid4

from ares.watcher.database import WatcherDatabase
from ares.watcher.models import Monitor


def parse_interval(value: str) -> int:
    text = str(value).strip().lower()
    multipliers = {"s":1,"m":60,"h":3600,"d":86400}
    if text[-1:] in multipliers:
        seconds = int(text[:-1]) * multipliers[text[-1]]
    else:
        seconds = int(text)
    if seconds < 20:
        raise ValueError("Interval must be at least 20 seconds")
    return seconds


class WatcherCommands:
    def __init__(
        self,
        database_path: str | Path | None = None,
        defaults: Any | None = None,
        *,
        db: WatcherDatabase | None = None,
        goal_store: Any | None = None,
    ) -> None:
        self._owns_db = db is None
        self.db = db or WatcherDatabase(database_path or Path("~/.ares/data/watchers.db").expanduser())
        self.defaults = defaults
        self.goal_store = goal_store

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def execute(self, argument: str) -> dict[str, Any]:
        tokens = shlex.split(argument, posix=False)
        action = tokens.pop(0).lower() if tokens else "list"
        if action in {"list", "ls"}:
            return {"action":"list","monitors":[item.public_dict() for item in self.db.list_monitors()]}
        if action == "add":
            monitor, goal_id = self._add(tokens)
            return {"action":"add","monitor":monitor.public_dict(),"linked_goal_id":goal_id}
        if not tokens:
            raise ValueError(f"{action} requires a monitor ID")
        monitor = self._resolve(tokens[0])
        if action in {"status", "show"}:
            return {
                "action":"status","monitor":monitor.public_dict(),
                "events":[item.to_dict() for item in self.db.list_events(monitor.id,limit=5)],
                "linked_goals":[
                    {"goal_id":goal["goal_id"],"title":goal["title"],"status":goal["status"]}
                    for goal in (self.goal_store.linked_goals(link_type="watcher", ref_id=monitor.id) if self.goal_store is not None else [])
                ],
            }
        if action in {"pause", "resume"}:
            monitor.enabled = action == "resume"
            if monitor.enabled: monitor.error_count, monitor.next_check_at = 0, None
            self.db.update_monitor(monitor)
            return {"action":action,"monitor":monitor.public_dict()}
        if action in {"remove", "delete", "rm"}:
            self.db.delete_monitor(monitor.id)
            unlinked = self.goal_store.unlink_reference(link_type="watcher", ref_id=monitor.id) if self.goal_store is not None else []
            return {"action":"remove","monitor":monitor.public_dict(),"unlinked_goal_ids":unlinked}
        if action == "events":
            return {"action":"events","monitor":monitor.public_dict(),"events":[item.to_dict() for item in self.db.list_events(monitor.id,limit=20)]}
        if action in {"test", "check"}:
            return {"action":"test","monitor":monitor.public_dict()}
        raise ValueError("Usage: /monitor [list|add NAME URL|status ID|pause ID|resume ID|remove ID|events ID|test ID]")

    def _resolve(self, value: str) -> Monitor:
        exact = self.db.get_monitor(value)
        if exact: return exact
        matches = [item for item in self.db.list_monitors() if item.id.startswith(value) or item.name.lower() == value.lower()]
        if len(matches) == 1: return matches[0]
        raise ValueError("Monitor not found or identifier is ambiguous")

    def _add(self, tokens: list[str]) -> tuple[Monitor, int | None]:
        positional, options = [], {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.startswith("--"):
                key = token[2:].replace("-", "_")
                if index + 1 >= len(tokens): raise ValueError(f"Missing value for {token}")
                options[key] = tokens[index + 1].strip('"'); index += 2
            else:
                positional.append(token.strip('"')); index += 1
        if len(positional) < 2:
            raise ValueError('Usage: /monitor add "NAME" URL [--interval 15m] [--type website] [--action notify] [--goal ID]')
        goal_id = int(options["goal"]) if options.get("goal") is not None else None
        if goal_id is not None and (self.goal_store is None or self.goal_store.get(goal_id) is None):
            raise ValueError(f"Goal #{goal_id} was not found")
        default_interval = getattr(self.defaults, "interval_seconds", 900)
        default_action = getattr(self.defaults, "ai_action", "notify")
        monitor = Monitor(id=str(uuid4()),name=positional[0],url=positional[1],type=options.get("type","website"),
            interval_seconds=parse_interval(options.get("interval",str(default_interval))),ai_action=options.get("action",default_action),
            config={"change_detection":options.get("detection","diff")})
        self.db.insert_monitor(monitor)
        if goal_id is not None:
            try:
                self.goal_store.link(goal_id, link_type="watcher", ref_id=monitor.id)
            except Exception:
                self.db.delete_monitor(monitor.id)
                raise
        return monitor, goal_id
