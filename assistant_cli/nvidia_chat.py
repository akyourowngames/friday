from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from openai import OpenAI

from .config import Settings


FALLBACK_SYSTEM_PROMPT = """You are Friday, a fast local CLI assistant.
Be direct, practical, and useful. Use saved memory context when it is provided.
If the saved context does not contain a requested fact, say that plainly."""


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
        if conversation_messages is None:
            messages = [dict(message) for message in self.messages]
        else:
            messages = [{"role": "system", "content": self.system_prompt}]
            messages.extend(conversation_messages[-self.settings.last_messages :])
        if not memory_context:
            return messages

        messages.append({"role": "system", "content": memory_context})
        return messages

    def stream_reply(
        self,
        memory_context: str = "",
        conversation_messages: list[dict[str, str]] | None = None,
        temperature: float | None = None,
    ) -> Iterator[str]:
        stream = self.client.chat.completions.create(
            model=self.settings.model,
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
