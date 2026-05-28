from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import SchedulerConfig, load_config
from .store import SchedulerStore

ActionRunner = Callable[[str, dict], Any]


@dataclass
class ScheduledItem:
    id: int
    title: str
    action: str
    arguments: dict
    scheduled_for: str
    status: str = "pending"
    created_at: str = ""
    updated_at: str = ""
    last_result: dict | None = None
    retry_count: int = 0
    completed_at: str | None = None
    related_note_title: str | None = None
    related_memory_id: str | None = None
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledItem":
        return cls(
            id=int(data.get("id") or 0),
            title=str(data.get("title") or ""),
            action=str(data.get("action") or ""),
            arguments=dict(data.get("arguments") or {}),
            scheduled_for=str(data.get("scheduled_for") or ""),
            status=str(data.get("status") or "pending"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_result=data.get("last_result") if isinstance(data.get("last_result"), dict) else None,
            retry_count=int(data.get("retry_count") or 0),
            completed_at=data.get("completed_at"),
            related_note_title=data.get("related_note_title"),
            related_memory_id=data.get("related_memory_id"),
            tags=list(data.get("tags") or []),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "action": self.action,
            "arguments": dict(self.arguments),
            "scheduled_for": self.scheduled_for,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_result": self.last_result,
            "retry_count": self.retry_count,
            "completed_at": self.completed_at,
            "related_note_title": self.related_note_title,
            "related_memory_id": self.related_memory_id,
            "tags": list(self.tags),
        }


class Scheduler:
    def __init__(
        self,
        config: SchedulerConfig,
        store: SchedulerStore | None = None,
        action_runner: ActionRunner | None = None,
        note_writer: Callable[[str, str, str], dict] | None = None,
        memory_writer: Callable[[str, float], dict] | None = None,
        clock: Callable[[], datetime] | None = None,
        allowed_actions: Iterable[str] | None = None,
    ):
        self.config = config
        self.store = store or SchedulerStore()
        self._action_runner = action_runner or _registry_action_runner
        self._note_writer = note_writer or _default_note_writer
        self._memory_writer = memory_writer or _default_memory_writer
        self._clock = clock or datetime.now
        runtime_whitelist = set(allowed_actions) if allowed_actions is not None else set(self.config.action_whitelist)
        self._allowed_actions = runtime_whitelist

    @property
    def allowed_actions(self) -> set[str]:
        return set(self._allowed_actions)

    def list_items(self, status: str | None = None) -> list[dict]:
        items = self.store.all_items()
        if status is None:
            return items
        wanted = str(status).strip().lower()
        return [item for item in items if str(item.get("status", "")).lower() == wanted]

    def schedule(
        self,
        title: str,
        action: str,
        scheduled_for: str,
        arguments: dict | None = None,
        tags: Iterable[str] | None = None,
        link_to_note: bool | None = None,
        link_to_memory: bool | None = None,
    ) -> dict:
        clean_title = str(title or "").strip()
        clean_action = str(action or "").strip()
        if not clean_title:
            raise ValueError("title must not be empty")
        if not clean_action:
            raise ValueError("action must not be empty")
        scheduled_iso = _normalize_iso(scheduled_for)
        if scheduled_iso is None:
            raise ValueError("scheduled_for must be ISO 8601 datetime")
        if clean_action not in self._allowed_actions:
            raise ValueError(f"action '{clean_action}' is not on the scheduler whitelist")

        now_iso = self.store.now_iso()
        item = ScheduledItem(
            id=0,
            title=clean_title,
            action=clean_action,
            arguments=dict(arguments or {}),
            scheduled_for=scheduled_iso,
            status="pending",
            created_at=now_iso,
            updated_at=now_iso,
            tags=[str(tag).strip() for tag in (tags or []) if str(tag).strip()],
        )
        record = self.store.upsert_item(item.to_dict())

        memory_linkage = self.config.memory_linkage
        if (link_to_memory if link_to_memory is not None else memory_linkage.get("remember_on_create")):
            try:
                memory_payload = self._memory_writer(
                    f"Scheduled '{clean_title}' for {scheduled_iso} (action {clean_action}).",
                    float(memory_linkage.get("importance_default", 0.6)),
                )
                memory_id = (memory_payload or {}).get("id") if isinstance(memory_payload, dict) else None
                if memory_id:
                    record["related_memory_id"] = str(memory_id)
                    self.store.upsert_item(record)
            except Exception:
                pass

        notes_linkage = self.config.notes_linkage
        if (link_to_note if link_to_note is not None else notes_linkage.get("note_on_complete")):
            prefix = str(notes_linkage.get("note_title_prefix") or "Scheduled: ")
            tag_list = [tag.strip() for tag in str(notes_linkage.get("note_tags") or "").split(",") if tag.strip()]
            note_title = f"{prefix.rstrip()} {clean_title} #{record['id']}".strip()
            try:
                self._note_writer(
                    note_title,
                    f"Scheduled at {scheduled_iso}\nAction: {clean_action}\nArgs: {item.arguments}\n",
                    ",".join(tag_list),
                )
                record["related_note_title"] = note_title
                self.store.upsert_item(record)
            except Exception:
                pass

        self.store.append_log({"event": "scheduled", "id": record["id"], "title": clean_title, "scheduled_for": scheduled_iso, "at": now_iso})
        return record

    def cancel(self, item_id: int) -> bool:
        item = self.store.get_item(item_id)
        if item is None:
            return False
        item["status"] = "cancelled"
        item["updated_at"] = self.store.now_iso()
        self.store.upsert_item(item)
        self.store.append_log({"event": "cancelled", "id": item_id, "at": item["updated_at"]})
        return True

    def delete(self, item_id: int) -> bool:
        deleted = self.store.remove_item(item_id)
        if deleted:
            self.store.append_log({"event": "deleted", "id": item_id, "at": self.store.now_iso()})
        return deleted

    def run_due(self, horizon_minutes: int | None = None, now: datetime | None = None) -> dict:
        now = now or self._clock()
        horizon_minutes = int(horizon_minutes or 0)
        if horizon_minutes <= 0:
            cutoff = now
        else:
            cutoff = now + timedelta(minutes=horizon_minutes)

        ran: list[dict] = []
        skipped: list[dict] = []
        max_items = max(1, int(self.config.max_items_per_run))
        for raw_item in self.store.all_items():
            if len(ran) >= max_items:
                break
            if str(raw_item.get("status")) != "pending":
                continue
            scheduled_at = _parse_iso(raw_item.get("scheduled_for"))
            if scheduled_at is None or scheduled_at > cutoff:
                continue
            action = str(raw_item.get("action") or "")
            if action not in self._allowed_actions:
                skipped.append({"id": raw_item.get("id"), "reason": "action_not_whitelisted", "action": action})
                self._mark_item(raw_item, "skipped", {"reason": "action_not_whitelisted"})
                continue
            outcome = self._execute_item(raw_item, now)
            ran.append(outcome)
        return {
            "ran": ran,
            "skipped": skipped,
            "ran_count": len(ran),
            "skipped_count": len(skipped),
            "checked_at": now.isoformat(timespec="seconds"),
            "horizon_minutes": horizon_minutes,
        }

    def _execute_item(self, raw_item: dict, now: datetime) -> dict:
        action = str(raw_item.get("action") or "")
        arguments = dict(raw_item.get("arguments") or {})
        try:
            output = self._action_runner(action, arguments)
            success = True
            error = None
        except Exception as exc:  # noqa: BLE001
            output = None
            success = False
            error = f"{type(exc).__name__}: {exc}"

        retry_policy = self.config.reschedule_policy
        max_retries = int(retry_policy.get("failed_item_max_retries") or 0)
        retry_minutes = int(retry_policy.get("failed_item_retry_minutes") or 0)

        if success:
            evidence = {"action": action, "output": _summarize(output)}
            self._mark_item(raw_item, "completed", evidence, now)
            self._post_complete_hooks(raw_item, evidence)
            return {"id": raw_item.get("id"), "status": "completed", "action": action}

        retry_count = int(raw_item.get("retry_count") or 0) + 1
        evidence = {"action": action, "error": error, "retry_count": retry_count}
        if retry_minutes > 0 and retry_count <= max_retries:
            new_time = now + timedelta(minutes=retry_minutes)
            raw_item["scheduled_for"] = new_time.isoformat(timespec="seconds")
            raw_item["retry_count"] = retry_count
            self._mark_item(raw_item, "pending", evidence, now, append_log_event="retry_scheduled")
            return {"id": raw_item.get("id"), "status": "retry_scheduled", "action": action, "error": error}

        raw_item["retry_count"] = retry_count
        self._mark_item(raw_item, "failed", evidence, now)
        return {"id": raw_item.get("id"), "status": "failed", "action": action, "error": error}

    def _mark_item(self, raw_item: dict, status: str, evidence: dict, now: datetime | None = None, append_log_event: str | None = None) -> None:
        now = now or self._clock()
        raw_item["status"] = status
        raw_item["updated_at"] = now.isoformat(timespec="seconds")
        raw_item["last_result"] = evidence
        if status == "completed":
            raw_item["completed_at"] = now.isoformat(timespec="seconds")
        self.store.upsert_item(raw_item)
        self.store.append_log(
            {
                "event": append_log_event or status,
                "id": raw_item.get("id"),
                "action": raw_item.get("action"),
                "evidence": evidence,
                "at": raw_item["updated_at"],
            }
        )

    def _post_complete_hooks(self, raw_item: dict, evidence: dict) -> None:
        memory_linkage = self.config.memory_linkage
        if memory_linkage.get("remember_on_complete"):
            try:
                self._memory_writer(
                    f"Completed scheduled '{raw_item.get('title','')}' (action {raw_item.get('action','')}).",
                    float(memory_linkage.get("importance_default", 0.6)),
                )
            except Exception:
                pass

        notes_linkage = self.config.notes_linkage
        title = raw_item.get("related_note_title")
        if notes_linkage.get("note_on_complete") and title:
            try:
                self._note_writer(
                    str(title),
                    f"Completed at {raw_item.get('updated_at','')}\nResult: {evidence}\n",
                    ",".join(str(tag) for tag in (raw_item.get("tags") or []) if str(tag).strip()),
                )
            except Exception:
                pass


def build_scheduler(
    repo_root: str | Path = ".",
    config_path: str | Path | None = None,
    allowed_actions: Iterable[str] | None = None,
) -> Scheduler:
    config = load_config(repo_root, config_path)
    return Scheduler(config, allowed_actions=allowed_actions)


def _registry_action_runner(action: str, arguments: dict) -> Any:
    from tools.registry import execute_tool

    return execute_tool(action, **dict(arguments or {}))


def _default_note_writer(title: str, content: str, tags: str) -> dict:
    from tools.notes import note_save

    return note_save(title=title, content=content, tags=tags, response_format="structured")


def _default_memory_writer(text: str, importance: float) -> dict:
    from memory.brain import Brain

    brain = Brain()
    return brain.remember(text, importance=float(importance))


def _normalize_iso(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = _parse_iso(text)
    if parsed is None:
        return None
    return parsed.isoformat(timespec="seconds")


def _parse_iso(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _summarize(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > 400:
        return text[:400] + "..."
    return text
