"""LLM API client for OpenCode Zen, NVIDIA, OpenAI, Anthropic, and other OpenAI-compatible APIs."""

import asyncio
import json
import os
from typing import Any, AsyncIterator

import httpx

from ares.config import load_config

# All available models grouped by provider
MODEL_REGISTRY = {
    "free": {
        "label": "Free Models",
        "models": [
            {"id": "deepseek-v4-flash-free", "label": "DeepSeek V4 Flash", "provider": "DeepSeek"},
            {"id": "mimo-v2.5-free", "label": "MiMo V2.5", "provider": "MiMo"},
            {"id": "qwen3.6-plus-free", "label": "Qwen 3.6 Plus", "provider": "Qwen"},
            {"id": "minimax-m3-free", "label": "MiniMax M3", "provider": "MiniMax"},
            {"id": "nemotron-3-ultra-free", "label": "Nemotron 3 Ultra", "provider": "NVIDIA"},
            {"id": "north-mini-code-free", "label": "North Mini Code", "provider": "North"},
            {"id": "big-pickle", "label": "Big Pickle", "provider": "OpenCode"},
        ],
    },
    "claude": {
        "label": "Claude",
        "models": [
            {"id": "claude-fable-5", "label": "Fable 5", "provider": "Anthropic"},
            {"id": "claude-opus-4-8", "label": "Opus 4.8", "provider": "Anthropic"},
            {"id": "claude-opus-4-7", "label": "Opus 4.7", "provider": "Anthropic"},
            {"id": "claude-opus-4-6", "label": "Opus 4.6", "provider": "Anthropic"},
            {"id": "claude-opus-4-5", "label": "Opus 4.5", "provider": "Anthropic"},
            {"id": "claude-opus-4-1", "label": "Opus 4.1", "provider": "Anthropic"},
            {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6", "provider": "Anthropic"},
            {"id": "claude-sonnet-4-5", "label": "Sonnet 4.5", "provider": "Anthropic"},
            {"id": "claude-sonnet-4", "label": "Sonnet 4", "provider": "Anthropic"},
            {"id": "claude-haiku-4-5", "label": "Haiku 4.5", "provider": "Anthropic"},
        ],
    },
    "gpt": {
        "label": "GPT",
        "models": [
            {"id": "gpt-5.5", "label": "GPT-5.5", "provider": "OpenAI"},
            {"id": "gpt-5.5-pro", "label": "GPT-5.5 Pro", "provider": "OpenAI"},
            {"id": "gpt-5.4", "label": "GPT-5.4", "provider": "OpenAI"},
            {"id": "gpt-5.4-pro", "label": "GPT-5.4 Pro", "provider": "OpenAI"},
            {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini", "provider": "OpenAI"},
            {"id": "gpt-5.4-nano", "label": "GPT-5.4 Nano", "provider": "OpenAI"},
            {"id": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark", "provider": "OpenAI"},
            {"id": "gpt-5.3-codex", "label": "GPT-5.3 Codex", "provider": "OpenAI"},
            {"id": "gpt-5.2", "label": "GPT-5.2", "provider": "OpenAI"},
            {"id": "gpt-5.2-codex", "label": "GPT-5.2 Codex", "provider": "OpenAI"},
            {"id": "gpt-5.1", "label": "GPT-5.1", "provider": "OpenAI"},
            {"id": "gpt-5.1-codex-max", "label": "GPT-5.1 Codex Max", "provider": "OpenAI"},
            {"id": "gpt-5.1-codex", "label": "GPT-5.1 Codex", "provider": "OpenAI"},
            {"id": "gpt-5.1-codex-mini", "label": "GPT-5.1 Codex Mini", "provider": "OpenAI"},
            {"id": "gpt-5", "label": "GPT-5", "provider": "OpenAI"},
            {"id": "gpt-5-codex", "label": "GPT-5 Codex", "provider": "OpenAI"},
            {"id": "gpt-5-nano", "label": "GPT-5 Nano", "provider": "OpenAI"},
        ],
    },
    "gemini": {
        "label": "Gemini",
        "models": [
            {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "Google"},
            {"id": "gemini-3.1-pro", "label": "Gemini 3.1 Pro", "provider": "Google"},
            {"id": "gemini-3-flash", "label": "Gemini 3 Flash", "provider": "Google"},
        ],
    },
    "nvidia": {
        "label": "NVIDIA",
        "models": [
            {"id": "meta/llama-3.1-405b-instruct", "label": "Llama 3.1 405B", "provider": "NVIDIA"},
            {"id": "meta/llama-3.1-70b-instruct", "label": "Llama 3.1 70B", "provider": "NVIDIA"},
            {"id": "meta/llama-3.1-8b-instruct", "label": "Llama 3.1 8B", "provider": "NVIDIA"},
            {"id": "meta/llama-3.3-70b-instruct", "label": "Llama 3.3 70B", "provider": "NVIDIA"},
            {"id": "deepseek-ai/deepseek-v4-flash", "label": "DeepSeek V4 Flash", "provider": "NVIDIA"},
            {"id": "deepseek-ai/deepseek-v4-pro", "label": "DeepSeek V4 Pro", "provider": "NVIDIA"},
            {"id": "qwen/qwen3-72b-instruct", "label": "Qwen3 72B", "provider": "NVIDIA"},
            {"id": "qwen/qwen3-8b", "label": "Qwen3 8B", "provider": "NVIDIA"},
        ],
    },
    "other": {
        "label": "Other Models",
        "models": [
            {"id": "grok-build-0.1", "label": "Grok Build 0.1", "provider": "xAI"},
            {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "provider": "DeepSeek"},
            {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "provider": "DeepSeek"},
            {"id": "glm-5.1", "label": "GLM 5.1", "provider": "Zhipu"},
            {"id": "glm-5", "label": "GLM 5", "provider": "Zhipu"},
            {"id": "minimax-m2.7", "label": "MiniMax M2.7", "provider": "MiniMax"},
            {"id": "minimax-m2.5", "label": "MiniMax M2.5", "provider": "MiniMax"},
            {"id": "kimi-k2.6", "label": "Kimi K2.6", "provider": "Moonshot"},
            {"id": "kimi-k2.5", "label": "Kimi K2.5", "provider": "Moonshot"},
            {"id": "qwen3.6-plus", "label": "Qwen 3.6 Plus", "provider": "Qwen"},
            {"id": "qwen3.5-plus", "label": "Qwen 3.5 Plus", "provider": "Qwen"},
        ],
    },
    "copilot": {
        "label": "GitHub Copilot",
        "models": [
            {"id": "auto", "label": "Automatic selection", "provider": "GitHub Copilot"},
        ],
    },
}

# Flat list of free model IDs for fallback
FREE_MODELS = [m["id"] for m in MODEL_REGISTRY["free"]["models"]]

# Zen is an API gateway: model families such as Claude and DeepSeek still use
# the OpenCode endpoint. NVIDIA NIM is the only separately routed catalogue.
_MODEL_BACKENDS = {
    group_key: "nim" if group_key == "nvidia" else "copilot" if group_key == "copilot" else "opencode"
    for group_key in MODEL_REGISTRY
}

PROVIDER_BASE_URLS = {
    "opencode": "https://opencode.ai/zen/v1",
    "nim": "https://integrate.api.nvidia.com/v1",
    "copilot": "",
}

PROVIDER_ALIASES = {"nvidia": "nim"}
SUPPORTED_PROVIDERS = tuple(PROVIDER_BASE_URLS)


def normalize_provider(provider: str | None) -> str:
    """Return the canonical transport-provider name."""
    name = str(provider or "opencode").strip().lower()
    return PROVIDER_ALIASES.get(name, name)


def provider_for_model(model: str | None) -> str | None:
    """Return the endpoint provider for a registered model, if known."""
    for group_key, group in MODEL_REGISTRY.items():
        if any(item["id"] == model for item in group["models"]):
            return _MODEL_BACKENDS[group_key]
    return None


def models_for_provider(provider: str | None) -> list[dict[str, str]]:
    """Return registered models served by one transport provider."""
    target = normalize_provider(provider)
    return [
        model
        for group_key, group in MODEL_REGISTRY.items()
        if _MODEL_BACKENDS[group_key] == target
        for model in group["models"]
    ]


def default_model_for_provider(provider: str | None) -> str:
    """Return a known-working default for a transport provider."""
    target = normalize_provider(provider)
    if target == "nim":
        return "deepseek-ai/deepseek-v4-flash"
    if target == "copilot":
        return "auto"
    models = models_for_provider(target)
    if not models:
        raise ValueError(f"Unsupported provider: {provider}")
    return models[0]["id"]


def _normalized_url(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def resolve_provider_base_url(provider: str | None, base_url: str | None = None) -> str:
    """Resolve the base URL without retaining another provider's default."""
    normalized_provider = normalize_provider(provider)
    expected = PROVIDER_BASE_URLS.get(normalized_provider)
    if expected is None:
        raise ValueError(f"Unsupported provider: {provider}")
    if base_url:
        candidate = str(base_url).rstrip("/")
        known_defaults = {_normalized_url(url) for url in PROVIDER_BASE_URLS.values()}
        if _normalized_url(candidate) not in known_defaults or _normalized_url(candidate) == _normalized_url(expected):
            return candidate
    return expected


def configured_provider_api_key(
    config: Any, provider: str | None, *, include_legacy_active_key: bool = True
) -> str:
    """Select a provider-scoped key without sending one provider's key to another."""
    normalized_provider = normalize_provider(provider)
    environment_names = {
        "opencode": ("OPENCODE_API_KEY",),
        "nim": ("NIM_API_KEY", "NVIDIA_API_KEY"),
        "copilot": ("COPILOT_GITHUB_TOKEN",),
    }
    for name in environment_names.get(normalized_provider, ()):
        if os.environ.get(name):
            return os.environ[name]
    configured = getattr(config, "provider_api_keys", {}) or {}
    if normalized_provider == "copilot":
        return str(getattr(config, "copilot_github_token", "") or "")
    if configured.get(normalized_provider):
        return str(configured[normalized_provider])
    active_provider = normalize_provider(getattr(config, "provider", None))
    if include_legacy_active_key and active_provider == normalized_provider:
        return str(getattr(config, "api_key", "") or "")
    return ""


def activate_provider_config(config: Any, provider: str) -> str:
    """Switch a config to a provider while retaining keys per provider."""
    target = normalize_provider(provider)
    if target not in PROVIDER_BASE_URLS:
        raise ValueError(f"Unsupported provider: {provider}")
    previous = normalize_provider(getattr(config, "provider", None))
    keys = dict(getattr(config, "provider_api_keys", {}) or {})
    active_key = str(getattr(config, "api_key", "") or "")
    if active_key:
        keys[previous] = active_key
    config.provider_api_keys = keys
    config.provider = target
    config.api_key = "" if target == "copilot" else configured_provider_api_key(
        config, target, include_legacy_active_key=False
    )
    config.api_base_url = (
        str(getattr(config, "api_base_url", "") or "").rstrip("/")
        if previous == target
        else PROVIDER_BASE_URLS[target]
    )
    return target


class LLMClient:
    """Async client for calling OpenCode Zen or any OpenAI-compatible API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, config: Any | None = None, provider: str | None = None):
        config = config or load_config()
        self.config = config
        self.model = model or config.model
        configured_provider = normalize_provider(provider or getattr(config, "provider", None))
        # Repair legacy state such as a free Zen model persisted alongside the
        # former ``nvidia`` provider before the very first request is made.
        self.provider = "copilot" if configured_provider == "copilot" else provider_for_model(self.model) or configured_provider
        self.api_key = (
            api_key
            if api_key is not None
            else configured_provider_api_key(config, self.provider)
        )
        configured_base_url = base_url if base_url is not None else getattr(config, "api_base_url", None)
        self.base_url = resolve_provider_base_url(self.provider, configured_base_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=60.0)
        self._copilot_client = None

    def _copilot(self):
        from ares.copilot import CopilotLLMClient

        token = configured_provider_api_key(self.config, "copilot")
        if self._copilot_client is None or self._copilot_client.token != token:
            self._copilot_client = CopilotLLMClient(token=token, model=self.model)
        else:
            self._copilot_client.model = self.model
        return self._copilot_client

    @staticmethod
    def _sanitize_tool_call_ids(messages: list[dict]) -> list[dict]:
        """Ensure all messages are valid for the OpenAI-compatible API.

        Fixes issues from conversation history loaded from DB:
        - Assistant messages with malformed tool_calls (missing 'function',
          missing 'id', server format like {"tool": ..., "args": ...}) → strip
          the tool_calls and any following orphaned tool result messages
        - Assistant messages with valid tool_calls but missing 'id' → generate one
        - Orphaned tool result messages (no matching assistant) → strip
        - Tool result messages with empty tool_call_id → generate one
        """
        # Step 1: validate each assistant message's tool_calls
        #   valid = every entry has both "id" (non-empty) and "function"
        msg_valid: list[bool | None] = [None] * len(messages)
        for i, msg in enumerate(messages):
            tcs = msg.get("tool_calls")
            if tcs and isinstance(tcs, list):
                msg_valid[i] = all(
                    tc.get("id") and tc.get("function")
                    for tc in tcs
                )

        # Step 2: build clean list — strip malformed tool_calls and orphans
        result = []
        global_idx = 0
        pending_orphan_strip = False  # True right after stripping an assistant's tool_calls
        for i, msg in enumerate(messages):
            if msg_valid[i] is True:
                # Valid tool_calls — just ensure all IDs are non-empty
                pending_orphan_strip = False
                msg = dict(msg)
                fixed = []
                for tc in msg["tool_calls"]:
                    if not tc.get("id"):
                        tc = dict(tc)
                        tc["id"] = f"call_{global_idx}"
                    fixed.append(tc)
                    global_idx += 1
                msg["tool_calls"] = fixed
                result.append(msg)

            elif msg_valid[i] is False:
                # Malformed tool_calls — strip them, mark orphans for removal
                pending_orphan_strip = True
                msg = dict(msg)
                msg.pop("tool_calls", None)
                result.append(msg)

            elif msg.get("role") == "tool":
                if pending_orphan_strip:
                    continue  # orphaned tool result — skip
                if not msg.get("tool_call_id"):
                    msg = dict(msg)
                    msg["tool_call_id"] = f"call_{global_idx}"
                    global_idx += 1
                result.append(msg)

            else:
                pending_orphan_strip = False
                result.append(msg)

        return result

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(4.0, 0.5 * (2 ** attempt))

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   tool_choice: str = "auto") -> dict:
        """Send a chat completion request. Returns the full response dict."""
        if self.provider == "copilot":
            return await self._copilot().chat(messages, tools)
        messages = self._sanitize_tool_call_ids(messages)
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

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]
                body = resp.text[:1000]
                last_error = Exception(f"LLM API error {resp.status_code}: {body}")
                if not self._is_retryable_status(resp.status_code) or attempt == 2:
                    raise last_error
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt == 2:
                    raise Exception(f"LLM API transport error: {exc}") from exc
            await asyncio.sleep(self._retry_delay(attempt))
        raise last_error or Exception("LLM API error")

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        """Stream a chat completion and yield structured content/tool chunks."""
        if self.provider == "copilot":
            async for chunk in self._copilot().chat_stream(messages, tools):
                yield chunk
            return
        messages = self._sanitize_tool_call_ids(messages)
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

        for attempt in range(3):
            try:
                async with self._client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = ""
                        async for chunk in resp.aiter_text():
                            body += chunk
                            if len(body) > 1000:
                                break
                        if self._is_retryable_status(resp.status_code) and attempt < 2:
                            await asyncio.sleep(self._retry_delay(attempt))
                            continue
                        raise Exception(f"LLM API error {resp.status_code}: {body}")
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
                    return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 2:
                    raise Exception(f"LLM API transport error: {exc}") from exc
                await asyncio.sleep(self._retry_delay(attempt))

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
        if self._copilot_client is not None:
            await self._copilot_client.close()
