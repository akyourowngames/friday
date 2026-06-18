"""Core agent loop: LLM interaction, tool execution, context building."""

import json
from typing import AsyncIterator

from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.conversations import ConversationStore
from ares.tools import ToolExecutor, get_tool_definitions
from ares.llm import LLMClient
from ares.prompts import SYSTEM_PROMPT, build_context_prompt


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
    ):
        self.memory_store = memory_store
        self.task_store = task_store
        self.conversation_store = conversation_store
        self.tool_executor = ToolExecutor(
            memory_store=memory_store,
            task_store=task_store,
            conversation_store=conversation_store,
        )
        self.tools = get_tool_definitions()

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if model:
            kwargs["model"] = model
        self.llm = LLMClient(**kwargs)
        self.tool_executor.config = self.llm.config

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
        """Retrieve relevant memories and pending tasks for context."""
        memories = self.memory_store.search(user_input, limit=5)
        tasks = self.task_store.list_pending()
        summaries = []
        if self.conversation_store is not None:
            summaries = self.conversation_store.get_recent_summaries(limit=5)
        return build_context_prompt(memories, tasks, summaries)

    def set_model(self, model: str) -> None:
        """Switch the underlying chat model."""
        self.llm.model = model
        self.llm.config.model = model

    def process_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Execute tool calls locally and return results with local metadata."""
        results = []
        for call in tool_calls:
            tool_name = call.get("function", {}).get("name", "unknown")
            try:
                fn = call["function"]
                tool_name = fn["name"]
                args = json.loads(fn.get("arguments") or "{}")
                result = self.tool_executor.execute(tool_name, args)
            except Exception as e:
                result = f"Error: {e}"
            results.append({
                "tool_call_id": call.get("id", ""),
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
        max_iterations = 5
        for _ in range(max_iterations):
            response = await self.llm.chat(messages, tools=self.tools)

            # Check for tool calls
            if response.get("tool_calls"):
                # Add assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": response.get("content") or "",
                    "tool_calls": response["tool_calls"],
                })

                # Execute tools
                tool_results = self.process_tool_calls(response["tool_calls"])
                messages.extend(self._tool_messages(tool_results))

                # Let LLM process tool results and continue
                continue

            # No tool calls — LLM produced final text response
            content = response.get("content", "")
            if content:
                yield content
            return

    async def run_stream(self, user_input: str, conversation_history: list[dict]) -> AsyncIterator[str]:
        """Run with streaming-first tool detection."""
        context = self.get_context(user_input)
        messages = self.build_messages(user_input, conversation_history, context)

        max_iterations = 5
        for _ in range(max_iterations):
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
                continue

            return

    async def close(self):
        """Clean up resources."""
        await self.llm.close()
