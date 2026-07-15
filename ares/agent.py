"""Core agent loop: LLM interaction, tool execution, context building."""

import asyncio
import json
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator

from ares.context import ProjectContext
from ares.user_context import build_user_context
from ares.autonomy import AutonomousWorkflowRunner
from ares.browser_control import BrowserTaskController
from ares.memory import MemoryStore
from ares.conversations import ConversationStore
from ares.tools import ToolExecutor, get_tool_definitions
from ares.llm import LLMClient
from ares.models import AppConfig
from ares.profile import ProfileManager
from ares.reflection import ReflectionService
from ares.prompts import SYSTEM_PROMPT
from ares.soul import SoulManager
from ares.skills import SkillManager
from ares.tools.datetime_tool import get_current_datetime_result

_SESSION_UNSET = object()
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
    ):
        self.memory_store = memory_store
        self.conversation_store = conversation_store
        self._session_store = session_store
        self._default_session_id = session_id
        self._session_context: ContextVar[str | None | object] = ContextVar(
            f"ares_agent_session_{id(self)}", default=_SESSION_UNSET
        )
        self.tool_executor = ToolExecutor(
            memory_store=memory_store,
            conversation_store=conversation_store,
            config=config,
            mcp_manager=mcp_manager,
            session_store=session_store,
        )
        self._session_store = session_store or self.tool_executor.session_store
        self.mcp_manager = mcp_manager
        self.browser_controller = BrowserTaskController()
        # Browser pages are a single mutable surface. Other MCP servers, local
        # tools, and LLM turns can safely proceed in parallel across chats.
        self._playwright_tool_lock = asyncio.Lock()
        self.is_cron_session = is_cron_session
        self.is_voice_session = is_voice_session
        self.refresh_tools()
        self.last_messages: list[dict] = []

        kwargs = {}
        if api_key or config:
            kwargs["api_key"] = api_key or (config.api_key if config else "")
        if base_url or config:
            kwargs["base_url"] = base_url or (config.api_base_url if config else "")
        if model or config:
            kwargs["model"] = model or (config.model if config else "")
        self.llm = LLMClient(**kwargs)
        if config is not None:
            self.llm.config = config
        self.tool_executor.config = self.llm.config
        self.config = self.llm.config
        self.tool_executor.set_session_id(session_id)
        if getattr(self.tool_executor, "telephony", None) is not None:
            # Phone transcripts use the normal agent loop, so call-time tool
            # access and memory behavior remain identical to chat.
            self.tool_executor.telephony.voice_agent.agent = self
        self.people_store = self.tool_executor.people_store
        self.action_ledger = self.tool_executor.action_ledger
        self.task_store = self.tool_executor.task_store
        self.goal_store = self.tool_executor.goal_store
        self.commitment_store = self.tool_executor.commitment_store
        self.workflow_runner: AutonomousWorkflowRunner | None = None
        if self.task_store is not None and self.action_ledger is not None:
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
                profile_manager=self.profile_manager,
                config=getattr(self.config, "reflection", self.config),
                llm_client=self.llm,
            )
        skill_dirs = list(self.config.skill_dirs or [])
        self.skill_manager = SkillManager(skill_dirs=skill_dirs or None)
        self.tool_executor.skill_manager = self.skill_manager

    def set_session_id(self, session_id: str | None) -> None:
        """Update local provenance scope when a long-lived surface switches chats."""
        self._default_session_id = session_id
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

    def refresh_tools(self) -> None:
        """Refresh the advertised tool list, including connected MCP tools."""
        self.tools = get_tool_definitions()
        if getattr(self, "is_cron_session", False) or getattr(self, "is_voice_session", False):
            cron_names = {"create_cron_job", "list_cron_jobs", "get_cron_job", "update_cron_job", "delete_cron_job", "run_cron_job_now", "get_cron_logs"}
            self.tools = [tool for tool in self.tools if tool.get("function", {}).get("name") not in cron_names]
        if self.mcp_manager is not None:
            self.tools.extend(getattr(self.mcp_manager, "tool_definitions", []))

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
        system_content = SYSTEM_PROMPT
        runtime = get_current_datetime_result()
        system_content += (
            "\n\n## Runtime"
            f"\nCurrent local datetime: {runtime['datetime']}"
            f"\nCurrent local date: {runtime['date']} ({runtime['day_of_week']})"
            f"\nCurrent local time: {runtime['time']}"
            f"\nTimezone: {runtime['timezone']}"
        )
        skill_manager = getattr(self, "skill_manager", None)
        if getattr(self.config, "skills_enabled", True) and skill_manager is not None:
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
        return self._process_tool_calls_core(tool_calls, mcp_results=None)

    async def _execute_external_tool(self, tool_name: str, args: dict) -> str:
        """Execute one MCP call, serializing only the shared browser surface."""
        resolved_args = self._resolve_external_person_arguments(tool_name, args)
        is_browser = self.browser_controller.is_playwright_tool(tool_name)

        async def execute() -> str:
            preflight = self.browser_controller.before_call(
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
                result = await self.mcp_manager.call_tool(tool_name, resolved_args)
            if not used_cached_snapshot:
                result = self.browser_controller.after_call(
                    self.session_id, tool_name, resolved_args, result
                )
            if (
                self.mcp_manager is not None
                and self.browser_controller.should_recover_stale_ref(
                    self.session_id, tool_name, resolved_args, result
                )
            ):
                snapshot_tool = "mcp__playwright__browser_snapshot"
                snapshot = await self.mcp_manager.call_tool(snapshot_tool, {})
                snapshot = self.browser_controller.after_call(
                    self.session_id, snapshot_tool, {}, snapshot
                )
                executor = getattr(self, "tool_executor", None)
                if executor is not None:
                    executor.record_external_action(snapshot_tool, {}, snapshot)
                if self.browser_controller.result_succeeded(snapshot):
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

    async def process_tool_calls_async(
        self,
        tool_calls: list[dict],
        progress_callback: ToolProgressCallback | None = None,
    ) -> list[dict]:
        """Execute local and MCP tool calls and return results with metadata."""
        mcp_results: dict[int, str] = {}
        local_results: dict[int, str] = {}
        for i, call in enumerate(tool_calls):
            tool_name = call.get("function", {}).get("name", "unknown")
            try:
                args = self._tool_call_args(call)
                if progress_callback is not None:
                    await progress_callback(tool_name, "Preparing input")
                if tool_name.startswith("mcp__"):
                    if progress_callback is not None:
                        await progress_callback(tool_name, "Calling connected tool")
                    result = await self._execute_external_tool(tool_name, args)
                    executor = getattr(self, "tool_executor", None)
                    if executor is not None:
                        executor.record_external_action(tool_name, args, result)
                    mcp_results[i] = result
                elif tool_name == "run_task":
                    if progress_callback is not None:
                        await progress_callback(tool_name, "Running workflow steps")
                    if getattr(self, "workflow_runner", None) is None:
                        local_results[i] = "Error: Workflow runner is unavailable because local task storage is not configured."
                    else:
                        local_results[i] = await self.workflow_runner.run(
                            str(args.get("task_id", "")),
                            confirm=bool(args.get("confirm", False)),
                            max_steps=int(args.get("max_steps", 25)),
                        )
                else:
                    if progress_callback is not None:
                        await progress_callback(tool_name, "Running locally")
                    local_results[i] = await self.tool_executor.execute_async(tool_name, args)
            except BaseException as exc:
                result = f"Error: {exc}"
                if tool_name.startswith("mcp__"):
                    mcp_results[i] = result
                else:
                    local_results[i] = result
        return self._process_tool_calls_core(
            tool_calls,
            mcp_results=mcp_results,
            local_results=local_results,
        )

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

    async def run(
        self,
        user_input: str,
        conversation_history: list[dict],
        *,
        reflection_input: str | None = None,
    ) -> AsyncIterator[str]:
        """Run the agent loop. Yields text tokens from the final response."""
        if self.reflection_service is not None:
            # Tests and embedders sometimes replace ``agent.llm`` after
            # construction. Reflection is a separate call, but it should use
            # the agent's current client rather than a stale network client.
            self.reflection_service.llm = self.llm
            self.reflection_service.reflector.llm = self.llm
            await self.reflection_service.before_turn(self.session_id)
        # Build context
        context = self.get_context(user_input, conversation_history)
        messages = self.build_messages(user_input, conversation_history, context)

        # Agent loop: keep going while LLM wants to call tools
        max_iterations = self.config.agent_max_iterations
        for iteration in range(max_iterations):
            response = await self.llm.chat(messages, tools=self.tools)

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
        yield "\n\n[Warning: Reached maximum tool iterations limit. Some steps may not have completed.]"

    async def run_stream(
        self,
        user_input: str,
        conversation_history: list[dict],
        *,
        reflection_input: str | None = None,
    ) -> AsyncIterator[str]:
        """Run with streaming-first tool detection."""
        if self.reflection_service is not None:
            self.reflection_service.llm = self.llm
            self.reflection_service.reflector.llm = self.llm
            await self.reflection_service.before_turn(self.session_id)
        context = self.get_context(user_input, conversation_history)
        messages = self.build_messages(user_input, conversation_history, context)

        max_iterations = self.config.agent_max_iterations
        for iteration in range(max_iterations):
            tool_calls: dict[int, dict] = {}
            content_parts: list[str] = []
            has_tool_calls = False

            async for chunk in self.llm.chat_stream(messages, tools=self.tools):
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
                try:
                    while not tool_task.done():
                        try:
                            tool_name, detail = await asyncio.wait_for(
                                progress.get(), timeout=1.0
                            )
                            if tool_name not in active_tools:
                                active_tools[tool_name] = asyncio.get_running_loop().time()
                                yield f"[tool_start:{tool_name}]"
                            yield f"[tool_progress:{tool_name}:{detail}]"
                        except asyncio.TimeoutError:
                            now = asyncio.get_running_loop().time()
                            for tool_name, started_at in active_tools.items():
                                elapsed = max(1, round(now - started_at))
                                yield f"[tool_progress:{tool_name}:Still working · {elapsed}s]"
                    while not progress.empty():
                        tool_name, detail = progress.get_nowait()
                        if tool_name not in active_tools:
                            active_tools[tool_name] = asyncio.get_running_loop().time()
                            yield f"[tool_start:{tool_name}]"
                        yield f"[tool_progress:{tool_name}:{detail}]"
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
        self.last_messages = messages
        yield "[Warning: Reached maximum tool iterations limit. Some steps may not have completed.]"

    async def close(self):
        """Clean up resources."""
        if self.reflection_service is not None:
            await self.reflection_service.close()
        self.tool_executor.close()
        await self.llm.close()
