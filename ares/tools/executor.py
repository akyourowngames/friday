"""ToolExecutor — dispatches tool calls to local implementations."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlparse

from PIL import Image

if TYPE_CHECKING:
    from ares.context.conversations import ConversationStore
    from ares.watcher.service import WatcherService

from ares.tools.exporter import (
    build_export_payload,
    default_export_path,
    export_data,
)
from ares.tools.advanced_export import (
    AdvancedExportError,
    plan_advanced_export,
    write_advanced_export,
)
from ares.tools.filesystem import (
    list_directory, read_file, search_files, search_files_async, get_file_info as _get_file_info_impl,
    glob_pattern as _glob_pattern_impl, disk_usage as _disk_usage_impl,
    checksum as _checksum_impl, copy_file as _copy_file_impl,
    find_duplicates as _find_duplicates_impl, tail_file as _tail_file_impl,
    head_file as _head_file_impl, count_lines as _count_lines_impl,
    file_tree as _file_tree_impl,
)
from ares.memory import MemoryConflictError, MemoryStore, calculate_importance
from ares.config import save_config
from ares.models import AppConfig, DEFAULT_MCP_SERVERS
from ares.memory.people import (
    PeopleStore, PersonConflictError, PersonResolutionError,
    mask_email, mask_phone, normalize_reference,
)
from ares.skills.actions import ActionLedger
from ares.sessions import SessionStore
from ares.skills.tasks import TaskStore, TaskToolHandlers
from ares.skills.goals import GoalStore, GoalToolHandlers
from ares.skills.discovery import SkillManager
from ares.delegation.upgrades import DelegationUpgradeError, rank_skills_for_delegation
from ares.integrations.mcp_registry import MCPRegistryClient
from ares.skills.registry import (
    SafeSkillInstaller,
    SkillRegistryClient,
    SkillValidationError,
    marketplace_record,
)
from ares.tools.research import ResearchWorkspace, json_result
from ares.tools.research_upgrades import (
    ResearchUpgradeStore,
    advanced_extract,
    advanced_fetch,
    create_advanced_report,
)
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
from ares.tools.image_edit import crop_geometry as _crop_geometry
from ares.tools.image_edit import resize_geometry as _resize_geometry
from ares.tools.image_edit import transform_image as _transform_image
from ares.memory.policy import memory_rejection_reason
from ares.skills.commitments import CommitmentStore
from ares.skills.followups import FollowUpStore, future_utc
from ares.cron.store import CronStore
from ares.cron.tools import CronToolHandlers
from ares.tools.datetime_tool import get_current_datetime_result as _get_current_datetime_impl
from ares.tools import adb_bridge as _adb_bridge
from ares.tools import kdeconnect_bridge as _kdeconnect_bridge
from ares.tools.phone_upgrades import (
    call_preflight,
    normalize_call_status,
    prepare_notifications,
    preview_sms,
    rank_contact_candidates,
    sms_delivery_status,
    validate_post_call_note,
)
from ares.tools.shell_execution import resolve_project_command
from ares.tools.project_checks import run_project_check
from ares.tools.results import error_result, structured_result, wants_structured
from ares.tools.file_upgrades import (
    advanced_edit, advanced_read, advanced_search, advanced_write, plan_batch, project_scan,
)
from ares.tools.media_export_upgrades import (
    UpgradeValidationError,
    build_image_variation_manifest,
    plan_image_batch_transform,
    plan_image_transform,
    project_action_history,
    validate_image_metadata,
    validate_transform_result,
)
from ares.tools.runtime_upgrades import RuntimeUpgradeManager
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
        commitment_store: CommitmentStore | None = None,
        follow_up_store: FollowUpStore | None = None,
        session_store: SessionStore | None = None,
        telephony_manager: TelephonyManager | None = None,
        vision_service: Any | None = None,
    ):
        self.memory = memory_store
        self.self_improvement_store: Any | None = None
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
        # Vision has its own small metadata-only database.  It deliberately
        # does not share the memory connection: frame/event retention has a
        # separate lifecycle and must remain safe even when memory is erased.
        from ares.tools.vision_tools import VisionToolHandlers
        from ares.vision.service import VisionService

        vision_path = (
            Path(db_path).with_name("vision.db")
            if db_path is not None
            else (session_data_dir or data_root or Path("~/.ares/data").expanduser()) / "vision.db"
        )
        self._owns_vision_service = vision_service is None
        self.vision_service = vision_service or VisionService(
            database_path=vision_path,
            memory_store=memory_store,
            config=getattr(config, "vision", None),
            action_ledger=self.action_ledger,
        )
        self.vision_tools = VisionToolHandlers(
            self.vision_service,
            session_id_provider=lambda: self.session_id,
        )
        self.task_store = task_store or (TaskStore(data_root) if data_root is not None else None)
        self.task_tools = TaskToolHandlers(self.task_store, lambda: self.session_id) if self.task_store is not None else None
        self._owns_goal_store = goal_store is None and db_path is not None
        self.goal_store = goal_store or (
            GoalStore(db_path=db_path, connection=shared_connection, task_store=self.task_store)
            if db_path is not None else None
        )
        self.commitment_store = commitment_store or (
            CommitmentStore(db_path=db_path, connection=shared_connection)
            if db_path is not None else None
        )
        reflection_timezone = str(
            getattr(getattr(config, "reflection", None), "local_timezone", "") or ""
        ).strip() or None
        self.follow_up_store = follow_up_store or (
            FollowUpStore(
                db_path=db_path,
                connection=shared_connection,
                timezone_name=reflection_timezone,
            )
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
        research_data_dir = session_data_dir or data_root or Path("~/.ares/data").expanduser()
        self.research = ResearchWorkspace(research_data_dir)
        self.research_upgrades = ResearchUpgradeStore(research_data_dir)
        self.runtime_upgrades = RuntimeUpgradeManager(
            self.repl,
            (session_data_dir or data_root or Path("~/.ares/data").expanduser()) / "runtime",
        )
        # Set by the local Telegram channel at runtime. Keeping the bridge
        # unattached by default prevents a web/CLI process from sending files.
        self.telegram_channel: Any | None = None
        self._closed = False

    async def shutdown(self) -> None:
        """Clean up sources before closing the stores they depend on."""
        if self._closed:
            return
        self._closed = True
        if self._owns_vision_service and self.vision_service is not None:
            await self.vision_service.shutdown()
        self.research_upgrades.close()
        self.runtime_upgrades.close()
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

    def close(self) -> None:
        """Compatibility shutdown for synchronous callers."""
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.shutdown())
            return
        loop.create_task(self.shutdown(), name="ares-tool-executor-shutdown")

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
            "list_learning_reviews": self._list_learning_reviews,
            "review_learning": self._review_learning,
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
            "run_project_check": self._run_project_check,
            "generate_image": self._generate_image,
            "image_info": self._image_info,
            "resize_image": self._resize_image,
            "convert_image": self._convert_image,
            "crop_image": self._crop_image,
            "batch_transform_images": self._batch_transform_images,
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
            "vision_observe": self._vision_async_required,
            "vision_watch": self._vision_async_required,
            "vision_compare": self._vision_async_required,
            "vision_verify": self._vision_async_required,
            "vision_remember": self._vision_async_required,
            "vision_list_watches": self._vision_async_required,
            "vision_cancel_watch": self._vision_async_required,
            "vision_start_source": self._vision_async_required,
            "vision_stop_source": self._vision_async_required,
            "vision_stop_all_sources": self._vision_async_required,
            "vision_list_sources": self._vision_async_required,
            "vision_list_events": self._vision_async_required,
            "vision_delete_event": self._vision_async_required,
            "vision_erase_recent_events": self._vision_async_required,
            "vision_delete_memory_frame": self._vision_async_required,
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
            "list_follow_ups": self._list_follow_ups,
            "snooze_follow_up": self._snooze_follow_up,
            "dismiss_follow_up": self._dismiss_follow_up,
            "resolve_follow_up": self._resolve_follow_up,
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
        if tool_name.startswith("vision_"):
            return await self.vision_tools.dispatch(tool_name, arguments)
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
        if tool_name == "batch_transform_images":
            return await asyncio.to_thread(self.execute, tool_name, arguments)
        if tool_name == "search_files":
            return await search_files_async(
                query=arguments.get("query", ""),
                path=arguments.get("path", "."),
                name_pattern=arguments.get("name_pattern", ""),
                max_results=int(arguments.get("max_results", 20)),
            )
        # Pure filesystem, document, image, shell, and REPL handlers may block
        # on disk or subprocess I/O. Keep them off the shared event loop. DB-
        # backed handlers intentionally remain on their owning thread because
        # sqlite connections enforce thread affinity.
        from ares.multi_agent.policy import (
            FILESYSTEM_READ_TOOLS,
            FILESYSTEM_WRITE_TOOLS,
            REPL_TOOLS,
            SHELL_TOOLS,
        )

        offload = (
            set(FILESYSTEM_READ_TOOLS)
            | set(FILESYSTEM_WRITE_TOOLS)
            | set(REPL_TOOLS)
            | set(SHELL_TOOLS)
            | {"fetch_url"}
        ) - {"export_data"}
        if tool_name in offload:
            return await asyncio.to_thread(self.execute, tool_name, arguments)
        return self.execute(tool_name, arguments)

    # ── Memory tools ──────────────────────────────────────────────

    def _list_learning_reviews(self, args: dict) -> str:
        store = self.self_improvement_store
        if store is None:
            return "Error: Hermes procedural learning is unavailable."
        status = str(args.get("status") or "pending_approval").strip().casefold()
        limit = max(1, min(int(args.get("limit", 20)), 100))
        rows = store.list(status=status, limit=limit)
        return json.dumps(
            {"status": status, "count": len(rows), "learnings": rows},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _review_learning(self, args: dict) -> str:
        store = self.self_improvement_store
        if store is None:
            return "Error: Hermes procedural learning is unavailable."
        improvement_id = int(args["improvement_id"])
        decision = str(args["decision"]).strip().casefold()
        if decision not in {"approve", "reject"}:
            return "Error: decision must be approve or reject."
        existing = store.get(improvement_id)
        if existing is None:
            return f"Error: learning #{improvement_id} was not found."
        if existing.get("status") != "pending_approval":
            return f"Learning #{improvement_id} is already {existing.get('status')}."
        updated = (
            store.approve(improvement_id)
            if decision == "approve"
            else store.reject(improvement_id)
        )
        return json.dumps(
            {
                "decision": decision,
                "learning": updated,
                "message": (
                    f"Learning #{improvement_id} approved and active."
                    if decision == "approve"
                    else f"Learning #{improvement_id} rejected."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _store_memory(self, args: dict) -> str:
        content = args["content"]
        category = args.get("category", "note")
        confidence = float(args.get("confidence", 1.0))
        structured = wants_structured(args)
        rejection = memory_rejection_reason(
            content,
            category=category,
            confidence=confidence,
        )
        if rejection:
            summary = f"Memory not stored: {rejection}."
            return error_result(summary, code="memory_policy") if structured else summary
        suggestions = self.memory.suggest_merge(content, category=category)
        duplicate = next((item for item in suggestions if item["kind"] == "duplicate"), None)
        merge_mode = str(args.get("merge_mode") or "skip").strip().casefold()
        if merge_mode not in {"skip", "merge", "store"}:
            summary = "merge_mode must be skip, merge, or store"
            return error_result(summary, code="validation") if structured else summary
        if duplicate and merge_mode == "skip":
            summary = (
                f"Memory not stored: duplicate of #{duplicate['fact_id']}. "
                f"{duplicate['recommendation']}"
            )
            return structured_result(
                summary, status="conflict", data={"duplicate": duplicate},
                warnings=["No new memory was created."],
            ) if structured else summary
        if duplicate and merge_mode == "merge":
            fact_id = int(duplicate["fact_id"])
            existing = self.memory.get(fact_id) or {}
            tags = list(dict.fromkeys([*existing.get("tags", []), *(args.get("tags") or [])]))
            links = {kind: list(values) for kind, values in existing.get("links", {}).items()}
            for kind, values in (args.get("links") or {}).items():
                incoming = values if isinstance(values, list) else [values]
                links[kind] = list(dict.fromkeys([*links.get(kind, []), *(str(value) for value in incoming)]))
            self.memory.update(
                fact_id, tags=tags, links=links,
                valid_from=args.get("valid_from", existing.get("valid_from")),
                expires_at=args.get("expires_at", existing.get("expires_at")),
                project=args.get("project", existing.get("project")),
                change_summary="merged duplicate metadata",
            )
            memory = self.memory.get(fact_id) or existing
            summary = f"Merged duplicate memory into #{fact_id}."
            return structured_result(summary, data={"memory": memory, "duplicate": duplicate}) if structured else summary
        importance = (
            float(args["importance"]) if args.get("importance") is not None
            else calculate_importance(content, category)
        )
        fact_id = self.memory.store(
            content,
            category=category,
            confidence=confidence,
            importance=importance,
            source=str(args.get("source") or "conversation"),
            source_conversation_id=args.get("source_conversation_id"),
            source_message_id=args.get("source_message_id"),
            tags=args.get("tags") or [],
            valid_from=args.get("valid_from"),
            expires_at=args.get("expires_at"),
            supersedes_memory_id=args.get("supersedes_memory_id"),
            project=args.get("project"),
            links=args.get("links") or {},
        )
        contradictions: list[dict[str, Any]] = []
        for suggestion in suggestions:
            if suggestion.get("kind") != "possible_conflict":
                continue
            other_id = int(suggestion["fact_id"])
            self.memory._add_relation(fact_id, other_id, "contradiction", float(suggestion.get("confidence") or 0.5))
            self.memory._add_relation(other_id, fact_id, "contradiction", float(suggestion.get("confidence") or 0.5))
            contradictions.append(suggestion)
        if contradictions:
            self.memory.conn.commit()
        summary = f"Stored memory #{fact_id}: {content}"
        if not structured:
            return summary
        return structured_result(
            summary,
            data={"memory": self.memory.get(fact_id), "contradictions": contradictions},
            warnings=["Potential contradiction detected; both versions were preserved."] if contradictions else [],
            provenance={
                "source": str(args.get("source") or "conversation"),
                "conversation_id": args.get("source_conversation_id"),
                "message_id": args.get("source_message_id"),
            },
            metrics={"calculated_importance": importance, "duplicate_candidates": len(suggestions)},
        )

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
        structured = wants_structured(args)
        mode = str(args.get("mode") or "relevant").strip().casefold()
        filter_keys = {
            "category", "tags", "min_confidence", "min_importance", "person", "goal",
            "action", "file", "project", "source", "date_from", "date_to", "include_outdated",
        }
        if mode != "relevant" or any(key in args for key in filter_keys) or args.get("task"):
            filters = {key: args.get(key) for key in filter_keys if key in args}
            try:
                records = self.memory.search_advanced(
                    query, mode=mode, limit=limit, memory_id=args.get("memory_id"),
                    task=str(args.get("task") or ""), filters=filters,
                )
            except (TypeError, ValueError) as exc:
                return error_result(str(exc), code="validation") if structured else self._json({"ok": False, "error": str(exc)})
            summary = f"Found {len(records)} durable memory result(s) in {mode} mode."
            if structured:
                return structured_result(
                    summary, data={"mode": mode, "query": query, "results": records},
                    provenance={"sources": ["facts"]}, metrics={"result_count": len(records)},
                )
            return self._json({"ok": True, "mode": mode, "query": query, "result_count": len(records), "results": records})
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
        if structured:
            return structured_result(
                payload.get("message") or f"Found {len(records)} local recall result(s).",
                data=payload,
                provenance={"sources": sorted(sources)},
                metrics={"result_count": len(records), "source_counts": counts},
            )
        return self._json(payload)

    def _update_memory(self, args: dict) -> str:
        fact_id = int(args["fact_id"])
        structured = wants_structured(args)
        mode = str(args.get("mode") or "replace").strip().casefold()
        if mode not in {"replace", "append", "merge", "outdated"}:
            summary = "mode must be replace, append, merge, or outdated"
            return error_result(summary, code="validation") if structured else summary
        try:
            if mode == "merge":
                source_ids = [int(value) for value in (args.get("merge_memory_ids") or [])]
                if not source_ids:
                    raise ValueError("merge_memory_ids is required for merge mode")
                memory = self.memory.merge_memories(
                    fact_id, source_ids, expected_revision=args.get("expected_revision"),
                )
                summary = f"Merged {len(source_ids)} memory record(s) into #{fact_id}."
                return structured_result(summary, data={"memory": memory, "merged_ids": source_ids}) if structured else summary
            update_kwargs: dict[str, Any] = {
                "fact_text": args.get("content"),
                "category": args.get("category"),
                "confidence": float(args["confidence"]) if args.get("confidence") is not None else None,
                "importance": float(args["importance"]) if args.get("importance") is not None else None,
                "append": mode == "append",
                "mark_outdated": True if mode == "outdated" else None,
                "expected_revision": args.get("expected_revision"),
                "change_summary": mode,
            }
            for key in ("tags", "valid_from", "expires_at", "project", "links"):
                if key in args:
                    update_kwargs[key] = args[key]
            updated = self.memory.update(fact_id, **update_kwargs)
        except MemoryConflictError as exc:
            return error_result(str(exc), code="revision_conflict", status="conflict") if structured else self._json({"ok": False, "error": str(exc), "conflict": True})
        except (TypeError, ValueError) as exc:
            return error_result(str(exc), code="validation") if structured else self._json({"ok": False, "error": str(exc)})
        if not updated:
            summary = f"Memory #{fact_id} was not found."
            return error_result(summary, code="not_found", status="not_found") if structured else summary
        memory = self.memory.get(fact_id)
        summary = f"Updated memory #{fact_id}: {memory['fact_text']}"
        if not structured:
            return summary
        return structured_result(
            summary, data={"memory": memory, "history": self.memory.revision_history(fact_id)},
            metrics={"revision": memory.get("revision") if memory else None},
        )

    def _delete_memory(self, args: dict) -> str:
        fact_id = int(args["fact_id"])
        if self.memory.delete(fact_id):
            # Visual memory links own any explicitly retained event artifact.
            # Removing the ordinary memory must clean those up as well.
            with suppress(Exception):
                self.vision_service.forget_memory_link(fact_id)
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
            "phone_hint": person.get("phone_hint") or "",
            "email_hint": person.get("email_hint") or "",
            "important_dates": dict(person.get("important_dates") or {}),
            "notes": person.get("notes") or "",
            "pronouns": person.get("pronouns") or "",
            "preferred_address": person.get("preferred_address") or "",
            "timezone": person.get("timezone") or "",
            "communication_preferences": dict(person.get("communication_preferences") or {}),
            "preferred_contact_method": person.get("preferred_contact_method") or "",
            "organization": person.get("organization") or "",
            "role": person.get("role") or "",
            "interests": list(person.get("interests") or []),
            "reminder_preferences": dict(person.get("reminder_preferences") or {}),
            "links": dict(person.get("links") or {}),
            "timeline": list(person.get("timeline") or []),
            "last_referenced_at": person.get("last_referenced_at"),
            "last_contacted_at": person.get("last_contacted_at"),
            "last_contacted_via": person.get("last_contacted_via"),
            "created_at": person.get("created_at"),
            "updated_at": person.get("updated_at"),
            "source": person.get("source", "manual"),
            "revision": person.get("revision", 1),
            **({"match_score": person.get("match_score"), "match_reason": person.get("match_reason"), "recommended_channel": person.get("recommended_channel")} if "match_score" in person else {}),
        }

    def _people_unavailable(self) -> str:
        return self._json({"ok": False, "error": "People store is unavailable because Ares has no local data path."})

    def _remember_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        structured = wants_structured(args)
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
                pronouns=args.get("pronouns", ""),
                preferred_address=args.get("preferred_address", ""),
                timezone=args.get("timezone", ""),
                communication_preferences=args.get("communication_preferences") or {},
                preferred_contact_method=args.get("preferred_contact_method", ""),
                organization=args.get("organization", ""),
                role=args.get("role", ""),
                interests=args.get("interests") or [],
                reminder_preferences=args.get("reminder_preferences") or {},
                links=args.get("links") or {},
                timeline=args.get("timeline") or [],
            )
        except (ValueError, PersonConflictError) as exc:
            return error_result(str(exc), code="person_conflict" if isinstance(exc, PersonConflictError) else "validation") if structured else self._json({"ok": False, "error": str(exc)})
        payload = self._person_view(person)
        if structured:
            return structured_result(
                f"Remembered {payload['canonical_name']} as person #{payload['person_id']}.",
                data={"action": "remembered", "person": payload},
                provenance={"source": payload.get("source"), "explicit_tool_call": True},
            )
        return self._json({"ok": True, "action": "remembered", "person": payload})

    def _search_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        structured = wants_structured(args)
        include_sensitive = bool(args.get("include_sensitive", True))
        try:
            people = self.people_store.search_advanced(
                args.get("query", ""), limit=int(args.get("limit", 5)),
                relation=str(args.get("relation") or ""), channel=str(args.get("channel") or ""),
                purpose=str(args.get("purpose") or ""), include_sensitive=include_sensitive,
            )
        except ValueError as exc:
            return error_result(str(exc), code="validation") if structured else self._json({"ok": False, "error": str(exc)})
        views = [self._person_view(person) for person in people]
        if structured:
            ambiguous = len(views) > 1 and abs(float(views[0].get("match_score") or 0) - float(views[1].get("match_score") or 0)) < 0.08
            return structured_result(
                f"Found {len(views)} matching people record(s).",
                data={"people": views, "ambiguous": ambiguous},
                warnings=["Multiple similarly ranked people require disambiguation before an external action."] if ambiguous else [],
                metrics={"result_count": len(views)},
            )
        return self._json({"ok": True, "people": views})

    def _update_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        structured = wants_structured(args)
        person_id = int(args.get("person_id", 0))
        mode = str(args.get("mode") or "replace").strip().casefold()
        if mode not in {"replace", "aliases", "append_note", "merge"}:
            summary = "mode must be replace, aliases, append_note, or merge"
            return error_result(summary, code="validation") if structured else self._json({"ok": False, "error": summary})
        if mode == "merge":
            try:
                duplicate_id = int(args.get("merge_person_id") or 0)
                if not duplicate_id:
                    raise ValueError("merge_person_id is required for merge mode")
                person = self.people_store.merge_people(
                    person_id, duplicate_id, expected_revision=args.get("expected_revision"),
                )
            except (TypeError, ValueError, PersonConflictError) as exc:
                code = "person_conflict" if isinstance(exc, PersonConflictError) else "validation"
                return error_result(str(exc), code=code, status="conflict" if isinstance(exc, PersonConflictError) else "failed") if structured else self._json({"ok": False, "error": str(exc)})
            view = self._person_view(person)
            summary = f"Merged person #{duplicate_id} into #{person_id}."
            return structured_result(summary, data={"action": "merged", "person": view, "merged_person_id": duplicate_id}) if structured else self._json({"ok": True, "action": "merged", "person": view})

        updates = {
            key: args[key]
            for key in (
                "canonical_name", "aliases", "relation", "phone", "email", "important_dates", "notes",
                "source", "confidence", "pronouns", "preferred_address", "timezone",
                "communication_preferences", "preferred_contact_method", "organization", "role",
                "interests", "reminder_preferences", "links", "timeline",
            )
            if key in args
        }
        existing = self.people_store.get(person_id, include_sensitive=True)
        if existing is None:
            summary = "Person not found."
            return error_result(summary, code="not_found", status="not_found") if structured else self._json({"ok": False, "error": summary})
        if mode == "aliases":
            aliases = list(existing.get("aliases") or [])
            aliases.extend(str(value) for value in (args.get("add_aliases") or []))
            remove = {normalize_reference(value) for value in (args.get("remove_aliases") or [])}
            updates["aliases"] = [value for value in dict.fromkeys(aliases) if normalize_reference(value) not in remove]
        if mode == "append_note":
            addition = str(args.get("append_note") or "").strip()
            if not addition:
                summary = "append_note is required for append_note mode"
                return error_result(summary, code="validation") if structured else self._json({"ok": False, "error": summary})
            updates["notes"] = "\n".join(part for part in (str(existing.get("notes") or "").rstrip(), addition) if part)
        if not updates:
            summary = "Provide at least one person field to update."
            return error_result(summary, code="validation") if structured else self._json({"ok": False, "error": summary})
        try:
            person = self.people_store.update(
                person_id,
                expected_revision=args.get("expected_revision"),
                **updates,
            )
        except (ValueError, PersonConflictError) as exc:
            code = "person_conflict" if isinstance(exc, PersonConflictError) else "validation"
            return error_result(str(exc), code=code, status="conflict" if isinstance(exc, PersonConflictError) else "failed") if structured else self._json({"ok": False, "error": str(exc)})
        if person is None:
            summary = "Person not found."
            return error_result(summary, code="not_found", status="not_found") if structured else self._json({"ok": False, "error": summary})
        view = self._person_view(person)
        if structured:
            return structured_result(
                f"Updated {view['canonical_name']} (revision {view['revision']}).",
                data={"action": "updated", "person": view, "history": self.people_store.revision_history(person_id)},
                metrics={"revision": view["revision"]},
            )
        return self._json({"ok": True, "action": "updated", "person": view})

    def _forget_person(self, args: dict) -> str:
        if self.people_store is None:
            return self._people_unavailable()
        try:
            deleted = self.people_store.delete(int(args.get("person_id", 0)), expected_revision=args.get("expected_revision"))
        except (ValueError, PersonConflictError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        return self._json({"ok": bool(deleted), "action": "forgotten" if deleted else "not_found"})

    def _search_actions(self, args: dict) -> str:
        if self.action_ledger is None:
            summary = "Action ledger is unavailable because Ares has no local data path."
            return error_result(summary, code="unavailable") if wants_structured(args) else self._json({"ok": False, "error": summary})
        advanced_keys = {
            "action_types", "tags", "tag_match", "target", "tool_name", "task_id", "session_id",
            "until", "cursor", "sort", "timeline_bucket", "chain_gap_seconds", "response_format",
        }
        advanced = any(key in args for key in advanced_keys)
        if advanced:
            filters = {
                key: args[key]
                for key in (
                    "action_types", "tags", "tag_match", "target", "tool_name", "task_id",
                    "session_id", "since", "until", "limit", "cursor", "sort",
                )
                if key in args
            }
            try:
                projection = project_action_history(
                    self.action_ledger.list_all(),
                    query=str(args.get("query") or ""),
                    filters=filters,
                    timeline_bucket=str(args.get("timeline_bucket") or "day"),
                    chain_gap_seconds=int(args.get("chain_gap_seconds") or 30 * 60),
                )
            except (UpgradeValidationError, TypeError, ValueError) as exc:
                return error_result(str(exc), code="action_history")
            query = projection["query"]
            summary = projection["summary"]
            return structured_result(
                f"Found {query['total']} matching local action records.",
                data=projection,
                warnings=list(query.get("warnings") or ()),
                provenance={"source": "local_action_ledger"},
                metrics={
                    "total": query["total"],
                    "page_size": len(query["items"]),
                    "chain_count": projection["chains"]["total"],
                    "action_types": summary["action_types"],
                },
            )
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

    def _follow_up_tools_unavailable(self) -> str:
        return self._json({
            "ok": False,
            "error": "Follow-up store is unavailable because Ares has no local data path.",
        })

    def _list_follow_ups(self, args: dict) -> str:
        if self.follow_up_store is None:
            return self._follow_up_tools_unavailable()
        return self._json({
            "ok": True,
            "follow_ups": self.follow_up_store.list_open(limit=int(args.get("limit", 20))),
        })

    def _snooze_follow_up(self, args: dict) -> str:
        if self.follow_up_store is None:
            return self._follow_up_tools_unavailable()
        eligible_at = args.get("until") or future_utc(int(args.get("hours", 24)))
        follow_up = self.follow_up_store.snooze(
            str(args.get("follow_up_id") or ""),
            eligible_at=str(eligible_at),
        )
        return self._json({
            "ok": follow_up is not None,
            "follow_up": follow_up,
            "error": None if follow_up is not None else "Open follow-up not found.",
        })

    def _dismiss_follow_up(self, args: dict) -> str:
        if self.follow_up_store is None:
            return self._follow_up_tools_unavailable()
        follow_up = self.follow_up_store.resolve(
            str(args.get("follow_up_id") or ""),
            status="dismissed",
            resolution=str(args.get("reason") or "Dismissed by the user."),
        )
        return self._json({
            "ok": follow_up is not None,
            "follow_up": follow_up,
            "error": None if follow_up is not None else "Open follow-up not found.",
        })

    def _resolve_follow_up(self, args: dict) -> str:
        if self.follow_up_store is None:
            return self._follow_up_tools_unavailable()
        follow_up = self.follow_up_store.resolve(
            str(args.get("follow_up_id") or ""),
            status=str(args.get("status") or "resolved"),
            resolution=str(args.get("resolution") or "Resolved by the user."),
        )
        return self._json({
            "ok": follow_up is not None,
            "follow_up": follow_up,
            "error": None if follow_up is not None else "Open follow-up not found.",
        })

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
        if lowered == "batch_transform_images":
            return ("image_edited", str(args.get("output_dir") or "image batch"), "Edited an image batch.", ["image", "batch"])
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
        if lowered in {"snooze_follow_up", "dismiss_follow_up", "resolve_follow_up"}:
            action_type = {
                "snooze_follow_up": "follow_up_snoozed",
                "dismiss_follow_up": "follow_up_dismissed",
                "resolve_follow_up": "follow_up_resolved",
            }[lowered]
            return (
                action_type,
                f"follow-up {args.get('follow_up_id', '')}",
                f"{action_type.replace('_', ' ').capitalize()}.",
                ["follow-up"],
            )
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
        if wants_structured(args):
            requested = args.get("requested_skills") or ()
            if isinstance(requested, str):
                requested = [requested]
            if not isinstance(requested, (list, tuple)):
                return error_result("requested_skills must be an array", code="invalid_argument")
            try:
                ranking = rank_skills_for_delegation(
                    str(args.get("task") or args.get("query") or ""),
                    skills,
                    limit=max(0, min(int(args.get("recommendation_limit") or 3), 12)),
                    explicitly_requested=[str(item) for item in requested],
                )
            except (DelegationUpgradeError, TypeError, ValueError) as exc:
                return error_result(str(exc), code="skill_ranking_error")
            catalog = [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "category": skill.category,
                    "version": skill.version,
                    "dependencies": list(skill.metadata.get("dependencies") or skill.metadata.get("requires") or ()),
                    "required_tools": list(skill.metadata.get("required_tools") or skill.metadata.get("required-tools") or ()),
                    "examples": list(skill.examples),
                    "test_commands": list(skill.test_commands),
                    "lint_messages": list(skill.lint_messages),
                    "path": str(skill.path),
                }
                for skill in skills
            ]
            return structured_result(
                f"Found {len(skills)} matching skills.",
                data={
                    "skills": catalog,
                    "recommendations": ranking.as_dict(),
                    "categories": manager.list_categories(),
                },
                warnings=list(ranking.warnings),
                next_actions=[
                    {"tool": "load_skill", "arguments": {"name": item.name}}
                    for item in ranking.selected
                ],
                provenance={"source": "local_skill_manager"},
                metrics={"skill_count": len(skills), "recommended_count": len(ranking.selected)},
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
        """Create a versioned skill, with an opt-in preview/lint activation path."""
        manager = self._skill_manager()
        structured = wants_structured(args)
        name = str(args["name"])
        category = str(args.get("category", "general") or "general")
        content = str(args["content"])
        advanced = structured or any(
            key in args for key in (
                "version", "description", "required_tools", "examples", "source_action_ids",
                "preview", "activate", "test_before_activation",
            )
        )
        required_tools = args.get("required_tools") or ()
        examples = args.get("examples") or ()
        source_action_ids = args.get("source_action_ids") or ()
        if isinstance(required_tools, str):
            required_tools = [required_tools]
        if isinstance(examples, str):
            examples = [examples]
        if isinstance(source_action_ids, (str, int)):
            source_action_ids = [source_action_ids]
        if not isinstance(required_tools, (list, tuple)) or not isinstance(examples, (list, tuple)) or not isinstance(source_action_ids, (list, tuple)):
            message = "required_tools, examples, and source_action_ids must be arrays"
            return error_result(message, code="invalid_argument") if structured else message

        source_actions: list[dict[str, Any]] = []
        warnings: list[str] = []
        requested_ids = {str(value) for value in source_action_ids if str(value).strip()}
        if requested_ids:
            if self.action_ledger is None:
                warnings.append("Action provenance is unavailable, so source_action_ids could not be verified.")
            else:
                source_actions = [
                    action for action in self.action_ledger.list_all()
                    if str(action.get("action_id")) in requested_ids
                ]
                found_ids = {str(action.get("action_id")) for action in source_actions}
                missing = sorted(requested_ids - found_ids)
                if missing:
                    warnings.append("No local action record found for: " + ", ".join(missing))

        generated_content = content
        if advanced and not generated_content.lstrip().startswith("---"):
            description = str(args.get("description") or f"Reusable {category} workflow for {name}.")
            version = str(args.get("version") or "1.0.0")
            frontmatter = {
                "description": description,
                "category": category,
                "version": version,
                "required_tools": [str(item) for item in required_tools if str(item).strip()],
                "examples": list(examples),
                "source_action_ids": [int(item) if str(item).isdigit() else str(item) for item in source_action_ids],
            }
            generated_content = "---\n" + "\n".join(
                f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items()
            ) + "\n---\n\n" + generated_content
        if source_actions:
            generated_content += "\n\n## Provenance\n\nDerived from successful local action summaries:\n" + "\n".join(
                f"- [{action.get('action_id')}] {action.get('tool_name')}: {action.get('summary')}"
                for action in source_actions
            )

        lint_messages: list[str] = []
        if advanced and bool(args.get("test_before_activation", False)):
            with tempfile.TemporaryDirectory(prefix="ares-skill-check-") as directory:
                candidate = Path(directory) / "SKILL.md"
                candidate.write_text(manager._ensure_frontmatter(name, generated_content, category), encoding="utf-8")
                lint_messages = SkillManager.lint_skill_file(candidate)
            if lint_messages and bool(args.get("activate", True)):
                message = "Skill activation blocked by validation: " + "; ".join(lint_messages)
                return structured_result(
                    message,
                    ok=False,
                    status="failed",
                    data={"name": name, "category": category, "lint_messages": lint_messages},
                    warnings=warnings,
                    next_actions=[{"tool": "create_skill", "arguments": {**args, "preview": True}}],
                    provenance={"source": "local_skill_manager"},
                ) if structured else message

        preview = advanced and (bool(args.get("preview", False)) or not bool(args.get("activate", True)))
        if preview:
            return structured_result(
                f"Skill '{name}' is ready for review; no files were written.",
                status="preview",
                data={
                    "name": name,
                    "category": category,
                    "content": generated_content,
                    "lint_messages": lint_messages,
                    "source_actions": source_actions,
                },
                warnings=warnings,
                next_actions=[{"tool": "create_skill", "arguments": {**args, "preview": False, "activate": True}}],
                provenance={"source": "local_skill_manager"},
            )

        skill = manager.create_skill(
            name=name,
            content=generated_content,
            category=category,
        )
        if structured:
            return structured_result(
                f"Created skill '{skill.name}' in category '{skill.category}'.",
                data={
                    "skill": {
                        "name": skill.name,
                        "category": skill.category,
                        "version": skill.version,
                        "path": str(skill.path),
                        "required_tools": list(skill.metadata.get("required_tools") or skill.metadata.get("required-tools") or ()),
                        "examples": list(skill.examples),
                        "test_commands": list(skill.test_commands),
                        "lint_messages": list(skill.lint_messages),
                    },
                    "source_actions": source_actions,
                },
                warnings=[*warnings, *skill.lint_messages],
                next_actions=[{"tool": "load_skill", "arguments": {"name": skill.name}}],
                provenance={"source": "local_skill_manager", "path": str(skill.path)},
            )
        return f"Created skill '{skill.name}' in category '{skill.category}' at {skill.path}."

    @staticmethod
    def _marketplace_async_required(_args: dict) -> str:
        return "Marketplace registry operations require the async agent execution path."

    @staticmethod
    def _watcher_async_required(_args: dict) -> str:
        return "Running a watcher immediately requires the async Ares runtime; start Ares with --all."

    @staticmethod
    def _vision_async_required(_args: dict) -> str:
        return "Vision observation requires the async Ares runtime."

    def _marketplace_config(self) -> AppConfig:
        if self.config is None:
            raise RuntimeError("Marketplace configuration is unavailable.")
        return self.config

    def _marketplace_skills_dir(self) -> Path:
        config = self._marketplace_config()
        dirs = list(config.skill_dirs or [])
        return Path(dirs[0] if dirs else "~/.ares/skills").expanduser()

    @staticmethod
    def _marketplace_strings(value: Any) -> list[str]:
        """Normalize a bounded list of marketplace identifiers."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        values: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in values:
                values.append(text)
        return values

    @staticmethod
    def _marketplace_requested_mode(args: dict, *, allowed: set[str], default: str) -> str:
        mode = str(args.get("mode") or default).strip().casefold()
        if mode not in allowed:
            raise ValueError("mode must be one of " + ", ".join(sorted(allowed)))
        return mode

    @staticmethod
    def _marketplace_advanced(args: dict, structured: bool, fields: set[str]) -> bool:
        return structured or any(field in args for field in fields)

    @staticmethod
    def _marketplace_error(
        message: str,
        *,
        structured: bool,
        code: str,
        data: dict[str, Any] | None = None,
        status: str = "failed",
    ) -> str:
        if structured:
            return structured_result(
                message,
                ok=False,
                status=status,
                data=data or {},
                errors=[{"code": code, "message": message}],
            )
        return message

    def _marketplace_dependency_status(self, dependencies: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """Project dependency availability without executing or installing anything."""
        config = self._marketplace_config()
        configured_servers = {
            str(server.get("name") or "").casefold()
            for server in config.mcp_servers
            if isinstance(server, dict)
        }
        try:
            from ares.tools.definitions import get_tool_definitions

            known_tools = {
                str(tool.get("function", {}).get("name") or "").casefold()
                for tool in get_tool_definitions()
            }
            known_tools.update(
                str(tool.get("function", {}).get("name") or "").casefold()
                for tool in (getattr(self.mcp_manager, "tool_definitions", []) or [])
            )
        except Exception:
            # Tool discovery is advisory. A failure here cannot make a safe
            # marketplace preview unavailable.
            known_tools = set()
        manager = self._skill_manager()
        records: list[dict[str, Any]] = []
        missing_required: list[str] = []
        for dependency in dependencies:
            if isinstance(dependency, dict):
                kind = str(dependency.get("type") or "mcp_server")
                name = str(dependency.get("name") or "")
                required = bool(dependency.get("required", True))
                auto_install = bool(dependency.get("auto_install", False))
            else:
                kind = str(getattr(dependency, "type", "mcp_server"))
                name = str(getattr(dependency, "name", ""))
                required = bool(getattr(dependency, "required", True))
                auto_install = bool(getattr(dependency, "auto_install", False))
            kind = kind.casefold()
            name = name.strip()
            if not name:
                continue
            if kind == "mcp_server":
                available = name.casefold() in configured_servers
            elif kind == "tool":
                available = name.casefold() in known_tools
            elif kind == "skill":
                available = manager.get_skill(name) is not None
            else:
                available = False
            record = {
                "type": kind,
                "name": name,
                "required": required,
                "auto_install": auto_install,
                "available": available,
            }
            records.append(record)
            if required and not available:
                missing_required.append(f"{kind}:{name}")
        return records, missing_required

    def _installed_marketplace_skill(self, detail: Any) -> dict[str, Any]:
        """Find a local marketplace installation using its provenance record."""
        registry = str(getattr(detail, "registry", "") or "").casefold()
        slug = str(getattr(detail, "slug", "") or "").casefold()
        reference = str(getattr(detail, "reference", "") or "").casefold()
        for skill in self._skill_manager().list_all():
            record = marketplace_record(skill)
            if not record:
                continue
            record_registry = str(record.get("registry") or "").casefold()
            record_slug = str(record.get("slug") or "").casefold()
            if record_registry != registry or record_slug not in {slug, reference}:
                continue
            return {
                "installed": True,
                "name": skill.name,
                "version": skill.version,
                "path": str(skill.path),
                "installed_at": record.get("installed_at"),
                "pinned_version": record.get("pinned_version"),
                "source_version": record.get("version"),
            }
        return {"installed": False}

    @staticmethod
    def _skill_risk_summary(item: Any, *, missing_dependencies: list[str] | None = None) -> dict[str, Any]:
        """Explain review signals from registry metadata without inventing trust."""
        signals: list[dict[str, str]] = []
        suspicious = bool(getattr(item, "suspicious", False))
        security_status = str(getattr(item, "security_status", "") or "").strip()
        canonical_url = str(getattr(item, "canonical_url", "") or "").strip()
        version = str(getattr(item, "version", "") or "").strip()
        if suspicious:
            signals.append({"level": "blocked", "code": "registry_flagged", "message": "The registry flagged this skill as suspicious."})
        if not security_status or security_status.casefold() in {"unknown", "unreviewed"}:
            signals.append({"level": "review", "code": "security_status_unknown", "message": "The registry did not publish a clear security verdict."})
        if not canonical_url:
            signals.append({"level": "review", "code": "source_url_missing", "message": "The registry did not publish a canonical source URL."})
        if not version or version.casefold() == "unknown":
            signals.append({"level": "review", "code": "version_unknown", "message": "The registry did not publish a concrete version."})
        if missing_dependencies:
            signals.append({
                "level": "review",
                "code": "missing_dependencies",
                "message": "Required local dependencies are not currently configured.",
            })
        level = "review" if signals else "low"
        return {
            "level": level,
            "suspicious": suspicious,
            "security_status": security_status or "unknown",
            "signals": signals,
        }

    async def _marketplace_skill_versions(
        self, client: Any, slug: str, registry: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        getter = getattr(client, "get_versions", None)
        if not callable(getter):
            return [], "This registry client does not expose version history."
        try:
            versions = await getter(slug, registry)
        except (RuntimeError, ValueError) as exc:
            return [], str(exc)
        return [
            {
                "version": str(getattr(item, "version", "unknown")),
                "created_at": str(getattr(item, "created_at", "") or ""),
                "changelog": str(getattr(item, "changelog", "") or ""),
                "security_status": str(getattr(item, "security_status", "") or ""),
            }
            for item in versions
        ], None

    @staticmethod
    def _skill_result_projection(item: Any) -> dict[str, Any]:
        return {
            "reference": str(getattr(item, "reference", "") or ""),
            "name": str(getattr(item, "name", "") or ""),
            "description": str(getattr(item, "description", "") or ""),
            "version": str(getattr(item, "version", "") or ""),
            "registry": str(getattr(item, "registry", "") or ""),
            "canonical_url": str(getattr(item, "canonical_url", "") or ""),
            "suspicious": bool(getattr(item, "suspicious", False)),
            "stars": getattr(item, "stars", None),
            "downloads": getattr(item, "downloads", None),
        }

    @staticmethod
    def _mcp_result_projection(item: Any) -> dict[str, Any]:
        return {
            "name": str(getattr(item, "name", "") or ""),
            "title": str(getattr(item, "title", "") or ""),
            "description": str(getattr(item, "description", "") or ""),
            "version": str(getattr(item, "version", "") or ""),
            "registry": str(getattr(item, "registry", "") or ""),
            "repository": str(getattr(item, "repository", "") or ""),
            "canonical_url": str(getattr(item, "canonical_url", "") or ""),
            "verified": bool(getattr(item, "verified", False)),
            "stars": getattr(item, "stars", None),
            "downloads": getattr(item, "downloads", None),
        }

    @staticmethod
    def _mcp_plan_projection(payload: dict[str, Any]) -> dict[str, Any]:
        """Expose a reviewable config plan without ever exposing env values."""
        return {
            "name": str(payload.get("name") or ""),
            "transport": str(payload.get("transport") or ""),
            "command": str(payload.get("command") or ""),
            "args": [str(item) for item in (payload.get("args") or [])],
            "server_url": str(payload.get("server_url") or ""),
            "required_environment": sorted(str(key) for key in (payload.get("env") or {})),
            "marketplace": dict(payload.get("marketplace") or {}),
        }

    def _mcp_risk_summary(self, payload: dict[str, Any], *, verified: bool, existing: bool) -> dict[str, Any]:
        signals: list[dict[str, str]] = []
        if not verified:
            signals.append({"level": "review", "code": "unverified_listing", "message": "The registry did not mark this MCP listing as verified."})
        if payload.get("command"):
            signals.append({"level": "review", "code": "local_process", "message": "This plan would start a local process after it is explicitly enabled and connected."})
        if payload.get("env"):
            signals.append({"level": "review", "code": "required_environment", "message": "The plan requires locally supplied environment values; registry values are never imported."})
        if existing:
            signals.append({"level": "review", "code": "existing_configuration", "message": "A configuration with this name already exists and needs an explicit update plan."})
        return {"level": "review" if signals else "low", "signals": signals, "verified": verified}

    async def _search_skill_marketplace(self, args: dict) -> str:
        try:
            structured = wants_structured(args)
            mode = self._marketplace_requested_mode(
                args, allowed={"search", "inspect", "compare"}, default="search",
            )
        except ValueError as exc:
            return error_result(str(exc), code="invalid_argument")
        advanced = self._marketplace_advanced(
            args,
            structured,
            {"mode", "compare_slugs", "include_details", "include_versions", "max_results"},
        )
        comparison_targets = self._marketplace_strings(args.get("compare_slugs"))
        query = str(args.get("query") or "").strip() or (comparison_targets[0] if comparison_targets else "")
        config = self._marketplace_config()
        client = SkillRegistryClient(config.skill_registries)
        try:
            results = await client.search(query, args.get("registry"))
        except (ValueError, RuntimeError) as exc:
            return self._marketplace_error(
                f"Marketplace search failed: {exc}", structured=structured, code="marketplace_search_failed",
            )
        if not advanced:
            if not results:
                errors = "; ".join(f"{name}: {message}" for name, message in client.last_errors.items())
                return "No marketplace skills found." + (f" Registry notes: {errors}" if errors else "")
            lines = [f"Marketplace skills ({len(results)}):"]
            for item in results[:12]:
                lines.append(
                    f"- {item.reference} [{item.registry}, {item.version}] — {item.description or 'No description.'}"
                )
            return "\n".join(lines)
        if not results:
            errors = "; ".join(f"{name}: {message}" for name, message in client.last_errors.items())
            return structured_result(
                "No marketplace skills found.",
                status="not_found",
                warnings=[errors] if errors else [],
                provenance={"source": "configured_skill_registries"},
                metrics={"result_count": 0},
            )

        try:
            max_results = max(1, min(int(args.get("max_results") or 12), 25))
        except (TypeError, ValueError):
            return self._marketplace_error("max_results must be an integer", structured=structured, code="invalid_argument")
        include_details = bool(args.get("include_details")) or mode in {"inspect", "compare"} or bool(comparison_targets)
        include_versions = bool(args.get("include_versions"))
        compare_keys = {item.casefold() for item in comparison_targets}
        if mode == "compare" and not compare_keys:
            compare_keys = {item.reference.casefold() for item in results[:2]}
        warnings: list[str] = []
        rows: list[dict[str, Any]] = []
        for item in results[:max_results]:
            is_compare_target = not compare_keys or item.reference.casefold() in compare_keys or item.slug.casefold() in compare_keys
            detail = None
            versions: list[dict[str, Any]] = []
            if include_details and (mode != "compare" or is_compare_target):
                try:
                    detail = await client.get_skill(item.reference, item.registry)
                except (RuntimeError, ValueError) as exc:
                    warnings.append(f"Could not inspect {item.reference}: {exc}")
            if include_versions and (mode != "compare" or is_compare_target):
                versions, version_warning = await self._marketplace_skill_versions(client, item.reference, item.registry)
                if version_warning:
                    warnings.append(f"{item.reference}: {version_warning}")
            dependencies = list(getattr(detail, "dependencies", []) or []) if detail is not None else []
            dependency_status, missing = self._marketplace_dependency_status(dependencies)
            subject = detail or item
            row = self._skill_result_projection(item)
            row.update(
                {
                    "risk": self._skill_risk_summary(subject, missing_dependencies=missing),
                    "permission_summary": {
                        "registry_access": "read-only metadata lookup",
                        "filesystem": "none during search",
                        "execution": "none",
                    },
                    "dependencies": dependency_status,
                    "compatibility": {
                        "known": detail is not None,
                        "missing_required": missing,
                        "installed": self._installed_marketplace_skill(subject),
                    },
                }
            )
            if detail is not None:
                row["detail"] = {
                    "files": [str(file) for file in (getattr(detail, "files", []) or [])],
                    "security_status": str(getattr(detail, "security_status", "") or "unknown"),
                    "owner": str(getattr(detail, "owner", "") or ""),
                }
            if versions:
                row["versions"] = versions
            rows.append(row)
        comparison = [
            row for row in rows
            if row["reference"].casefold() in compare_keys
            or row["reference"].lstrip("@").casefold() in compare_keys
        ] if compare_keys else []
        registry_errors = getattr(client, "last_errors", {}) or {}
        warnings.extend(f"{name}: {message}" for name, message in registry_errors.items())
        return structured_result(
            f"Found {len(results)} marketplace skill{'s' if len(results) != 1 else ''}.",
            status="partial" if registry_errors else "completed",
            data={"mode": mode, "results": rows, "comparison": comparison},
            warnings=warnings,
            next_actions=[
                {"tool": "install_marketplace_skill", "arguments": {"slug": row["reference"], "registry": row["registry"], "preview": True, "response_format": "structured"}}
                for row in rows[:3]
            ],
            provenance={"source": "configured_skill_registries", "registry": args.get("registry") or "all"},
            metrics={"result_count": len(results), "returned_count": len(rows), "comparison_count": len(comparison)},
        )

    async def _install_marketplace_skill(self, args: dict) -> str:
        try:
            structured = wants_structured(args)
            mode = self._marketplace_requested_mode(
                args, allowed={"install", "update", "preview"}, default="install",
            )
        except ValueError as exc:
            return error_result(str(exc), code="invalid_argument")
        preview = bool(args.get("preview", False)) or mode == "preview"
        advanced = self._marketplace_advanced(
            args,
            structured,
            {"mode", "version", "pin_version", "preview", "sandbox_validate", "replace", "expected_version"},
        )
        pass
        config = self._marketplace_config()
        slug = str(args.get("slug") or "").strip()
        client = SkillRegistryClient(config.skill_registries)
        try:
            detail = await client.get_skill(slug, args.get("registry"))
        except (RuntimeError, ValueError) as exc:
            return self._marketplace_error(
                f"Skill lookup failed: {exc}", structured=structured, code="marketplace_lookup_failed",
            )
        if detail is None:
            return self._marketplace_error(
                f"Skill '{slug}' was not found in configured registries.", structured=structured, code="not_found", status="not_found",
            )
        requested_version = str(args.get("pin_version") or args.get("version") or "").strip()
        warnings: list[str] = []
        versions: list[dict[str, Any]] = []
        if requested_version or bool(args.get("include_versions", False)):
            versions, version_warning = await self._marketplace_skill_versions(client, detail.reference, detail.registry)
            if version_warning:
                warnings.append(version_warning)
            elif requested_version and requested_version not in {entry["version"] for entry in versions}:
                message = f"Version '{requested_version}' is not published for skill '{detail.reference}'."
                return self._marketplace_error(message, structured=structured, code="version_not_found", data={"available_versions": versions})
        selected_version = requested_version or detail.version
        dependency_status, missing = self._marketplace_dependency_status(list(detail.dependencies or []))
        installed = self._installed_marketplace_skill(detail)
        expected_version = str(args.get("expected_version") or "").strip()
        if expected_version and installed.get("installed") and str(installed.get("source_version") or installed.get("version")) != expected_version:
            message = f"Installed skill version does not match expected_version '{expected_version}'."
            return self._marketplace_error(
                message,
                structured=structured,
                code="version_conflict",
                data={"installed": installed, "expected_version": expected_version},
                status="conflict",
            )
        risk = self._skill_risk_summary(detail, missing_dependencies=missing)
        replace = bool(args.get("replace", False)) or mode == "update"
        plan = {
            "reference": detail.reference,
            "registry": detail.registry,
            "selected_version": selected_version,
            "pinned_version": str(args.get("pin_version") or "") or None,
            "replace_existing": replace,
            "installed": installed,
            "dependencies": dependency_status,
            "missing_required_dependencies": missing,
            "risk": risk,
            "permission_summary": {
                "registry_access": "hosted instruction archive download from the selected configured registry",
                "filesystem": "temporary sandbox only" if preview else "local skill directory after explicit confirmation",
                "execution": "none; downloaded skills are never executed during install",
            },
            "available_versions": versions,
            "sandbox": {"requested": bool(args.get("sandbox_validate", False)), "performed": False},
        }
        if preview:
            if bool(args.get("sandbox_validate", False)):
                try:
                    archive = await client.download(detail.reference, selected_version, detail.registry)
                except (RuntimeError, ValueError) as exc:
                    archive = None
                    warnings.append(f"Sandbox validation download failed: {exc}")
                if archive is None:
                    warnings.append("Sandbox validation could not obtain a safe hosted ZIP archive.")
                else:
                    try:
                        with tempfile.TemporaryDirectory(prefix="ares-skill-marketplace-preview-") as directory:
                            validated = SafeSkillInstaller(Path(directory) / "skills").install(
                                archive,
                                provenance={"registry": detail.registry, "slug": detail.reference, "version": selected_version},
                            )
                        plan["sandbox"] = {
                            "requested": True,
                            "performed": True,
                            "valid": True,
                            "skill_name": validated.skill.name,
                            "files": [str(path.name) for path in validated.skill.files],
                        }
                    except (OSError, SkillValidationError) as exc:
                        plan["sandbox"] = {"requested": True, "performed": True, "valid": False, "error": str(exc)}
                        warnings.append("Sandbox validation rejected the archive.")
            summary = f"Install preview for skill '{detail.reference}' is ready; no files were written."
            if structured:
                return structured_result(
                    summary,
                    status="preview",
                    data={"plan": plan},
                    warnings=warnings,
                    next_actions=[
                        {"tool": "install_marketplace_skill", "arguments": {**args, "preview": False, "mode": "install", "confirm": True, "response_format": "structured"}}
                    ],
                    provenance={"registry": detail.registry, "reference": detail.reference},
                    metrics={"dependency_count": len(dependency_status), "missing_required_count": len(missing)},
                )
            return "PREVIEW: " + summary
        pass
        try:
            archive = await client.download(detail.reference, selected_version, detail.registry)
        except (RuntimeError, ValueError) as exc:
            return self._marketplace_error(
                f"Download failed: {exc}", structured=structured, code="marketplace_download_failed", data={"plan": plan},
            )
        if archive is None:
            return self._marketplace_error(
                "The registry did not provide a hosted ZIP archive.",
                structured=structured,
                code="archive_unavailable",
                data={"plan": plan},
            )
        if bool(args.get("sandbox_validate", False)):
            try:
                with tempfile.TemporaryDirectory(prefix="ares-skill-marketplace-validate-") as directory:
                    validated = SafeSkillInstaller(Path(directory) / "skills").install(
                        archive,
                        provenance={"registry": detail.registry, "slug": detail.reference, "version": selected_version},
                    )
                plan["sandbox"] = {
                    "requested": True,
                    "performed": True,
                    "valid": True,
                    "skill_name": validated.skill.name,
                }
            except (OSError, SkillValidationError) as exc:
                plan["sandbox"] = {"requested": True, "performed": True, "valid": False, "error": str(exc)}
                return self._marketplace_error(
                    f"Sandbox validation rejected the archive: {exc}",
                    structured=structured,
                    code="sandbox_validation_failed",
                    data={"plan": plan},
                )
        try:
            installation = SafeSkillInstaller(self._marketplace_skills_dir()).install(
                archive,
                provenance={
                    "registry": detail.registry,
                    "slug": detail.reference,
                    "version": selected_version,
                    "canonical_url": detail.canonical_url,
                    **({"pinned_version": str(args.get("pin_version"))} if args.get("pin_version") else {}),
                },
                replace=replace,
            )
        except (FileExistsError, SkillValidationError) as exc:
            return self._marketplace_error(
                f"Skill was not installed: {exc}", structured=structured, code="installation_rejected", data={"plan": plan},
            )
        self.skill_manager = SkillManager(skill_dirs=list(config.skill_dirs or []) or None)
        suffix = (
            " Missing MCP dependencies (not added automatically): " + ", ".join(missing) + "."
            if missing else ""
        )
        if structured:
            return structured_result(
                f"Installed skill '{installation.skill.name}'.",
                status="partial" if missing else "completed",
                data={
                    "installation": {
                        "name": installation.skill.name,
                        "version": installation.skill.version,
                        "path": str(installation.path),
                        "replaced": installation.replaced,
                    },
                    "plan": plan,
                },
                artifacts=[{"type": "skill", "path": str(installation.path), "name": installation.skill.name}],
                warnings=warnings,
                next_actions=[{"tool": "load_skill", "arguments": {"name": installation.skill.name}}],
                provenance={"registry": detail.registry, "reference": detail.reference, "version": selected_version},
                metrics={"dependency_count": len(dependency_status), "missing_required_count": len(missing)},
            )
        return f"Installed skill '{installation.skill.name}' at {installation.path}.{suffix}"

    async def _search_mcp_marketplace(self, args: dict) -> str:
        try:
            structured = wants_structured(args)
            mode = self._marketplace_requested_mode(
                args, allowed={"search", "inspect", "compare"}, default="search",
            )
        except ValueError as exc:
            return error_result(str(exc), code="invalid_argument")
        advanced = self._marketplace_advanced(
            args, structured, {"mode", "compare_names", "include_details", "max_results"},
        )
        comparison_targets = self._marketplace_strings(args.get("compare_names"))
        query = str(args.get("query") or "").strip() or (comparison_targets[0] if comparison_targets else "")
        config = self._marketplace_config()
        client = MCPRegistryClient(config.mcp_registries)
        try:
            results = await client.search(query, args.get("registry"))
        except (ValueError, RuntimeError) as exc:
            return self._marketplace_error(
                f"MCP marketplace search failed: {exc}", structured=structured, code="marketplace_search_failed",
            )
        if not advanced:
            if not results:
                errors = "; ".join(f"{name}: {message}" for name, message in client.last_errors.items())
                return "No MCP servers found." + (f" Registry notes: {errors}" if errors else "")
            lines = [f"MCP marketplace servers ({len(results)}):"]
            for item in results[:12]:
                trust = "verified" if item.verified else "registry listing"
                lines.append(f"- {item.name} [{item.registry}, {trust}] — {item.description or 'No description.'}")
            return "\n".join(lines)
        if not results:
            errors = "; ".join(f"{name}: {message}" for name, message in client.last_errors.items())
            return structured_result(
                "No MCP servers found.",
                status="not_found",
                warnings=[errors] if errors else [],
                provenance={"source": "configured_mcp_registries"},
                metrics={"result_count": 0},
            )
        try:
            max_results = max(1, min(int(args.get("max_results") or 12), 25))
        except (TypeError, ValueError):
            return self._marketplace_error("max_results must be an integer", structured=structured, code="invalid_argument")
        include_details = bool(args.get("include_details")) or mode in {"inspect", "compare"} or bool(comparison_targets)
        compare_keys = {item.casefold() for item in comparison_targets}
        if mode == "compare" and not compare_keys:
            compare_keys = {item.name.casefold() for item in results[:2]}
        configured_names = {
            str(server.get("name") or "").casefold()
            for server in config.mcp_servers
            if isinstance(server, dict)
        }
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for item in results[:max_results]:
            is_compare_target = not compare_keys or item.name.casefold() in compare_keys
            detail = None
            plan = None
            if include_details and (mode != "compare" or is_compare_target):
                getter = getattr(client, "get_server", None)
                if callable(getter):
                    try:
                        detail = await getter(item.name, item.registry)
                    except (RuntimeError, ValueError) as exc:
                        warnings.append(f"Could not inspect {item.name}: {exc}")
                planner = getattr(client, "get_install_command", None)
                if callable(planner):
                    try:
                        plan = await planner(item.name, item.registry)
                    except (RuntimeError, ValueError) as exc:
                        warnings.append(f"Could not plan {item.name}: {exc}")
            payload = plan.as_config(existing_names=configured_names) if plan is not None else {}
            existing = item.name.casefold() in configured_names or str(payload.get("name") or "").casefold() in configured_names
            subject = detail or item
            row = self._mcp_result_projection(item)
            row.update(
                {
                    "risk": self._mcp_risk_summary(payload, verified=bool(getattr(subject, "verified", False)), existing=existing),
                    "permission_summary": {
                        "registry_access": "read-only metadata lookup",
                        "configuration": "none during search",
                        "execution": "none",
                    },
                    "compatibility": {
                        "configured": existing,
                        "install_plan_available": plan is not None,
                        "transport": str(payload.get("transport") or ""),
                        "required_environment": sorted(str(key) for key in (payload.get("env") or {})),
                    },
                }
            )
            if detail is not None:
                row["detail"] = {
                    "packages": len(getattr(detail, "packages", []) or []),
                    "remotes": len(getattr(detail, "remotes", []) or []),
                }
            if payload:
                row["plan"] = self._mcp_plan_projection(payload)
            rows.append(row)
        comparison = [row for row in rows if row["name"].casefold() in compare_keys] if compare_keys else []
        registry_errors = getattr(client, "last_errors", {}) or {}
        warnings.extend(f"{name}: {message}" for name, message in registry_errors.items())
        return structured_result(
            f"Found {len(results)} MCP marketplace server{'s' if len(results) != 1 else ''}.",
            status="partial" if registry_errors else "completed",
            data={"mode": mode, "results": rows, "comparison": comparison},
            warnings=warnings,
            next_actions=[
                {"tool": "add_marketplace_mcp", "arguments": {"name": row["name"], "registry": row["registry"], "preview": True, "response_format": "structured"}}
                for row in rows[:3]
            ],
            provenance={"source": "configured_mcp_registries", "registry": args.get("registry") or "all"},
            metrics={"result_count": len(results), "returned_count": len(rows), "comparison_count": len(comparison)},
        )

    async def _add_marketplace_mcp(self, args: dict) -> str:
        try:
            structured = wants_structured(args)
            mode = self._marketplace_requested_mode(
                args, allowed={"add", "update", "preview"}, default="add",
            )
        except ValueError as exc:
            return error_result(str(exc), code="invalid_argument")
        preview = bool(args.get("preview", False)) or mode == "preview"
        advanced = self._marketplace_advanced(
            args,
            structured,
            {"mode", "preview", "sandbox_validate", "pin_version", "version", "replace", "expected_version"},
        )
        config = self._marketplace_config()
        source_name = str(args.get("name") or "").strip()
        existing = {str(server.get("name") or "") for server in config.mcp_servers if isinstance(server, dict)}
        existing_folded = {name.casefold() for name in existing}
        if source_name in existing and not advanced:
            return f"MCP server '{source_name}' is already configured."
        builtin = next((server for server in DEFAULT_MCP_SERVERS if server["name"] == source_name), None)
        plan = None
        detail = None
        warnings: list[str] = []
        if builtin is not None:
            payload = dict(builtin)
            source = "Ares built-in configuration"
        else:
            client = MCPRegistryClient(config.mcp_registries)
            getter = getattr(client, "get_server", None)
            if callable(getter) and advanced:
                try:
                    detail = await getter(source_name, args.get("registry"))
                except (RuntimeError, ValueError) as exc:
                    warnings.append(f"Could not inspect registry metadata: {exc}")
            try:
                plan = await client.get_install_command(source_name, args.get("registry"))
            except (RuntimeError, ValueError) as exc:
                return self._marketplace_error(
                    f"No safe install plan was found for MCP server '{source_name}': {exc}",
                    structured=structured,
                    code="install_plan_unavailable",
                )
            if plan is None:
                return self._marketplace_error(
                    f"No safe install plan was found for MCP server '{source_name}'.",
                    structured=structured,
                    code="install_plan_unavailable",
                )
            payload = plan.as_config(existing_names=existing_folded)
            source = f"{plan.registry} registry"
        requested_pin = str(args.get("pin_version") or args.get("version") or "").strip()
        detail_version = str(getattr(detail, "version", "") or "").strip()
        if requested_pin:
            if builtin is not None:
                return self._marketplace_error(
                    "Version pinning is not available for built-in MCP configurations.",
                    structured=structured,
                    code="pin_unsupported",
                )
            if not detail_version:
                return self._marketplace_error(
                    "The registry did not provide a version that can be safely pinned.",
                    structured=structured,
                    code="pin_unverified",
                )
            if requested_pin != detail_version:
                return self._marketplace_error(
                    f"Requested MCP version '{requested_pin}' is not the registry's currently planned version '{detail_version}'.",
                    structured=structured,
                    code="version_not_available",
                    data={"available_version": detail_version},
                )
        if advanced:
            payload["marketplace"] = {
                "source_name": source_name,
                "registry": str(getattr(detail, "registry", "") or getattr(plan, "registry", "") or "builtin"),
                "version": detail_version or None,
                "pinned_version": requested_pin or None,
            }
        existing_index = next(
            (
                index for index, server in enumerate(config.mcp_servers)
                if isinstance(server, dict)
                and str(server.get("name") or "").casefold() in {source_name.casefold(), str(payload.get("name") or "").casefold()}
            ),
            None,
        )
        replacing = bool(args.get("replace", False)) or mode == "update"
        existing_server = config.mcp_servers[existing_index] if existing_index is not None else None
        if existing_index is not None and not replacing:
            if advanced:
                message = f"MCP server '{source_name}' is already configured; use mode='update' with a new explicit confirmation to replace it."
                return self._marketplace_error(
                    message,
                    structured=structured,
                    code="already_configured",
                    data={"existing": self._mcp_plan_projection(existing_server), "planned": self._mcp_plan_projection(payload)},
                    status="conflict",
                )
            return f"MCP server '{source_name}' is already configured."
        if mode == "update" and existing_index is None:
            return self._marketplace_error(
                f"MCP server '{source_name}' is not configured, so there is nothing to update.",
                structured=structured,
                code="not_found",
                status="not_found",
            )
        if existing_server is not None:
            payload["name"] = str(existing_server.get("name") or payload.get("name") or source_name)
        expected_version = str(args.get("expected_version") or "").strip()
        if expected_version and isinstance(existing_server, dict):
            installed_version = str((existing_server.get("marketplace") or {}).get("version") or "")
            if installed_version != expected_version:
                return self._marketplace_error(
                    f"Configured MCP version does not match expected_version '{expected_version}'.",
                    structured=structured,
                    code="version_conflict",
                    data={"installed_version": installed_version or None, "expected_version": expected_version},
                    status="conflict",
                )
        review = (
            f"MCP plan for {payload['name']}: source={source}; transport={payload['transport']}; "
            f"target={payload.get('server_url') or payload.get('command') or '-'}; "
            f"args={' '.join(payload.get('args') or []) or '-'}"
        )
        if preview:
            summary = f"MCP {'update' if replacing else 'add'} preview for '{payload['name']}' is ready; shared config was not changed."
            if structured:
                return structured_result(
                    summary,
                    status="preview",
                    data={
                        "plan": self._mcp_plan_projection(payload),
                        "existing": self._mcp_plan_projection(existing_server) if isinstance(existing_server, dict) else None,
                        "risk": self._mcp_risk_summary(
                            payload,
                            verified=bool(getattr(detail, "verified", builtin is not None)),
                            existing=existing_index is not None,
                        ),
                        "permission_summary": {
                            "configuration": "shared config is unchanged during preview",
                            "execution": "no MCP command or remote is executed during marketplace planning",
                        },
                        "sandbox": {
                            "requested": bool(args.get("sandbox_validate", False)),
                            "performed": False,
                            "reason": "MCP packages and remotes are never executed during marketplace planning.",
                        },
                    },
                    warnings=warnings,
                    next_actions=[
                        {"tool": "add_marketplace_mcp", "arguments": {**args, "preview": False, "mode": "update" if replacing else "add", "confirm": True, "response_format": "structured"}}
                    ],
                    provenance={"source": source, "registry": args.get("registry") or "builtin"},
                )
            return "PREVIEW: " + summary
        pass
        if existing_index is not None:
            config.mcp_servers[existing_index] = payload
        else:
            config.mcp_servers.append(payload)
        save_config(config)
        action = "Updated" if existing_index is not None else "Added"
        if structured:
            return structured_result(
                review + f". {action} shared config; use /mcp refresh before calling its tools.",
                data={"plan": self._mcp_plan_projection(payload), "mode": mode, "updated": existing_index is not None},
                warnings=warnings,
                next_actions=[{"tool": "mcp_status", "arguments": {}}],
                provenance={"source": source, "registry": args.get("registry") or "builtin"},
            )
        return review + ". Added to shared config; use /mcp refresh before calling its tools."

    # ── Export ─────────────────────────────────────────────────────

    def _export_data(self, args: dict) -> str:
        advanced_keys = {
            "preview", "redact", "incremental", "previous_manifest", "manifest_path",
            "verify", "response_format", "include_categories", "exclude_categories", "since", "until",
            "encryption_password",
        }
        advanced = any(key in args for key in advanced_keys)
        if advanced:
            profile = str(args.get("profile") or "full")
            output_path = Path(args["path"]).expanduser() if args.get("path") else default_export_path()
            manifest_path = Path(args.get("manifest_path") or f"{output_path}.manifest.json").expanduser()
            encryption_password = args.get("encryption_password")
            try:
                payload = build_export_payload(
                    memory_store=self.memory,
                    conversation_store=self.conversations,
                    people_store=self.people_store,
                    action_ledger=self.action_ledger,
                    goal_store=self.goal_store,
                    commitment_store=self.commitment_store,
                    config=self.config,
                    profile=profile,
                )
                plan = plan_advanced_export(
                    payload,
                    profile=profile,
                    redact=bool(args.get("redact", True)),
                    include_categories=args.get("include_categories"),
                    exclude_categories=args.get("exclude_categories"),
                    since=args.get("since"),
                    until=args.get("until"),
                    previous_manifest=args.get("previous_manifest"),
                    incremental=bool(args.get("incremental", False)),
                    output_path=output_path,
                    manifest_path=manifest_path,
                    encryption_password=encryption_password,
                )
            except (AdvancedExportError, UpgradeValidationError, OSError, TypeError, ValueError) as exc:
                return error_result(str(exc), code="export_plan")
            preview_data = plan.as_dict()
            safe_next_arguments = {
                key: value for key, value in args.items()
                if key != "encryption_password"
            }
            safe_next_arguments["preview"] = False
            next_action: dict[str, Any] = {
                "tool": "export_data",
                "arguments": safe_next_arguments,
            }
            if encryption_password is not None:
                next_action["note"] = "Provide encryption_password again when executing; it is intentionally not retained in the preview."
            if bool(args.get("preview", False)):
                return structured_result(
                    "Export plan is ready for review; no files were written.",
                    status="preview",
                    data=preview_data,
                    warnings=list(preview_data["warnings"]),
                    next_actions=[next_action],
                    provenance={"source": "local_export_planner"},
                    metrics={
                        "sections": preview_data["write_manifest"]["section_counts"],
                        "payload_bytes": preview_data["write"]["payload_bytes"],
                        "encrypted": bool(preview_data["encryption"]["enabled"]),
                    },
                )
            try:
                result = write_advanced_export(plan, encryption_password=encryption_password)
            except (AdvancedExportError, UpgradeValidationError, OSError, TypeError, ValueError) as exc:
                return error_result(f"Export write failed: {exc}", code="export_write")
            verification = result["verification"]
            file_verification = verification.get("file") if isinstance(verification, dict) else None
            file_ok = bool(file_verification.get("ok")) if isinstance(file_verification, dict) else False
            round_trip = verification.get("round_trip") if isinstance(verification, dict) else None
            ok = file_ok and (round_trip in {None, True})
            return structured_result(
                f"Exported Ares data to {result['output_path']}" if ok else f"Export wrote to {result['output_path']}, but verification failed.",
                ok=ok,
                status="completed" if ok else "failed",
                data={
                    **preview_data,
                    "written_path": result["output_path"],
                    "manifest_path": result["manifest_path"],
                    "write_result": result,
                },
                artifacts=[
                    {
                        "path": artifact["path"],
                        "media_type": "application/json",
                        "description": "Encrypted Ares export" if result["encrypted"] else "Ares export",
                    }
                    for artifact in result["artifacts"]
                ],
                warnings=list(result["warnings"]),
                errors=[] if ok else [{"code": "export_verification", "message": "export verification did not complete"}],
                provenance={
                    "source": "local_export_planner",
                    "checksum_sha256": result["write_manifest"]["checksum_sha256"],
                    "encrypted": bool(result["encrypted"]),
                },
                metrics={
                    "sections": result["write_manifest"]["section_counts"],
                    "redactions": preview_data["redaction"]["count"],
                    "encrypted": bool(result["encrypted"]),
                },
            )
        path = export_data(
            memory_store=self.memory,
            conversation_store=self.conversations,
            people_store=self.people_store,
            action_ledger=self.action_ledger,
            goal_store=self.goal_store,
            commitment_store=self.commitment_store,
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
        if self._advanced_web_search_requested(args):
            structured = wants_structured(args)
            advanced_args = dict(args)
            mode = str(advanced_args.get("search_mode") or "quick").casefold()
            if mode == "web":
                advanced_args["search_mode"] = "quick"
            try:
                state = self.research_upgrades.search(
                    advanced_args,
                    lambda request, fetch_top: self._research_search_payload(
                        request, fetch_top=fetch_top,
                    ),
                )
            except (TypeError, ValueError) as exc:
                return error_result(str(exc), code="research_validation") if structured else json_result({
                    "ok": False, "error": str(exc),
                })
            if not structured:
                return json_result(state)
            retrieval_errors = [
                {"code": "retrieval_error", "message": message}
                for message in state.get("errors", [])
            ]
            return structured_result(
                f"Research session {state['research_id']} contains "
                f"{len(state.get('sources', []))} source(s) and {len(state.get('claims', []))} claim(s).",
                status="partial" if retrieval_errors else "completed",
                data=state,
                warnings=[state["uncertainty"]] if state.get("uncertainty") else [],
                errors=retrieval_errors,
                next_actions=[{
                    "tool": "web_search",
                    "arguments": {"research_id": state["research_id"], "follow_up": "...", "response_format": "structured"},
                    "description": "Continue this research graph with a focused follow-up.",
                }],
                provenance={"providers": state.get("providers", []), "research_id": state["research_id"]},
                metrics={
                    "source_count": len(state.get("sources", [])),
                    "claim_count": len(state.get("claims", [])),
                    "conflict_count": len(state.get("conflicts", [])),
                    "subquery_count": len(state.get("subqueries", [])),
                },
            )
        payload = self._research_search_payload(args, fetch_top=int(args.get("fetch_top", 3)))
        if wants_structured(args):
            return structured_result(
                f"Found {len(payload.get('results', []))} web result(s).",
                status="partial" if payload.get("errors") else "completed",
                data=payload,
                errors=[{"code": "retrieval_error", "message": str(item)} for item in payload.get("errors", [])],
                provenance={"provider": payload.get("provider")},
                metrics={"result_count": len(payload.get("results", [])), "fetched_count": len(payload.get("fetched", []))},
            )
        return payload_to_json(payload)

    @staticmethod
    def _advanced_web_search_requested(args: dict[str, Any]) -> bool:
        mode = str(args.get("search_mode") or "web").casefold()
        return (
            mode in {"quick", "deep", "fact-check", "compare", "primary", "recommend", "latest"}
            or bool(args.get("research_id") or args.get("follow_up"))
            or str(args.get("response_format") or "legacy").casefold() == "structured"
        )

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
        if self._advanced_web_search_requested(args):
            return await self._run_blocking_web_search(args)
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
        advanced_keys = {
            "selector", "heading", "anchor", "pattern", "extract",
            "follow_same_domain", "max_follow_pages", "snapshot", "compare",
        }
        if any(key in args for key in advanced_keys) or wants_structured(args):
            structured = wants_structured(args)
            try:
                payload = advanced_fetch(args, self.research_upgrades)
            except (OSError, TypeError, ValueError, re.error) as exc:
                return error_result(str(exc), code="fetch_validation") if structured else json_result({
                    "ok": False, "error": str(exc),
                })
            if not structured:
                return json_result(payload)
            fetch_error = str(payload.get("error") or "")
            return structured_result(
                f"Fetched {payload.get('final_url') or payload.get('url') or args.get('url')}",
                ok=not bool(fetch_error),
                status="partial" if fetch_error else "completed",
                data=payload,
                warnings=["The page changed since its previous snapshot."] if payload.get("snapshot", {}).get("changed") else [],
                errors=[{"code": "fetch_error", "message": fetch_error}] if fetch_error else [],
                provenance={"url": payload.get("url"), "final_url": payload.get("final_url")},
                metrics={"characters": len(str(payload.get("content") or ""))},
            )
        return fetch_url_tool(args)

    def _download_online_file(self, args: dict) -> str:
        return json_result(self.research.download(
            str(args["url"]),
            filename=str(args.get("filename") or ""),
            max_bytes=int(args.get("max_bytes", 20 * 1024 * 1024)),
        ))

    def _extract_document(self, args: dict) -> str:
        advanced_keys = {"documents", "paths", "urls", "mode", "pages", "sheet", "range", "ocr", "tables", "entities"}
        if any(key in args for key in advanced_keys) or wants_structured(args):
            structured = wants_structured(args)
            try:
                payload = advanced_extract(self.research, args)
            except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
                return error_result(str(exc), code="document_validation") if structured else json_result({
                    "ok": False, "error": str(exc),
                })
            if not structured:
                return json_result(payload)
            documents = payload.get("documents", [])
            warnings = [
                str(document.get("ocr", {}).get("warning"))
                for document in documents if document.get("ocr", {}).get("warning")
            ]
            return structured_result(
                f"Extracted {len(documents)} document(s) in {payload.get('mode')} mode.",
                status="partial" if warnings else "completed",
                data=payload,
                artifacts=[{
                    "path": document.get("path"), "name": document.get("name"), "kind": document.get("kind"),
                } for document in documents],
                warnings=warnings,
                metrics={
                    "document_count": len(documents),
                    "comparison_count": len(payload.get("comparison", [])),
                    "character_count": sum(len(str(document.get("content") or "")) for document in documents),
                },
            )
        return json_result(self.research.extract_document(
            path=str(args.get("path") or ""),
            url=str(args.get("url") or ""),
            filename=str(args.get("filename") or ""),
            max_bytes=int(args.get("max_bytes", 20 * 1024 * 1024)),
            max_chars=int(args.get("max_chars", 30_000)),
        ))

    def _create_research_report(self, args: dict) -> str:
        if any(key in args for key in ("style", "research_id")) or wants_structured(args):
            structured = wants_structured(args)
            try:
                payload = create_advanced_report(
                    self.research,
                    self.research_upgrades,
                    args,
                    lambda request, fetch_top: self._research_search_payload(
                        request, fetch_top=fetch_top,
                    ),
                )
            except (OSError, TypeError, ValueError) as exc:
                return error_result(str(exc), code="research_report_error") if structured else json_result({
                    "ok": False, "error": str(exc),
                })
            if not structured:
                return json_result(payload)
            state = payload.pop("state", {})
            return structured_result(
                f"Created {payload.get('style')} research report with {payload.get('sources')} source(s).",
                data={**payload, "research": state},
                artifacts=[{"path": payload.get("path"), "name": payload.get("name"), "kind": payload.get("kind")}],
                warnings=[state.get("uncertainty")] if state.get("uncertainty") else [],
                provenance={"research_id": payload.get("research_id"), "providers": state.get("providers", [])},
                metrics={"source_count": payload.get("sources", 0), "claim_count": payload.get("claims", 0)},
            )
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
        advanced = str(args.get("mode") or "lines").casefold() != "lines" or wants_structured(args) or any(
            key in args for key in ("symbol", "heading", "selector", "cursor", "encoding")
        )
        if advanced:
            try:
                data = advanced_read(args)
            except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                return error_result(str(exc), code="file_read") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            if wants_structured(args):
                return structured_result(
                    f"Read {data['path']} in {data['mode']} mode.", data=data,
                    provenance={"path": data["path"], "encoding": data["encoding"]},
                    metrics={"total_lines": data["total_lines"]},
                )
            return self._json({"ok": True, **data})
        return read_file(
            args["path"],
            start_line=int(args.get("start_line", 1)),
            num_lines=int(args.get("num_lines", 200)),
        )

    def _search_files(self, args: dict) -> str:
        advanced = str(args.get("mode") or "text").casefold() != "text" or wants_structured(args) or any(
            key in args for key in ("symbol", "changed_only", "date_from", "date_to", "group_by", "include_related_tests", "cursor")
        )
        if advanced:
            try:
                data = advanced_search(args)
            except (OSError, UnicodeError, ValueError, re.error) as exc:
                return error_result(str(exc), code="file_search") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            if wants_structured(args):
                return structured_result(
                    f"Found {len(data['results'])} file result(s) in {data['mode']} mode.",
                    data=data, provenance={"root": data["root"], "git_available": data["git_available"]},
                    metrics={"page_results": len(data["results"]), "total_results": data["total_results"]},
                )
            return self._json({"ok": True, **data})
        return search_files(
            query=args.get("query", ""),
            path=args.get("path", "."),
            name_pattern=args.get("name_pattern", ""),
            max_results=int(args.get("max_results", 20)),
        )

    def _list_directory(self, args: dict) -> str:
        if str(args.get("mode") or "legacy").casefold() == "project" or wants_structured(args):
            try:
                data = project_scan(args)
            except (OSError, ValueError) as exc:
                return error_result(str(exc), code="directory_scan") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            return structured_result(
                f"Scanned {data['root']} ({data['total_items']} item(s)).", data=data,
                provenance={"root": data["root"]}, metrics=data["summary"],
            ) if wants_structured(args) else self._json({"ok": True, **data})
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

    def _verify_file_change(self, args: dict, data: dict[str, Any]) -> str | None:
        command = str(args.get("verify_command") or "").strip()
        if not command or data.get("dry_run") or data.get("confirm_required") or not data.get("changed"):
            return None
        cwd = str(Path(data["path"]).parent)
        resolved = resolve_project_command(command, cwd)
        output = self.repl.execute_shell(resolved, timeout=int(args.get("verify_timeout", 120)), cwd=cwd, profile="test")
        passed = "Exit code: 0" in output and "Error:" not in output
        if passed:
            data["verification"] = {"ok": True, "command": command, "output": output}
            return None
        rollback = ""
        if data.get("undo_id"):
            backup = Path(str(data["undo_id"]))
            try:
                shutil.copy2(backup, Path(data["path"]))
                rollback = f"Restored the exact pre-change snapshot {backup}."
            except OSError as exc:
                rollback = f"Rollback failed: {exc}"
        elif data.get("created"):
            try:
                Path(data["path"]).unlink()
                rollback = "Removed the newly created file."
            except OSError as exc:
                rollback = f"Rollback failed: {exc}"
        data["verification"] = {"ok": False, "command": command, "output": output, "rollback": rollback}
        summary = f"Verification failed after changing {data['path']}; the change was rolled back."
        if wants_structured(args):
            return structured_result(
                summary, ok=False, status="failed", data=data,
                errors=[{"code": "verification_failed", "message": output}],
                warnings=[rollback] if rollback else [],
            )
        return self._json({"ok": False, "error": summary, **data})

    def _write_file(self, args: dict) -> str:
        advanced = str(args.get("mode") or "overwrite").casefold() != "overwrite" or wants_structured(args) or any(
            key in args for key in ("patch", "template", "variables", "encoding", "newline", "formatter", "validation", "verify_command")
        )
        if advanced:
            try:
                data = advanced_write(args)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                return error_result(str(exc), code="file_write") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            verification = self._verify_file_change(args, data)
            if verification is not None:
                return verification
            if wants_structured(args):
                status = "preview" if data.get("dry_run") or data.get("confirm_required") else "completed"
                warnings = ["Explicit confirmation is required before overwriting this file."] if data.get("confirm_required") else []
                return structured_result(
                    f"Prepared {data['mode']} operation for {data['path']}." if status == "preview" else f"Updated {data['path']}.",
                    status=status, data=data, artifacts=[{"path": data["path"], "kind": "file"}] if data.get("changed") else [],
                    warnings=warnings, metrics={"bytes": data.get("bytes", 0), "changed": bool(data.get("changed"))},
                    undo_id=data.get("undo_id"),
                )
            return self._json({"ok": True, **data})
        return _write_file_impl(
            args["path"],
            args["content"],
            dry_run=bool(args.get("dry_run", False)),
            confirm=bool(args.get("confirm", False)),
        )

    def _edit_file(self, args: dict) -> str:
        advanced = str(args.get("mode") or "replace").casefold() != "replace" or wants_structured(args) or any(
            key in args for key in ("match_index", "pattern", "start_line", "patch", "symbol", "fields", "encoding", "formatter", "validation", "verify_command")
        )
        if advanced:
            try:
                data = advanced_edit(args)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError, re.error, SyntaxError) as exc:
                return error_result(str(exc), code="file_edit") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            verification = self._verify_file_change(args, data)
            if verification is not None:
                return verification
            if wants_structured(args):
                return structured_result(
                    f"{'Previewed' if data.get('dry_run') else 'Edited'} {data['path']} in {data['mode']} mode.",
                    status="preview" if data.get("dry_run") else "completed", data=data,
                    artifacts=[{"path": data["path"], "kind": "file"}] if data.get("changed") else [],
                    metrics={"changed": bool(data.get("changed"))}, undo_id=data.get("undo_id"),
                )
            return self._json({"ok": True, **data})
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
        return _delete_file_impl(path, dry_run=dry_run)

    def _move_file(self, args: dict) -> str:
        source = args["source"]
        destination = args["destination"]
        dry_run = bool(args.get("dry_run", False))
        return _move_file_impl(source, destination, dry_run=dry_run)

    def _batch_edit(self, args: dict) -> str:
        if str(args.get("mode") or "execute").casefold() == "plan" or wants_structured(args) or any(
            isinstance(operation, dict) and (operation.get("depends_on") or operation.get("condition"))
            for operation in args.get("operations", [])
        ):
            try:
                plan = plan_batch(list(args.get("operations") or []))
            except (TypeError, ValueError) as exc:
                return error_result(str(exc), code="batch_plan") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            if str(args.get("mode") or "execute").casefold() == "plan" or bool(args.get("dry_run", False)):
                data = {**plan, "executed": False}
                return structured_result(
                    f"Planned {len(plan['runnable'])} runnable operation(s); {len(plan['skipped'])} skipped.",
                    status="preview", data=data, metrics={"runnable": len(plan["runnable"]), "skipped": len(plan["skipped"])},
                ) if wants_structured(args) else self._json({"ok": True, **data})
            operations = [
                {key: value for key, value in operation.items() if key not in {"id", "depends_on", "condition"}}
                for operation in plan["runnable"]
            ]
            snapshots: dict[Path, bytes | None] = {}
            for operation in operations:
                for key in ("path", "destination"):
                    if not operation.get(key):
                        continue
                    path = Path(str(operation[key])).expanduser().resolve()
                    if path not in snapshots:
                        snapshots[path] = path.read_bytes() if path.is_file() else None
            result = _batch_edit_impl(
                operations=operations, dry_run=False, confirm=bool(args.get("confirm", False)),
                max_operations=int(args.get("max_operations", 100)),
            )
            ok = "failed and rolled back" not in result.casefold()
            verification_results: list[dict[str, Any]] = []
            if ok:
                for command in args.get("verification") or []:
                    output = self.repl.execute_shell(str(command), timeout=120, cwd=str(args.get("cwd") or "."), profile="test")
                    passed = "Exit code: 0" in output and "Error:" not in output
                    verification_results.append({"command": command, "ok": passed, "output": output})
                    if not passed:
                        ok = False
                        if bool(args.get("rollback_on_failure", True)):
                            for path, content in snapshots.items():
                                if content is None:
                                    if path.is_file():
                                        path.unlink()
                                else:
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    path.write_bytes(content)
                            result += "\nVerification failed; file changes were rolled back."
                        break
            data = {**plan, "executed": True, "result": result, "verification": verification_results}
            if wants_structured(args):
                return structured_result(
                    "Batch edit completed." if ok else "Batch edit failed and rolled back.", ok=ok,
                    status="completed" if ok else "failed", data=data,
                    errors=[] if ok else [{"code": "batch_failed", "message": result}],
                    metrics={"runnable": len(operations), "skipped": len(plan["skipped"])},
                )
            return self._json({"ok": ok, **data})
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
        if str(args.get("mode") or "legacy").casefold() == "project" or wants_structured(args):
            try:
                data = project_scan(args, tree=True)
            except (OSError, ValueError) as exc:
                return error_result(str(exc), code="file_tree") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            return structured_result(
                f"Scanned project tree with {data['total_items']} item(s).", data=data,
                provenance={"root": data["root"]}, metrics=data["summary"],
            ) if wants_structured(args) else self._json({"ok": True, **data})
        return _file_tree_impl(
            path=args.get("path", "."),
            max_depth=int(args.get("max_depth", 3)),
            show_files=bool(args.get("show_files", True)),
        )

    # ── Code execution tools ───────────────────────────────────────

    def _run_code(self, args: dict) -> str:
        mode = str(args.get("mode") or "execute").casefold()
        if mode != "execute" or wants_structured(args) or any(key in args for key in ("session_id", "cell_name", "checkpoint_id", "capture_artifacts")):
            try:
                data = self.runtime_upgrades.python(args)
            except (OSError, ValueError, RuntimeError) as exc:
                return error_result(str(exc), code="runtime") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            if wants_structured(args):
                ok = bool(data.get("execution", {}).get("ok", True))
                return structured_result(
                    f"Python {mode} {'completed' if ok else 'failed'} in session {data.get('session_id', 'default')}.",
                    ok=ok, status="completed" if ok else "failed", data=data,
                    artifacts=list(data.get("artifacts") or []),
                    errors=[] if ok else [{"code": "python_execution", "message": str(data.get("output") or "Execution failed")}],
                    metrics=dict(data.get("metrics") or {}),
                )
            return self._json({"ok": True, **data})
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
        mode = str(args.get("mode") or "execute").casefold()
        command = args.get("command") or (f"@{args.get('command_key', '')}" if args.get("command_key") else "")
        if command:
            command = resolve_project_command(command, args.get("cwd"))
        if mode != "execute" or wants_structured(args) or any(key in args for key in ("session_id", "job_id", "stdin", "detach", "retry", "checkpoint_id")):
            advanced_args = {**args, "command": command}
            try:
                data = self.runtime_upgrades.command(advanced_args)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                return error_result(str(exc), code="runtime") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            if wants_structured(args):
                parsed = dict(data.get("parsed") or {})
                output = str(data.get("output") or "")
                exit_code = parsed.get("exit_code")
                ok = exit_code in (None, 0) and "Error:" not in output
                if mode in {"start", "inspect", "attach", "follow", "stop", "stdin", "jobs", "history", "discover", "git_summary", "checkpoint"}:
                    ok = True
                artifacts = []
                if data.get("job"):
                    artifacts.append({"kind": "runtime_job", "job_id": data["job"]["job_id"]})
                return structured_result(
                    f"Shell {mode} {'completed' if ok else 'failed'}.", ok=ok,
                    status="completed" if ok else "failed", data=data, artifacts=artifacts,
                    errors=[] if ok else [{"code": "command_failed", "message": output}],
                    metrics=dict(data.get("metrics") or {}),
                )
            return self._json({"ok": True, **data})
        timeout = int(args.get("timeout", 30))
        cwd = args.get("cwd")
        if bool(args.get("reset", False)):
            self.repl.reset_shell()
        result = self.repl.execute_shell(command, timeout=timeout, cwd=cwd, profile=args.get("profile"))
        if bool(args.get("include_fingerprint", False)) or bool(args.get("reset", False)):
            result += f"\nSession: shell generation={self.repl.shell_generation}; dependency_fingerprint={self.repl.dependency_fingerprint(cwd)}"
        return result

    def _run_project_check(self, args: dict) -> str:
        """Run only a pre-approved project verification check, never shell text."""
        return run_project_check(
            str(args.get("check") or ""),
            cwd=str(args.get("cwd") or "."),
            trusted_checks=args.get("_trusted_agent_checks"),
            timeout_seconds=int(args.get("timeout_seconds") or 180),
        )

    # ── Image tools ────────────────────────────────────────────────

    @staticmethod
    def _decoded_image_metadata(path: str | Path) -> dict[str, Any]:
        image_path = Path(path).expanduser()
        if not image_path.is_file():
            raise ValueError(f"image file does not exist: {image_path}")
        with Image.open(image_path) as image:
            image.load()
            return {
                "path": str(image_path),
                "width": int(image.width),
                "height": int(image.height),
                "format": str(image.format or "").upper(),
                "mode": str(image.mode),
                "bytes": image_path.stat().st_size,
                "frame_count": int(getattr(image, "n_frames", 1) or 1),
                "animated": bool(getattr(image, "is_animated", False)),
            }

    @staticmethod
    def _image_result_path(message: str) -> str | None:
        match = re.search(r"(?:Image saved to|saved to)\s+(.+?)(?:\r?\n|$)", str(message or ""), re.IGNORECASE)
        if match is None:
            return None
        candidate = match.group(1).strip()
        return candidate if candidate else None

    @staticmethod
    def _image_upgrade_requested(args: dict) -> bool:
        # ``response_format=legacy`` must not itself switch a legacy tool onto
        # the JSON path.  Schema-aware callers often send the default value.
        if wants_structured(args):
            return True
        if bool(args.get("preview")) or bool(args.get("estimate_only")):
            return True
        if any(args.get(key) not in (None, "", [], {}) for key in (
            "style", "aspect_ratio", "negative_prompt", "fallbacks", "expected", "preset",
            "crop_width", "crop_height", "target_bytes",
        )):
            return True
        try:
            variations = int(args.get("variations", 1))
        except (TypeError, ValueError):
            return True
        return any((
            variations != 1,
            str(args.get("fit") or "contain").casefold() != "contain",
            args.get("clamp", True) is not True,
            bool(args.get("flatten_animation", False)),
            bool(args.get("pad", False)),
            str(args.get("pad_color") or "transparent").casefold() != "transparent",
            str(args.get("metadata_policy") or "strip").casefold() != "strip",
            str(args.get("transparency_policy") or "flatten").casefold() != "flatten",
            str(args.get("background") or "white").casefold() != "white",
            str(args.get("mode") or "box").casefold() != "box",
            str(args.get("anchor") or "center").casefold() != "center",
        ))

    def _generate_image(self, args: dict) -> str:
        prompt = args["prompt"]
        width = int(args.get("width", 1024))
        height = int(args.get("height", 1024))
        model = args.get("model", "flux")
        seed = args.get("seed")
        if seed is not None:
            seed = int(seed)
        if self._image_upgrade_requested(args):
            try:
                manifest = build_image_variation_manifest(
                    prompt,
                    width=width,
                    height=height,
                    model=model,
                    seed=seed,
                    variations=int(args.get("variations") or 1),
                    style=args.get("style"),
                    aspect_ratio=args.get("aspect_ratio"),
                    negative_prompt=args.get("negative_prompt"),
                    fallbacks=args.get("fallbacks"),
                    request_id=str(args.get("request_id") or "") or None,
                )
            except (UpgradeValidationError, TypeError, ValueError) as exc:
                return error_result(str(exc), code="image_plan")
            if bool(args.get("preview", False)):
                return structured_result(
                    "Image generation plan is ready for review; no image was generated.",
                    status="preview",
                    data={"manifest": manifest},
                    warnings=["The current image provider adapter does not send negative_prompt separately; it remains in the reproducibility manifest."] if manifest["negative_prompt"] else [],
                    next_actions=[{"tool": "generate_image", "arguments": {**args, "preview": False}}],
                    provenance={"source": "image_generation_planner", "reproducibility_id": manifest["reproducibility_id"]},
                    metrics={"variations": len(manifest["variants"])},
                )
            attempts: list[dict[str, Any]] = []
            artifacts: list[dict[str, Any]] = []
            warnings: list[str] = []
            if manifest["negative_prompt"]:
                warnings.append("The current image provider adapter does not send negative_prompt separately; it remains in the reproducibility manifest.")
            for variant in manifest["variants"]:
                candidates = [manifest["model"], *[
                    fallback.get("model", manifest["model"])
                    for fallback in manifest["fallback_policy"]
                ]]
                generated = ""
                used_model = manifest["model"]
                for attempt_index, candidate_model in enumerate(candidates, 1):
                    generated = generate_image(
                        variant["prompt"],
                        width=variant["target_size"]["width"],
                        height=variant["target_size"]["height"],
                        model=str(candidate_model),
                        seed=variant["seed"],
                    )
                    attempts.append({
                        "variant_id": variant["variant_id"],
                        "attempt": attempt_index,
                        "model": candidate_model,
                        "ok": not str(generated).lstrip().startswith("Error:"),
                        "result": generated,
                    })
                    used_model = str(candidate_model)
                    if not str(generated).lstrip().startswith("Error:"):
                        break
                variant["executed_model"] = used_model
                variant["result"] = generated
                image_path = self._image_result_path(generated)
                if image_path is None:
                    continue
                try:
                    actual = self._decoded_image_metadata(image_path)
                    verification = validate_image_metadata(actual, variant["target_size"])
                    variant["actual"] = actual
                    variant["verification"] = verification
                    artifacts.append({
                        "path": image_path,
                        "media_type": f"image/{actual['format'].casefold()}",
                        "description": f"Generated image variation {variant['index'] + 1}",
                    })
                    if not verification["ok"]:
                        warnings.extend(verification["errors"])
                except (OSError, UpgradeValidationError, ValueError) as exc:
                    warnings.append(f"Could not verify generated image {image_path}: {exc}")
            completed = sum(1 for variant in manifest["variants"] if not str(variant.get("result") or "").lstrip().startswith("Error:"))
            ok = completed == len(manifest["variants"])
            return structured_result(
                f"Generated {completed}/{len(manifest['variants'])} requested image variations.",
                ok=ok,
                status="completed" if ok else "partial" if completed else "failed",
                data={"manifest": manifest, "attempts": attempts},
                artifacts=artifacts,
                warnings=list(dict.fromkeys(warnings)),
                errors=[
                    {"variant_id": variant["variant_id"], "message": variant.get("result")}
                    for variant in manifest["variants"]
                    if str(variant.get("result") or "").lstrip().startswith("Error:")
                ],
                provenance={"source": "image_generation", "reproducibility_id": manifest["reproducibility_id"]},
                metrics={"requested": len(manifest["variants"]), "completed": completed, "attempts": len(attempts)},
            )
        return generate_image(prompt, width=width, height=height, model=model, seed=seed)

    def _image_info(self, args: dict) -> str:
        if wants_structured(args):
            try:
                metadata = self._decoded_image_metadata(args["path"])
            except (OSError, ValueError) as exc:
                return error_result(str(exc), code="image_metadata")
            expected = args.get("expected")
            try:
                verification = validate_image_metadata(metadata, expected) if isinstance(expected, dict) else None
            except (UpgradeValidationError, TypeError, ValueError) as exc:
                return error_result(str(exc), code="image_validation")
            return structured_result(
                f"Loaded image metadata for {metadata['path']}.",
                ok=verification is None or bool(verification["ok"]),
                status="completed" if verification is None or verification["ok"] else "failed",
                data={"image": metadata, "verification": verification},
                artifacts=[{"path": metadata["path"], "media_type": f"image/{metadata['format'].casefold()}", "description": "Inspected image"}],
                errors=[] if verification is None or verification["ok"] else [{"code": "image_validation", "message": error} for error in verification["errors"]],
                provenance={"source": "Pillow"},
            )
        return _image_info(args["path"])

    def _image_transform_plan(self, args: dict, *, operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
        source = self._decoded_image_metadata(args["path"])
        resize = None
        crop = None
        convert = None
        if operation == "resize":
            resize = {
                key: args[key]
                for key in ("width", "height", "percent", "fit", "preset", "pad", "pad_color", "metadata_policy")
                if key in args
            }
        elif operation == "crop":
            crop = {
                key: args[key]
                for key in (
                    "left", "top", "right", "bottom", "clamp", "mode", "percent", "aspect_ratio",
                    "crop_width", "crop_height", "anchor", "metadata_policy",
                )
                if key in args
            }
        elif operation == "convert":
            convert = {
                key: args[key]
                for key in (
                    "format", "quality", "flatten_animation", "metadata_policy", "transparency_policy", "background",
                )
                if key in args
            }
        advanced_resize = operation == "resize" and (
            any(key in resize for key in ("preset", "pad", "pad_color", "metadata_policy"))
            or str(resize.get("fit") or "contain").casefold() == "pad"
        )
        advanced_crop = operation == "crop" and any(
            key in crop for key in ("mode", "percent", "aspect_ratio", "crop_width", "crop_height", "anchor", "metadata_policy")
        )
        if advanced_resize:
            geometry = _resize_geometry(
                source["width"], source["height"], width=resize.get("width"), height=resize.get("height"),
                percent=resize.get("percent"), fit=str(resize.get("fit") or "contain"),
                preset=resize.get("preset"), pad=bool(resize.get("pad", False)),
            )
            plan = {
                "kind": "image_transform_plan", "schema_version": 1, "source": source,
                "operations": [{"operation": "resize", "before": {"width": source["width"], "height": source["height"]}, "after": geometry["target"], "details": geometry}],
                "target": {**source, **geometry["target"], **({"output": str(Path(args["output"]).expanduser())} if args.get("output") else {})},
                "warnings": [], "valid": True,
            }
        elif advanced_crop:
            geometry = _crop_geometry(
                source["width"], source["height"], left=crop.get("left", 0), top=crop.get("top", 0),
                right=crop.get("right"), bottom=crop.get("bottom"), mode=str(crop.get("mode") or "box"),
                percent=crop.get("percent"), aspect_ratio=crop.get("aspect_ratio"),
                crop_width=crop.get("crop_width"), crop_height=crop.get("crop_height"),
                anchor=str(crop.get("anchor") or "center"), clamp=bool(crop.get("clamp", True)),
            )
            plan = {
                "kind": "image_transform_plan", "schema_version": 1, "source": source,
                "operations": [{"operation": "crop", "before": {"width": source["width"], "height": source["height"]}, "after": geometry["target"], "details": geometry["details"]}],
                "target": {**source, **geometry["target"], **({"output": str(Path(args["output"]).expanduser())} if args.get("output") else {})},
                "warnings": ["crop coordinates were clamped to the image bounds"] if geometry["details"]["clamped"] else [],
                "valid": True,
            }
        else:
            # The shared planner owns the baseline geometry and animation
            # validation used by both single and batch image transforms.
            normalized_resize = {key: value for key, value in (resize or {}).items() if key in {"width", "height", "percent", "fit"}} or None
            normalized_crop = {key: value for key, value in (crop or {}).items() if key in {"left", "top", "right", "bottom", "clamp"}} or None
            normalized_convert = {key: value for key, value in (convert or {}).items() if key in {"format", "quality", "flatten_animation"}} or None
            plan = plan_image_transform(source, resize=normalized_resize, crop=normalized_crop, convert=normalized_convert, output=args.get("output"))
            if operation == "convert" and convert:
                plan["operations"][0]["details"].update({
                    key: convert[key] for key in ("metadata_policy", "transparency_policy", "background") if key in convert
                })
        estimated_bytes = max(1, round(
            int(source["bytes"]) * (plan["target"]["width"] * plan["target"]["height"]) / (source["width"] * source["height"])
        ))
        plan["estimate"] = {"bytes": estimated_bytes, "basis": "source-byte-to-pixel-ratio", "exact": False}
        return source, plan

    def _structured_image_transform(self, args: dict, *, operation: str) -> str:
        try:
            source, plan = self._image_transform_plan(args, operation=operation)
        except (OSError, UpgradeValidationError, TypeError, ValueError) as exc:
            return error_result(str(exc), code="image_plan")
        target_bytes = args.get("target_bytes")
        if target_bytes is not None:
            try:
                maximum = int(target_bytes)
            except (TypeError, ValueError):
                return error_result("target_bytes must be a positive integer", code="image_plan")
            if maximum <= 0:
                return error_result("target_bytes must be a positive integer", code="image_plan")
            plan["target_bytes"] = maximum
            if plan["estimate"]["bytes"] > maximum:
                plan["warnings"].append("Estimated output is above target_bytes; adjust dimensions or compression quality before writing.")
        if bool(args.get("preview", False)) or bool(args.get("estimate_only", False)):
            return structured_result(
                f"{operation.title()} plan is ready for review; no image was changed.",
                status="preview",
                data={"source": source, "plan": plan},
                warnings=list(plan["warnings"]),
                next_actions=[{"tool": f"{operation}_image", "arguments": {**args, "preview": False}}],
                provenance={"source": "image_transform_planner"},
            )
        if operation == "resize":
            result = _resize_image(
                args["path"], width=args.get("width"), height=args.get("height"),
                percent=args.get("percent"), output=args.get("output"), fit=str(args.get("fit") or "contain"),
                preset=args.get("preset"), pad=bool(args.get("pad", False)),
                pad_color=args.get("pad_color", "transparent"), metadata_policy=str(args.get("metadata_policy") or "strip"),
            )
        elif operation == "crop":
            result = _crop_image(
                args["path"], left=int(args.get("left", 0)), top=int(args.get("top", 0)),
                right=args.get("right"), bottom=args.get("bottom"), output=args.get("output"),
                clamp=bool(args.get("clamp", True)),
                mode=str(args.get("mode") or "box"), percent=args.get("percent"),
                aspect_ratio=args.get("aspect_ratio"), crop_width=args.get("crop_width"),
                crop_height=args.get("crop_height"), anchor=str(args.get("anchor") or "center"),
                metadata_policy=str(args.get("metadata_policy") or "strip"),
            )
        else:
            result = _convert_image(
                args["path"], format=args["format"], output=args.get("output"),
                quality=int(args.get("quality", 85)), flatten_animation=bool(args.get("flatten_animation", False)),
                metadata_policy=str(args.get("metadata_policy") or "strip"),
                transparency_policy=str(args.get("transparency_policy") or "flatten"),
                background=args.get("background", "white"),
            )
        if str(result).lstrip().startswith("Error:"):
            return error_result(str(result), code="image_transform")
        output_path = self._image_result_path(result) or str(args.get("output") or args["path"])
        try:
            actual = self._decoded_image_metadata(output_path)
            verification = validate_transform_result(plan, actual)
        except (OSError, UpgradeValidationError, ValueError) as exc:
            return error_result(f"Image was transformed but verification failed: {exc}", code="image_verification")
        return structured_result(
            f"{operation.title()} image completed.",
            ok=bool(verification["ok"]),
            status="completed" if verification["ok"] else "failed",
            data={"source": source, "plan": plan, "result": result, "actual": actual, "verification": verification},
            artifacts=[{"path": output_path, "media_type": f"image/{actual['format'].casefold()}", "description": f"{operation.title()} image"}],
            warnings=[*plan["warnings"], *verification["warnings"]],
            errors=[] if verification["ok"] else [{"code": "image_verification", "message": error} for error in verification["errors"]],
            provenance={"source": "Pillow", "operation": operation},
            metrics={"width": actual["width"], "height": actual["height"], "bytes": actual["bytes"]},
        )

    def _resize_image(self, args: dict) -> str:
        if self._image_upgrade_requested(args):
            return self._structured_image_transform(args, operation="resize")
        return _resize_image(
            args["path"],
            width=args.get("width"),
            height=args.get("height"),
            percent=args.get("percent"),
            output=args.get("output"),
        )

    def _convert_image(self, args: dict) -> str:
        if self._image_upgrade_requested(args):
            return self._structured_image_transform(args, operation="convert")
        return _convert_image(
            args["path"],
            format=args["format"],
            output=args.get("output"),
            quality=int(args.get("quality", 85)),
        )

    def _crop_image(self, args: dict) -> str:
        advanced_crop = (
            args.get("percent") is not None
            or args.get("aspect_ratio") is not None
            or args.get("crop_width") is not None
            or args.get("crop_height") is not None
            or str(args.get("mode") or "box").casefold() != "box"
            or str(args.get("anchor") or "center").casefold() != "center"
            or str(args.get("metadata_policy") or "strip").casefold() != "strip"
        )
        if self._image_upgrade_requested(args) or advanced_crop:
            return self._structured_image_transform(args, operation="crop")
        return _crop_image(
            args["path"],
            left=int(args.get("left", 0)),
            top=int(args.get("top", 0)),
            right=args["right"],
            bottom=args["bottom"],
            output=args.get("output"),
        )

    def _batch_transform_images(self, args: dict) -> str:
        """Preview and, after confirmation, atomically transform a file batch.

        Batch writes deliberately require an output directory.  That makes the
        default operation copy-on-write and allows a failed batch to remove all
        outputs it created instead of leaving a half-completed set behind.
        """
        raw_paths = args.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            return error_result("paths must be a non-empty array of image paths", code="image_batch")
        if len(raw_paths) > 100:
            return error_result("batch size is limited to 100 images", code="image_batch")
        paths = [str(value).strip() for value in raw_paths]
        if any(not path for path in paths):
            return error_result("paths cannot contain empty values", code="image_batch")
        if len({str(Path(path).expanduser()).casefold() for path in paths}) != len(paths):
            return error_result("paths must not contain duplicates", code="image_batch")
        transform = args.get("transform")
        if not isinstance(transform, dict):
            return error_result("transform must be an object containing resize, crop, and/or convert", code="image_batch")
        if not any(transform.get(key) is not None for key in ("resize", "crop", "convert")):
            return error_result("transform must contain at least one of resize, crop, or convert", code="image_batch")
        if transform.get("output"):
            return error_result("batch transforms use output_dir; transform.output is not allowed", code="image_batch")
        output_dir_value = str(args.get("output_dir") or "").strip()
        if not output_dir_value:
            return error_result("output_dir is required for a safe batch transform", code="image_batch")
        output_dir = Path(output_dir_value).expanduser()
        collision_policy = str(args.get("collision_policy") or "suffix").casefold()
        if collision_policy not in {"suffix", "error"}:
            return error_result("collision_policy must be suffix or error", code="image_batch")

        # The shared planner currently owns the exact batch geometry.  Keep
        # richer single-file controls separate rather than silently applying a
        # partially supported pad/aspect/metadata policy to a batch.
        allowed_options = {
            "resize": {"width", "height", "percent", "fit"},
            "crop": {"left", "top", "right", "bottom", "clamp"},
            "convert": {"format", "quality", "flatten_animation"},
        }
        for operation, allowed in allowed_options.items():
            options = transform.get(operation)
            if options is None:
                continue
            if not isinstance(options, dict):
                return error_result(f"transform.{operation} must be an object", code="image_batch")
            unsupported = set(options) - allowed
            if unsupported:
                fields = ", ".join(sorted(unsupported))
                return error_result(
                    f"Batch {operation} does not support {fields}; preview and apply that advanced option per image.",
                    code="image_batch",
                )

        sources: list[dict[str, Any]] = []
        source_errors: dict[int, str] = {}
        for index, path in enumerate(paths):
            try:
                sources.append(self._decoded_image_metadata(path))
            except (OSError, ValueError) as exc:
                # Supply a deliberately invalid record so the shared planner
                # retains index alignment and produces a per-item preview.
                sources.append({"path": path, "width": 0, "height": 0, "format": "PNG"})
                source_errors[index] = str(exc)
        try:
            plan = plan_image_batch_transform(
                sources,
                transform,
                output_dir=output_dir,
                collision_policy=collision_policy,
            )
        except (UpgradeValidationError, TypeError, ValueError) as exc:
            return error_result(str(exc), code="image_batch")

        allow_overwrite = bool(args.get("allow_overwrite", False))
        for item in plan["items"]:
            index = int(item["index"])
            if index in source_errors:
                item.update({"ok": False, "error": source_errors[index]})
                continue
            if not item.get("ok"):
                continue
            source_path = Path(paths[index]).expanduser()
            output_path = Path(item["plan"]["target"]["output"]).expanduser()
            try:
                same_path = source_path.resolve() == output_path.resolve()
            except OSError:
                same_path = str(source_path) == str(output_path)
            if same_path:
                item.update({"ok": False, "error": "planned output would overwrite its source; choose another output_dir"})
            elif output_path.exists() and not allow_overwrite:
                item.update({"ok": False, "error": f"planned output already exists: {output_path}"})
            item["source_path"] = str(source_path)
            item["output_path"] = str(output_path)
        plan["summary"] = {
            "total": len(plan["items"]),
            "valid": sum(1 for item in plan["items"] if item.get("ok")),
            "invalid": sum(1 for item in plan["items"] if not item.get("ok")),
        }
        valid_items = [item for item in plan["items"] if item.get("ok")]
        invalid_errors = [str(item.get("error") or "invalid image plan") for item in plan["items"] if not item.get("ok")]
        preview = bool(args.get("preview", True))
        confirm = args.get("confirm") is True
        next_arguments = {**args, "preview": False, "confirm": True}
        if not valid_items:
            return error_result("No valid batch items remain after planning", code="image_batch")
        if allow_overwrite and len(valid_items) > 1:
            return error_result(
                "Multi-file batch overwrite is not supported; choose a new output_dir to keep rollback safe.",
                code="image_batch",
            )

        completed: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        created_paths: list[Path] = []
        overwrite_backups: list[tuple[Path, Path]] = []
        for item in valid_items:
            source_path = str(item["source_path"])
            output_path = Path(item["output_path"])
            if allow_overwrite and output_path.exists():
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{output_path.stem}.ares-batch-backup.",
                    suffix=output_path.suffix,
                    dir=output_path.parent,
                )
                os.close(descriptor)
                backup_path = Path(backup_name)
                try:
                    shutil.copy2(output_path, backup_path)
                except OSError as exc:
                    with suppress(FileNotFoundError):
                        backup_path.unlink()
                    failures.append({"path": source_path, "error": f"could not back up existing output: {exc}"})
                    break
                overwrite_backups.append((output_path, backup_path))
            result = _transform_image(
                source_path,
                resize=transform.get("resize"),
                crop=transform.get("crop"),
                convert=transform.get("convert"),
                output=str(output_path),
                action="batch_transform_images",
            )
            if str(result).lstrip().startswith("Error:"):
                failures.append({"path": source_path, "error": result})
                break
            try:
                actual = self._decoded_image_metadata(output_path)
                verification = validate_transform_result(item["plan"], actual)
            except (OSError, UpgradeValidationError, ValueError) as exc:
                failures.append({"path": source_path, "error": f"verification failed: {exc}"})
                with suppress(FileNotFoundError):
                    output_path.unlink()
                break
            if not verification["ok"]:
                failures.append({"path": source_path, "error": "; ".join(verification["errors"])})
                with suppress(FileNotFoundError):
                    output_path.unlink()
                break
            created_paths.append(output_path)
            completed.append({
                "source": source_path,
                "output": str(output_path),
                "result": result,
                "actual": actual,
                "verification": verification,
            })
        rolled_back: list[str] = []
        if failures:
            for path in reversed(created_paths):
                with suppress(FileNotFoundError, OSError):
                    path.unlink()
                    rolled_back.append(str(path))
            for output_path, backup_path in reversed(overwrite_backups):
                with suppress(OSError):
                    shutil.copy2(backup_path, output_path)
                    rolled_back.append(str(output_path))
                with suppress(FileNotFoundError, OSError):
                    backup_path.unlink()
            completed = []
        else:
            for _output_path, backup_path in overwrite_backups:
                with suppress(FileNotFoundError, OSError):
                    backup_path.unlink()
        artifacts = [
            {"path": item["output"], "media_type": f"image/{item['actual']['format'].casefold()}", "description": "Batch-transformed image"}
            for item in completed
        ]
        return structured_result(
            f"Batch image transform {'completed' if not failures else 'failed and rolled back'} for {len(completed)} of {len(valid_items)} image(s).",
            ok=not failures and not invalid_errors,
            status="completed" if not failures and not invalid_errors else "partial" if not failures else "failed",
            data={"plan": plan, "completed": completed, "rolled_back": rolled_back},
            artifacts=artifacts,
            warnings=list(dict.fromkeys(invalid_errors)),
            errors=failures,
            provenance={"source": "Pillow", "operation": "batch_transform"},
            metrics={"requested": len(paths), "planned": len(valid_items), "completed": len(completed), "rolled_back": len(rolled_back)},
        )

    # ── Terminal ───────────────────────────────────────────────────

    def _terminal_exec(self, args: dict) -> str:
        """Run with exactly run_command semantics plus observable display state."""
        mode = str(args.get("mode") or "execute").casefold()
        advanced = mode != "execute" or wants_structured(args) or any(key in args for key in ("session_id", "job_id", "stdin", "signal", "rows", "columns"))
        command = args.get("command") or (f"@{args.get('command_key', '')}" if args.get("command_key") else "")
        if command:
            command = resolve_project_command(command, args.get("cwd"))
        if advanced:
            command_args = {**args, "command": command, "detach": mode == "start" or not bool(args.get("wait", True))}
            if args.get("signal"):
                command_args["mode"] = "stop"
            try:
                data = self.runtime_upgrades.command(command_args)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                return error_result(str(exc), code="terminal") if wants_structured(args) else self._json({"ok": False, "error": str(exc)})
            data["terminal"] = {"rows": args.get("rows"), "columns": args.get("columns"), "display_requested": bool(command)}
            callback = getattr(self, "_terminal_display_callback", None)
            display = "unavailable"
            if callback is not None and command:
                try:
                    callback(command)
                    display = "delivered"
                except Exception as exc:
                    display = f"failed: {exc}"
            data["terminal"]["display_delivery"] = display
            if wants_structured(args):
                return structured_result(
                    f"Terminal {mode} completed.", data=data,
                    artifacts=[{"kind": "terminal_job", "job_id": data["job"]["job_id"]}] if data.get("job") else [],
                    metrics=dict(data.get("metrics") or {}),
                )
            return self._json({"ok": True, **data})
        result = self._run_command(args)
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
        advanced = self._phone_advanced_requested(args, {
            "applications", "app", "person", "keywords", "unread_only", "since", "until",
            "group_by", "collapse_duplicates", "content_mode", "metadata_only", "response_format",
        })
        raw = _kdeconnect_bridge.get_recent_notifications(limit=int(args.get("limit", 20)))
        if not advanced:
            return raw
        payload = self._phone_payload(raw)
        structured = wants_structured(args)
        if not payload.get("ok"):
            message = str(payload.get("error") or "Could not read the live notification snapshot.")
            return error_result(message, code="phone_bridge") if structured else self._json(payload)
        try:
            content_mode = "metadata" if bool(args.get("metadata_only", False)) else str(args.get("content_mode") or "metadata")
            prepared = prepare_notifications(
                payload.get("notifications") if isinstance(payload.get("notifications"), list) else [],
                applications=args.get("applications", args.get("app")), person=args.get("person"),
                keywords=args.get("keywords"), unread_only=bool(args.get("unread_only", False)),
                since=args.get("since"), until=args.get("until"), group_by=str(args.get("group_by") or "none"),
                collapse_duplicates=bool(args.get("collapse_duplicates", True)), content_mode=content_mode,
                limit=int(args.get("limit", 20)),
            )
        except ValueError as exc:
            return error_result(str(exc), code="validation") if structured else self._json({"ok": False, "error": str(exc)})
        result = {
            "ok": True,
            "snapshot": bool(payload.get("snapshot", True)),
            "notifications": prepared["notifications"],
            "groups": prepared["groups"],
            "filters": prepared["filters"],
            "privacy": prepared["privacy"],
            "metrics": prepared["metrics"],
        }
        if structured:
            return structured_result(
                prepared["summary"], data=result, metrics=prepared["metrics"],
                provenance={"source": "live_phone_notification_snapshot", "persisted": False},
            )
        return self._json(result)

    def _phone_search_contact(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        advanced = self._phone_advanced_requested(args, {
            "mode", "channel", "purpose", "include_people", "limit", "response_format",
        })
        raw = _kdeconnect_bridge.search_contacts(str(args["query"]), limit=int(args.get("limit", 20)))
        if not advanced:
            return raw
        payload = self._phone_payload(raw)
        structured = wants_structured(args)
        if not payload.get("ok"):
            message = str(payload.get("error") or "Could not search live phone contacts.")
            return error_result(message, code="phone_bridge") if structured else self._json(payload)
        saved_people: list[dict[str, Any]] = []
        if bool(args.get("include_people", True)) and self.people_store is not None:
            try:
                saved_people = self.people_store.search_advanced(
                    str(args["query"]), limit=max(1, min(int(args.get("limit", 20)), 50)),
                    channel=str(args.get("channel") or ""), purpose=str(args.get("purpose") or ""),
                    include_sensitive=True,
                )
            except (TypeError, ValueError):
                # Device results remain useful if a non-search field was
                # supplied in a format the People Store cannot interpret.
                saved_people = self.people_store.search(str(args["query"]), limit=max(1, min(int(args.get("limit", 20)), 50)))
        try:
            action = str(args.get("purpose") or args.get("channel") or "sms")
            ranked = rank_contact_candidates(
                args["query"], device_contacts=payload.get("contacts") or [], saved_people=saved_people,
                action=action, limit=int(args.get("limit", 20)), reveal_contact_values=False,
            )
        except ValueError as exc:
            return error_result(str(exc), code="validation") if structured else self._json({"ok": False, "error": str(exc)})
        requested_channel = str(args.get("channel") or "").casefold()
        if requested_channel in {"phone", "sms", "email"}:
            channel = "phone" if requested_channel == "sms" else requested_channel
            ranked["candidates"] = [
                item for item in ranked["candidates"]
                if any(entry.get("kind") == channel for entry in item.get("channels", []))
            ]
            ranked["best_candidate_id"] = None if ranked["requires_disambiguation"] else (
                ranked["candidates"][0]["candidate_id"] if ranked["candidates"] else None
            )
        result = {"ok": True, "contacts": ranked, "bridge": {"snapshot": True, "limit": payload.get("limit")}}
        if structured:
            return structured_result(
                f"Found {len(ranked['candidates'])} ranked contact candidate(s).", data=result,
                metrics=ranked["metrics"], provenance={"sources": ["live_phone_contacts", "saved_people"], "persisted": False},
                warnings=["Choose an explicit candidate when requires_disambiguation is true."] if ranked["requires_disambiguation"] else [],
            )
        return self._json(result)

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
        advanced = self._phone_advanced_requested(args, {
            "mode", "template", "variables", "include_message", "retry", "delivery", "response_format",
        })
        if not advanced:
            return self._phone_result(_kdeconnect_bridge.send_sms(number, args["message"]), person)
        structured = wants_structured(args)
        mode = str(args.get("mode") or "send").casefold()
        if mode not in {"preview", "send", "status"}:
            message = "mode must be preview, send, or status"
            return error_result(message, code="validation") if structured else self._json({"ok": False, "error": message})
        if mode == "status":
            delivery = sms_delivery_status(args.get("delivery") if isinstance(args.get("delivery"), dict) else {})
            return structured_result("SMS delivery status normalized.", data={"delivery": delivery}) if structured else self._json({"ok": delivery["ok"], "delivery": delivery})
        bridge = _kdeconnect_bridge.status()
        try:
            preview = preview_sms(
                number, message=args.get("message"), template=args.get("template"),
                variables=args.get("variables") if isinstance(args.get("variables"), dict) else {},
                include_message=False, bridge_ready=bool(bridge.get("ok")),
            )
        except ValueError as exc:
            return error_result(str(exc), code="validation") if structured else self._json({"ok": False, "sent": False, "error": str(exc)})
        if mode == "preview" or not bool(args.get("confirm", False)):
            result = {"ok": bool(preview.get("ok")), "sent": False, "mode": "preview", "preview": preview, "recipient": person.get("canonical_name") if person else None}
            if structured:
                return structured_result(
                    "SMS preview is ready; explicit confirmation is required before sending.",
                    ok=bool(preview.get("ok")), status="preview", data=result,
                    next_actions=[{"tool": "phone_send_sms", "arguments": {"number": args.get("number"), "confirm": True, "mode": "send"}}],
                    provenance={"persisted": False, "recipient_resolved_from_people": bool(person)},
                )
            return self._json(result)
        # Render the template a second time internally so a raw message never
        # has to be copied into a preview response or an action-ledger field.
        try:
            outbound = preview_sms(
                number, message=args.get("message"), template=args.get("template"),
                variables=args.get("variables") if isinstance(args.get("variables"), dict) else {},
                include_message=True, bridge_ready=bool(bridge.get("ok")),
            )
        except ValueError as exc:
            return error_result(str(exc), code="validation") if structured else self._json({"ok": False, "sent": False, "error": str(exc)})
        if not outbound.get("ok"):
            return error_result(str(outbound.get("error") or "SMS preview failed."), code="validation") if structured else self._json({"ok": False, "sent": False, "preview": preview, "error": outbound.get("error")})
        bridge_result = self._phone_payload(_kdeconnect_bridge.send_sms(number, str(outbound["message"])))
        delivery = sms_delivery_status(bridge_result)
        result = {
            "ok": bool(bridge_result.get("ok")), "sent": bool(bridge_result.get("sent")), "mode": "send",
            "recipient": person.get("canonical_name") if person else preview.get("recipient"),
            "preview": preview, "delivery": delivery,
            "retry_requested": bool(args.get("retry", False)),
        }
        warnings = []
        if args.get("retry"):
            warnings.append("No automatic resend was performed; retry is only safe after an explicit follow-up request and a classified transport failure.")
        if structured:
            return structured_result(
                "SMS submitted to the paired phone." if result["sent"] else "SMS was not submitted.",
                ok=result["ok"], status="completed" if result["ok"] else "failed", data=result, warnings=warnings,
                errors=[] if result["ok"] else [{"code": "phone_bridge", "message": delivery.get("error") or "Phone bridge failed to submit the SMS."}],
                provenance={"persisted": False, "recipient_resolved_from_people": bool(person)},
            )
        return self._json(result)

    def _phone_call_number(self, args: dict) -> str:
        if not self.config or not self.config.phone.enabled:
            return self._phone_disabled()
        try:
            number, person = self._resolve_phone_recipient(args.get("number"))
        except PersonResolutionError as exc:
            return self._json({"ok": False, "dialed": False, "error": str(exc)})
        advanced = self._phone_advanced_requested(args, {"mode", "device_id", "post_call_note", "call_status", "response_format"})
        if not advanced:
            return self._phone_result(
                _adb_bridge.call_number(number, confirm=bool(args.get("confirm", False))),
                person,
            )
        structured = wants_structured(args)
        mode = str(args.get("mode") or "call").casefold()
        if mode not in {"preflight", "call", "status"}:
            message = "mode must be preflight, call, or status"
            return error_result(message, code="validation") if structured else self._json({"ok": False, "error": message})
        if mode == "status":
            status = normalize_call_status(args.get("call_status") if isinstance(args.get("call_status"), dict) else args)
            return structured_result("Call status normalized.", data={"call": status}) if structured else self._json({"ok": True, "call": status})
        phone_status = self._phone_payload(_adb_bridge.phone_status())
        preflight = call_preflight(
            number, phone_status=phone_status, device_id=args.get("device_id"),
            recipient=person.get("canonical_name") if person else None, confirm=bool(args.get("confirm", False)),
            reveal_number=False,
        )
        if mode == "preflight" or not preflight["ok"]:
            result = {"ok": bool(preflight["ready"]), "dialed": False, "mode": "preflight", "preflight": preflight}
            if structured:
                return structured_result(
                    "Call preflight is ready." if preflight["ready"] else "Call preflight failed.",
                    ok=bool(preflight["ready"]), status="preview" if preflight["ready"] else "failed", data=result,
                    warnings=preflight["warnings"], errors=[{"code": "phone_preflight", "message": message} for message in preflight["errors"]],
                    next_actions=[{"tool": "phone_call_number", "arguments": {"number": args.get("number"), "confirm": True, "mode": "call"}}] if preflight["ready"] and not args.get("confirm") else [],
                )
            return self._json(result)
        bridge_result = self._phone_payload(_adb_bridge.call_number(number, confirm=True))
        normalized = normalize_call_status({**bridge_result, "status": "initiated" if bridge_result.get("dialed") else "failed"})
        note_result: dict[str, Any] | None = None
        if bridge_result.get("dialed") and args.get("post_call_note") is not None:
            note_result = validate_post_call_note(
                args.get("post_call_note"), person_id=person.get("person_id") if person else None,
                call_id=normalized.get("call_id"),
            )
            if note_result.get("ok") and person is not None and self.people_store is not None:
                try:
                    self.people_store.update(
                        int(person["person_id"]),
                        timeline=[{"type": "phone_call_note", "note": note_result["note"]}],
                    )
                    note_result = {"ok": True, "attached": True, "person_id": person["person_id"], "persisted": True}
                except Exception as exc:
                    note_result = {"ok": False, "attached": False, "error": str(exc), "persisted": False}
            elif note_result.get("ok"):
                note_result = {"ok": False, "attached": False, "error": "Post-call notes require an explicitly saved person recipient.", "persisted": False}
        result = {
            "ok": bool(bridge_result.get("ok")), "dialed": bool(bridge_result.get("dialed")), "mode": "call",
            "recipient": person.get("canonical_name") if person else preflight.get("number"),
            "call": normalized, "preflight": preflight, "post_call_note": note_result,
        }
        if structured:
            return structured_result(
                "Call initiated through the paired phone." if result["dialed"] else "Call was not initiated.",
                ok=result["ok"], status="completed" if result["ok"] else "failed", data=result,
                errors=[] if result["ok"] else [{"code": "phone_bridge", "message": str(bridge_result.get("error") or "Phone bridge failed to initiate the call.")}],
                provenance={"recipient_resolved_from_people": bool(person)},
            )
        return self._json(result)

    @staticmethod
    def _phone_payload(value: Any) -> dict[str, Any]:
        try:
            payload = json.loads(value) if isinstance(value, str) else value
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _phone_advanced_requested(args: dict[str, Any], fields: set[str]) -> bool:
        try:
            return wants_structured(args) or any(field in args for field in fields)
        except ValueError:
            return True

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
