"""Core agent loop: LLM interaction, tool execution, context building."""

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from ares.context import ProjectContext
from ares.context_blend import build_context_prompt, get_model_budgets
from ares.actions import extract_since_reference, has_reference_language
from ares.autonomy import AutonomousWorkflowRunner
from ares.memory import MemoryStore
from ares.conversations import ConversationStore
from ares.tools import ToolExecutor, get_tool_definitions
from ares.llm import LLMClient
from ares.models import AppConfig
from ares.profile import ProfileManager
from ares.prompts import SYSTEM_PROMPT
from ares.soul import SoulManager
from ares.skills import SkillManager
from ares.tools.datetime_tool import get_current_datetime_result

_SESSION_UNSET = object()


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
            self._session_context.reset(token)

    def refresh_tools(self) -> None:
        """Refresh the advertised tool list, including connected MCP tools."""
        self.tools = get_tool_definitions()
        if getattr(self, "is_cron_session", False) or getattr(self, "is_voice_session", False):
            cron_names = {"create_cron_job", "list_cron_jobs", "get_cron_job", "update_cron_job", "delete_cron_job", "run_cron_job_now", "get_cron_logs"}
            self.tools = [tool for tool in self.tools if tool.get("function", {}).get("name") not in cron_names]
        if self.mcp_manager is not None:
            self.tools.extend(getattr(self.mcp_manager, "tool_definitions", []))
    def build_messages(self, user_input: str, conversation_history: list[dict],
                       context: str = "") -> list[dict]:
        """Build the message list for the LLM."""
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

        turn_guard = (
            "## Current Turn Guard\n"
            "The previous conversation is context only. Answer the next user message as the current task. "
            "Do not continue, repeat, or summarize an earlier user request unless the next user message "
            "explicitly asks to continue it. If tool results are used, base the final answer on the current "
            "user request plus those tool results."
        )

        messages = [{"role": "system", "content": system_content}]
        messages.extend(conversation_history)
        messages.append({"role": "system", "content": turn_guard})
        messages.append({"role": "user", "content": user_input})
        return messages

    def get_context(self, user_input: str) -> str:
        """Build full context: soul + profile + project + memories.

        Budgets scale automatically with the model's context window.
        """
        budgets = get_model_budgets(self.config.model)
        token_budget = budgets["context_token_budget"]
        max_retrieval = budgets["max_memory_retrieval"]

        # Scale sub-budgets proportionally
        soul_budget = max(200, token_budget // 10)
        profile_budget = max(400, token_budget // 5)
        project_budget = max(400, token_budget // 5)

        soul_ctx = self.soul_manager.get_context(token_budget=soul_budget)
        profile_ctx = self.profile_manager.get_context(token_budget=profile_budget)
        project_ctx = ""
        if self.config.project_context_enabled:
            project_ctx = self.project_context.get_context(token_budget=project_budget)

        # Session-scoped memory search
        session_id = self.session_id
        search_scope = "session" if session_id else "all"
        memories = self.memory_store.search(
            user_input, limit=max_retrieval,
            scope=search_scope, session_id=session_id,
            recent_sessions=getattr(self.config, "memory_session_scope", 3),
        )

        # Local people records are included as saved so an explicit request can
        # retrieve complete contact and relationship information.
        people: list[dict] = []
        if self.people_store:
            # A named saved person wins over generic recency so an explicit
            # reference resolves to the intended saved record first.
            named_people = self.people_store.mentioned_in(user_input, limit=4)
            recent_people = self.people_store.recent_for_context(limit=max(3, min(max_retrieval, 8)))
            seen_people: set[int] = set()
            for person in [*named_people, *recent_people]:
                person_id = int(person.get("person_id", 0) or 0)
                if person_id and person_id not in seen_people:
                    seen_people.add(person_id)
                    people.append(person)

        recent_actions = self.action_ledger.recent(limit=5) if self.action_ledger else []
        active_goals = self.goal_store.list_all(statuses=["active"], limit=8) if self.goal_store else []
        if self.goal_store and active_goals:
            active_goals = self.goal_store.contextualize_goals(
                active_goals, max_age_hours=48, max_surfaced=3, per_goal=3, mark_surfaced=False,
            )
        goals_due_soon = self.goal_store.due_soon(within_days=7) if self.goal_store else []
        goals_overdue = self.goal_store.overdue() if self.goal_store else []
        relevant_actions: list[dict] = []
        conversation_recall: list[dict] = []
        since = None
        explicit_recall = has_reference_language(user_input)
        if explicit_recall:
            try:
                since = extract_since_reference(user_input)
            except ValueError:
                since = None

        if self.action_ledger and explicit_recall:
            try:
                relevant_actions = self.action_ledger.search(user_input, since=since, limit=max(3, min(max_retrieval, 8)))
                fallback_actions = self.action_ledger.search(since=since, limit=max(3, min(max_retrieval, 8)))
                known_action_ids = {action.get("action_id") for action in relevant_actions}
                relevant_actions.extend(
                    action for action in fallback_actions
                    if action.get("action_id") not in known_action_ids
                )
                relevant_actions = relevant_actions[: max(3, min(max_retrieval, 8))]
            except ValueError:
                relevant_actions = []

        if self.conversation_store is not None and explicit_recall and not session_id:
            try:
                # Search exact wording first, then fall back to a bounded
                # recent/time-filtered window so "continue" never becomes a
                # false negative merely because it has no lexical match.
                conversation_recall = self.conversation_store.search_recall(
                    user_input, since=since, limit=max(3, min(max_retrieval, 8))
                )
                fallback_recall = self.conversation_store.search_recall(
                    since=since, limit=max(3, min(max_retrieval, 8))
                )
                known_message_ids = {record.get("id") for record in conversation_recall}
                conversation_recall.extend(
                    record for record in fallback_recall
                    if record.get("id") not in known_message_ids
                )
                conversation_recall = conversation_recall[: max(3, min(max_retrieval, 8))]
            except ValueError:
                conversation_recall = []

        if self._session_store is not None and explicit_recall:
            try:
                recall_limit = max(3, min(max_retrieval, 8))
                scan_limit = min(100, recall_limit * 3) if session_id else recall_limit
                session_recall = self._session_store.search_recall(
                    user_input, since=since, limit=scan_limit
                )
                # A reference such as "continue" commonly has no lexical
                # overlap with the historical answer.  Include a bounded
                # fallback window so the JSONL archive cannot be skipped.
                fallback_session_recall = self._session_store.search_recall(
                    since=since, limit=scan_limit
                )
                if session_id:
                    # The current turn is already present in live history and
                    # can otherwise outrank the older session the user named.
                    # Explicit continuation recall should search the archive,
                    # not echo the active session back into its own context.
                    session_recall = [
                        record for record in session_recall
                        if record.get("session_id") != session_id
                    ]
                    fallback_session_recall = [
                        record for record in fallback_session_recall
                        if record.get("session_id") != session_id
                    ]
                session_recall = session_recall[:recall_limit]
                fallback_session_recall = fallback_session_recall[:recall_limit]
                known_session_sources = {record.get("source_id") for record in session_recall}
                session_recall.extend(
                    record for record in fallback_session_recall
                    if record.get("source_id") not in known_session_sources
                )
                # Add stable identifiers to the SQLite records too, then
                # deduplicate mixed persistence sources by provenance.
                for record in conversation_recall:
                    record.setdefault(
                        "source_id",
                        f"conversation:{record.get('conversation_id')}:message:{record.get('id')}",
                    )
                known_sources = {record.get("source_id") for record in conversation_recall}
                conversation_recall.extend(
                    record for record in session_recall
                    if record.get("source_id") not in known_sources
                )
                conversation_recall = conversation_recall[:recall_limit]
            except ValueError:
                # An invalid relative-time phrase must not break normal
                # conversation context; the explicit tool reports it instead.
                pass

        file_action_types = {
            "file_created", "file_edited", "file_deleted", "file_moved", "file_copied",
            "directory_created", "files_batch_changed", "image_generated", "image_edited",
            "export_created",
        }
        recent_file_actions = [
            action for action in [*relevant_actions, *recent_actions]
            if action.get("action_type") in file_action_types
        ][:3]

        summaries = []
        if self.conversation_store is not None and not session_id:
            summaries = self.conversation_store.get_recent_summaries(limit=5)

        # Read previous session summary from JSONL
        prev_summary = None
        if session_id and self._session_store:
            block_context = getattr(self.config, "block_session_context", False)
            prev_summary = self._session_store.get_previous_summary(session_id, block=block_context)

        prepared_context = build_context_prompt(
            soul_context=soul_ctx,
            profile_context=profile_ctx,
            project_context=project_ctx,
            memories=memories,
            people=people,
            goals=active_goals,
            goals_due_soon=goals_due_soon,
            goals_overdue=goals_overdue,
            recent_actions=recent_actions,
            relevant_actions=relevant_actions,
            recent_file_actions=recent_file_actions,
            conversation_summaries=summaries,
            conversation_recall=conversation_recall,
            previous_session_summary=prev_summary,
            token_budget=token_budget,
        )
        if self.goal_store and active_goals:
            surfaced_ids = [
                int(signal["signal_id"])
                for goal in active_goals
                for signal in goal.get("watcher_signals") or []
                if f"signal #{signal.get('signal_id')} " in prepared_context
            ]
            self.goal_store.mark_watcher_signals_surfaced(surfaced_ids)
        return prepared_context

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
        if self.soul_manager.soul_path != soul_path:
            self.soul_manager = SoulManager(data_dir=data_dir, soul_path=config.soul_path)
            self.soul_manager.ensure_exists()
        self.project_context.enabled = config.project_context_enabled
        self.project_context.max_files = max(0, int(config.project_context_max_files))

        skill_dirs = list(config.skill_dirs or [])
        configured_skill_dirs = [Path(path).expanduser() for path in skill_dirs]
        current_skill_dirs = getattr(self.skill_manager, "skill_dirs", [])
        if configured_skill_dirs and current_skill_dirs[: len(configured_skill_dirs)] != configured_skill_dirs:
            self.skill_manager = SkillManager(skill_dirs=configured_skill_dirs)
            self.tool_executor.skill_manager = self.skill_manager

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

    async def process_tool_calls_async(self, tool_calls: list[dict]) -> list[dict]:
        """Execute local and MCP tool calls and return results with metadata."""
        mcp_results: dict[int, str] = {}
        local_results: dict[int, str] = {}
        for i, call in enumerate(tool_calls):
            tool_name = call.get("function", {}).get("name", "unknown")
            try:
                args = self._tool_call_args(call)
                if tool_name.startswith("mcp__"):
                    if self.mcp_manager is None:
                        result = "Error: MCP manager is not configured."
                    else:
                        result = await self.mcp_manager.call_tool(
                            tool_name,
                            self._resolve_external_person_arguments(tool_name, args),
                        )
                    result = self._with_playwright_recovery(tool_name, result)
                    executor = getattr(self, "tool_executor", None)
                    if executor is not None:
                        executor.record_external_action(tool_name, args, result)
                    mcp_results[i] = result
                elif tool_name == "run_task":
                    if getattr(self, "workflow_runner", None) is None:
                        local_results[i] = "Error: Workflow runner is unavailable because local task storage is not configured."
                    else:
                        local_results[i] = await self.workflow_runner.run(
                            str(args.get("task_id", "")),
                            confirm=bool(args.get("confirm", False)),
                            max_steps=int(args.get("max_steps", 25)),
                        )
                else:
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

    @staticmethod
    def _with_playwright_recovery(tool_name: str, result: str) -> str:
        """Turn stale Playwright evidence into one deterministic next step.

        The agent must not repeat a click/type with an old accessibility ref:
        page mutations invalidate it.  Giving the model this compact recovery
        instruction prevents blind retries while preserving the original error.
        """
        lowered_tool = str(tool_name).casefold()
        text = str(result or "")
        lowered = text.casefold()
        is_browser_interaction = lowered_tool.startswith("mcp__playwright__browser_") and any(
            token in lowered_tool for token in ("click", "type", "fill", "select", "press")
        )
        stale_ref = ("ref" in lowered or "reference" in lowered) and any(
            token in lowered for token in ("stale", "not found", "does not exist", "invalid", "unknown")
        )
        if not is_browser_interaction or not stale_ref:
            return text
        return (
            f"{text}\n\nPlaywright recovery: the prior page reference is invalid. "
            "Do not retry that ref. Take one fresh browser_snapshot, choose a ref from that snapshot, "
            "and make at most one evidence-based retry. If the snapshot cannot be obtained, stop and report the browser state."
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

    async def run(self, user_input: str, conversation_history: list[dict]) -> AsyncIterator[str]:
        """Run the agent loop. Yields text tokens from the final response."""
        # Build context
        context = self.get_context(user_input)
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
            if content:
                yield content
            return

        # If we exhaust all iterations, warn the user
        yield "\n\n[Warning: Reached maximum tool iterations limit. Some steps may not have completed.]"

    async def run_stream(self, user_input: str, conversation_history: list[dict]) -> AsyncIterator[str]:
        """Run with streaming-first tool detection."""
        context = self.get_context(user_input)
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
                for call in formatted_calls:
                    tool_name = call.get("function", {}).get("name") or "unknown"
                    yield f"[tool_start:{tool_name}]"
                tool_results = await self.process_tool_calls_async(formatted_calls)
                for tr in tool_results:
                    yield f"[tool:{tr['tool_name']}:{tr['content']}]"
                messages.extend(self._tool_messages(tool_results))

                continue

            # Save messages for conversation history before returning
            final_content = "".join(content_parts)
            messages.append({"role": "assistant", "content": final_content})
            self.last_messages = messages
            if final_content:
                yield final_content
            return

        # If we exhaust all iterations, warn the user
        self.last_messages = messages
        yield "[Warning: Reached maximum tool iterations limit. Some steps may not have completed.]"

    async def close(self):
        """Clean up resources."""
        self.tool_executor.close()
        await self.llm.close()
