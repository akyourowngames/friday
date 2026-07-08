"""Extracts new memories from conversations using LLM judgment."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
from typing import Any

from ares.memory_policy import memory_rejection_reason


class MemoryExtractor:
    """Extracts new facts and preferences from conversations."""

    def __init__(self, llm_client: Any, memory_store: Any, config: Any | None = None):
        self.llm = llm_client
        self.memory_store = memory_store
        self.config = config

    def extract_and_store(self, history: list[dict]) -> list[dict]:
        """Extract new memories from conversation and store them."""
        filtered_history = self._filter_private_phone_tool_output(history)
        user_messages = [
            msg for msg in filtered_history
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
            "- Current date, weather, browser, app, phone, or tool state\n"
            "- Insults, frustration, venting, or one-off moods\n"
            "- Assistant guesses or things inferred from tool output\n"
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

    def _filter_private_phone_tool_output(self, history: list[dict]) -> list[dict]:
        """Drop phone notification/SMS tool output from memory extraction unless explicitly enabled."""
        phone_cfg = getattr(self.config, "phone", None)
        if bool(getattr(phone_cfg, "store_notification_content", False)):
            return history

        private_call_ids: set[str] = set()
        filtered: list[dict] = []
        for msg in history:
            for call in msg.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") in {"phone_get_notifications", "phone_get_recent_sms"}:
                    call_id = call.get("id")
                    if call_id:
                        private_call_ids.add(call_id)
            if msg.get("role") == "tool" and msg.get("tool_call_id") in private_call_ids:
                continue
            filtered.append(msg)
        return filtered

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
            confidence = float(fact.get("confidence", 0.8))
            category = fact.get("category", "note")
            if memory_rejection_reason(
                fact["fact_text"],
                category=category,
                confidence=confidence,
            ):
                continue
            fact_id = self.memory_store.store(
                fact_text=fact["fact_text"],
                category=category,
                confidence=confidence,
                importance=float(fact.get("importance", 0.5)),
                source="conversation_extract",
            )
            stored.append({**fact, "fact_id": fact_id})

        return stored
