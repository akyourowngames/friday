from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from openai import OpenAI

from .config import AssistantSettings


SYSTEM_PROMPT = """You are Friday, a fast, direct local CLI assistant.
Use the provided local memory, SQLite recall, and knowledge RAG when relevant.
The last 20 messages are already included for conversation continuity.
Be helpful with frustrated users; do not scold casual profanity.
Keep answers practical and concise unless the user asks for depth."""


class NvidiaChat:
    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=60.0, max_retries=0)

    def build_messages(self, recent_messages: list[dict[str, str]], rag_context: str = "") -> list[dict[str, str]]:
        system = SYSTEM_PROMPT
        if rag_context.strip():
            system += "\n\nLocal retrieved context:\n" + rag_context.strip()
        return [{"role": "system", "content": system}] + recent_messages[-self.settings.last_messages :]

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        stream = self.client.chat.completions.create(
            model=self.settings.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=self.settings.temperature,
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

    def complete(self, messages: list[dict[str, str]]) -> str:
        response = self.client.chat.completions.create(
            model=self.settings.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
        )
        return response.choices[0].message.content or ""

    def ping(self) -> str:
        response = self.client.chat.completions.create(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": "Answer with one short sentence."},
                {"role": "user", "content": "Say Friday is online and name your model."},
            ],
            temperature=0,
            max_tokens=60,
        )
        return response.choices[0].message.content or ""
