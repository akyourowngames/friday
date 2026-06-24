"""Core agent loop: LLM interaction, tool execution, context building."""

import json
from pathlib import Path
from typing import Any, AsyncIterator

from ares.context import ProjectContext
from ares.context_blend import build_context_prompt, get_model_budgets
from ares.memory import MemoryStore
from ares.tools.tasks import TaskStore
from ares.conversations import ConversationStore
from ares.tools import ToolExecutor, get_tool_definitions
from ares.llm import LLMClient
from ares.models import AppConfig
from ares.profile import ProfileManager
from ares.prompts import SYSTEM_PROMPT
from ares.soul import SoulManager


class Agent:
    """The core agent that orchestrates LLM calls and tool execution."""

    def __init__(
        self,
        memory_store: MemoryStore,
        task_store: TaskStore,
        conversation_store: ConversationStore | None = None,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        config: AppConfig | None = None,
        task_executor: Any | None = None,
    ):
        self.memory_store = memory_store
        self.task_store = task_store
        self.conversation_store = conversation_store
        self.tool_executor = ToolExecutor(
            memory_store=memory_store,
            task_store=task_store,
            conversation_store=conversation_store,
            task_executor=task_executor,
        )
        self.tools = get_tool_definitions()
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

    def build_messages(self, user_input: str, conversation_history: list[dict],
                       context: str = "") -> list[dict]:
        """Build the message list for the LLM."""
        system_content = SYSTEM_PROMPT
        if context:
            system_content += f"\n\n## Current Context\n{context}"

        messages = [{"role": "system", "content": system_content}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_input})
        return messages

    def get_context(self, user_input: str) -> str:
        """Build full context: soul + profile + project + memories + tasks.

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
        memories = self.memory_store.search(user_input, limit=max_retrieval)
        tasks = self.task_store.list_pending()
        summaries = []
        if self.conversation_store is not None:
            summaries = self.conversation_store.get_recent_summaries(limit=5)
        return build_context_prompt(
            soul_context=soul_ctx,
            profile_context=profile_ctx,
            project_context=project_ctx,
            memories=memories,
            tasks=tasks,
            conversation_summaries=summaries,
            token_budget=token_budget,
        )

    def set_model(self, model: str) -> None:
        """Switch the underlying chat model."""
        self.llm.model = model
        self.llm.config.model = model

    @staticmethod
    def _tool_call_args(call: dict) -> dict:
        """Parse tool call arguments into a dict."""
        raw_args = call.get("function", {}).get("arguments") or "{}"
        if isinstance(raw_args, str):
            return json.loads(raw_args)
        return raw_args or {}

    @staticmethod
    def _auto_task_final_message(tool_results: list[dict]) -> str:
        """Build the final chat response after queueing an auto task."""
        for result in tool_results:
            if result.get("auto_task_created"):
                first_line = str(result.get("content", "")).splitlines()[0]
                return f"{first_line}. The background executor will handle it and track events/artifacts."
        return "Auto-executable task queued. The background executor will handle it and track events/artifacts."

    def process_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Execute tool calls locally and return results with local metadata."""
        results = []
        auto_task_created = False
        for i, call in enumerate(tool_calls):
            tool_name = call.get("function", {}).get("name", "unknown")

            if auto_task_created:
                result = (
                    "Skipped: an auto-executable task was just queued. "
                    "The background TaskExecutor must perform the work so events and artifacts are tracked."
                )
                results.append({
                    "tool_call_id": call.get("id") or f"call_{i}",
                    "role": "tool",
                    "content": result,
                    "tool_name": tool_name,
                    "skipped_after_auto_task": True,
                })
                continue

            try:
                fn = call["function"]
                tool_name = fn["name"]
                args = self._tool_call_args(call)
                result = self.tool_executor.execute(tool_name, args)
            except Exception as e:
                result = f"Error: {e}"
                args = {}

            is_auto_task = tool_name == "create_task" and bool(args.get("auto_executable", False)) and not str(result).lower().startswith("error:")
            if is_auto_task:
                auto_task_created = True

            results.append({
                "tool_call_id": call.get("id") or f"call_{i}",
                "role": "tool",
                "content": result,
                "tool_name": tool_name,
                "auto_task_created": is_auto_task,
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
                tool_results = self.process_tool_calls(response["tool_calls"])
                messages.extend(self._tool_messages(tool_results))

                if any(result.get("auto_task_created") for result in tool_results):
                    yield self._auto_task_final_message(tool_results)
                    return

                # Let LLM process tool results and continue
                continue

            # No tool calls — LLM produced final text response
            content = response.get("content", "")
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
                        content_parts.append(text)
                        yield text

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
                tool_results = self.process_tool_calls(formatted_calls)
                for tr in tool_results:
                    yield f"[tool:{tr['tool_name']}:{tr['content']}]"
                messages.extend(self._tool_messages(tool_results))

                if any(result.get("auto_task_created") for result in tool_results):
                    final_message = self._auto_task_final_message(tool_results)
                    self.last_messages = messages
                    yield final_message
                    return

                continue

            # Save messages for conversation history before returning
            self.last_messages = messages
            return

        # If we exhaust all iterations, warn the user
        self.last_messages = messages
        yield "[Warning: Reached maximum tool iterations limit. Some steps may not have completed.]"

    async def close(self):
        """Clean up resources."""
        await self.llm.close()
