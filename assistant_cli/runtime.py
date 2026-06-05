from __future__ import annotations

from openai import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError

from .config import AssistantSettings
from .memory_writer import AutoMemoryWriter
from .nvidia_chat import NvidiaChat
from .rag import AgenticRAG, KnowledgeRAG
from .session_store import SessionStore


class FridayRuntime:
    def __init__(self, settings: AssistantSettings, session_id: str | None = None) -> None:
        self.settings = settings
        self.store = SessionStore(settings, session_id=session_id)
        self.rag = KnowledgeRAG(settings)
        self.agentic_rag = AgenticRAG(settings, self.rag, self.store)
        self.chat = NvidiaChat(settings)
        self.memory_writer = AutoMemoryWriter(settings)

    def answer_once(self, user_text: str, stream: bool = True) -> str:
        user_message_id = self.store.append_message("user", user_text)
        rag_context = self.agentic_rag.retrieve(user_text)
        recent = self.store.recent_messages(self.settings.last_messages)
        messages = self.chat.build_messages(recent, rag_context)

        if stream:
            answer = ""
            for token in self.chat.stream(messages):
                print(token, end="", flush=True)
                answer += token
            print()
        else:
            answer = self.chat.complete(messages)

        self.store.append_message("assistant", answer)
        self._write_auto_memory(user_text, answer, user_message_id)
        return answer

    def _write_auto_memory(self, user_text: str, answer: str, source_message_id: int) -> None:
        recent = self.store.recent_text(self.settings.last_messages)
        for fact in self.memory_writer.extract(user_text, answer, recent):
            inserted = self.store.append_fact(fact.bucket, fact.fact, source_message_id)
            if inserted:
                self.rag.append_fact(fact.bucket, fact.fact)

    def close(self) -> None:
        self.store.end()


def format_error(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return "Authentication failed. Check NVIDIA_API_KEY in .env."
    if isinstance(exc, RateLimitError):
        return "NVIDIA rate limit hit. Try again shortly."
    if isinstance(exc, APIStatusError):
        return f"NVIDIA API error {exc.status_code}: {exc.message}"
    if isinstance(exc, APIConnectionError):
        return "Connection failed. Check internet and NVIDIA_BASE_URL."
    return f"{type(exc).__name__}: {exc}"
