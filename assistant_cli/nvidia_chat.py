from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
import json

from openai import OpenAI

from .config import Settings


FALLBACK_SYSTEM_PROMPT = """You are Friday, a fast local CLI assistant.
Be direct, practical, and useful. Use saved memory context when it is provided.
If the saved context does not contain a requested fact, say that plainly."""

GROUNDED_RESPONSE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_grounded_response",
        "description": "Return a concise response grounded only in current tool evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "response": {"type": "string"},
                "reports_success": {"type": "boolean"},
                "reports_failure": {"type": "boolean"},
                "reports_new_creation": {"type": "boolean"},
                "reports_new_update": {"type": "boolean"},
            },
            "required": [
                "response",
                "reports_success",
                "reports_failure",
                "reports_new_creation",
                "reports_new_update",
            ],
            "additionalProperties": False,
        },
    },
}


class NvidiaChat:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=60.0, max_retries=0)
        self.system_prompt = self._load_persona()
        self.messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt}]

    def _load_persona(self) -> str:
        persona_path = Path(self.settings.persona_file)
        if not persona_path.is_absolute():
            persona_path = Path.cwd() / persona_path
        try:
            text = persona_path.read_text(encoding="utf-8").strip()
        except OSError:
            return FALLBACK_SYSTEM_PROMPT
        return text or FALLBACK_SYSTEM_PROMPT

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def reset(self) -> None:
        self.system_prompt = self._load_persona()
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def _messages_for_request(
        self,
        memory_context: str = "",
        conversation_messages: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        system_content = self.system_prompt
        if memory_context:
            system_content = f"{system_content}\n\n## Current Turn Context\n{memory_context}"
        if conversation_messages is None:
            messages = [{"role": "system", "content": system_content}]
            messages.extend(dict(message) for message in self.messages if message.get("role") != "system")
        else:
            messages = [{"role": "system", "content": system_content}]
            messages.extend(conversation_messages[-self.settings.last_messages :])
        return messages

    def stream_reply(
        self,
        memory_context: str = "",
        conversation_messages: list[dict[str, str]] | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Iterator[str]:
        stream = self.client.chat.completions.create(
            model=model or self.settings.model,
            messages=self._messages_for_request(memory_context, conversation_messages),  # type: ignore[arg-type]
            temperature=self.settings.temperature if temperature is None else temperature,
            max_tokens=self.settings.max_tokens,
            stream=True,
        )

        for chunk in stream:
            if not chunk.choices:
                continue
            delta: Any = chunk.choices[0].delta
            token = getattr(delta, "content", None)
            if token:
                yield token

    def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0,
        max_tokens: int = 500,
        timeout: float | None = None,
        model: str | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = self.client.chat.completions.create(
            model=model or self.settings.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        return response.choices[0].message.content or ""

    def choose_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        timeout: float,
        model: str | None = None,
        tool_choice: Any = "auto",
    ) -> list[dict[str, Any]]:
        response = self.client.chat.completions.create(
            model=model or self.settings.model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,
            tool_choice=tool_choice,
            temperature=0,
            max_tokens=500,
            timeout=timeout,
        )
        message = response.choices[0].message
        planned: list[dict[str, Any]] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            planned.append(
                {
                    "id": call.id,
                    "tool": call.function.name,
                    "arguments": arguments,
                }
            )
        return planned

    def grounded_reply(self, context: str, user_text: str, results: list[Any]) -> str:
        expected_success = any(bool(getattr(result, "ok", False)) for result in results)
        expected_failure = any(not bool(getattr(result, "ok", False)) for result in results)
        expected_creation = any(_result_created(result) for result in results)
        expected_update = any(_result_updated(result) for result in results)
        messages = [
            {
                "role": "system",
                "content": (
                    f"{self.system_prompt}\n\n{context}\n\n"
                    "Return the forced grounded-response function only. "
                    "Set the reporting booleans to describe the response you wrote. "
                    "Do not use assumptions from the user request when tool evidence disagrees."
                ),
            },
            {"role": "user", "content": str(user_text or "")},
        ]
        models = [self.settings.tool_response_model, self.settings.tool_verifier_fallback_model]
        seen: set[str] = set()
        for model in models:
            if not model or model in seen:
                continue
            seen.add(model)
            try:
                calls = self.choose_tools(
                    messages,
                    [GROUNDED_RESPONSE_TOOL],
                    timeout=self.settings.tool_planner_timeout_seconds,
                    model=model,
                    tool_choice={"type": "function", "function": {"name": "submit_grounded_response"}},
                )
            except Exception:
                continue
            if not calls:
                continue
            arguments = calls[0].get("arguments", {})
            if not isinstance(arguments, dict):
                continue
            response = str(arguments.get("response") or "").strip()
            if not response:
                continue
            if bool(arguments.get("reports_success")) != expected_success:
                continue
            if bool(arguments.get("reports_failure")) != expected_failure:
                continue
            if bool(arguments.get("reports_new_creation")) != expected_creation:
                continue
            if bool(arguments.get("reports_new_update")) != expected_update:
                continue
            return response
        return "\n".join(
            str(getattr(result, "text", "") or "").strip()
            for result in results
            if str(getattr(result, "text", "") or "").strip()
        )

    def ping(self) -> str:
        response = self.client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": "Say you are online and ready in one short sentence."},
            ],
            temperature=0,
            max_tokens=40,
        )
        return response.choices[0].message.content or ""


def _result_created(result: Any) -> bool:
    if not bool(getattr(result, "ok", False)):
        return False
    data = getattr(result, "data", {})
    if not isinstance(data, dict):
        return False
    return bool(data.get("created")) or int(data.get("created_count") or 0) > 0


def _result_updated(result: Any) -> bool:
    if not bool(getattr(result, "ok", False)):
        return False
    data = getattr(result, "data", {})
    if not isinstance(data, dict):
        return False
    if int(data.get("updated_count") or 0) > 0:
        return True
    return str(data.get("action") or "") in {
        "project_update",
        "project_archive",
        "task_update",
        "task_bulk_update",
        "task_complete",
        "task_complete_all",
        "task_reopen_all",
        "task_delete",
    }
