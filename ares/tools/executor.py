"""ToolExecutor — dispatches tool calls to local implementations."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ares.conversations import ConversationStore
    from ares.watcher.service import WatcherService

from ares.tools.exporter import export_data
from ares.tools.filesystem import (
    list_directory, read_file, search_files, search_files_async, get_file_info as _get_file_info_impl,
    glob_pattern as _glob_pattern_impl, disk_usage as _disk_usage_impl,
    checksum as _checksum_impl, copy_file as _copy_file_impl,
    find_duplicates as _find_duplicates_impl, tail_file as _tail_file_impl,
    head_file as _head_file_impl, count_lines as _count_lines_impl,
    file_tree as _file_tree_impl,
)
from ares.memory import MemoryStore
from ares.config import save_config
from ares.models import AppConfig, DEFAULT_MCP_SERVERS
from ares.people import PeopleStore, PersonConflictError, PersonResolutionError, mask_email, mask_phone
from ares.actions import ActionLedger
from ares.sessions import SessionStore
from ares.tasks import TaskStore, TaskToolHandlers
from ares.goals import GoalStore, GoalToolHandlers
from ares.skills import SkillManager
from ares.mcp_registry import MCPRegistryClient
from ares.skill_registry import SafeSkillInstaller, SkillRegistryClient, SkillValidationError
from ares.tools.research import ResearchWorkspace, json_result
from ares.tools.web import fetch_url, fetch_url_tool, payload_to_json, web_search_payload
from ares.tools.filesystem_write import write_file as _write_file_impl
from ares.tools.filesystem_write import edit_file as _edit_file_impl
from ares.tools.filesystem_write import create_directory as _create_directory_impl
from ares.tools.filesystem_write import delete_file as _delete_file_impl
from ares.tools.filesystem_write import move_file as _move_file_impl
from ares.tools.filesystem_write import batch_edit as _batch_edit_impl
from ares.tools.filesystem_write import glob_apply as _glob_apply_impl
from ares.tools.filesystem_write import show_file_with_line_numbers as _show_file_with_line_numbers_impl
from ares.tools.filesystem_write import insert_line as _insert_line_impl
from ares.tools.filesystem_write import replace_lines as _replace_lines_impl
from ares.tools.filesystem_write import delete_lines as _delete_lines_impl
from ares.tools.filesystem_write import preview_diff as _preview_diff_impl
from ares.tools.filesystem_write import backup_file as _backup_file_impl
from ares.tools.filesystem_write import undo_last_edit as _undo_last_edit_impl
from ares.tools.filesystem_write import batch_file_ops as _batch_file_ops_impl
from ares.tools.filesystem_write import find_text as _find_text_impl
from ares.tools.filesystem_write import append_to_file as _append_to_file_impl
from ares.tools.filesystem_write import prepend_to_file as _prepend_to_file_impl
from ares.tools.filesystem_write import compare_files as _compare_files_impl
from ares.tools.filesystem_write import create_file_from_template as _create_file_from_template_impl
from ares.tools.filesystem_write import safe_path_status as _safe_path_status_impl
from ares.tools.repl import PersistentREPL
from ares.tools.image_generate import generate_image
from ares.tools.image_edit import image_info as _image_info
from ares.tools.image_edit import resize_image as _resize_image
from ares.tools.image_edit import convert_image as _convert_image
from ares.tools.image_edit import crop_image as _crop_image
from ares.memory_policy import memory_rejection_reason
from ares.cron.store import CronStore
from ares.cron.tools import CronToolHandlers
from ares.tools.datetime_tool import get_current_datetime_result as _get_current_datetime_impl
from ares.tools import adb_bridge as _adb_bridge
from ares.tools import kdeconnect_bridge as _kdeconnect_bridge
from ares.tools.shell_execution import resolve_project_command
from ares.telephony import TelephonyManager, TelephonyStore
from ares.telephony.models import CallStatus
from ares.watcher.database import resolve_watcher_database_path
from ares.watcher.tools import WatcherToolHandlers

_SESSION_UNSET = object()

# Search providers and HTTP fallbacks are synchronous libraries.  Never run
# them directly on the desktop server's event loop: one stalled provider would
# otherwise freeze every workspace session, even though Telegram/CLI continue
# to use their own execution paths.
WEB_SEARCH_BLOCKING_TIMEOUT_SECONDS = 45.0
WEB_FETCH_TIMEOUT_SECONDS = 30.0


class ToolExecutor:
    """Executes tool calls locally."""

    def __init__(
        self,
        memory_store: MemoryStore,
        conversation_store: ConversationStore | None = None,
        config: AppConfig | None = None,
        mcp_manager: Any | None = None,
        people_store: PeopleStore | None = None,
        action_ledger: ActionLedger | None = None,
        task_store: TaskStore | None = None,
        goal_store: GoalStore | None = None,
        session_store: SessionStore | None = None,
        telephony_manager: TelephonyManager | None = None,
    ):
        self.memory = memory_store
        self.conversations = conversation_store
        self.config = config
        self.mcp_manager = mcp_manager
        self._default_session_id: str | None = None
        self._session_context: ContextVar[str | None | object] = ContextVar(
            f"ares_tool_session_{id(self)}", default=_SESSION_UNSET
        )
        self.repl = PersistentREPL()
        data_root = None
        if config is not None:
            data_root = Path(config.data_dir).expanduser().parent
        session_data_dir = Path(config.data_dir).expanduser() if config is not None else None
        db_path = getattr(memory_store, "db_path", None)
        if db_path is not None:
            db_path = Path(db_path)
            data_root = data_root or db_path.parent
            session_data_dir = session_data_dir or db_path.parent
        elif config is not None:
            db_path = Path(config.data_dir).expanduser() / "ares.db"
        shared_connection = getattr(memory_store, "conn", None)
        self._owns_people_store = people_store is None and db_path is not None
        self._owns_action_ledger = action_ledger is None and db_path is not None
        self.people_store = people_store or (
            PeopleStore(db_path=db_path, connection=shared_connection) if db_path is not None else None
        )
        self.action_ledger = action_ledger or (
            ActionLedger(db_path=db_path, connection=shared_connection) if db_path is not None else None
        )
        self.task_store = task_store or (TaskStore(data_root) if data_root is not None else None)
        self.task_tools = TaskToolHandlers(self.task_store, lambda: self.session_id) if self.task_store is not None else None
        self._owns_goal_store = goal_store is None and db_path is not None
        self.goal_store = goal_store or (
            GoalStore(db_path=db_path, connection=shared_connection, task_store=self.task_store)
            if db_path is not None else None
        )
        self.goal_tools = GoalToolHandlers(self.goal_store, self.task_store, self.action_ledger) if self.goal_store is not None else None
        self.session_store = session_store or (SessionStore(session_data_dir) if session_data_dir is not None else None)
        self._owns_telephony_manager = telephony_manager is None and db_path is not None
        self.telephony = telephony_manager or (
            TelephonyManager(
                config or AppConfig(data_dir=str(session_data_dir or db_path.parent)),
                store=TelephonyStore(
                    db_path,
                    connection=shared_connection,
                    data_dir=session_data_dir or db_path.parent,
                ),
                memory_store=memory_store,
                conversation_store=conversation_store,
            ) if db_path is not None else None
        )
        self.workflow_runner: Any | None = None
        self.cron = CronToolHandlers(CronStore(data_root))
        watcher_config = getattr(config, "watcher", None)
        watcher_path = (
            resolve_watcher_database_path(config)
            if watcher_config is not None and config is not None
            else Path(db_path).with_name("watchers.db") if db_path is not None
            else Path("~/.ares/data/watchers.db").expanduser()
        )
        self.watcher_tools = WatcherToolHandlers(
            watcher_path,
            tool_monitors_enabled=bool(getattr(watcher_config, "tool_monitors_enabled", True)),
            allow_mutating_tool_steps=bool(getattr(watcher_config, "allow_mutating_tool_steps", False)),
            capabilities_provider=self._watcher_capability_names,
            goal_store=self.goal_store,
        )
        if self.goal_tools is not None:
            self.goal_tools.watcher_db_provider = lambda: self.watcher_tools.db
        self.research = ResearchWorkspace(session_data_dir or data_root or Path("~/.ares/data").expanduser())
        # Set by the local Telegram channel at runtime. Keeping the bridge
        # unattached by default prevents a web/CLI process from sending files.
        self.telegram_channel: Any | None = None

    def close(self) -> None:
        """Clean up persistent sessions."""
        self.repl.close()
        self.watcher_tools.close()
        if self._owns_people_store and self.people_store is not None:
            self.people_store.close()
        if self._owns_action_ledger and self.action_ledger is not None:
            self.action_ledger.close()
        if self._owns_goal_store and self.goal_store is not None:
            self.goal_store.close()
        if self._owns_telephony_manager and self.telephony is not None:
            self.telephony.close()

    def set_session_id(self, session_id: str | None) -> None:
        """Attach local provenance records to the current agent session."""
        self._default_session_id = session_id

    @property
    def session_id(self) -> str | None:
        scoped = self._session_context.get()
        return self._default_session_id if scoped is _SESSION_UNSET else scoped  # type: ignore[return-value]

    @contextmanager
    def session_scope(self, session_id: str | None) -> Iterator[None]:
        """Bind provenance to one concurrent agent run without mutating siblings."""
        token = self._session_context.set(session_id)
        try:
            yield
        finally:
            # Async-generator cancellation can finalize in a copied context.
            # The scoped value is already unreachable there; never turn a
            # disconnected client into an unhandled ContextVar exception.
            with suppress(ValueError):
                self._session_context.reset(token)

    def set_watcher_service(self, service: WatcherService | None) -> None:
        """Attach the watcher service owned by the current Ares runtime."""
        self.watcher_tools.set_service(service)

    def set_telegram_channel(self, channel: Any | None) -> None:
        """Attach the configured, allowlisted local Telegram channel."""
        self.telegram_channel = channel

    def _watcher_capability_names(self) -> list[str]:
        definitions = getattr(self.mcp_manager, "tool_definitions", []) or []
        return [
            str(item.get("function", {}).get("name") or "")
            for item in definitions
            if item.get("function", {}).get("name")
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool by name with the given arguments. Returns a result string."""
        handlers = {
            "store_memory": self._store_memory,
            "search_memory": self._search_memory,
            "update_memory": self._update_memory,
            "delete_memory": self._delete_memory,
            "remember_person": self._remember_person,
            "search_person": self._search_person,
            "update_person": self._update_person,
            "forget_person": self._forget_person,
            "search_actions": self._search_actions,
            "list_skills": self._list_skills_tool,
            "load_skill": self._load_skill_tool,
            "create_skill": self._create_skill_tool,
            "search_skill_marketplace": self._marketplace_async_required,
            "install_marketplace_skill": self._marketplace_async_required,
            "search_mcp_marketplace": self._marketplace_async_required,
            "add_marketplace_mcp": self._marketplace_async_required,
            "export_data": self._export_data,
            "web_search": self._web_search,
            "fetch_url": self._fetch_url,
            "download_online_file": self._download_online_file,
            "extract_document": self._extract_document,
            "create_research_report": self._create_research_report,
            "telegram_send_file": self._telegram_async_required,
            "read_file": self._read_file,
            "search_files": self._search_files,
            "list_directory": self._list_directory,
            "get_file_info": self._get_file_info,
            "glob_pattern": self._glob_pattern,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "create_directory": self._create_directory,
            "delete_file": self._delete_file,
            "move_file": self._move_file,
            "batch_edit": self._batch_edit,
            "glob_apply": self._glob_apply,
            "show_file_with_line_numbers": self._show_file_with_line_numbers,
            "insert_line": self._insert_line,
            "replace_lines": self._replace_lines,
            "delete_lines": self._delete_lines,
            "preview_diff": self._preview_diff,
            "backup_file": self._backup_file,
            "undo_last_edit": self._undo_last_edit,
            "batch_file_ops": self._batch_file_ops,
            "find_text": self._find_text,
            "append_to_file": self._append_to_file,
            "prepend_to_file": self._prepend_to_file,
            "compare_files": self._compare_files,
            "create_file_from_template": self._create_file_from_template,
            "safe_path_status": self._safe_path_status,
            "disk_usage": self._disk_usage,
            "checksum": self._checksum,
            "copy_file": self._copy_file,
            "find_duplicates": self._find_duplicates,
            "tail_file": self._tail_file,
            "head_file": self._head_file,
            "count_lines": self._count_lines,
            "file_tree": self._file_tree,
            "run_code": self._run_code,
            "run_command": self._run_command,
            "generate_image": self._generate_image,
            "image_info": self._image_info,
            "resize_image": self._resize_image,
            "convert_image": self._convert_image,
            "crop_image": self._crop_image,
            "terminal_exec": self._terminal_exec,
            "create_cron_job": self.cron.create_cron_job,
            "list_cron_jobs": self.cron.list_cron_jobs,
            "get_cron_job": self.cron.get_cron_job,
            "update_cron_job": self.cron.update_cron_job,
            "delete_cron_job": self.cron.delete_cron_job,
            "run_cron_job_now": self.cron.run_cron_job_now,
            "get_cron_logs": self.cron.get_cron_logs,
            "get_watcher_capabilities": self.watcher_tools.capabilities,
            "create_watcher": self.watcher_tools.create,
            "list_watchers": self.watcher_tools.list,
            "get_watcher": self.watcher_tools.get,
            "update_watcher": self.watcher_tools.update,
            "pause_watcher": self.watcher_tools.pause,
            "resume_watcher": self.watcher_tools.resume,
            "list_watcher_events": self.watcher_tools.events,
            "acknowledge_watcher_event": self.watcher_tools.acknowledge,
            "get_watcher_overview": self.watcher_tools.overview,
            "delete_watcher": self.watcher_tools.delete,
            "run_watcher_now": self._watcher_async_required,
            "create_task": self._create_task,
            "list_tasks": self._list_tasks,
            "get_task_status": self._get_task_status,
            "update_task": self._update_task,
            "cancel_task": self._cancel_task,
            "run_task": self._run_task_unavailable,
            "create_goal": self._create_goal,
            "update_goal": self._update_goal,
            "list_goals": self._list_goals,
            "get_goal_status": self._get_goal_status,
            "complete_goal": self._complete_goal,
            "pause_goal": self._pause_goal,
            "abandon_goal": self._abandon_goal,
            "decompose_goal": self._decompose_goal,
            "link_goal_task": self._link_goal_task,
            "link_goal_action": self._link_goal_action,
            "link_goal_watcher": self._link_goal_watcher,
            "unlink_goal_watcher": self._unlink_goal_watcher,
            "get_goal_signals": self._get_goal_signals,
            "acknowledge_goal_signal": self._acknowledge_goal_signal,
            "snooze_goal_signal": self._snooze_goal_signal,
            "sync_goal_progress": self._sync_goal_progress,
            "record_goal_progress": self._record_goal_progress,
            "delete_goal": self._delete_goal,
            "phone_status": self._phone_status,
            "phone_get_notifications": self._phone_get_notifications,
            "phone_search_contact": self._phone_search_contact,
            "phone_send_sms": self._phone_send_sms,
            "phone_call_number": self._phone_call_number,
            "phone_launch_app": self._phone_launch_app,
            "phone_open_url": self._phone_open_url,
            "telephony_status": self._telephony_status,
            "telephony_call": self._telephony_call,
            "telephony_answer": self._telephony_answer,
            "telephony_hangup": self._telephony_hangup,
            "telephony_mute": self._telephony_mute,
            "telephony_get_call": self._telephony_get_call,
            "telephony_list_calls": self._telephony_list_calls,
            "telephony_list_contacts": self._telephony_list_contacts,
            "telephony_save_contact": self._telephony_save_contact,
            "telephony_transfer": self._telephony_transfer,
            "update_config": self._update_config,
            "get_current_datetime": self._get_current_datetime,
        }
        try:
            handler = handlers[tool_name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {tool_name}") from exc
        prestate = self._action_prestate(tool_name, arguments)
        result = handler(arguments)
        self._record_consequential_action(tool_name, arguments, result, prestate=prestate)
        return result

    async def execute_async(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool, allowing local tools to use async integrations."""
        if tool_name == "run_watcher_now":
            return await self.watcher_tools.run_now(arguments)
        if tool_name == "search_skill_marketplace":
            return await self._search_skill_marketplace(arguments)
        if tool_name == "install_marketplace_skill":
            return await self._install_marketplace_skill(arguments)
        if tool_name == "search_mcp_marketplace":
            return await self._search_mcp_marketplace(arguments)
        if tool_name == "add_marketplace_mcp":
            return await self._add_marketplace_mcp(arguments)
        if tool_name == "web_search":
            return await self._web_search_async(arguments)
        if tool_name == "telegram_send_file":
            return await self._telegram_send_file(arguments)
        if tool_name == "search_files":
            return await search_files_async(
                query=arguments.get("query", ""),
                path=arguments.get("path", "."),
                name_pattern=arguments.get("name_pattern", ""),
                max_results=int(arguments.get("max_results", 20)),
            )
        return self.execute(tool_name, arguments)

    # ── Memory tools ──────────────────────────────────────────────

    def _store_memory(self, args: dict) -> str:
        content = args["content"]
        category = args.get("category", "note")
        confidence = float(args.get("confidence", 1.0))
        rejection = memory_rejection_reason(
            content,
            category=category,
            confidence=confidence,
        )
        if rejection:
            return f"Memory not stored: {rejection}."
        suggestions = self.memory.suggest_merge(content, category=category)
        duplicate = next((item for item in suggestions if item["kind"] == "duplicate"), None)
        if duplicate:
            return (
                f"Memory not stored: duplicate of #{duplicate['fact_id']}. "
                f"{duplicate['recommendation']}"
            )
        fact_id = self.memory.store(
            content,
            category=category,
            confidence=confidence,
            importance=float(args.get("importance", 0.5)),
        )
        return f"Stored memory #{fact_id}: {content}"

    def _search_memory(self, args: dict) -> str:
        """Search every local memory surface, including persisted JSONL sessions.

        The session archive is deliberately read directly rather than through an
        index.  That means old sessions produced by earlier Ares versions are
        immediately recallable and cannot disappear because an index was never
        populated or is stale.
        """
        query = str(args.get("query", "")).strip()
        limit = max(1, min(int(args.get("limit", 12)), 50))
        since = args.get("since")
        requested_sources = args.get("sources") or ["facts", "people", "conversations", "sessions", "actions"]
        if isinstance(requested_sources, str):
            requested_sources = [requested_sources]
        aliases = {
            "fact": "facts",
            "person": "people",
            "conversation": "conversations",
            "session": "sessions",
            "action": "actions",
        }
        sources = {aliases.get(str(source).casefold(), str(source).casefold()) for source in requested_sources}
        allowed_sources = {"facts", "people", "conversations", "sessions", "actions"}
        unknown = sorted(sources - allowed_sources)
        if unknown:
            return self._json({"ok": False, "error": f"Unknown memory source(s): {', '.join(unknown)}."})

        records: list[dict[str, Any]] = []
        counts = {source: 0 for source in sorted(sources)}

        if "facts" in sources:
            facts = self.memory.search(query, limit=limit) if query else self.memory.get_recent(limit=limit)
            for fact in facts:
                records.append({
                    "source": "fact",
                    "source_id": f"fact:{fact['fact_id']}",
                    "fact_id": fact["fact_id"],
                    "content": fact.get("fact_text", ""),
                    "category": fact.get("category", "note"),
                    "importance": fact.get("importance", 0.5),
                    "confidence": fact.get("confidence"),
                    "created_at": fact.get("created_at"),
                    "updated_at": fact.get("updated_at"),
                })
            counts["facts"] = len(facts)

        if "people" in sources and self.people_store is not None:
            people = (
                self.people_store.search(query, limit=limit, include_sensitive=True)
                if query else self.people_store.list_all(include_sensitive=True)[:limit]
            )
            for person in people:
                person_view = self._person_view(person)
                records.append({
                    "source": "person",
                    "source_id": f"person:{person_view.get('person_id')}",
                    "content": person_view.get("canonical_name", ""),
                    "person": person_view,
                    "updated_at": person_view.get("updated_at"),
                })
            counts["people"] = len(people)

        if "conversations" in sources and self.conversations is not None:
            try:
                conversations = self.conversations.search_recall(query, since=since, limit=limit)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)})
            for record in conversations:
                records.append({
                    "source": "conversation",
                    "source_id": f"conversation:{record.get('conversation_id')}:message:{record.get('id')}",
                    "conversation_id": record.get("conversation_id"),
                    "message_id": record.get("id"),
                    "role": record.get("role"),
                    "timestamp": record.get("created_at"),
                    "content": record.get("content", ""),
                })
            counts["conversations"] = len(conversations)

        if "sessions" in sources and self.session_store is not None:
            try:
                sessions = self.session_store.search_recall(query, since=since, limit=limit)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)})
            records.extend(sessions)
            counts["sessions"] = len(sessions)

        if "actions" in sources and self.action_ledger is not None:
            try:
                actions = self.action_ledger.search(query, since=since, limit=limit)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)})
            for action in actions:
                records.append({
                    "source": "action",
                    "source_id": f"action:{action.get('action_id')}",
                    "content": action.get("summary", ""),
                    "action": action,
                    "timestamp": action.get("created_at"),
                })
            counts["actions"] = len(actions)

        # Every source has already ranked its own results.  Preserve source
        # relevance while ensuring the best scored historical session evidence
        # wins when a query was split across neighboring turns.
        records.sort(
            key=lambda record: (
                int(record.get("score", 0)),
                str(record.get("timestamp") or record.get("updated_at") or record.get("created_at") or ""),
                str(record.get("source_id") or ""),
            ),
            reverse=True,
        )
        records = records[:limit]
        payload = {
            "ok": True,
            "query": query,
            "since": since,
            "sources": sorted(sources),
            "counts": counts,
            "result_count": len(records),
            "results": records,
        }
        if not records:
            payload["message"] = f"No matching local recall records found for '{query}'."
        return self._json(payload)

    def _update_memory(self, args: dict) -> str:
        fact_id = int(args["fact_id"])
        updated = self.memory.update(
            fact_id,
            fact_text=args.get("content"),
            category=args.get("category"),
            confidence=float(args["confidence"]) if args.get("confidence") is not None else None,
            importance=float(args["importance"]) if args.get("importance") is not None else None,
        )
        if not updated:
            return f"Memory #{fact_id} was not found."
        memory = self.memory.get(fact_id)
        return f"Updated memory #{fact_id}: {memory['fact_text']}"

    def _delete_memory(self, args: dict) -> str:
        fact_id = int(args["fact_id"])
        if self.memory.delete(fact_id):
            return f"Forgot memory #{fact_id}."
        return f"Memory #{fact_id} was not found."

    # ── People, relationships, and action history ────────────────

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _person_view(person: dict[str, Any]) -> dict[str, Any]:
        """Return the complete local person record for explicit retrieval."""
        return {
            "person_id": person.get("person_id"),
            "canonical_name": person.get("canonical_name", ""),
            "aliases": list(person.get("aliases") or []),
            "relation": person.get("relation", ""),
            "phone": person.get("phone") or "",
            "email": person.get("email") or "",
            "important_dates": dict(person.get("important_dates") or {}),
            "notes": person.get("notes") or "",
            "last_referenced_at": person.get("last_referenced_at"),
            "last_contacted_at": person.get("last_contacted_at"),
            "last_contacted_via": person.get("last_contacted_via"),
            "created_at": person.get("created_at"),
            "updated_at": person.get("updated_at"),
            "source": person.get("source", "manual"),
            "revision": person.get("revision", 1),
        }

    def _people_unavailable(self) -> str:
        return self._json({"ok": False, "error": "People store is unavailable because Ares has no local data path."})

    def _remember_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        try:
            person = self.people_store.create(
                args.get("canonical_name", ""),
                aliases=args.get("aliases") or [],
                relation=args.get("relation", ""),
                phone=args.get("phone") or None,
                email=args.get("email") or None,
                important_dates=args.get("important_dates") or {},
                notes=args.get("notes", ""),
                source=args.get("source", "manual"),
                confidence=float(args.get("confidence", 1.0)),
            )
        except (ValueError, PersonConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "action": "remembered", "person": self._person_view(person)})

    def _search_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        try:
            people = self.people_store.search(
                args.get("query", ""),
                limit=int(args.get("limit", 5)),
                include_sensitive=True,
            )
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "people": [self._person_view(person) for person in people]})

    def _update_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        updates = {
            key: args[key]
            for key in ("canonical_name", "aliases", "relation", "phone", "email", "important_dates", "notes", "source", "confidence")
            if key in args
        }
        if not updates:
            return self._json({"ok": False, "error": "Provide at least one person field to update."})
        try:
            person = self.people_store.update(
                int(args.get("person_id", 0)),
                expected_revision=args.get("expected_revision"),
                **updates,
            )
        except (ValueError, PersonConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        if person is None:
            return self._json({"ok": False, "error": "Person not found."})
        return self._json({"ok": True, "action": "updated", "person": self._person_view(person)})

    def _forget_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        if not bool(args.get("confirm", False)):
            return self._json({
                "ok": False,
                "confirm_required": True,
                "error": "Deleting a saved person permanently removes the local record. Re-call with confirm=true after explicit approval.",
            })
        try:
            deleted = self.people_store.delete(int(args.get("person_id", 0)), expected_revision=args.get("expected_revision"))
        except (ValueError, PersonConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": bool(deleted), "action": "forgotten" if deleted else "not_found"})

    def _search_actions(self, args: dict) -> str:
        if self.action_ledger is None:
            return self._json({"ok": False, "error": "Action ledger is unavailable because Ares has no local data path."})
        try:
            actions = self.action_ledger.search(
                args.get("query", ""),
                since=args.get("since"),
                limit=int(args.get("limit", 20)),
            )
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "actions": actions})

    def _task_tools_unavailable(self) -> str:
        return self._json({"ok": False, "error": "Task store is unavailable because Ares has no local data path."})

    def _create_task(self, args: dict) -> str:
        return self.task_tools.create_task(args) if self.task_tools is not None else self._task_tools_unavailable()

    def _list_tasks(self, args: dict) -> str:
        return self.task_tools.list_tasks(args) if self.task_tools is not None else self._task_tools_unavailable()

    def _get_task_status(self, args: dict) -> str:
        return self.task_tools.get_task_status(args) if self.task_tools is not None else self._task_tools_unavailable()

    def _update_task(self, args: dict) -> str:
        return self.task_tools.update_task(args) if self.task_tools is not None else self._task_tools_unavailable()

    def _cancel_task(self, args: dict) -> str:
        return self.task_tools.cancel_task(args) if self.task_tools is not None else self._task_tools_unavailable()

    def _run_task_unavailable(self, args: dict) -> str:
        return self._json({"ok": False, "error": "run_task must be executed through the Ares Agent workflow runner."})

    def _goal_tools_unavailable(self) -> str:
        return self._json({"ok": False, "error": "Goal store is unavailable because Ares has no local data path."})

    def _goal_call(self, method: str, args: dict) -> str:
        if self.goal_tools is None:
            return self._goal_tools_unavailable()
        return getattr(self.goal_tools, method)(args)

    def _create_goal(self, args: dict) -> str:
        return self._goal_call("create_goal", args)

    def _update_goal(self, args: dict) -> str:
        return self._goal_call("update_goal", args)

    def _list_goals(self, args: dict) -> str:
        return self._goal_call("list_goals", args)

    def _get_goal_status(self, args: dict) -> str:
        return self._goal_call("get_goal_status", args)

    def _complete_goal(self, args: dict) -> str:
        return self._goal_call("complete_goal", args)

    def _pause_goal(self, args: dict) -> str:
        return self._goal_call("pause_goal", args)

    def _abandon_goal(self, args: dict) -> str:
        return self._goal_call("abandon_goal", args)

    def _decompose_goal(self, args: dict) -> str:
        return self._goal_call("decompose_goal", args)

    def _link_goal_task(self, args: dict) -> str:
        return self._goal_call("link_goal_task", args)

    def _link_goal_action(self, args: dict) -> str:
        return self._goal_call("link_goal_action", args)

    def _link_goal_watcher(self, args: dict) -> str:
        return self._goal_call("link_goal_watcher", args)

    def _unlink_goal_watcher(self, args: dict) -> str:
        return self._goal_call("unlink_goal_watcher", args)

    def _get_goal_signals(self, args: dict) -> str:
        return self._goal_call("get_goal_signals", args)

    def _acknowledge_goal_signal(self, args: dict) -> str:
        return self._goal_call("acknowledge_goal_signal", args)

    def _snooze_goal_signal(self, args: dict) -> str:
        return self._goal_call("snooze_goal_signal", args)

    def _sync_goal_progress(self, args: dict) -> str:
        return self._goal_call("sync_goal_progress", args)

    def _record_goal_progress(self, args: dict) -> str:
        return self._goal_call("record_goal_progress", args)

    def _delete_goal(self, args: dict) -> str:
        return self._goal_call("delete_goal", args)

    # ── Consequential-action provenance ───────────────────────────

    @staticmethod
    def _action_prestate(tool_name: str, args: dict) -> dict[str, Any]:
        """Capture only filesystem existence needed to classify a completed action."""
        lowered = str(tool_name).casefold()
        state: dict[str, Any] = {}
        try:
            if lowered in {"write_file", "create_file_from_template", "edit_file", "delete_file"}:
                state["path_existed"] = Path(str(args.get("path", ""))).expanduser().exists()
            elif lowered == "move_file":
                state["destination_existed"] = Path(str(args.get("destination", ""))).expanduser().exists()
        except (OSError, ValueError):
            state["path_existed"] = True
        return state

    @staticmethod
    def _action_succeeded(result: str) -> bool:
        text = str(result or "").strip()
        lowered = text.casefold()
        if not text or lowered.startswith("error:") or "confirm required" in lowered or "dry run" in lowered:
            return False
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            if payload.get("ok") is False or payload.get("error"):
                return False
            if payload.get("sent") is False or payload.get("dialed") is False:
                return False
        match = re.search(r"^Exit code:\s*(-?\d+)", text, flags=re.MULTILINE)
        return not match or int(match.group(1)) == 0

    @staticmethod
    def _masked_recipient(value: Any) -> str:
        raw = str(value or "").strip()
        if "@" in raw:
            return mask_email(raw)
        if re.fullmatch(r"[+0-9 ()-]{3,40}", raw):
            return mask_phone(raw)
        return raw[:160] or "recipient"

    @staticmethod
    def _safe_web_target(value: Any) -> str:
        """Keep signed URLs and query strings out of the provenance ledger."""
        try:
            host = (urlparse(str(value or "")).hostname or "").strip().casefold()
        except (TypeError, ValueError):
            host = ""
        return host or "online source"

    @staticmethod
    def _result_json(result: str) -> dict[str, Any]:
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _command_may_change_state(command: Any) -> bool:
        """A conservative heuristic; the command itself is never recorded."""
        text = str(command or "").casefold()
        return bool(re.search(
            r"\b(?:mkdir|rmdir|rm|del|move|mv|copy|cp|touch|rename|git\s+(?:commit|push|reset|merge|rebase|checkout)|"
            r"npm\s+(?:install|ci|publish)|pip\s+install|uv\s+(?:add|sync)|docker\s+(?:build|push|run)|"
            r"powershell\s+.*(?:remove-item|set-content|new-item)|set-content|add-content)\b|(?:^|\s)>+",
            text,
        ))

    def _action_spec(
        self,
        tool_name: str,
        args: dict,
        result: str,
        *,
        prestate: dict[str, Any] | None = None,
    ) -> tuple[str, str, str, list[str]] | None:
        """Map completed tool calls to content-free ledger metadata."""
        lowered = str(tool_name).casefold()
        base = lowered.rsplit("__", 1)[-1]
        prestate = prestate or {}
        path = str(args.get("path") or "")
        if lowered == "write_file":
            return ("file_edited" if prestate.get("path_existed") else "file_created", path, "Wrote a local file.", ["file"])
        if lowered in {"edit_file", "insert_line", "replace_lines", "delete_lines", "append_to_file", "prepend_to_file", "undo_last_edit"}:
            return ("file_edited", path, "Edited a local file.", ["file"])
        if lowered == "delete_file":
            return ("file_deleted", path, "Deleted a local file or directory.", ["file"])
        if lowered == "move_file":
            target = f"{args.get('source', '')} -> {args.get('destination', '')}".strip()
            return ("file_moved", target, "Moved a local file or directory.", ["file"])
        if lowered == "copy_file":
            target = f"{args.get('source', '')} -> {args.get('destination', '')}".strip()
            return ("file_copied", target, "Copied a local file.", ["file"])
        if lowered == "create_directory":
            return ("directory_created", path, "Created a local directory.", ["file"])
        if lowered in {"batch_edit", "batch_file_ops", "glob_apply"}:
            return ("files_batch_changed", "local filesystem", "Completed a batch file operation.", ["file", "batch"])
        if lowered == "create_file_from_template":
            return ("file_created", path, "Created a file from a template.", ["file"])
        if lowered == "generate_image":
            return ("image_generated", "generated image", "Generated an image.", ["image"])
        if lowered in {"resize_image", "convert_image", "crop_image"}:
            return ("image_edited", str(args.get("output") or args.get("path") or "image"), "Edited an image.", ["image"])
        if lowered in {"run_command", "terminal_exec"} and self._command_may_change_state(args.get("command") or args.get("command_key")):
            return ("command_run", str(args.get("cwd") or "working directory"), "Ran a state-changing command.", ["command"])
        if lowered == "create_cron_job":
            return ("cron_job_created", str(args.get("name") or "cron job"), "Created a scheduled job.", ["cron"])
        if lowered == "export_data":
            return ("export_created", str(args.get("path") or "Ares export"), "Created a local Ares export.", ["export"])
        if lowered == "download_online_file":
            return ("research_file_downloaded", self._safe_web_target(args.get("url")), "Downloaded an online research file locally.", ["research", "file"])
        if lowered == "create_research_report":
            return ("research_report_created", "research brief", "Created a cited local research report.", ["research", "report"])
        if lowered == "telegram_send_file":
            return ("telegram_file_sent", self._masked_recipient(args.get("chat_id") or "allowlisted Telegram chat"), "Sent a file to an allowlisted Telegram chat.", ["telegram", "file"])
        if lowered == "phone_send_sms":
            return ("sms_sent", self._masked_recipient(args.get("number")), "Sent an SMS.", ["phone"])
        if lowered == "phone_call_number":
            return ("phone_call_placed", self._masked_recipient(args.get("number")), "Placed a phone call.", ["phone"])
        if lowered == "telephony_call":
            return ("telephony_call_placed", self._masked_recipient(args.get("recipient")), "Placed a provider-backed phone call.", ["telephony", "phone"])
        if lowered == "telephony_hangup":
            return ("telephony_call_ended", str(args.get("call_id") or "call"), "Ended a provider-backed phone call.", ["telephony", "phone"])
        if lowered == "telephony_transfer":
            return ("telephony_call_transferred", str(args.get("call_id") or "call"), "Transferred a provider-backed phone call.", ["telephony", "phone"])
        if base in {"gmail_send", "gmail_reply"}:
            return ("email_sent", self._masked_recipient(args.get("to") or args.get("recipient")), "Sent an email.", ["email"])
        if base == "calendar_create_event":
            return ("calendar_event_created", str(args.get("calendar_id") or "primary calendar"), "Created a calendar event.", ["calendar"])
        if lowered == "remember_person":
            person = self._result_json(result).get("person", {})
            return ("person_remembered", str(person.get("canonical_name") or "person"), "Saved an explicitly confirmed person record.", ["people"])
        if lowered == "update_person":
            person = self._result_json(result).get("person", {})
            return ("person_updated", str(person.get("canonical_name") or "person"), "Updated an explicitly confirmed person record.", ["people"])
        if lowered == "forget_person":
            return ("person_forgotten", f"person #{args.get('person_id', '')}", "Deleted an explicitly confirmed person record.", ["people"])
        if lowered == "create_task":
            task = self._result_json(result).get("task", {})
            return ("task_created", str(task.get("task_id") or "task"), "Created a durable workflow task.", ["workflow"])
        if lowered == "cancel_task":
            return ("task_cancelled", str(args.get("task_id") or "task"), "Cancelled a workflow task.", ["workflow"])
        if lowered in {"create_goal", "complete_goal", "pause_goal", "abandon_goal", "record_goal_progress", "sync_goal_progress"}:
            goal = self._result_json(result).get("goal", {})
            action_type = {
                "create_goal": "goal_created",
                "complete_goal": "goal_completed",
                "pause_goal": "goal_paused",
                "abandon_goal": "goal_abandoned",
                "record_goal_progress": "goal_progress_recorded",
                "sync_goal_progress": "goal_progress_synced",
            }[lowered]
            return (action_type, str(goal.get("title") or f"goal #{args.get('goal_id', '')}"), f"{action_type.replace('_', ' ').capitalize()}.", ["goal"])
        if lowered == "decompose_goal":
            return ("goal_decomposed", f"goal #{args.get('goal_id', '')}", "Decomposed a goal into sub-goals.", ["goal"])
        if lowered == "link_goal_task":
            return ("goal_task_linked", f"goal #{args.get('goal_id', '')}", "Linked a durable task to a goal.", ["goal", "workflow"])
        if lowered == "link_goal_action":
            return ("goal_action_linked", f"goal #{args.get('goal_id', '')}", "Linked action evidence to a goal.", ["goal", "action"])
        if lowered == "link_goal_watcher":
            return ("goal_watcher_linked", f"goal #{args.get('goal_id', '')}", "Linked a proactive watcher to a goal.", ["goal", "watcher"])
        if lowered == "unlink_goal_watcher":
            return ("goal_watcher_unlinked", f"goal #{args.get('goal_id', '')}", "Unlinked a proactive watcher from a goal.", ["goal", "watcher"])
        if lowered == "acknowledge_goal_signal":
            return ("goal_signal_acknowledged", f"signal #{args.get('signal_id', '')}", "Reviewed a watcher signal linked to a goal.", ["goal", "watcher"])
        if lowered == "snooze_goal_signal":
            return ("goal_signal_snoozed", f"signal #{args.get('signal_id', '')}", "Snoozed a watcher signal linked to a goal.", ["goal", "watcher"])
        if lowered == "delete_goal":
            return ("goal_deleted", f"goal #{args.get('goal_id', '')}", "Deleted a goal.", ["goal"])
        return None

    def _record_consequential_action(
        self,
        tool_name: str,
        args: dict,
        result: str,
        *,
        prestate: dict[str, Any] | None = None,
    ) -> int | None:
        if self.action_ledger is None or not self._action_succeeded(result):
            return None
        spec = self._action_spec(tool_name, args, result, prestate=prestate)
        if spec is None:
            return None
        action_type, target, summary, tags = spec
        try:
            return self.action_ledger.record(
                action_type,
                target=target,
                summary=summary,
                tool_name=tool_name,
                session_id=self.session_id,
                tags=tags,
            )
        except Exception:
            # Provenance must never turn a successfully completed user action
            # into an apparent failure. The local action is still authoritative.
            return None

    def record_external_action(self, tool_name: str, args: dict, result: str) -> int | None:
        """Record consequential MCP outcomes after Agent dispatch completes.

        For a saved person alias, successful communication also updates local
        relationship state.  Direct addresses/numbers are intentionally not
        imported or matched: the user must explicitly save a person first.
        """
        action_id = self._record_consequential_action(tool_name, args, result)
        if action_id is None or self.people_store is None:
            return action_id
        spec = self._action_spec(tool_name, args, result)
        if spec is None:
            return action_id
        action_type = spec[0]
        channel_by_action = {
            "email_sent": "email",
            "sms_sent": "sms",
            "phone_call_placed": "phone",
        }
        channel = channel_by_action.get(action_type)
        if channel is None:
            return action_id
        reference = args.get("to") or args.get("recipient") or args.get("number")
        for candidate in str(reference or "").split(","):
            alias = candidate.strip()
            if not alias or "@" in alias:
                continue
            try:
                self.people_store.mark_contacted(alias, channel=channel)
            except Exception:
                # Contact-status bookkeeping must never turn a completed
                # external action into a reported failure.
                continue
        return action_id

    # ── Skills ─────────────────────────────────────────────────────

    def _skill_manager(self) -> SkillManager:
        manager = getattr(self, "skill_manager", None)
        if manager is None:
            skill_dirs = list((self.config.skill_dirs if self.config else []) or [])
            manager = SkillManager(skill_dirs=skill_dirs or None)
            self.skill_manager = manager
        return manager

    def _list_skills_tool(self, args: dict | None = None) -> str:
        args = args or {}
        manager = self._skill_manager()
        skills = manager.search(
            query=args.get("query", ""),
            category=args.get("category", ""),
        )
        if not skills:
            return "No matching skills found."
        lines = [f"Available skills ({len(skills)}):"]
        lines.extend(skill.summary_line() for skill in skills)
        cats = manager.list_categories()
        if cats:
            lines.append("Categories: " + ", ".join(f"{name} ({count})" for name, count in cats.items()))
        return "\n".join(lines)

    def _load_skill_tool(self, args: dict) -> str:
        skill = self._skill_manager().get_skill(args["name"])
        if skill is None:
            return f"Skill '{args['name']}' was not found."
        files = ""
        if skill.files:
            rel_files = [str(path.relative_to(skill.root)) for path in skill.files[:20]]
            files = "\n\nSupporting files:\n" + "\n".join(f"- {file}" for file in rel_files)
        tests = ""
        if skill.test_commands:
            tests = "\n\nTest commands:\n" + "\n".join(f"- {cmd}" for cmd in skill.test_commands)
        lint = ""
        if skill.lint_messages:
            lint = "\n\nSkill lint:\n" + "\n".join(f"- {msg}" for msg in skill.lint_messages)
        return (
            f"# Skill: {skill.name}\n"
            f"Category: {skill.category}\n"
            f"Description: {skill.description}\n"
            f"Version: {skill.version}\n\n"
            f"{skill.content}{files}{tests}{lint}"
        )

    def _create_skill_tool(self, args: dict) -> str:
        skill = self._skill_manager().create_skill(
            name=args["name"],
            content=args["content"],
            category=args.get("category", "general"),
        )
        return f"Created skill '{skill.name}' in category '{skill.category}' at {skill.path}."

    @staticmethod
    def _marketplace_async_required(_args: dict) -> str:
        return "Marketplace registry operations require the async agent execution path."

    @staticmethod
    def _watcher_async_required(_args: dict) -> str:
        return "Running a watcher immediately requires the async Ares runtime; start Ares with --all."

    def _marketplace_config(self) -> AppConfig:
        if self.config is None:
            raise RuntimeError("Marketplace configuration is unavailable.")
        return self.config

    def _marketplace_skills_dir(self) -> Path:
        config = self._marketplace_config()
        dirs = list(config.skill_dirs or [])
        return Path(dirs[0] if dirs else "~/.ares/skills").expanduser()

    async def _search_skill_marketplace(self, args: dict) -> str:
        config = self._marketplace_config()
        client = SkillRegistryClient(config.skill_registries)
        try:
            results = await client.search(str(args.get("query") or ""), args.get("registry"))
        except (ValueError, RuntimeError) as exc:
            return f"Marketplace search failed: {exc}"
        if not results:
            errors = "; ".join(f"{name}: {message}" for name, message in client.last_errors.items())
            return "No marketplace skills found." + (f" Registry notes: {errors}" if errors else "")
        lines = [f"Marketplace skills ({len(results)}):"]
        for item in results[:12]:
            lines.append(
                f"- {item.reference} [{item.registry}, {item.version}] — {item.description or 'No description.'}"
            )
        return "\n".join(lines)

    async def _install_marketplace_skill(self, args: dict) -> str:
        if not bool(args.get("confirm", False)):
            return "CONFIRM REQUIRED: Installing a community skill writes instructions to disk. Re-call only after the user explicitly approves this exact skill with confirm=true."
        config = self._marketplace_config()
        slug = str(args.get("slug") or "").strip()
        client = SkillRegistryClient(config.skill_registries)
        detail = await client.get_skill(slug, args.get("registry"))
        if detail is None:
            return f"Skill '{slug}' was not found in configured registries."
        if detail.suspicious:
            return "Install blocked: the selected registry skill is flagged suspicious. Ask the user to review the source manually."
        archive = await client.download(detail.reference, detail.version, detail.registry)
        if archive is None:
            return "Install blocked: the registry did not provide a safe hosted ZIP archive."
        try:
            installation = SafeSkillInstaller(self._marketplace_skills_dir()).install(
                archive,
                provenance={
                    "registry": detail.registry,
                    "slug": detail.reference,
                    "version": detail.version,
                    "canonical_url": detail.canonical_url,
                },
            )
        except (FileExistsError, SkillValidationError) as exc:
            return f"Skill was not installed: {exc}"
        self.skill_manager = SkillManager(skill_dirs=list(config.skill_dirs or []) or None)
        missing = [
            dependency.name
            for dependency in installation.dependencies
            if dependency.type == "mcp_server"
            and not any(str(server.get("name") or "").casefold() == dependency.name.casefold() for server in config.mcp_servers)
        ]
        suffix = (
            " Missing MCP dependencies (not added automatically): " + ", ".join(missing) + "."
            if missing else ""
        )
        return f"Installed skill '{installation.skill.name}' at {installation.path}.{suffix}"

    async def _search_mcp_marketplace(self, args: dict) -> str:
        config = self._marketplace_config()
        client = MCPRegistryClient(config.mcp_registries)
        try:
            results = await client.search(str(args.get("query") or ""), args.get("registry"))
        except (ValueError, RuntimeError) as exc:
            return f"MCP marketplace search failed: {exc}"
        if not results:
            errors = "; ".join(f"{name}: {message}" for name, message in client.last_errors.items())
            return "No MCP servers found." + (f" Registry notes: {errors}" if errors else "")
        lines = [f"MCP marketplace servers ({len(results)}):"]
        for item in results[:12]:
            trust = "verified" if item.verified else "registry listing"
            lines.append(f"- {item.name} [{item.registry}, {trust}] — {item.description or 'No description.'}")
        return "\n".join(lines)

    async def _add_marketplace_mcp(self, args: dict) -> str:
        config = self._marketplace_config()
        source_name = str(args.get("name") or "").strip()
        existing = {str(server.get("name") or "") for server in config.mcp_servers if isinstance(server, dict)}
        if source_name in existing:
            return f"MCP server '{source_name}' is already configured."
        builtin = next((server for server in DEFAULT_MCP_SERVERS if server["name"] == source_name), None)
        plan = None
        if builtin is not None:
            payload = dict(builtin)
            source = "Ares built-in configuration"
        else:
            client = MCPRegistryClient(config.mcp_registries)
            plan = await client.get_install_command(source_name, args.get("registry"))
            if plan is None:
                return f"No safe install plan was found for MCP server '{source_name}'."
            payload = plan.as_config(existing_names=existing)
            source = f"{plan.registry} registry"
        review = (
            f"MCP plan for {payload['name']}: source={source}; transport={payload['transport']}; "
            f"target={payload.get('server_url') or payload.get('command') or '-'}; "
            f"args={' '.join(payload.get('args') or []) or '-'}"
        )
        if not bool(args.get("confirm", False)):
            return "CONFIRM REQUIRED: " + review + ". Re-call with confirm=true only after the user approves this exact plan."
        config.mcp_servers.append(payload)
        save_config(config)
        return review + ". Added to shared config; use /mcp refresh before calling its tools."

    # ── Export ─────────────────────────────────────────────────────

    def _export_data(self, args: dict) -> str:
        path = export_data(
            memory_store=self.memory,
            conversation_store=self.conversations,
            people_store=self.people_store,
            action_ledger=self.action_ledger,
            goal_store=self.goal_store,
            config=self.config,
            path=args.get("path"),
            profile=args.get("profile", "full"),
        )
        return f"Exported Ares data to {path}"

    # ── Web tools ──────────────────────────────────────────────────

    @staticmethod
    def _search_options_from_args(args: dict, *, fetch_top: int) -> dict[str, Any]:
        """Normalize the extended research-search options at one boundary."""
        return {
            "query": args["query"],
            "max_results": int(args.get("max_results", 5)),
            "provider": args.get("provider"),
            "fetch_top": fetch_top,
            "max_fetch_chars": int(args.get("max_fetch_chars", 8000)),
            "domains": args.get("domains") or [],
            "exclude_domains": args.get("exclude_domains") or [],
            "file_type": str(args.get("file_type") or ""),
            "search_mode": str(args.get("search_mode") or "web"),
            "recency_days": args.get("recency_days"),
            "cache_ttl_seconds": int(args.get("cache_ttl_seconds", 300)),
        }

    def _research_search_payload(self, args: dict, *, fetch_top: int) -> dict[str, Any]:
        """Build a cached, filterable payload while retaining old plugin compatibility."""
        options = self._search_options_from_args(args, fetch_top=fetch_top)
        try:
            return web_search_payload(**options)
        except TypeError as exc:
            # Existing third-party provider shims may still implement the
            # legacy six-argument call. Do not make a search unavailable just
            # because that optional layer has not added filters yet.
            if "unexpected keyword argument" not in str(exc):
                raise
            legacy = {
                key: options[key]
                for key in ("query", "max_results", "provider", "fetch_top", "max_fetch_chars")
            }
            return web_search_payload(**legacy)

    def _web_search(self, args: dict) -> str:
        payload = self._research_search_payload(args, fetch_top=int(args.get("fetch_top", 3)))
        return payload_to_json(payload)

    @staticmethod
    def _web_timeout_error(operation: str, timeout: float) -> TimeoutError:
        return TimeoutError(
            f"{operation} timed out after {timeout:g}s. Try again with a narrower query or fewer sources."
        )

    async def _run_blocking_web_search(self, args: dict) -> str:
        """Run a provider-backed search away from the shared async loop."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._web_search, args),
                timeout=WEB_SEARCH_BLOCKING_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise self._web_timeout_error("Web search", WEB_SEARCH_BLOCKING_TIMEOUT_SECONDS) from exc

    async def _run_blocking_research_payload(self, args: dict, *, fetch_top: int) -> dict[str, Any]:
        """Keep the synchronous search provider from blocking other chats."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._research_search_payload, args, fetch_top=fetch_top),
                timeout=WEB_SEARCH_BLOCKING_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise self._web_timeout_error("Web search", WEB_SEARCH_BLOCKING_TIMEOUT_SECONDS) from exc

    async def _run_blocking_fetch(self, url: str, *, max_chars: int) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fetch_url, url, max_chars=max_chars),
                timeout=WEB_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return {
                "url": url,
                "title": "",
                "content": "",
                "truncated": False,
                "error": f"Local fetch timed out after {WEB_FETCH_TIMEOUT_SECONDS:g}s.",
                "fetcher": "local",
            }

    async def _web_search_async(self, args: dict) -> str:
        fetcher = str(args.get("fetcher", "auto") or "auto").lower()
        if fetcher not in {"auto", "mcp", "local"}:
            fetcher = "auto"
        fetch_top = int(args.get("fetch_top", 3))
        max_fetch_chars = int(args.get("max_fetch_chars", 8000))

        if fetcher == "local" or fetch_top <= 0:
            return await self._run_blocking_web_search(args)

        payload = await self._run_blocking_research_payload(args, fetch_top=0)

        fetched: list[dict[str, Any]] = []
        for result in payload.get("results", [])[:fetch_top]:
            url = result.get("url", "")
            if not url:
                continue

            try:
                page, error = await asyncio.wait_for(
                    self._fetch_with_mcp(url, max_fetch_chars),
                    timeout=WEB_FETCH_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                page, error = {}, f"MCP fetch timed out after {WEB_FETCH_TIMEOUT_SECONDS:g}s."
            if error:
                if fetcher == "mcp":
                    payload.setdefault("errors", []).append(error)
                    continue
                page = await self._run_blocking_fetch(url, max_chars=max_fetch_chars)

            if not page.get("error"):
                fetched.append({
                    "url": url,
                    "title": page.get("title") or result.get("title", ""),
                    "content": page.get("content", ""),
                    "truncated": page.get("truncated", False),
                    "fetcher": page.get("fetcher", "mcp"),
                })
            elif fetcher == "auto":
                payload.setdefault("errors", []).append(
                    f"Failed to fetch {url}: {page['error']}"
                )

        payload["fetched"] = fetched
        return payload_to_json(payload)

    def _fetch_mcp_tool_name(self) -> str:
        manager = getattr(self, "mcp_manager", None)
        if manager is None:
            return ""
        definitions = getattr(manager, "tool_definitions", []) or []
        names = [
            tool.get("function", {}).get("name", "")
            for tool in definitions
        ]
        if "mcp__fetch__fetch" in names:
            return "mcp__fetch__fetch"
        for name in names:
            if name.startswith("mcp__fetch__"):
                return name
        if "fetch" in getattr(manager, "sessions", {}):
            return "mcp__fetch__fetch"
        return ""

    async def _fetch_with_mcp(
        self, url: str, max_chars: int
    ) -> tuple[dict[str, Any], str]:
        tool_name = self._fetch_mcp_tool_name()
        if not tool_name:
            return {}, "Fetch MCP server is not connected."

        result = await self.mcp_manager.call_tool(
            tool_name,
            {"url": url, "max_length": max_chars},
        )
        if result.startswith("Error:"):
            return {}, result

        content = result
        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Content truncated at {max_chars} chars]"
            truncated = True
        return {
            "url": url,
            "title": "",
            "content": content,
            "content_type": "text/plain; source=mcp-fetch",
            "truncated": truncated,
            "error": "",
            "fetcher": "mcp",
        }, ""

    def _fetch_url(self, args: dict) -> str:
        return fetch_url_tool(args)

    def _download_online_file(self, args: dict) -> str:
        return json_result(self.research.download(
            str(args["url"]),
            filename=str(args.get("filename") or ""),
            max_bytes=int(args.get("max_bytes", 20 * 1024 * 1024)),
        ))

    def _extract_document(self, args: dict) -> str:
        return json_result(self.research.extract_document(
            path=str(args.get("path") or ""),
            url=str(args.get("url") or ""),
            filename=str(args.get("filename") or ""),
            max_bytes=int(args.get("max_bytes", 20 * 1024 * 1024)),
            max_chars=int(args.get("max_chars", 30_000)),
        ))

    def _create_research_report(self, args: dict) -> str:
        return json_result(self.research.create_report(
            str(args["query"]),
            title=str(args.get("title") or ""),
            max_results=int(args.get("max_results", 8)),
            fetch_top=int(args.get("fetch_top", 5)),
            provider=args.get("provider"),
            domains=args.get("domains") or [],
            exclude_domains=args.get("exclude_domains") or [],
            file_type=str(args.get("file_type") or ""),
            search_mode=str(args.get("search_mode") or "web"),
            recency_days=args.get("recency_days"),
        ))

    @staticmethod
    def _telegram_async_required(_args: dict) -> str:
        return "telegram_send_file requires asynchronous Ares execution."

    async def _telegram_send_file(self, args: dict) -> str:
        if args.get("confirm") is not True:
            return "Confirm required: only send a Telegram file after the user explicitly asks for this delivery."
        channel = self.telegram_channel
        if channel is None:
            return "Error: Telegram delivery is unavailable because the local Telegram channel is not enabled."
        prestate = self._action_prestate("telegram_send_file", args)
        result = await channel.deliver_file(
            path=str(args["path"]),
            chat_id=args.get("chat_id"),
            caption=str(args.get("caption") or ""),
        )
        serialized = json_result(result)
        self._record_consequential_action("telegram_send_file", args, serialized, prestate=prestate)
        return serialized

    # ── Filesystem (read) tools ────────────────────────────────────

    def _read_file(self, args: dict) -> str:
        return read_file(
            args["path"],
            start_line=int(args.get("start_line", 1)),
            num_lines=int(args.get("num_lines", 200)),
        )

    def _search_files(self, args: dict) -> str:
        return search_files(
            query=args.get("query", ""),
            path=args.get("path", "."),
            name_pattern=args.get("name_pattern", ""),
            max_results=int(args.get("max_results", 20)),
        )

    def _list_directory(self, args: dict) -> str:
        return list_directory(
            path=args.get("path", "."),
            max_items=int(args.get("max_items", 30)),
        )

    def _get_file_info(self, args: dict) -> str:
        return _get_file_info_impl(args["path"])

    def _glob_pattern(self, args: dict) -> str:
        return _glob_pattern_impl(
            args["pattern"],
            path=args.get("path", "."),
            max_results=int(args.get("max_results", 50)),
        )

    # ── Filesystem (write) tools ───────────────────────────────────

    def _write_file(self, args: dict) -> str:
        return _write_file_impl(
            args["path"],
            args["content"],
            dry_run=bool(args.get("dry_run", False)),
            confirm=bool(args.get("confirm", False)),
        )

    def _edit_file(self, args: dict) -> str:
        return _edit_file_impl(
            args["path"],
            args["old_text"],
            args["new_text"],
            dry_run=bool(args.get("dry_run", False)),
        )

    def _create_directory(self, args: dict) -> str:
        return _create_directory_impl(
            args["path"],
            dry_run=bool(args.get("dry_run", False)),
        )

    def _delete_file(self, args: dict) -> str:
        path = args["path"]
        dry_run = bool(args.get("dry_run", False))
        confirm = bool(args.get("confirm", False))

        if not confirm and not dry_run:
            return (
                f"⚠ CONFIRM REQUIRED: This will delete {path}. "
                f"Re-call with confirm=true to proceed."
            )

        return _delete_file_impl(path, dry_run=dry_run)

    def _move_file(self, args: dict) -> str:
        source = args["source"]
        destination = args["destination"]
        dry_run = bool(args.get("dry_run", False))
        confirm = bool(args.get("confirm", False))

        # Check if destination exists and needs confirmation
        from ares.tools.filesystem import resolve_path as read_resolve
        try:
            dst_resolved = read_resolve(destination)
            dst_exists = dst_resolved.exists()
        except ValueError:
            dst_exists = False

        if dst_exists and not confirm and not dry_run:
            return (
                f"⚠ CONFIRM REQUIRED: Destination {destination} already exists. "
                f"Re-call with confirm=true to proceed (will overwrite)."
            )

        return _move_file_impl(source, destination, dry_run=dry_run)

    def _batch_edit(self, args: dict) -> str:
        return _batch_edit_impl(
            operations=args.get("operations", []),
            dry_run=bool(args.get("dry_run", False)),
            confirm=bool(args.get("confirm", False)),
            max_operations=int(args.get("max_operations", 100)),
        )

    def _glob_apply(self, args: dict) -> str:
        return _glob_apply_impl(
            pattern=args["pattern"],
            action=args.get("action", "list"),
            path=args.get("path", "."),
            destination=args.get("destination", ""),
            replacement=args.get("replacement", ""),
            dry_run=bool(args.get("dry_run", True)),
            confirm=bool(args.get("confirm", False)),
            max_matches=int(args.get("max_matches", 100)),
        )

    def _show_file_with_line_numbers(self, args: dict) -> str:
        return _show_file_with_line_numbers_impl(args["path"], args.get("start"), args.get("end"))

    def _insert_line(self, args: dict) -> str:
        return _insert_line_impl(
            args["path"],
            int(args["line"]),
            args.get("text", ""),
            position=args.get("position", "after"),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _replace_lines(self, args: dict) -> str:
        return _replace_lines_impl(
            args["path"],
            int(args["start"]),
            int(args["end"]),
            args.get("new_text", ""),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _delete_lines(self, args: dict) -> str:
        return _delete_lines_impl(
            args["path"],
            int(args["start"]),
            int(args["end"]),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _preview_diff(self, args: dict) -> str:
        return _preview_diff_impl(args["path"], args.get("new_content", ""))

    def _backup_file(self, args: dict) -> str:
        return _backup_file_impl(args["path"], label=args.get("label", ""))

    def _undo_last_edit(self, args: dict) -> str:
        return _undo_last_edit_impl(args["path"], dry_run=bool(args.get("dry_run", False)))

    def _batch_file_ops(self, args: dict) -> str:
        return _batch_file_ops_impl(
            args.get("ops", []),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
            max_operations=int(args.get("max_operations", 100)),
        )

    def _find_text(self, args: dict) -> str:
        return _find_text_impl(
            args["path"],
            args["query"],
            context=int(args.get("context", 2)),
            max_results=int(args.get("max_results", 20)),
        )

    def _append_to_file(self, args: dict) -> str:
        return _append_to_file_impl(
            args["path"],
            args.get("text", ""),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _prepend_to_file(self, args: dict) -> str:
        return _prepend_to_file_impl(
            args["path"],
            args.get("text", ""),
            dry_run=bool(args.get("dry_run", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _compare_files(self, args: dict) -> str:
        return _compare_files_impl(args["left"], args["right"])

    def _create_file_from_template(self, args: dict) -> str:
        return _create_file_from_template_impl(
            args["path"],
            template=args.get("template", "notes"),
            dry_run=bool(args.get("dry_run", False)),
            confirm=bool(args.get("confirm", False)),
            confirm_dangerous=bool(args.get("confirm_dangerous", False)),
        )

    def _safe_path_status(self, args: dict) -> str:
        return _safe_path_status_impl(args["path"])
    def _disk_usage(self, args: dict) -> str:
        return _disk_usage_impl(
            path=args.get("path", "."),
            max_depth=int(args.get("max_depth", 2)),
        )

    def _checksum(self, args: dict) -> str:
        return _checksum_impl(
            path=args["path"],
            algorithm=args.get("algorithm", "sha256"),
        )

    def _copy_file(self, args: dict) -> str:
        return _copy_file_impl(
            source=args["source"],
            destination=args["destination"],
            overwrite=bool(args.get("overwrite", False)),
            dry_run=bool(args.get("dry_run", False)),
        )

    def _find_duplicates(self, args: dict) -> str:
        return _find_duplicates_impl(
            path=args.get("path", "."),
            min_size=int(args.get("min_size", 1024)),
            max_results=int(args.get("max_results", 50)),
        )

    def _tail_file(self, args: dict) -> str:
        return _tail_file_impl(
            path=args["path"],
            num_lines=int(args.get("num_lines", 20)),
        )

    def _head_file(self, args: dict) -> str:
        return _head_file_impl(
            path=args["path"],
            num_lines=int(args.get("num_lines", 20)),
        )

    def _count_lines(self, args: dict) -> str:
        return _count_lines_impl(
            path=args.get("path", "."),
            pattern=args.get("pattern", ""),
            name_pattern=args.get("name_pattern", ""),
        )

    def _file_tree(self, args: dict) -> str:
        return _file_tree_impl(
            path=args.get("path", "."),
            max_depth=int(args.get("max_depth", 3)),
            show_files=bool(args.get("show_files", True)),
        )

    # ── Code execution tools ───────────────────────────────────────

    def _run_code(self, args: dict) -> str:
        code = args["code"]
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        if bool(args.get("reset", False)):
            self.repl.reset_python()
        result = self.repl.execute_python(code, timeout=timeout, cwd=cwd)
        if bool(args.get("include_fingerprint", False)) or bool(args.get("reset", False)):
            result += f"\nSession: python generation={self.repl.python_generation}; dependency_fingerprint={self.repl.dependency_fingerprint(cwd)}"
        return result

    def _run_command(self, args: dict) -> str:
        command = args.get("command") or f"@{args.get('command_key', '')}"
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        command = resolve_project_command(command, cwd)
        if bool(args.get("reset", False)):
            self.repl.reset_shell()
        result = self.repl.execute_shell(command, timeout=timeout, cwd=cwd, profile=args.get("profile"))
        if bool(args.get("include_fingerprint", False)) or bool(args.get("reset", False)):
            result += f"\nSession: shell generation={self.repl.shell_generation}; dependency_fingerprint={self.repl.dependency_fingerprint(cwd)}"
        return result

    # ── Image tools ────────────────────────────────────────────────

    def _generate_image(self, args: dict) -> str:
        prompt = args["prompt"]
        width = int(args.get("width", 1024))
        height = int(args.get("height", 1024))
        model = args.get("model", "flux")
        seed = args.get("seed")
        if seed is not None:
            seed = int(seed)
        return generate_image(prompt, width=width, height=height, model=model, seed=seed)

    def _image_info(self, args: dict) -> str:
        return _image_info(args["path"])

    def _resize_image(self, args: dict) -> str:
        return _resize_image(
            args["path"],
            width=args.get("width"),
            height=args.get("height"),
            percent=args.get("percent"),
            output=args.get("output"),
        )

    def _convert_image(self, args: dict) -> str:
        return _convert_image(
            args["path"],
            format=args["format"],
            output=args.get("output"),
            quality=int(args.get("quality", 85)),
        )

    def _crop_image(self, args: dict) -> str:
        return _crop_image(
            args["path"],
            left=int(args.get("left", 0)),
            top=int(args.get("top", 0)),
            right=args["right"],
            bottom=args["bottom"],
            output=args.get("output"),
        )

    # ── Terminal ───────────────────────────────────────────────────

    def _terminal_exec(self, args: dict) -> str:
        """Run with exactly run_command semantics plus observable display state."""
        result = self._run_command(args)
        command = resolve_project_command(
            args.get("command") or f"@{args.get('command_key', '')}",
            args.get("cwd"),
        )
        callback = getattr(self, "_terminal_display_callback", None)
        if callback is None:
            return result + "\nDisplay delivery: unavailable (no visual terminal attached)."
        try:
            callback(command)
        except Exception as exc:
            # Display is intentionally non-fatal, but never invisible.
            return result + f"\nDisplay delivery: failed (non-fatal): {exc}"
        return result + "\nDisplay delivery: delivered."


    # ── Phone tools ───────────────────────────────────────────────

    def _phone_disabled(self) -> str:
        return '{"ok": false, "error": "Phone bridge is disabled. Set phone.enabled=true in config."}'

    def _phone_status(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _adb_bridge.phone_status()

    def _phone_get_notifications(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _kdeconnect_bridge.get_recent_notifications(limit=int(args.get("limit", 20)))

    def _phone_search_contact(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        return _kdeconnect_bridge.search_contacts(args["query"])

    def _resolve_phone_recipient(self, value: Any) -> tuple[str, dict[str, Any] | None]:
        """Resolve an exact saved alias to its stored phone number."""
        raw = str(value or "").strip()
        if not raw:
            raise PersonResolutionError("A phone number or saved person alias is required.")
        if re.fullmatch(r"[+0-9 ()-]{3,40}", raw):
            return raw, None
        if self.people_store is None:
            raise PersonResolutionError("People store is unavailable; provide a phone number instead.")
        person = self.people_store.resolve(raw, require="phone")
        return str(person["phone"]), person

    @staticmethod
    def _phone_result(result: str, person: dict[str, Any] | None = None) -> str:
        """Attach the resolved local person name without masking bridge results."""
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return result
        if not isinstance(payload, dict):
            return result
        if person is not None:
            payload.setdefault("recipient", person.get("canonical_name", "saved person"))
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _phone_send_sms(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        try:
            number, person = self._resolve_phone_recipient(args.get("number"))
        except PersonResolutionError as exc:
            return self._json({"ok": False, "sent": False, "error": str(exc)})
        return self._phone_result(_kdeconnect_bridge.send_sms(number, args["message"]), person)

    def _phone_call_number(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        try:
            number, person = self._resolve_phone_recipient(args.get("number"))
        except PersonResolutionError as exc:
            return self._json({"ok": False, "dialed": False, "error": str(exc)})
        return self._phone_result(
            _adb_bridge.call_number(number, confirm=bool(args.get("confirm", False))),
            person,
        )

    def _phone_launch_app(self, args: dict) -> str:
        """Launch an app on the phone by package name."""
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        package = args.get("package", "")
        if not package:
            import json as _json
            return _json.dumps({"ok": False, "error": "Package name is required."})
        return _adb_bridge.launch_app(package)

    def _phone_open_url(self, args: dict) -> str:
        """Open a URL on the phone."""
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        url = args.get("url", "")
        if not url:
            import json as _json
            return _json.dumps({"ok": False, "error": "URL is required."})
        return _adb_bridge.launch_url(url)

    # ── Provider-backed telephony ────────────────────────────────

    def _telephony_unavailable(self) -> str:
        if self.telephony is None:
            return self._json({"ok": False, "error": "Telephony storage is unavailable because Ares has no local data path."})
        if not self.telephony.enabled:
            return self._json({"ok": False, "error": "Telephony is disabled. Enable telephony in local settings and configure Twilio first."})
        return ""

    def _telephony_status(self, _args: dict) -> str:
        if self.telephony is None:
            return self._json({"ok": False, "error": "Telephony storage is unavailable because Ares has no local data path."})
        return self._json({"ok": True, **self.telephony.status()})

    def _telephony_call(self, args: dict) -> str:
        unavailable = self._telephony_unavailable()
        if unavailable:
            return unavailable
        try:
            call = self.telephony.place_call(str(args.get("recipient") or ""), confirm=bool(args.get("confirm", False)))
        except PermissionError as exc:
            return self._json({"ok": False, "confirm_required": True, "error": str(exc)})
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "call": call.to_dict(include_transcript=False)})

    def _telephony_answer(self, args: dict) -> str:
        unavailable = self._telephony_unavailable()
        if unavailable:
            return unavailable
        call = self.telephony.store.update_call(str(args.get("call_id") or ""), status=CallStatus.IN_PROGRESS)
        if call is None:
            return self._json({"ok": False, "error": "Call session not found."})
        return self._json({"ok": True, "call": call.to_dict(include_transcript=False)})

    def _telephony_hangup(self, args: dict) -> str:
        unavailable = self._telephony_unavailable()
        if unavailable:
            return unavailable
        try:
            call = self.telephony.hangup(str(args.get("call_id") or ""))
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "call": call.to_dict(include_transcript=False)})

    def _telephony_mute(self, args: dict) -> str:
        unavailable = self._telephony_unavailable()
        if unavailable:
            return unavailable
        try:
            result = self.telephony.mute(str(args.get("call_id") or ""), bool(args.get("muted", True)))
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": bool(result.get("ok", False)), "result": result})

    def _telephony_get_call(self, args: dict) -> str:
        if self.telephony is None:
            return self._telephony_unavailable()
        call = self.telephony.store.get_call(str(args.get("call_id") or ""))
        return self._json({"ok": bool(call), "call": call.to_dict() if call else None, "error": "" if call else "Call session not found."})

    def _telephony_list_calls(self, args: dict) -> str:
        if self.telephony is None:
            return self._telephony_unavailable()
        calls = self.telephony.store.list_calls(int(args.get("limit", 20)))
        return self._json({"ok": True, "calls": [call.to_dict(include_transcript=False) for call in calls]})

    def _telephony_list_contacts(self, args: dict) -> str:
        if self.telephony is None:
            return self._telephony_unavailable()
        contacts = self.telephony.list_contacts(int(args.get("limit", 100)))
        return self._json({"ok": True, "contacts": [contact.to_dict() for contact in contacts]})

    def _telephony_save_contact(self, args: dict) -> str:
        if self.telephony is None:
            return self._telephony_unavailable()
        try:
            contact = self.telephony.add_contact(
                str(args.get("name") or ""), str(args.get("phone_number") or ""),
                nickname=str(args.get("nickname") or ""), notes=str(args.get("notes") or ""),
            )
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "contact": contact.to_dict()})

    def _telephony_transfer(self, args: dict) -> str:
        unavailable = self._telephony_unavailable()
        if unavailable:
            return unavailable
        try:
            call = self.telephony.transfer(str(args.get("call_id") or ""), str(args.get("destination") or ""))
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": True, "call": call.to_dict(include_transcript=False)})

    def _update_config(self, args: dict) -> str:
        """Surgically update a single config field."""
        from ares.config import update_config_field
        import json
        path = args.get("path", "")
        if not path:
            return json.dumps({"ok": False, "error": "Path is required."})
        value = args.get("value")
        result = update_config_field(path, value)
        return json.dumps(result, indent=2)

    # ── DateTime tool ─────────────────────────────────────────────

    def _get_current_datetime(self, args: dict) -> str:
        """Get the current date and time."""
        import json
        result = _get_current_datetime_impl(timezone_name=args.get("timezone"))
        return json.dumps(result, indent=2)
