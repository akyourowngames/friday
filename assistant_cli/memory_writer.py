from __future__ import annotations

import json
import re
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
        self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=30.0, max_retries=0)

    def extract(self, user_text: str, assistant_text: str, recent_context: str = "") -> list[MemoryFact]:
        if not self.settings.auto_memory_enabled:
            return []
        prompt = (
            "Extract only durable user/project/preferences facts worth remembering. "
            "Ignore greetings, insults, temporary moods, and one-off chat filler. "
            "Return JSON only: {\"facts\":[{\"bucket\":\"personal|user|preferences|project\",\"fact\":\"...\"}]}.\n\n"
            f"Recent context:\n{recent_context[-1600:] or '(none)'}\n\n"
            f"User said:\n{user_text}\n\nAssistant replied:\n{assistant_text[:1200]}"
        )
        facts: list[MemoryFact] = []
        try:
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=[
                    {"role": "system", "content": "You extract durable memory facts as strict JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=220,
            )
            raw = response.choices[0].message.content or ""
            data = json.loads(raw.strip().strip("`"))
            for item in data.get("facts", []):
                bucket = str(item.get("bucket", "")).strip().lower()
                fact = str(item.get("fact", "")).strip()
                bucket, fact = self._normalize_fact(bucket, fact)
                if bucket in {"personal", "user", "preferences", "project"} and fact:
                    facts.append(MemoryFact(bucket, fact))
        except Exception:
            facts = []
        return self._merge_fallbacks(user_text, facts)

    def _normalize_fact(self, bucket: str, fact: str) -> tuple[str, str]:
        match = re.fullmatch(r"name\s*=\s*(.+)", fact.strip(), flags=re.IGNORECASE)
        if match:
            name = self._clean_name(match.group(1))
            if name:
                return "personal", f"The user's name is {name}."
        return bucket, fact

    def _merge_fallbacks(self, user_text: str, facts: list[MemoryFact]) -> list[MemoryFact]:
        out = list(facts)
        lower_facts = {fact.fact.lower() for fact in out}
        fallback = self._name_fallback(user_text)
        if fallback and fallback.fact.lower() not in lower_facts:
            out.append(fallback)
        return out

    def _name_fallback(self, user_text: str) -> MemoryFact | None:
        text = " ".join(str(user_text or "").split())
        patterns = [
            r"\bmy name is\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bcall me\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bmyself\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bi am\s+([A-Za-z][A-Za-z .'-]{1,60})",
            r"\bi'm\s+([A-Za-z][A-Za-z .'-]{1,60})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            name = match.group(1).strip(" .'-")
            words = [word for word in name.split() if word]
            if not (1 <= len(words) <= 4):
                continue
            clean = " ".join(word[:1].upper() + word[1:] for word in words)
            return MemoryFact("personal", f"The user's name is {clean}.")
        return None

    def _clean_name(self, value: str) -> str:
        name = str(value or "").strip(" .'-")
        words = [word for word in name.split() if word]
        if not (1 <= len(words) <= 4):
            return ""
        return " ".join(word[:1].upper() + word[1:] for word in words)
