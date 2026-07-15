"""Core agent loop: LLM interaction, tool execution, context building."""

import asyncio
import json
import re
import time
import uuid
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Iterator

from ares.context import ProjectContext
from ares.user_context import build_user_context
from ares.autonomy import AutonomousWorkflowRunner
from ares.browser_control import BrowserTaskController
from ares.followups import FollowUpStore
from ares.memory import MemoryStore
from ares.conversations import ConversationStore
from ares.tools import ToolExecutor, get_tool_definitions
from ares.llm import LLMClient
from ares.models import AppConfig
from ares.delegation_router import (
    DelegationAvailability,
    DelegationDecision,
    DelegationMode,
    DelegationRouter,
    runtime_failure_decision,
)
from ares.multi_agent import AgentTask, AgentTeamResult, ContextMode
from ares.multi_agent_policy import ActionGrantRegistry
from ares.multi_agent_resources import ResourceCoordinator
from ares.profile import ProfileManager
from ares.reflection import ReflectionService
from ares.prompts import SYSTEM_PROMPT
from ares.soul import SoulManager
from ares.skills import SkillManager
from ares.tools.datetime_tool import get_current_datetime_result
from ares.turn_policy import (
    ActionGrant as TurnActionGrant,
    ActionGrantUseRegistry,
    TurnExecutionContext,
    authorize_turn_tool,
    build_turn_execution_context,
)

_SESSION_UNSET = object()
_TURN_UNSET = object()
ToolProgressCallback = Callable[[str, str], Awaitable[None]]


class Agent:
    """The core agent that orchestrates LLM calls and tool execution."""

    def __init__(
        self,
        memory_store: MemoryStore,
        conversation_store: ConversationStore | None = None,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        config: AppConfig | None = None,
        mcp_manager: Any | None = None,
        is_cron_session: bool = False,
        is_voice_session: bool = False,
        session_store: Any | None = None,
        session_id: str | None = None,
        tool_executor: ToolExecutor | None = None,
        llm_client: LLMClient | None = None,
        browser_controller: BrowserTaskController | None = None,
        playwright_tool_lock: asyncio.Lock | None = None,
        skill_manager: SkillManager | None = None,
        system_prompt_override: str | None = None,
        tool_schema_filter: Callable[[list[dict]], list[dict]] | None = None,
        tool_authorizer: Callable[[str, dict], Any] | None = None,
        delegation_depth: int = 0,
        multi_agent_runtime: Any | None = None,
        context_mode: ContextMode | str = ContextMode.FULL,
        allowed_context: Iterable[str] = (),
        resource_coordinator: ResourceCoordinator | None = None,
        action_grant_registry: ActionGrantRegistry | None = None,
        root_run_id: str = "",
        child_run_id: str = "",
        request_id: str = "",
    ):
        self.memory_store = memory_store
        self.conversation_store = conversation_store
        self._session_store = session_store
        self._default_session_id = session_id
        self._session_context: ContextVar[str | None | object] = ContextVar(
            f"ares_agent_session_{id(self)}", default=_SESSION_UNSET
        )
        self._turn_context: ContextVar[TurnExecutionContext | object] = ContextVar(
            f"ares_agent_turn_{id(self)}", default=_TURN_UNSET
        )
        self._turn_grant_uses = ActionGrantUseRegistry()
        self._pending_turn_grants: tuple[TurnActionGrant, ...] = ()
        self._execution_records: dict[str, dict[str, Any]] = {}
        self._owns_tool_executor = tool_executor is None
        self.tool_executor = tool_executor or ToolExecutor(
            memory_store=memory_store,
            conversation_store=conversation_store,
            config=config,
            mcp_manager=mcp_manager,
            session_store=session_store,
        )
        self._session_store = session_store or self.tool_executor.session_store
        self.mcp_manager = mcp_manager
        self.browser_controller = browser_controller or BrowserTaskController()
        # Browser pages are a single mutable surface. Other MCP servers, local
        # tools, and LLM turns can safely proceed in parallel across chats.
        self._playwright_tool_lock = playwright_tool_lock or asyncio.Lock()
        self._windows_snapshot_cache: dict[str, tuple[float, str, str]] = {}
        self.system_prompt_override = system_prompt_override
        self._tool_schema_filter = tool_schema_filter
        self._tool_authorizer = tool_authorizer
        self.delegation_depth = delegation_depth
        self.multi_agent_runtime = multi_agent_runtime
        self.context_mode = (
            context_mode if isinstance(context_mode, ContextMode) else ContextMode(str(context_mode))
        )
        self.allowed_context = frozenset(
            str(value).strip() for value in allowed_context if str(value).strip()
        )
        self.resource_coordinator = resource_coordinator or ResourceCoordinator()
        self.action_grant_registry = action_grant_registry or ActionGrantRegistry()
        self.root_run_id = str(root_run_id or "")
        self.child_run_id = str(child_run_id or "")
        self.request_id = str(request_id or "")
        self.is_cron_session = is_cron_session
        self.is_voice_session = is_voice_session
        self.refresh_tools()
        self.last_messages: list[dict] = []
        self.last_iteration_count = 0
        self.tool_execution_records: list[dict[str, Any]] = []
        self.unresponsive_tool_records: list[dict[str, str]] = []

        kwargs = {}
        if api_key or config:
            kwargs["api_key"] = api_key or (config.api_key if config else "")
        if base_url or config:
            kwargs["base_url"] = base_url or (config.api_base_url if config else "")
        if model or config:
            kwargs["model"] = model or (config.model if config else "")
        self.llm = llm_client or LLMClient(**kwargs)
        if config is not None:
            self.llm.config = config
        self.config = self.llm.config
        if self._owns_tool_executor:
            self.tool_executor.config = self.llm.config
            self.tool_executor.set_session_id(session_id)
        if self._owns_tool_executor and getattr(self.tool_executor, "telephony", None) is not None:
            # Phone transcripts use the normal agent loop, so call-time tool
            # access and memory behavior remain identical to chat.
            self.tool_executor.telephony.voice_agent.agent = self
        self.people_store = self.tool_executor.people_store
        self.action_ledger = self.tool_executor.action_ledger
        self.task_store = self.tool_executor.task_store
        self.goal_store = self.tool_executor.goal_store
        self.commitment_store = self.tool_executor.commitment_store
        follow_up_timezone = str(
            getattr(getattr(self.config, "reflection", None), "local_timezone", "") or ""
        ).strip() or None
        executor_follow_ups = getattr(self.tool_executor, "follow_up_store", None)
        self.follow_up_store = executor_follow_ups or FollowUpStore(
            connection=self.memory_store.conn,
            timezone_name=follow_up_timezone,
        )
        self.tool_executor.follow_up_store = self.follow_up_store
        self.workflow_runner: AutonomousWorkflowRunner | None = getattr(self.tool_executor, "workflow_runner", None)
        if self._owns_tool_executor and self.task_store is not None and self.action_ledger is not None:
            self.workflow_runner = AutonomousWorkflowRunner(
                task_store=self.task_store,
                action_ledger=self.action_ledger,
                execute_tool=self._execute_workflow_step,
            )
            self.tool_executor.workflow_runner = self.workflow_runner

        data_dir = Path(self.config.data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.soul_manager = SoulManager(data_dir=data_dir, soul_path=self.config.soul_path)
        self.profile_manager = ProfileManager(data_dir=data_dir, profile_path=self.config.profile_path)
        self.project_context = ProjectContext(
            enabled=self.config.project_context_enabled,
            max_files=self.config.project_context_max_files,
        )
        self.soul_manager.ensure_exists()
        self.profile_manager.ensure_exists()
        self.reflection_service: ReflectionService | None = None
        if (
            not self.is_cron_session
            and self.goal_store is not None
            and self.commitment_store is not None
            and bool(getattr(getattr(self.config, "reflection", None), "enabled", True))
        ):
            self.reflection_service = ReflectionService(
                memory_store=self.memory_store,
                goal_store=self.goal_store,
                commitment_store=self.commitment_store,
                follow_up_store=self.follow_up_store,
                profile_manager=self.profile_manager,
                config=getattr(self.config, "reflection", self.config),
                llm_client=self.llm,
            )
        skill_dirs = list(self.config.skill_dirs or [])
        self.skill_manager = skill_manager or SkillManager(skill_dirs=skill_dirs or None)
        if self._owns_tool_executor:
            self.tool_executor.skill_manager = self.skill_manager

        # The root owns one lightweight supervisor. Child specialists share its
        # stores and executor but never receive recursive delegation by default.
        if self.multi_agent_runtime is None and delegation_depth == 0 and self.config.multi_agent.enabled:
            from ares.multi_agent_runtime import MultiAgentRuntime

            self.multi_agent_runtime = MultiAgentRuntime(self)
            self.refresh_tools()

    def set_session_id(self, session_id: str | None) -> None:
        """Update local provenance scope when a long-lived surface switches chats."""
        self._default_session_id = session_id
        if self._owns_tool_executor:
            self.tool_executor.set_session_id(session_id)

    @property
    def session_id(self) -> str | None:
        scoped = self._session_context.get()
        return self._default_session_id if scoped is _SESSION_UNSET else scoped  # type: ignore[return-value]

    @contextmanager
    def session_scope(self, session_id: str | None) -> Iterator[None]:
        """Isolate memory and tool provenance for one concurrent conversation."""
        token = self._session_context.set(session_id)
        try:
            with self.tool_executor.session_scope(session_id):
                yield
        finally:
            # Async-generator cleanup can run after a cancelled client task in
            # a different ContextVar context. That context cannot retain this
            # task-local value, so skip the invalid cross-context reset.
            with suppress(ValueError):
                self._session_context.reset(token)

    @property
    def turn_context(self) -> TurnExecutionContext | None:
        """Return the immutable authority context for the active request."""
        turn_var = getattr(self, "_turn_context", None)
        if turn_var is None:
            return None
        scoped = turn_var.get()
        return None if scoped is _TURN_UNSET else scoped  # type: ignore[return-value]

    @contextmanager
    def turn_scope(self, context: TurnExecutionContext) -> Iterator[None]:
        """Bind one request's authority without leaking it across concurrent chats."""
        token = self._turn_context.set(context)
        try:
            yield
        finally:
            with suppress(ValueError):
                self._turn_context.reset(token)

    def set_pending_turn_grants(self, grants: Iterable[TurnActionGrant]) -> None:
        """Set root-issued grants to consume on the next user-facing turn only."""
        self._pending_turn_grants = tuple(grants)

    def _new_turn_context(
        self,
        user_input: str,
        *,
        request_id: str | None = None,
        confirmation_grants: Iterable[TurnActionGrant] = (),
    ) -> TurnExecutionContext:
        explicit = tuple(confirmation_grants)
        pending = tuple(getattr(self, "_pending_turn_grants", ()))
        grants = explicit or pending
        # Pending grants are one-turn capabilities.  The use registry separately
        # guarantees that a matching grant cannot be replayed within that turn.
        self._pending_turn_grants = ()
        return build_turn_execution_context(
            user_input,
            request_id=request_id or getattr(self, "request_id", "") or f"req_{uuid.uuid4().hex}",
            session_id=str(self.session_id) if self.session_id is not None else None,
            confirmation_grants=grants,
            root_run_id=getattr(self, "root_run_id", "") or None,
            child_run_id=getattr(self, "child_run_id", "") or None,
        )

    @staticmethod
    def _resolve_referential_delegation(
        user_input: str, conversation_history: list[dict]
    ) -> tuple[str, str | None]:
        """Resolve explicit 'use agents for that' only to a concrete prior user turn."""
        from ares.turn_policy import has_explicit_delegation_signal

        text = str(user_input or "").strip()
        referential = bool(
            has_explicit_delegation_signal(text)
            and (
                re.search(r"\b(?:for|on|do|handle)\s+(?:that|it|this)\b", text, re.I)
                or re.match(r"^(?:yeah|yes|yep|ok(?:ay)?)\b.*\bmulti[-\s]?agent\b", text, re.I)
            )
        )
        if not referential:
            return text, None
        prior = next((
            str(message.get("content") or "").strip()
            for message in reversed(conversation_history)
            if message.get("role") == "user" and len(str(message.get("content") or "").strip()) >= 4
        ), "")
        if not prior:
            return text, (
                "I can use native agents, but this message does not identify the task and no concrete prior user task is available. "
                "Please name the task to delegate."
            )
        return f"{text}\n\nResolved prior task: {prior[:12_000]}", None

    @staticmethod
    def _execution_session_key(session_id: str | None) -> str:
        return str(session_id or "__unscoped__")

    def _set_execution_record(self, context: TurnExecutionContext, record: dict[str, Any]) -> None:
        payload = {
            "request_id": context.request_id,
            "session_id": context.session_id,
            "recorded_at": time.time(),
            **record,
        }
        self._execution_records[self._execution_session_key(context.session_id)] = payload

    def _last_execution_record(self, context: TurnExecutionContext) -> dict[str, Any] | None:
        record = self._execution_records.get(self._execution_session_key(context.session_id))
        return dict(record) if record is not None else None

    def _ensure_ordinary_execution_record(self, context: TurnExecutionContext) -> None:
        key = self._execution_session_key(context.session_id)
        current = self._execution_records.get(key)
        if current is not None and current.get("request_id") == context.request_id:
            return
        self._set_execution_record(context, {
            "kind": "ordinary",
            "agent_count": 0,
            "tool_call_count": 0,
            "tools": [],
            "execution_waves": [],
            "status": "succeeded",
        })

    def _record_ordinary_tool_calls(
        self,
        context: TurnExecutionContext,
        tool_calls: list[dict],
        waves: Iterable[Iterable[int]],
        results: list[dict],
    ) -> None:
        names = [str(call.get("function", {}).get("name") or "unknown") for call in tool_calls]
        key = self._execution_session_key(context.session_id)
        existing = self._execution_records.get(key)
        if existing is None or existing.get("request_id") != context.request_id:
            existing = {
                "request_id": context.request_id,
                "session_id": context.session_id,
                "recorded_at": time.time(),
                "kind": "ordinary",
                "agent_count": 0,
                "tool_call_count": 0,
                "tools": [],
                "execution_waves": [],
                "status": "succeeded",
            }
        existing["tool_call_count"] = int(existing.get("tool_call_count") or 0) + len(names)
        existing["tools"] = [*list(existing.get("tools") or []), *names]
        existing["execution_waves"] = [
            *list(existing.get("execution_waves") or []),
            *[[names[index] for index in wave] for wave in waves],
        ]
        if any(str(result.get("content") or "").startswith("Error:") for result in results):
            existing["status"] = "partial_or_failed"
        self._execution_records[key] = existing

    def refresh_tools(self) -> None:
        """Refresh the advertised tool list, including connected MCP tools."""
        self.tools = get_tool_definitions()
        if getattr(self, "is_cron_session", False) or getattr(self, "is_voice_session", False):
            cron_names = {"create_cron_job", "list_cron_jobs", "get_cron_job", "update_cron_job", "delete_cron_job", "run_cron_job_now", "get_cron_logs"}
            self.tools = [tool for tool in self.tools if tool.get("function", {}).get("name") not in cron_names]
        if self.mcp_manager is not None:
            self.tools.extend(getattr(self.mcp_manager, "tool_definitions", []))
        if not getattr(getattr(getattr(self, "config", None), "multi_agent", None), "enabled", False) or getattr(self, "delegation_depth", 0) > 0:
            delegation_names = {
                "list_agents", "delegate_task", "delegate_tasks_parallel", "get_agent_run",
                "list_agent_runs", "get_latest_agent_run", "cancel_agent_run", "resume_agent_run",
            }
            self.tools = [tool for tool in self.tools if tool.get("function", {}).get("name") not in delegation_names]
        schema_filter = getattr(self, "_tool_schema_filter", None)
        if schema_filter is not None:
            self.tools = schema_filter(self.tools)

    def _tools_for_turn(
        self,
        context: TurnExecutionContext,
        delegation_decision: Any | None = None,
    ) -> list[dict]:
        """Select schemas for this request without mutating shared Agent state."""
        if getattr(self, "delegation_depth", 0) > 0:
            return list(self.tools)
        from ares.tool_registry import select_root_tools

        return select_root_tools(
            self.tools,
            context,
            delegation_decision=delegation_decision,
        )

    def _live_mcp_context(self) -> str:
        """Describe authoritative current MCP readiness for the next LLM turn."""
        manager = getattr(self, "mcp_manager", None)
        if manager is None:
            return ""
        try:
            report = manager.readiness_report()
        except Exception:
            return ""
        servers = report.get("servers") or {}
        bounded = getattr(self, "context_mode", ContextMode.FULL) is ContextMode.BOUNDED_SPECIALIST
        permitted_servers: set[str] | None = None
        if bounded:
            permitted_servers = {
                name.split("__", 2)[1]
                for schema in getattr(self, "tools", ())
                for name in [str(schema.get("function", {}).get("name") or "")]
                if name.startswith("mcp__") and name.count("__") >= 2
            }
            if not permitted_servers:
                return ""
            servers = {
                name: state for name, state in servers.items()
                if name in permitted_servers
            }
        ready = [name for name, state in sorted(servers.items()) if state.get("ready")]
        unavailable = [
            name for name, state in sorted(servers.items()) if not state.get("ready")
        ]
        lines = [
            "## Live MCP State (authoritative for this turn)",
            f"Ready now: {', '.join(ready) if ready else 'none'}.",
        ]
        if unavailable:
            lines.append(f"Not ready now: {', '.join(unavailable)}.")
        lines.extend([
            "This live state overrides older assistant messages and old tool failures in conversation history.",
            "If the user explicitly requests a ready MCP, call that MCP now. Never claim it is unavailable "
            "without either this live state marking it not ready or a failure returned by that MCP during this turn.",
            "Do not silently switch to Playwright or another integration when the requested MCP is ready.",
        ])
        return "\n".join(lines)

    def build_messages(self, user_input: str, conversation_history: list[dict],
                       context: str = "") -> list[dict]:
        """Build the message list for the LLM."""
        # MCPs can reconnect independently while a Telegram/web conversation
        # remains open. Advertise the current registry at every turn instead of
        # letting a stale assistant statement become the perceived truth.
        if getattr(self, "mcp_manager", None) is not None:
            self.refresh_tools()
        system_content = getattr(self, "system_prompt_override", None) or SYSTEM_PROMPT
        runtime = get_current_datetime_result()
        system_content += (
            "\n\n## Runtime"
            f"\nCurrent local datetime: {runtime['datetime']}"
            f"\nCurrent local date: {runtime['date']} ({runtime['day_of_week']})"
            f"\nCurrent local time: {runtime['time']}"
            f"\nTimezone: {runtime['timezone']}"
        )
        skill_manager = getattr(self, "skill_manager", None)
        bounded_specialist = (
            getattr(self, "context_mode", ContextMode.FULL) is ContextMode.BOUNDED_SPECIALIST
        )
        if (
            not bounded_specialist
            and getattr(self.config, "skills_enabled", True)
            and skill_manager is not None
        ):
            system_content += f"\n\n{skill_manager.compact_index()}"
            if getattr(self.config, "skill_auto_suggest", True):
                skill_context = skill_manager.auto_context(user_input)
                if skill_context:
                    system_content += f"\n\n{skill_context}"
        if context:
            system_content += f"\n\n## Current Context\n{context}"
        browser_controller = getattr(self, "browser_controller", None)
        browser_guidance = (
            browser_controller.begin_turn(self.session_id, user_input)
            if browser_controller is not None
            else ""
        )
        if browser_guidance:
            system_content += f"\n\n{browser_guidance}"

        turn_guard = (
            "## Current Turn Guard\n"
            "The previous conversation is context only. Answer the next user message as the current task. "
            "Do not continue, repeat, or summarize an earlier user request unless the next user message "
            "explicitly asks to continue it. If tool results are used, base the final answer on the current "
            "user request plus those tool results."
        )
        live_mcp_context = self._live_mcp_context()
        if live_mcp_context:
            turn_guard += f"\n\n{live_mcp_context}"

        messages = [{"role": "system", "content": system_content}]
        messages.extend(conversation_history)
        messages.append({"role": "system", "content": turn_guard})
        messages.append({"role": "user", "content": user_input})
        return messages

    def get_context(
        self,
        user_input: str,
        conversation_history: list[dict] | None = None,
    ) -> str:
        """Build every durable user-context layer through one retrieval path."""
        if getattr(self, "context_mode", ContextMode.FULL) is ContextMode.BOUNDED_SPECIALIST:
            # Specialist agents receive only the assignment and context the
            # root explicitly delegated; never add global personal context.
            return ""
        return build_user_context(
            user_input,
            config=self.config,
            soul_manager=self.soul_manager,
            profile_manager=self.profile_manager,
            project_context=self.project_context,
            memory_store=self.memory_store,
            conversation_store=self.conversation_store,
            session_store=self._session_store,
            session_id=self.session_id,
            people_store=self.people_store,
            action_ledger=self.action_ledger,
            task_store=self.task_store,
            goal_store=self.goal_store,
            commitment_store=self.commitment_store,
            follow_up_store=self.follow_up_store,
            conversation_history=conversation_history,
        )

    def set_model(self, model: str) -> None:
        """Switch the underlying chat model."""
        self.llm.model = model
        self.llm.config.model = model

    def set_mcp_manager(self, mcp_manager: Any | None) -> None:
        """Replace MCP connections after a shared config update."""
        self.mcp_manager = mcp_manager
        self.tool_executor.mcp_manager = mcp_manager
        self.refresh_tools()

    def apply_config(self, config: AppConfig) -> None:
        """Apply config reloaded by another Ares surface without a restart."""
        # Reconcile the supervisor first. A topology change can be rejected
        # while runs are active; in that case the Agent must remain wholly on
        # the previous configuration rather than split from its store/runtime.
        existing_runtime = getattr(self, "multi_agent_runtime", None)
        if self.delegation_depth == 0 and existing_runtime is not None:
            existing_runtime.apply_config(config)
        self.config = config
        self.llm.config = config
        self.tool_executor.config = config
        if getattr(self.tool_executor, "telephony", None) is not None:
            self.tool_executor.telephony.apply_config(config)
        self.set_model(config.model)

        data_dir = Path(config.data_dir).expanduser()
        profile_path = Path(config.profile_path).expanduser() if config.profile_path else data_dir / "profile.md"
        soul_path = Path(config.soul_path).expanduser() if config.soul_path else data_dir / "soul.md"
        if self.profile_manager.profile_path != profile_path:
            self.profile_manager = ProfileManager(data_dir=data_dir, profile_path=config.profile_path)
            self.profile_manager.ensure_exists()
            if self.reflection_service is not None:
                self.reflection_service.profile_manager = self.profile_manager
                self.reflection_service.applier.profile_manager = self.profile_manager
        if self.soul_manager.soul_path != soul_path:
            self.soul_manager = SoulManager(data_dir=data_dir, soul_path=config.soul_path)
            self.soul_manager.ensure_exists()
        self.project_context.enabled = config.project_context_enabled
        self.project_context.max_files = max(0, int(config.project_context_max_files))
        if self.reflection_service is not None:
            reflection_config = getattr(config, "reflection", config)
            self.reflection_service.config = reflection_config
            self.reflection_service.reflector.config = reflection_config
            self.reflection_service.applier.config = reflection_config

        skill_dirs = list(config.skill_dirs or [])
        configured_skill_dirs = [Path(path).expanduser() for path in skill_dirs]
        current_skill_dirs = getattr(self.skill_manager, "skill_dirs", [])
        if configured_skill_dirs and current_skill_dirs[: len(configured_skill_dirs)] != configured_skill_dirs:
            self.skill_manager = SkillManager(skill_dirs=configured_skill_dirs)
            self.tool_executor.skill_manager = self.skill_manager
        if self.delegation_depth == 0:
            if self.multi_agent_runtime is None and config.multi_agent.enabled:
                from ares.multi_agent_runtime import MultiAgentRuntime

                self.multi_agent_runtime = MultiAgentRuntime(self)
        self.refresh_tools()

    def reload_runtime_content(self) -> None:
        """Refresh local instructions that may change while ``ares --all`` runs.

        Skills, profile, and soul content are deliberately read from local files
        at turn time. Replacing the catalog here also picks up created/deleted
        skills and supporting instruction files without changing an active
        conversation's memory or cancelling its work.
        """
        self.profile_manager.ensure_exists()
        self.soul_manager.ensure_exists()
        skill_dirs = list(self.config.skill_dirs or [])
        self.skill_manager = SkillManager(skill_dirs=skill_dirs or None)
        self.tool_executor.skill_manager = self.skill_manager
        self.refresh_tools()

    @staticmethod
    def _tool_call_args(call: dict) -> dict:
        """Parse tool call arguments into a dict."""
        raw_args = call.get("function", {}).get("arguments") or "{}"
        if isinstance(raw_args, str):
            return json.loads(raw_args)
        return raw_args or {}

    def _resolve_email_reference(self, value: Any) -> str:
        """Resolve a saved name locally while never surfacing the email to the LLM."""
        text = str(value or "").strip()
        people_store = getattr(self, "people_store", None)
        if not text or people_store is None:
            return text
        entries = [item.strip() for item in text.split(",") if item.strip()]
        resolved: list[str] = []
        for entry in entries:
            if "@" in entry:
                resolved.append(entry)
                continue
            person = people_store.resolve(entry, require="email")
            resolved.append(str(person["email"]))
        return ", ".join(resolved)

    def _resolve_external_person_arguments(self, tool_name: str, args: dict) -> dict:
        """Resolve Gmail/calendar aliases before the MCP boundary only."""
        lowered = str(tool_name).casefold()
        people_store = getattr(self, "people_store", None)
        if not lowered.startswith("mcp__") or people_store is None:
            return args
        resolved = dict(args)
        if lowered.endswith("__gmail_send"):
            if "to" in resolved:
                resolved["to"] = self._resolve_email_reference(resolved.get("to"))
            if resolved.get("cc"):
                resolved["cc"] = self._resolve_email_reference(resolved.get("cc"))
        elif lowered.endswith("__calendar_create_event") and resolved.get("attendees"):
            raw_attendees = resolved.get("attendees")
            if isinstance(raw_attendees, str):
                raw_attendees = [item.strip() for item in raw_attendees.split(",") if item.strip()]
            if not isinstance(raw_attendees, list):
                raise ValueError("calendar attendees must be a list of email addresses or saved person aliases")
            emails = []
            for attendee in raw_attendees:
                text = str(attendee or "").strip()
                if not text:
                    continue
                emails.append(text if "@" in text else str(people_store.resolve(text, require="email")["email"]))
            resolved["attendees"] = emails
        return resolved

    async def _execute_workflow_step(self, tool_name: str, arguments: dict) -> str:
        """Run one workflow step through the same normal Agent dispatcher."""
        call = {
            "id": f"workflow_{tool_name}",
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }
        results = await self.process_tool_calls_async([call])
        return str(results[0]["content"]) if results else "Error: Workflow step produced no result."

    def process_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Execute tool calls from synchronous callers.

        MCP tools are asynchronous; use ``process_tool_calls_async`` when already
        inside an event loop. This wrapper keeps older tests and integrations that
        call local tools synchronously working.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.process_tool_calls_async(tool_calls))
        if any((call.get("function", {}).get("name") or "").startswith("mcp__") for call in tool_calls):
            raise RuntimeError("MCP tool calls require awaiting process_tool_calls_async().")
        return self._process_tool_calls_sync(tool_calls)

    def _process_tool_calls_sync(self, tool_calls: list[dict]) -> list[dict]:
        """Execute non-MCP tool calls locally and return local metadata."""
        results = self._process_tool_calls_core(tool_calls, mcp_results=None)
        context = self.turn_context
        if context is not None and getattr(self, "delegation_depth", 0) == 0:
            self._record_ordinary_tool_calls(
                context, tool_calls, ((index,) for index in range(len(tool_calls))), results
            )
        return results

    async def _execute_external_tool(self, tool_name: str, args: dict) -> str:
        """Execute one MCP call, serializing only the shared browser surface."""
        resolved_args = self._resolve_external_person_arguments(tool_name, args)
        browser_controller = getattr(self, "browser_controller", None)
        if browser_controller is None:
            if self.mcp_manager is None:
                return "Error: MCP manager is not configured."
            return await self.mcp_manager.call_tool(tool_name, resolved_args)
        from ares.multi_agent_policy import ToolResource, classify_tool

        is_browser = (
            browser_controller.is_playwright_tool(tool_name)
            or classify_tool(tool_name) in {
                ToolResource.BROWSER_READ,
                ToolResource.BROWSER_INTERACTION,
            }
        )

        async def execute() -> str:
            preflight = browser_controller.before_call(
                self.session_id, tool_name, resolved_args
            )
            used_cached_snapshot = preflight.cached_result is not None
            if preflight.cached_result is not None:
                result = preflight.cached_result
            elif not preflight.allowed:
                result = preflight.message
            elif self.mcp_manager is None:
                result = "Error: MCP manager is not configured."
            else:
                lowered = tool_name.casefold()
                is_windows_snapshot = lowered.startswith("mcp__windows__") and lowered.endswith("__snapshot")
                cache_key = str(self.session_id or "root")
                args_key = json.dumps(resolved_args, sort_keys=True, default=str)
                cached = self._windows_snapshot_cache.get(cache_key)
                cache_seconds = float(getattr(self.config, "windows_snapshot_cache_seconds", 1.5))
                if is_windows_snapshot and cached and cached[2] == args_key and time.monotonic() - cached[0] <= cache_seconds:
                    result = cached[1]
                elif is_windows_snapshot:
                    timeout = float(getattr(self.config, "windows_snapshot_timeout_seconds", 12.0))
                    try:
                        async with asyncio.timeout(timeout):
                            result = await self.mcp_manager.call_tool(tool_name, resolved_args)
                    except TimeoutError:
                        screenshot_tool = next(
                            (
                                str(schema.get("function", {}).get("name") or "")
                                for schema in getattr(self.mcp_manager, "tool_definitions", [])
                                if str(schema.get("function", {}).get("name") or "").casefold().endswith("__screenshot")
                                and str(schema.get("function", {}).get("name") or "").casefold().startswith("mcp__windows__")
                            ),
                            "",
                        )
                        if not screenshot_tool:
                            result = f"Error: Windows UI-tree Snapshot exceeded {timeout:.0f}s. Use the fast Windows Screenshot tool or retry Snapshot with use_ui_tree=false."
                        else:
                            fast = await self.mcp_manager.call_tool(screenshot_tool, {"use_annotation": False})
                            result = (
                                f"Windows UI-tree Snapshot exceeded {timeout:.0f}s, so Ares automatically returned a fast screenshot-only capture. "
                                "Use Snapshot again only if interactive element IDs are essential.\n\n"
                                f"{fast}"
                            )
                    self._windows_snapshot_cache[cache_key] = (time.monotonic(), str(result), args_key)
                    if len(self._windows_snapshot_cache) > 64:
                        oldest = min(self._windows_snapshot_cache, key=lambda key: self._windows_snapshot_cache[key][0])
                        self._windows_snapshot_cache.pop(oldest, None)
                else:
                    if lowered.startswith("mcp__windows__"):
                        self._windows_snapshot_cache.pop(cache_key, None)
                    result = await self.mcp_manager.call_tool(tool_name, resolved_args)
            if not used_cached_snapshot:
                result = browser_controller.after_call(
                    self.session_id, tool_name, resolved_args, result
                )
            if (
                self.mcp_manager is not None
                and browser_controller.should_recover_stale_ref(
                    self.session_id, tool_name, resolved_args, result
                )
            ):
                snapshot_tool = "mcp__playwright__browser_snapshot"
                snapshot = await self.mcp_manager.call_tool(snapshot_tool, {})
                snapshot = browser_controller.after_call(
                    self.session_id, snapshot_tool, {}, snapshot
                )
                executor = getattr(self, "tool_executor", None)
                if executor is not None:
                    executor.record_external_action(snapshot_tool, {}, snapshot)
                if browser_controller.result_succeeded(snapshot):
                    return (
                        f"{result}\n\nPlaywright recovery: captured one fresh snapshot automatically. "
                        "Do not retry the old ref. Choose a ref from the snapshot below and make at most one "
                        f"evidence-based retry.\n\n{snapshot}"
                    )
                return (
                    f"{result}\n\nPlaywright recovery failed to capture a fresh snapshot. "
                    f"Stop retrying and report the browser blocker.\n\n{snapshot}"
                )
            return result

        if is_browser:
            async with self._playwright_tool_lock:
                return await execute()
        return await execute()

    def _authorize_tool(self, tool_name: str, args: dict) -> None:
        turn_context = None
        turn_var = getattr(self, "_turn_context", None)
        if turn_var is not None and getattr(self, "delegation_depth", 0) == 0:
            scoped = turn_var.get()
            if scoped is _TURN_UNSET:
                raise PermissionError(
                    "root tool dispatch requires an immutable current-turn authorization context"
                )
            turn_context = scoped
        if turn_context is not None:
            decision = authorize_turn_tool(
                turn_context,
                tool_name,
                args,
                grant_uses=getattr(self, "_turn_grant_uses", None),
            )
            if not decision.allowed:
                raise PermissionError(decision.reason)

        authorizer = getattr(self, "_tool_authorizer", None)
        if authorizer is None:
            return
        decision = authorizer(tool_name, args)
        allowed = bool(getattr(decision, "allowed", decision))
        if not allowed:
            reason = str(getattr(decision, "reason", "tool call is not authorized"))
            raise PermissionError(reason)

    async def _dispatch_one_tool_async(
        self,
        tool_name: str,
        args: dict,
        progress_callback: ToolProgressCallback | None,
    ) -> tuple[bool, str]:
        """Dispatch one already-authorized call inside its resource lease."""
        if progress_callback is not None:
            await progress_callback(tool_name, "Preparing input")
        if tool_name.startswith("mcp__"):
            if progress_callback is not None:
                await progress_callback(tool_name, "Calling connected tool")
            result = await self._execute_external_tool(tool_name, args)
            executor = getattr(self, "tool_executor", None)
            if executor is not None:
                executor.record_external_action(tool_name, args, result)
            external = True
        elif tool_name in {
            "list_agents", "delegate_task", "delegate_tasks_parallel", "get_agent_run",
            "list_agent_runs", "get_latest_agent_run", "cancel_agent_run", "resume_agent_run",
        }:
            if self.multi_agent_runtime is None:
                result = "Error: Native multi-agent mode is disabled. No agents ran."
            else:
                if progress_callback is not None:
                    await progress_callback(tool_name, "Running native specialists")
                runtime_args = dict(args)
                turn_context = self.turn_context
                if turn_context is not None:
                    runtime_args.setdefault("request_id", turn_context.request_id)
                result = await self.multi_agent_runtime.execute_tool(
                    tool_name, runtime_args, session_id=self.session_id
                )
            external = False
        elif tool_name == "run_task":
            if progress_callback is not None:
                await progress_callback(tool_name, "Running durable workflow steps")
            if self.workflow_runner is None:
                result = "Error: Workflow runner is unavailable because local task storage is not configured."
            else:
                result = await self.workflow_runner.run(
                    str(args.get("task_id", "")),
                    confirm=bool(args.get("confirm", False)),
                    max_steps=int(args.get("max_steps", 25)),
                )
            external = False
        else:
            if progress_callback is not None:
                await progress_callback(tool_name, "Running locally")
            result = await self.tool_executor.execute_async(tool_name, args)
            external = False
        if progress_callback is not None:
            await progress_callback(tool_name, "Finished")
        return external, str(result)

    async def _execute_one_tool_async(
        self,
        index: int,
        call: dict,
        progress_callback: ToolProgressCallback | None,
    ) -> tuple[int, bool, str]:
        tool_name = call.get("function", {}).get("name", "unknown")
        try:
            args = self._tool_call_args(call)
            self._authorize_tool(tool_name, args)
            coordinator = getattr(self, "resource_coordinator", None)
            multi_agent = getattr(getattr(self, "config", None), "multi_agent", None)
            timeout_seconds = max(1.0, float(getattr(multi_agent, "tool_operation_timeout_seconds", 120.0)))
            cleanup_grace = max(0.1, float(getattr(multi_agent, "tool_cancel_grace_seconds", 2.0)))

            async def await_operation(dispatch: asyncio.Task[tuple[bool, str]]) -> tuple[bool, str]:
                done, _pending = await asyncio.wait({dispatch}, timeout=timeout_seconds)
                if not done:
                    dispatch.cancel()
                    done, _pending = await asyncio.wait({dispatch}, timeout=cleanup_grace)
                    if not done:
                        if coordinator is not None:
                            record = await coordinator.quarantine_call(
                                tool_name, dispatch,
                                owner_run_id=self.root_run_id or self.child_run_id,
                                reason="timeout",
                            )
                            self.unresponsive_tool_records.append(record)
                        return tool_name.startswith("mcp__"), (
                            f"Error: {tool_name} timed out after {timeout_seconds:g}s; "
                            "the resource is quarantined until its operation exits."
                        )
                if dispatch.cancelled():
                    return tool_name.startswith("mcp__"), f"Error: {tool_name} was cancelled after timeout."
                return dispatch.result()

            if coordinator is None:
                dispatch = asyncio.create_task(
                    self._dispatch_one_tool_async(tool_name, args, progress_callback)
                )
                try:
                    external, result = await await_operation(dispatch)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    dispatch.cancel()
                    await asyncio.wait({dispatch}, timeout=cleanup_grace)
                    raise
            else:
                async with coordinator.acquire_call(tool_name, args):
                    dispatch = asyncio.create_task(
                        self._dispatch_one_tool_async(tool_name, args, progress_callback)
                    )
                    try:
                        external, result = await await_operation(dispatch)
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None:
                            current.uncancel()
                        dispatch.cancel()
                        done, _pending = await asyncio.wait({dispatch}, timeout=cleanup_grace)
                        if not done:
                            record = await coordinator.quarantine_call(
                                tool_name, dispatch,
                                owner_run_id=self.root_run_id or self.child_run_id,
                                reason="cancelled_with_unresponsive_tool",
                            )
                            self.unresponsive_tool_records.append(record)
                        raise
            self.tool_execution_records.append({"tool": tool_name, "result": str(result)[:50_000]})
            return index, external, str(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if progress_callback is not None:
                await progress_callback(tool_name, f"Failed: {exc}")
            return index, tool_name.startswith("mcp__"), f"Error: {exc}"

    async def process_tool_calls_async(
        self,
        tool_calls: list[dict],
        progress_callback: ToolProgressCallback | None = None,
    ) -> list[dict]:
        """Execute safe independent calls concurrently and preserve result order."""
        from ares.multi_agent_policy import call_resource, execution_waves

        mcp_results: dict[int, str] = {}
        local_results: dict[int, str] = {}
        resources = []
        for i, call in enumerate(tool_calls):
            try:
                arguments = self._tool_call_args(call)
            except (json.JSONDecodeError, TypeError, ValueError):
                arguments = {}
            resources.append(call_resource(i, call, arguments))
        wave_plan = execution_waves(resources)
        for wave in wave_plan:
            completed = await asyncio.gather(*(
                self._execute_one_tool_async(i, tool_calls[i], progress_callback)
                for i in wave
            ))
            for index, external, result in completed:
                (mcp_results if external else local_results)[index] = result
        results = self._process_tool_calls_core(
            tool_calls,
            mcp_results=mcp_results,
            local_results=local_results,
        )
        context = self.turn_context
        if context is not None and getattr(self, "delegation_depth", 0) == 0:
            self._record_ordinary_tool_calls(context, tool_calls, wave_plan, results)
        return results

    def _process_tool_calls_core(
        self,
        tool_calls: list[dict],
        mcp_results: dict[int, str] | None,
        local_results: dict[int, str] | None = None,
    ) -> list[dict]:
        """Execute/assemble tool results."""
        results = []
        for i, call in enumerate(tool_calls):
            tool_name = call.get("function", {}).get("name", "unknown")

            try:
                fn = call["function"]
                tool_name = fn["name"]
                args = self._tool_call_args(call)
                if tool_name.startswith("mcp__"):
                    result = (mcp_results or {}).get(i, "Error: MCP tool was not executed.")
                elif local_results is not None and i in local_results:
                    result = local_results[i]
                else:
                    # Synchronous embedders still cross the same hard dispatch
                    # authorization boundary as the async model loop.
                    self._authorize_tool(tool_name, args)
                    result = self.tool_executor.execute(tool_name, args)
            except Exception as e:
                result = f"Error: {e}"

            results.append({
                "tool_call_id": call.get("id") or f"call_{i}",
                "role": "tool",
                "content": result,
                "tool_name": tool_name,
            })
        return results

    def _tool_messages(self, tool_results: list[dict]) -> list[dict]:
        """Strip local metadata before sending tool results back to the LLM."""
        return [
            {
                "tool_call_id": result["tool_call_id"],
                "role": result["role"],
                "content": result["content"],
            }
            for result in tool_results
        ]

    def _delegation_decision(self, context: TurnExecutionContext) -> DelegationDecision:
        """Route this request before the general model sees any tool schemas."""
        if getattr(self, "delegation_depth", 0) > 0:
            # A specialist prompt can legitimately mention agents, reviewers,
            # or multi-agent work. It is assignment data, never an instruction
            # to re-enter root-only delegation or meta routing.
            return DelegationDecision(
                mode=DelegationMode.NONE,
                should_delegate=False,
                reason="Child specialists cannot perform root delegation routing.",
            )
        runtime = getattr(self, "multi_agent_runtime", None)
        config = getattr(getattr(self, "config", None), "multi_agent", None)
        roles: tuple[str, ...] = ()
        if runtime is not None:
            try:
                roles = tuple(runtime.registry.snapshot())
            except Exception:
                try:
                    roles = tuple(str(item.get("name") or "") for item in runtime.list_agents())
                except Exception:
                    roles = ()
        availability = DelegationAvailability(
            enabled=bool(getattr(config, "enabled", False)),
            runtime_available=runtime is not None,
            available_roles=tuple(role for role in roles if role),
            max_tasks_per_run=max(1, int(getattr(config, "max_tasks_per_run", 8))),
        )
        return DelegationRouter().route(context, availability)

    async def _run_delegation_plan(
        self,
        context: TurnExecutionContext,
        decision: DelegationDecision,
    ) -> AgentTeamResult:
        runtime = getattr(self, "multi_agent_runtime", None)
        if runtime is None:
            raise RuntimeError("native multi-agent runtime is unavailable")
        tasks = tuple(
            AgentTask(
                task_id=item.task_id,
                agent=item.agent,
                prompt=item.prompt,
                depends_on=item.depends_on,
                result_format="json" if item.agent in {"researcher", "synthesizer"} else "text",
                allow_partial_dependencies=item.agent == "synthesizer",
                allowed_context=("task_dependencies",) if item.depends_on else (),
            )
            for item in decision.plan
        )
        return await runtime.delegate(
            tasks,
            shared_context=context.user_input[:12_000],
            session_id=context.session_id,
            request_id=context.request_id,
        )

    @staticmethod
    def _bounded_agent_evidence(value: Any, *, max_chars: int = 120_000) -> str:
        """Serialize runtime truth while preserving bounded child evidence and URLs."""
        payload: Any = value.as_dict() if isinstance(value, AgentTeamResult) else value
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        if len(rendered) <= max_chars:
            return rendered
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            compact = dict(payload)
            compact_results = []
            for raw in payload["results"]:
                item = dict(raw) if isinstance(raw, dict) else {"content": str(raw)}
                if len(str(item.get("content") or "")) > 12_000:
                    item["content"] = str(item["content"])[:12_000] + "\n[bounded evidence truncated]"
                compact_results.append(item)
            compact["results"] = compact_results
            rendered = json.dumps(compact, ensure_ascii=False, default=str)
        return rendered[:max_chars] + (
            "\n[bounded manifest serialization truncated]" if len(rendered) > max_chars else ""
        )

    async def _prepare_delegation_turn(
        self,
        context: TurnExecutionContext,
    ) -> tuple[DelegationDecision, str, str | None]:
        """Run deterministic delegation/introspection or return an honest blocker."""
        decision = self._delegation_decision(context)
        runtime = getattr(self, "multi_agent_runtime", None)

        if decision.mode is DelegationMode.META:
            lowered = context.user_input.casefold()
            if re.search(r"\b(?:resume|continue)\s+(?:the\s+)?agent\s+run\b", lowered):
                if runtime is None:
                    return decision, "", "The native multi-agent runtime is unavailable, so no agent run was resumed."
                explicit_id = re.search(r"\bma_[A-Za-z0-9_-]+\b", context.user_input)
                if explicit_id is None:
                    return decision, "", (
                        "Please specify the exact native agent run ID to resume; "
                        "use /agents runs to inspect your session-owned checkpoints."
                    )
                run_id = explicit_id.group(0)
                try:
                    # This direct-language route must satisfy the same immutable
                    # root-turn permission check as the exposed runtime tool.
                    self._authorize_tool("resume_agent_run", {"run_id": run_id})
                    team = await runtime.resume(
                        run_id, session_id=context.session_id, request_id=context.request_id
                    )
                except Exception as exc:
                    return decision, "", (
                        f"Could not resume native agent run {run_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                payload = team.as_dict()
                self._set_execution_record(context, {
                    "kind": "native", "agent_count": int(payload.get("agent_count") or len(team.results)),
                    "status": team.status, "payload": payload,
                })
                return decision, "", (
                    f"Resumed native agent run {run_id} as {team.root_run_id}.\n\n"
                    f"{self._render_execution_record(self._last_execution_record(context))}"
                )
            if re.search(r"\b(?:cancel|stop)\b", lowered):
                if runtime is None:
                    return decision, "", "The native multi-agent runtime is unavailable, so no agent run was cancelled."
                explicit_id = re.search(r"\bma_[A-Za-z0-9_-]+\b", context.user_input)
                latest = runtime.get_latest_run(session_id=context.session_id)
                run_id = explicit_id.group(0) if explicit_id else str((latest or {}).get("root_run_id") or (latest or {}).get("run_id") or "")
                if not run_id:
                    return decision, "", "No real native agent run exists in this session, so nothing was cancelled."
                try:
                    self._authorize_tool("cancel_agent_run", {"run_id": run_id})
                    cancelled = await runtime.cancel(run_id, session_id=context.session_id)
                except Exception as exc:
                    return decision, "", (
                        f"Could not cancel native agent run {run_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                if cancelled:
                    refreshed = runtime.get_run(run_id, session_id=context.session_id)
                    if refreshed is not None:
                        self._execution_records[self._execution_session_key(context.session_id)] = {
                            "kind": "native", "payload": refreshed,
                            "request_id": str(refreshed.get("request_id") or ""),
                            "session_id": context.session_id, "recorded_at": time.time(),
                        }
                    return decision, "", f"Cancelled native agent run {run_id}."
                return decision, "", f"Agent run {run_id} is not active in this session, so it was not cancelled."
            explicit_id = re.search(r"\bma_[A-Za-z0-9_-]+\b", context.user_input)
            if runtime is not None and explicit_id is not None:
                selected = runtime.get_run(
                    explicit_id.group(0), session_id=context.session_id
                )
                if selected is None:
                    return decision, "", "Agent run not found in this session."
                return decision, "", self._render_execution_record({
                    "kind": "native", "payload": selected,
                })
            if runtime is not None and re.search(r"\b(?:list|show)\b.*\bagent\s+runs\b", lowered):
                runs = runtime.list_runs(limit=30, session_id=context.session_id)
                if not runs:
                    return decision, "", "No native agent runs exist in this session."
                rendered = [
                    self._render_execution_record({"kind": "native", "payload": run})
                    for run in runs
                ]
                return decision, "", "\n".join(rendered)
            record = self._last_execution_record(context)
            if record is None and runtime is not None:
                run = runtime.get_latest_run(session_id=context.session_id)
                if run is not None:
                    record = {"kind": "native", "payload": run}
            return decision, "", self._render_execution_record(record)

        if not decision.should_delegate:
            if decision.mode is DelegationMode.EXPLICIT:
                reason = decision.honest_failure_message or decision.reason
                self._set_execution_record(context, {
                    "kind": "delegation_failure", "agent_count": 0,
                    "status": "failed", "reason": reason,
                })
                return decision, "", reason
            return decision, "", None

        try:
            team = await self._run_delegation_plan(context, decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = runtime_failure_decision(exc, mode=decision.mode)
            if decision.mode is DelegationMode.EXPLICIT:
                self._set_execution_record(context, {
                    "kind": "delegation_failure", "agent_count": 0,
                    "status": failed.failure_reason.value if failed.failure_reason else "failed",
                    "reason": failed.reason,
                })
                return failed, "", failed.reason
            return failed, self._bounded_agent_evidence({
                "agent_count": 0,
                "status": "delegation_failed",
                "reason": failed.reason,
            }), None
        payload = team.as_dict()
        self._set_execution_record(context, {
            "kind": "native", "agent_count": int(payload.get("agent_count") or len(team.results)),
            "status": team.status, "payload": payload,
        })
        if decision.mode is DelegationMode.EXPLICIT and not any(result.ok for result in team.results):
            return decision, "", self._render_execution_record(self._last_execution_record(context))
        return decision, self._bounded_agent_evidence(payload), None

    @staticmethod
    def _agent_evidence_instruction(evidence: str) -> str:
        return (
            "## Verified Native Agent Execution Evidence\n"
            "The JSON below was read directly from the current session's runtime/store. "
            "It is the only authority for whether agents ran, their count, roles, IDs, waves, "
            "timing, tools, artifacts, failures, and research sources. Parallel ordinary tool "
            "calls are not agents. Never infer agent facts from conversation prose or a plan. "
            "Do not claim more agents or higher research confidence than this evidence supports. "
            "Preserve source URLs, disagreements, conditions, and uncertainty in the answer.\n\n"
            f"```json\n{evidence}\n```"
        )

    @staticmethod
    def _strip_unverified_agent_claims(content: str) -> tuple[str, bool]:
        """Remove model-authored execution claims; runtime renders those facts."""
        count = r"(?:\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
        claim = re.compile(
            rf"(?:\b{count}\s+(?:native\s+)?(?:agents?|researchers?|specialists?)\b|"
            rf"\b{count}\s+(?:ordinary\s+)?tool\s+calls?\b|"
            r"\b(?:agent|researcher|builder|reviewer|specialist)s?\b.*"
            r"\b(?:ran|launched|spawned|used|wave|parallel|manifest|run\s+id)\b)",
            re.IGNORECASE,
        )
        kept: list[str] = []
        removed = False
        for line in str(content or "").splitlines():
            if claim.search(line):
                removed = True
                continue
            kept.append(line)
        return "\n".join(kept).strip(), removed

    @staticmethod
    def _native_record_payload(record: dict[str, Any]) -> dict[str, Any]:
        payload = record.get("payload")
        return dict(payload) if isinstance(payload, dict) else dict(record)

    def _render_execution_record(self, record: dict[str, Any] | None) -> str:
        if not record:
            return "No recorded execution exists for the prior request in this session: 0 native agents and 0 ordinary tool calls."
        kind = str(record.get("kind") or "")
        if kind == "ordinary":
            count = int(record.get("tool_call_count") or 0)
            tools = [str(item) for item in record.get("tools") or []]
            waves = record.get("execution_waves") or []
            details = f" Tools: {', '.join(tools)}." if tools else ""
            wave_text = f" Execution waves: {json.dumps(waves, ensure_ascii=False)}." if waves else ""
            inspection_tools = {
                "list_agents", "get_agent_run", "list_agent_runs", "get_latest_agent_run"
            }
            if tools and set(tools).issubset(inspection_tools):
                return (
                    "No new native agents were launched for this follow-up. "
                    f"The root inspected the existing run with {count} session-scoped tool "
                    f"call{'s' if count != 1 else ''}.{details}{wave_text}"
                )
            return (
                f"0 native agents ran for request {record.get('request_id') or 'unknown'}. "
                f"The root executed {count} ordinary tool call{'s' if count != 1 else ''}; ordinary parallel tool calls are not agents."
                f"{details}{wave_text}"
            )
        if kind == "delegation_failure":
            return (
                f"0 native agents completed for request {record.get('request_id') or 'unknown'}. "
                f"Delegation status: {record.get('status') or 'failed'}. "
                f"Reason: {record.get('reason') or 'unknown failure'}."
            )
        payload = self._native_record_payload(record)
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else payload
        children = manifest.get("child_runs") or payload.get("children") or payload.get("results") or []
        roles = [
            str(item.get("role") or item.get("agent_role") or item.get("agent") or "unknown")
            for item in children if isinstance(item, dict)
        ]
        tools = [
            str(tool)
            for item in children if isinstance(item, dict)
            for tool in (item.get("tools") or item.get("metadata", {}).get("tools") or [])
        ]
        run_id = str(manifest.get("root_run_id") or payload.get("root_run_id") or payload.get("run_id") or "unknown")
        count = int(manifest.get("agent_count") or len(children))
        waves = (
            manifest.get("execution_waves")
            or payload.get("execution_waves")
            or (manifest.get("metadata") or {}).get("execution_waves")
            or []
        )
        duration = manifest.get("duration_seconds", payload.get("duration_seconds"))
        status = str(manifest.get("status") or payload.get("status") or "unknown")
        duration_text = f", duration {float(duration):.3f}s" if isinstance(duration, (int, float)) else ""
        tool_text = f" Tools used by children: {', '.join(tools)}." if tools else ""
        return (
            f"Verified native run {run_id}: {count} agent{'s' if count != 1 else ''} "
            f"({', '.join(roles) or 'no child roles'}), status {status}{duration_text}. "
            f"Execution waves: {json.dumps(waves, ensure_ascii=False)}.{tool_text} "
            "They were launched by the root-owned native MultiAgentRuntime; ordinary parallel tool calls were not counted as agents."
        )

    def _render_execution_summary(self, record: dict[str, Any] | None) -> str:
        """Render runtime truth without leaking the internal audit manifest into chat."""
        if not record:
            return "Agent status: no agent run was recorded for that request."
        kind = str(record.get("kind") or "")
        if kind == "ordinary":
            tools = [str(item) for item in record.get("tools") or []]
            inspection_tools = {
                "list_agents", "get_agent_run", "list_agent_runs", "get_latest_agent_run"
            }
            if tools and set(tools).issubset(inspection_tools):
                return "Agent status: I inspected the existing run; I did not launch a new team for this follow-up."
            return "Agent status: this request used regular tools, not specialist agents."
        if kind == "delegation_failure":
            reason = str(record.get("reason") or "the run could not be started")
            return f"Agent status: no specialist completed the request — {reason}"

        payload = self._native_record_payload(record)
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else payload
        children = manifest.get("child_runs") or payload.get("children") or payload.get("results") or []
        count = int(manifest.get("agent_count") or len(children))
        status = str(manifest.get("status") or payload.get("status") or "unknown").replace("_", " ")
        roles = [
            str(item.get("role") or item.get("agent_role") or item.get("agent") or "specialist")
            for item in children if isinstance(item, dict)
        ]
        role_text = ", ".join(roles)
        role_suffix = f" ({role_text})" if role_text else ""
        return (
            f"Agent status: {count} specialist{'s' if count != 1 else ''}{role_suffix}; "
            f"run {status}."
        )

    def _guard_final_answer(
        self,
        context: TurnExecutionContext,
        decision: DelegationDecision,
        content: str,
    ) -> str:
        cleaned, removed = self._strip_unverified_agent_claims(content)
        record = self._last_execution_record(context)
        if decision.should_delegate and record and record.get("request_id") == context.request_id:
            payload = self._native_record_payload(record)
            evidence_text = json.dumps(payload, ensure_ascii=False, default=str)
            urls = tuple(dict.fromkeys(re.findall(r"https?://[^\s\"'<>\]]+", evidence_text)))
            footer = self._render_execution_summary(record)
            missing = [url.rstrip(".,;)") for url in urls if url.rstrip(".,;)") not in cleaned]
            if missing:
                footer += "\n\nVerified sources:\n" + "\n".join(f"- {url}" for url in missing[:50])
            return "\n\n".join(part for part in (cleaned, footer) if part).strip()
        if removed:
            truth = self._render_execution_summary(record)
            return "\n\n".join(part for part in (cleaned, truth) if part).strip()
        return cleaned or str(content or "")

    async def run(
        self,
        user_input: str,
        conversation_history: list[dict],
        *,
        reflection_input: str | None = None,
        request_id: str | None = None,
        confirmation_grants: Iterable[TurnActionGrant] = (),
    ) -> AsyncIterator[str]:
        """Run one request under immutable current-turn authority."""
        if self.reflection_service is not None:
            # Tests and embedders sometimes replace ``agent.llm`` after
            # construction. Reflection is a separate call, but it should use
            # the agent's current client rather than a stale network client.
            self.reflection_service.llm = self.llm
            self.reflection_service.reflector.llm = self.llm
            await self.reflection_service.before_turn(self.session_id)
        routing_input, reference_error = self._resolve_referential_delegation(
            user_input, conversation_history
        )
        turn_context = self._new_turn_context(
            routing_input,
            request_id=request_id,
            confirmation_grants=confirmation_grants,
        )
        with self.turn_scope(turn_context):
            if reference_error is not None:
                self._set_execution_record(turn_context, {
                    "kind": "delegation_failure", "agent_count": 0,
                    "status": "unresolved_reference", "reason": reference_error,
                })
                yield reference_error
                return
            decision, evidence, terminal = await self._prepare_delegation_turn(turn_context)
            if terminal is not None:
                self.last_messages = [
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": terminal},
                ]
                yield terminal
                return
            tools_override = (
                [] if decision.mode is DelegationMode.META or decision.should_delegate else None
            )
            async for chunk in self._run_scoped(
                user_input,
                conversation_history,
                turn_context,
                delegation_decision=decision,
                agent_evidence=evidence,
                tools_override=tools_override,
                reflection_input=reflection_input,
            ):
                yield chunk

    async def _run_scoped(
        self,
        user_input: str,
        conversation_history: list[dict],
        turn_context: TurnExecutionContext,
        *,
        delegation_decision: DelegationDecision,
        agent_evidence: str = "",
        tools_override: list[dict] | None = None,
        reflection_input: str | None = None,
    ) -> AsyncIterator[str]:
        """Run the non-streaming model loop inside an established turn scope."""
        # Build context
        context = self.get_context(user_input, conversation_history)
        messages = self.build_messages(user_input, conversation_history, context)
        if agent_evidence:
            messages.insert(-1, {
                "role": "system",
                "content": self._agent_evidence_instruction(agent_evidence),
            })
        turn_tools = (
            tools_override
            if tools_override is not None
            else self._tools_for_turn(turn_context, delegation_decision)
        )

        # Agent loop: keep going while LLM wants to call tools
        max_iterations = self.config.agent_max_iterations
        for iteration in range(max_iterations):
            self.last_iteration_count = iteration + 1
            response = await self.llm.chat(messages, tools=turn_tools)

            # Check for tool calls
            if response.get("tool_calls"):
                # Ensure every tool call has a non-empty id
                for i, tc in enumerate(response["tool_calls"]):
                    if not tc.get("id"):
                        tc["id"] = f"call_{iteration}_{i}"

                # Add assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": response.get("content") or "",
                    "tool_calls": response["tool_calls"],
                })

                # Execute tools
                tool_results = await self.process_tool_calls_async(response["tool_calls"])
                messages.extend(self._tool_messages(tool_results))

                # Let LLM process tool results and continue
                continue

            # No tool calls — LLM produced final text response
            content = response.get("content", "")
            if not delegation_decision.should_delegate:
                self._ensure_ordinary_execution_record(turn_context)
            content = self._guard_final_answer(
                turn_context, delegation_decision, str(content or "")
            )
            messages.append({"role": "assistant", "content": content})
            self.last_messages = messages
            if self.reflection_service is not None:
                self.reflection_service.enqueue_turn(
                    scope=self.session_id,
                    user_text=reflection_input if reflection_input is not None else user_input,
                    assistant_text=content,
                )
            if content:
                yield content
            return

        # If we exhaust all iterations, warn the user
        if not delegation_decision.should_delegate:
            self._ensure_ordinary_execution_record(turn_context)
        yield "\n\n[Warning: Reached maximum tool iterations limit. Some steps may not have completed.]"

    async def run_stream(
        self,
        user_input: str,
        conversation_history: list[dict],
        *,
        reflection_input: str | None = None,
        request_id: str | None = None,
        confirmation_grants: Iterable[TurnActionGrant] = (),
    ) -> AsyncIterator[str]:
        """Run streaming-first under immutable current-turn authority."""
        if self.reflection_service is not None:
            self.reflection_service.llm = self.llm
            self.reflection_service.reflector.llm = self.llm
            await self.reflection_service.before_turn(self.session_id)
        routing_input, reference_error = self._resolve_referential_delegation(
            user_input, conversation_history
        )
        turn_context = self._new_turn_context(
            routing_input,
            request_id=request_id,
            confirmation_grants=confirmation_grants,
        )
        with self.turn_scope(turn_context):
            if reference_error is not None:
                self._set_execution_record(turn_context, {
                    "kind": "delegation_failure", "agent_count": 0,
                    "status": "unresolved_reference", "reason": reference_error,
                })
                yield reference_error
                return
            decision, evidence, terminal = await self._prepare_delegation_turn(turn_context)
            if terminal is not None:
                self.last_messages = [
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": terminal},
                ]
                yield terminal
                return
            tools_override = (
                [] if decision.mode is DelegationMode.META or decision.should_delegate else None
            )
            async for chunk in self._run_stream_scoped(
                user_input,
                conversation_history,
                turn_context,
                delegation_decision=decision,
                agent_evidence=evidence,
                tools_override=tools_override,
                reflection_input=reflection_input,
            ):
                yield chunk

    async def _run_stream_scoped(
        self,
        user_input: str,
        conversation_history: list[dict],
        turn_context: TurnExecutionContext,
        *,
        delegation_decision: DelegationDecision,
        agent_evidence: str = "",
        tools_override: list[dict] | None = None,
        reflection_input: str | None = None,
    ) -> AsyncIterator[str]:
        """Run the streaming model loop inside an established turn scope."""
        context = self.get_context(user_input, conversation_history)
        messages = self.build_messages(user_input, conversation_history, context)
        if agent_evidence:
            messages.insert(-1, {
                "role": "system",
                "content": self._agent_evidence_instruction(agent_evidence),
            })
        turn_tools = (
            tools_override
            if tools_override is not None
            else self._tools_for_turn(turn_context, delegation_decision)
        )

        max_iterations = self.config.agent_max_iterations
        for iteration in range(max_iterations):
            self.last_iteration_count = iteration + 1
            tool_calls: dict[int, dict] = {}
            content_parts: list[str] = []
            has_tool_calls = False

            async for chunk in self.llm.chat_stream(messages, tools=turn_tools):
                chunk_type = chunk.get("type")

                if chunk_type == "content":
                    text = chunk.get("text", "")
                    if text:
                        # Some proxies return accumulated text (full response)
                        # instead of deltas. Detect and keep only the new part.
                        so_far = "".join(content_parts)
                        if text.startswith(so_far) and len(text) > len(so_far):
                            content_parts.append(text[len(so_far):])
                        elif so_far.startswith(text):
                            # Already accumulated this — skip (duplicate)
                            pass
                        else:
                            # Fresh chunk or non-accumulated token.
                            content_parts.append(text)

                elif chunk_type == "tool_call":
                    has_tool_calls = True
                    index = int(chunk.get("index", 0))
                    existing = tool_calls.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if chunk.get("id"):
                        existing["id"] = chunk["id"]
                    if chunk.get("name"):
                        existing["name"] = chunk["name"]

                elif chunk_type == "tool_call_delta":
                    has_tool_calls = True
                    index = int(chunk.get("index", 0))
                    existing = tool_calls.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    existing["arguments"] += chunk.get("arguments", "")

                elif chunk_type == "done":
                    break

            if has_tool_calls:
                formatted_calls = []
                for index in sorted(tool_calls):
                    call = tool_calls[index]
                    formatted_calls.append({
                        "id": call["id"] or f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    })

                messages.append({
                    "role": "assistant",
                    "content": "".join(content_parts),
                    "tool_calls": formatted_calls,
                })
                progress: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

                async def report_tool_progress(tool_name: str, detail: str) -> None:
                    await progress.put((tool_name, detail))

                tool_task = asyncio.create_task(
                    self.process_tool_calls_async(formatted_calls, report_tool_progress)
                )
                active_tools: dict[str, float] = {}
                reported_seconds: dict[str, int] = {}
                try:
                    while not tool_task.done():
                        try:
                            tool_name, detail = await asyncio.wait_for(
                                progress.get(), timeout=0.25
                            )
                            if tool_name not in active_tools:
                                active_tools[tool_name] = asyncio.get_running_loop().time()
                                yield f"[tool_start:{tool_name}]"
                            yield f"[tool_progress:{tool_name}:{detail}]"
                            if detail.casefold().startswith(("finished", "failed")):
                                active_tools.pop(tool_name, None)
                                reported_seconds.pop(tool_name, None)
                        except asyncio.TimeoutError:
                            now = asyncio.get_running_loop().time()
                            for tool_name, started_at in active_tools.items():
                                elapsed = max(1, round(now - started_at))
                                if reported_seconds.get(tool_name) != elapsed:
                                    reported_seconds[tool_name] = elapsed
                                    yield f"[tool_progress:{tool_name}:Still working · {elapsed}s]"
                    while not progress.empty():
                        tool_name, detail = progress.get_nowait()
                        if tool_name not in active_tools:
                            active_tools[tool_name] = asyncio.get_running_loop().time()
                            yield f"[tool_start:{tool_name}]"
                        yield f"[tool_progress:{tool_name}:{detail}]"
                        if detail.casefold().startswith(("finished", "failed")):
                            active_tools.pop(tool_name, None)
                            reported_seconds.pop(tool_name, None)
                    tool_results = await tool_task
                finally:
                    if not tool_task.done():
                        tool_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await tool_task
                for tr in tool_results:
                    yield f"[tool:{tr['tool_name']}:{tr['content']}]"
                messages.extend(self._tool_messages(tool_results))

                continue

            # Save messages for conversation history before returning
            final_content = "".join(content_parts)
            if not delegation_decision.should_delegate:
                self._ensure_ordinary_execution_record(turn_context)
            final_content = self._guard_final_answer(
                turn_context, delegation_decision, final_content
            )
            messages.append({"role": "assistant", "content": final_content})
            self.last_messages = messages
            if self.reflection_service is not None:
                # Queue before yielding so a disconnected streaming consumer
                # cannot lose the reflection job after receiving the answer.
                self.reflection_service.enqueue_turn(
                    scope=self.session_id,
                    user_text=reflection_input if reflection_input is not None else user_input,
                    assistant_text=final_content,
                )
            if final_content:
                yield final_content
            return

        # If we exhaust all iterations, warn the user
        if not delegation_decision.should_delegate:
            self._ensure_ordinary_execution_record(turn_context)
        self.last_messages = messages
        yield "[Warning: Reached maximum tool iterations limit. Some steps may not have completed.]"

    async def close(self):
        """Clean up resources."""
        if self.reflection_service is not None:
            await self.reflection_service.close()
        if self.multi_agent_runtime is not None and self.delegation_depth == 0:
            await self.multi_agent_runtime.close()
        if self._owns_tool_executor:
            self.tool_executor.close()
        await self.llm.close()
