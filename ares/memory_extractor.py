"""Extracts new memories from conversations using LLM judgment."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
from typing import Any


class MemoryExtractor:
    """Extracts new facts and preferences from conversations."""

    def __init__(self, llm_client: Any, memory_store: Any):
        self.llm = llm_client
        self.memory_store = memory_store

    def extract_and_store(self, history: list[dict]) -> list[dict]:
        """Extract new memories from conversation and store them."""
        user_messages = [
            msg for msg in history
            if msg.get("role") == "user" and msg.get("content")
        ][-10:]

        if not user_messages:
            return []

        conversation_text = "\n".join(
            f"User: {msg['content'][:500]}" for msg in user_messages
        )

        prompt = (
            "Analyze this conversation and extract any NEW facts, preferences, "
            "or information about the user that should be remembered.\n\n"
            "Only extract information that is:\n"
            "1. A stated preference (e.g., \"I prefer X over Y\")\n"
            "2. A fact about the user (e.g., \"I work as a developer at Z\")\n"
            "3. A habit or routine (e.g., \"I usually code at night\")\n"
            "4. A relationship (e.g., \"my colleague John works on the API\")\n\n"
            "Do NOT extract:\n"
            "- Temporary task details\n"
            "- Information already commonly known\n"
            "- Requests that don't contain personal information\n\n"
            "For each extracted fact, respond with a JSON array:\n"
            '[{"fact_text": "...", "category": "preference|fact|habit|relationship", '
            '"importance": 0.0-1.0, "confidence": 0.0-1.0}]\n\n'
            "If no new facts are found, respond with an empty array: []\n\n"
            f"Conversation:\n{conversation_text}\n\n"
            "New facts:"
        )

        try:
            result = self._call_llm_sync(prompt)
            return self._parse_and_store(result)
        except Exception:
            return []

    def _call_llm_sync(self, prompt: str) -> str:
        """Call the LLM synchronously."""
        messages = [{"role": "user", "content": prompt}]

        async def _do():
            resp = await self.llm.chat(messages, tools=[])
            return resp.get("content", "") or "[]"

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, _do()).result(timeout=30)
        elif loop:
            return loop.run_until_complete(_do())
        return asyncio.run(_do())

    def _parse_and_store(self, response: str) -> list[dict]:
        """Parse LLM response and store extracted memories."""
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if not match:
            return []

        try:
            facts = json.loads(match.group())
        except json.JSONDecodeError:
            return []

        stored = []
        for fact in facts:
            if not fact.get("fact_text"):
                continue
            fact_id = self.memory_store.store(
                fact_text=fact["fact_text"],
                category=fact.get("category", "note"),
                confidence=float(fact.get("confidence", 0.8)),
                importance=float(fact.get("importance", 0.5)),
                source="conversation_extract",
            )
            stored.append({**fact, "fact_id": fact_id})

        return stored
