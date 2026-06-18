"""LLM API client for OpenCode Zen (OpenAI-compatible)."""

import json
from typing import Any, AsyncIterator

import httpx

from ares.config import load_config

# Free models fallback order
FREE_MODELS = [
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "nemotron-3-ultra-free",
    "big-pickle",
    "north-mini-code-free",
]


class LLMClient:
    """Async client for calling OpenCode Zen or any OpenAI-compatible API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        config = load_config()
        self.config = config
        self.api_key = api_key or config.api_key
        self.base_url = (base_url or config.api_base_url).rstrip("/")
        self.model = model or config.model
        self._client = httpx.AsyncClient(timeout=60.0)

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   tool_choice: str = "auto") -> dict:
        """Send a chat completion request. Returns the full response dict."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        """Stream a chat completion and yield structured content/tool chunks."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {})

                    content = delta.get("content")
                    if content:
                        yield {"type": "content", "text": content}

                    if "tool_calls" in delta:
                        for tool_call in delta["tool_calls"]:
                            index = tool_call.get("index", 0)
                            fn = tool_call.get("function", {})
                            if tool_call.get("id") or fn.get("name"):
                                yield {
                                    "type": "tool_call",
                                    "index": index,
                                    "id": tool_call.get("id", ""),
                                    "name": fn.get("name", ""),
                                }
                            if fn.get("arguments"):
                                yield {
                                    "type": "tool_call_delta",
                                    "index": index,
                                    "arguments": fn["arguments"],
                                }
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
            yield {"type": "done"}

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
