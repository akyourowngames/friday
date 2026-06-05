from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI

from .config import AssistantSettings


@dataclass(frozen=True)
class MemoryFact:
    bucket: str
    fact: str


class AutoMemoryWriter:
    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=8.0, max_retries=0)

    def extract(self, user_text: str, assistant_text: str, recent_context: str = "") -> list[MemoryFact]:
        if not self.settings.auto_memory_enabled:
            return []
        prompt = {
            "user_message": str(user_text or "").strip(),
            "allowed_buckets": ["personal", "user", "preferences", "project"],
            "output_contract": {
                "facts": [
                    {
                        "bucket": "one allowed bucket",
                        "fact": "one durable fact written as a complete sentence",
                    }
                ]
            },
            "rules": [
                "Return JSON only.",
                "Return an empty facts list when the user message has no durable fact to save.",
                "Save stable identity, preference, project, and long-term user facts.",
                "Do not learn new facts from assistant wording.",
            ],
        }
        facts: list[MemoryFact] = []
        try:
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=[
                    {"role": "system", "content": "You extract durable memory facts as strict JSON."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                temperature=0,
                max_tokens=220,
            )
            raw = response.choices[0].message.content or ""
            data = self._parse_json_object(raw)
            for item in data.get("facts", []):
                if not isinstance(item, dict):
                    continue
                bucket = str(item.get("bucket", "")).strip().lower()
                fact = str(item.get("fact", "")).strip()
                if bucket in {"personal", "user", "preferences", "project"} and fact:
                    facts.append(MemoryFact(bucket, fact))
        except Exception:
            facts = []
        return facts

    def _parse_json_object(self, raw: str) -> dict:
        text = str(raw or "").strip()
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return {}
            try:
                value = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}
