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
            timeout=30,
            max_retries=1,
        )

    def check_api_key(self):
        return _check_api_key_cached(settings.nim_api_key)

    def stream(self, messages, tools=None):
        kwargs = {
            "model": settings.model_name,
            "messages": messages,
            "stream": True,
            "temperature": 0.3,
            "max_tokens": 2048,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            return self.client.chat.completions.create(**kwargs)
        except RateLimitError:
            raise RuntimeError("Rate limited by NVIDIA NIM. Wait a moment and try again.")
        except APITimeoutError:
            raise RuntimeError("Request timed out. The model might be busy on the free tier.")
        except APIError as e:
            raise RuntimeError(f"NVIDIA NIM API error: {e}")

    def extract_summary(self, messages: list) -> str:
        prompt = (
            "Summarize the key information from this conversation concisely "
            "(2-3 sentences). Focus on: user identity, facts discussed, "
            "tasks completed, decisions made. Omit greetings and pleasantries."
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

    def extract_facts(self, user_input: str, assistant_response: str):
        body = (
            "Extract personal facts about the user worth remembering. "
            "Return ONLY a JSON array of strings. "
            "ONLY extract if the fact is specific and personal to this user. "
            "Do NOT extract: descriptions of the assistant's behavior, general advice, "
            "common knowledge, pleasantries, or vague statements. "
            "Focus on: names, locations, health issues, preferences, relationships, work. "
            'Examples of GOOD: ["User name is Krish", "User lives in Bangalore", '
            '"User has heat stroke"] '
            'Examples of BAD: ["Assistant offered support", "Stay calm", '
            '"User is feeling uncertain"] '
            "Return [] if nothing worth remembering."
        )
        messages = [
            {"role": "system", "content": body},
            {"role": "user", "content": f"User: {user_input}\nAssistant: {assistant_response}"},
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
