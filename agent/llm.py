import json
from functools import lru_cache

from openai import OpenAI, APIError, APITimeoutError, RateLimitError

from config import settings


@lru_cache(maxsize=1)
def _check_api_key_cached(api_key: str) -> bool:
    """Cached API key validation."""
    return bool(api_key.strip())


class NIMClient:
    def __init__(self):
        self.client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=120,
            max_retries=2,
        )

    def check_api_key(self):
        return _check_api_key_cached(settings.nim_api_key)

    def stream(self, messages, tools=None, tool_choice=None):
        kwargs = {
            "model": settings.model_name,
            "messages": messages,
            "stream": True,
            "temperature": 0.3,
            "max_tokens": 2048,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        try:
            return self.client.chat.completions.create(**kwargs)
        except RateLimitError:
            raise RuntimeError("Rate limited by NVIDIA NIM. Wait a moment and try again.")
        except APITimeoutError:
            raise RuntimeError("Request timed out. The model might be busy on the free tier.")
        except APIError as e:
            raise RuntimeError(f"NVIDIA NIM API error: {e}")

    def extract_summary(self, messages: list, instruction: str = "") -> str:
        prompt = instruction.strip() or (
            "Summarize the key information from this conversation concisely "
            "(2-3 sentences). Focus on the user: their identity, what they asked for, "
            "what they were working on, and any decisions or unresolved threads. "
            "Record concrete outcomes when actions actually happened. Do not record the "
            "assistant's refusals or capability disclaimers. Omit greetings and pleasantries."
        )
        try:
            resp = self.client.chat.completions.create(
                model=settings.model_name,
                messages=[{"role": "system", "content": prompt}] + messages[-6:],
                temperature=0,
                max_tokens=200,
            )
            return resp.choices[0].message.content.strip()
        except (APIError, RateLimitError, APITimeoutError):
            return ""

    def extract_facts(self, user_input: str, assistant_response: str, recent_user_context: str = ""):
        body = (
            "Extract personal facts about the user worth remembering. "
            "Return ONLY a JSON array of strings. "
            "ONLY extract if the fact is specific and personal to this user. "
            "Use the user's own messages as the source of truth. "
            "Recent conversation context may resolve pronouns, corrections, and references, "
            "but assistant text is supporting context, not a source for new facts by itself. "
            "If the current user message asks to store the previous point, extract the latest "
            "specific user-provided fact from the recent context. "
            "Do NOT extract: descriptions of the assistant's behavior, general advice, "
            "common knowledge, pleasantries, or vague statements. "
            "Do NOT extract confidence levels, scores, rankings, retrieval metadata, "
            "or statements about whether the assistant has information. "
            "Do NOT extract temporary greeting corrections, the current time of day, "
            "or the user's reaction to a greeting. "
            "Focus on: names, locations, academic status, health issues, preferences, relationships, work. "
            "IMPORTANT: When user corrects or updates a fact (e.g. new location, recovered health), "
            "extract the NEW fact, not the old one. "
            'Examples of GOOD: ["User name is Krish", "User lives in Bangalore", '
            '"User has heat stroke", "User has recovered from illness", '
            '"User now lives in Delhi", "User is feeling better", "Ankita is in class 11th"] '
            'Examples of BAD: ["Assistant offered support", "Stay calm", '
            '"User is feeling uncertain", "User has medical records", '
            '"User confidence level is 0.32"] '
            'Also BAD: vague health statements like "medical records", '
            '"doctor\'s offices", "urgent care", "area with available" '
            "Return [] if nothing worth remembering."
        )
        messages = [
            {"role": "system", "content": body},
            {
                "role": "user",
                "content": (
                    f"Recent context:\n{recent_user_context or '(none)'}\n"
                    f"Current user: {user_input}\n"
                    f"Current assistant: {assistant_response}"
                ),
            },
        ]
        try:
            resp = self.client.chat.completions.create(
                model=settings.model_name,
                messages=messages,
                temperature=0,
                max_tokens=200,
            )
            text = resp.choices[0].message.content.strip()
            return json.loads(text)
        except (json.JSONDecodeError, APIError, RateLimitError, APITimeoutError):
            return []
