"""Adapter for GitHub's supported Copilot SDK integration."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from typing import Any, AsyncIterator


class CopilotConfigurationError(RuntimeError):
    """Raised when the optional Copilot provider is not ready to use."""


class CopilotProviderError(RuntimeError):
    """Raised when GitHub Copilot rejects or cannot complete a request."""


def _prompt_from_messages(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> str:
    """Project Ares' OpenAI-shaped turn onto the SDK's prompt interface."""
    parts = [
        "You are responding inside Ares. Treat this transcript as the complete conversation for this turn.",
        "## Transcript",
    ]
    for message in messages:
        role = str(message.get("role") or "user").upper()
        parts.append(f"\n### {role}\n{message.get('content') or ''}")
        if message.get("tool_calls"):
            parts.append("\nTool calls already made:\n" + json.dumps(message["tool_calls"], ensure_ascii=False))

    specs = []
    for item in tools or []:
        function = item.get("function") or {}
        name = str(function.get("name") or "").strip()
        if name:
            specs.append({
                "name": name,
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {"type": "object"},
            })
    if specs:
        parts.extend([
            "\n## Ares tool protocol",
            "Ares executes tools itself. Do not invoke Copilot runtime tools. To request an Ares tool, "
            "reply only with `<tool_name>{JSON object}</tool_name>` using an exact listed name; wait "
            "for a TOOL result before writing the final answer.",
            "\nAvailable tools:\n" + json.dumps(specs, ensure_ascii=False, separators=(",", ":")),
        ])
    return "\n".join(parts)


class CopilotLLMClient:
    """Async wrapper around ``github-copilot-sdk`` using an explicit user token."""

    def __init__(self, token: str, model: str = "auto") -> None:
        self.token = str(token or "").strip()
        self.model = str(model or "auto").strip() or "auto"
        self._client: Any | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _sdk() -> Any:
        try:
            from copilot import CopilotClient  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CopilotConfigurationError(
                "GitHub Copilot support is optional. Install it with `pip install -e .[copilot]`."
            ) from exc
        # The SDK warns to stderr after a failed send_and_wait. Ares catches
        # that failure and renders an actionable provider message, so showing
        # the duplicate SDK implementation detail in the terminal is noise.
        logging.getLogger("copilot.session").setLevel(logging.ERROR)
        return CopilotClient

    @staticmethod
    def _permission_handler() -> Any | None:
        """Use the SDK's documented session setup without exposing its tools."""
        try:
            from copilot.session import PermissionHandler  # type: ignore[import-not-found]
        except ImportError:
            # Keeps the adapter compatible with test doubles and older SDKs.
            return None
        return PermissionHandler.approve_all

    async def _ensure_client(self) -> Any:
        if not self.token:
            raise CopilotConfigurationError(
                "No GitHub OAuth token is configured. Run `/copilot login CLIENT_ID` or `/copilot token TOKEN`."
            )
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    client_class = self._sdk()
                    # This prevents fallback to an existing Copilot or gh CLI login.
                    self._client = client_class(
                        github_token=self.token,
                        use_logged_in_user=False,
                    )
                    await self._client.start()
        return self._client

    async def _session(self, *, streaming: bool) -> Any:
        client = await self._ensure_client()
        # Ares owns tool authorization/execution. An empty runtime allowlist
        # keeps the SDK from exposing its local built-in tools to this model.
        options = {
            "model": self.model,
            "streaming": streaming,
            "available_tools": [],
        }
        permission_handler = self._permission_handler()
        if permission_handler is not None:
            options["on_permission_request"] = permission_handler
        return await client.create_session(**options)

    @staticmethod
    def _event_type(event: Any) -> str:
        raw_type = getattr(event, "type", "")
        value = getattr(raw_type, "value", raw_type)
        return str(value or "").casefold().replace("_", ".")

    @staticmethod
    def _event_error(event: Any) -> str:
        data = getattr(event, "data", None)
        for field in ("message", "error", "detail", "reason", "description"):
            value = getattr(data, field, None)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _friendly_error(detail: str) -> CopilotProviderError:
        message = str(detail or "GitHub Copilot did not complete the request.").strip()
        normalized = message.casefold()
        if "monthly quota" in normalized or ("quota" in normalized and "exceed" in normalized):
            return CopilotProviderError(
                "GitHub Copilot quota is exhausted for this account. Wait for the quota to reset, "
                "or switch with /provider opencode or /provider nim."
            )
        if "authentication" in normalized or "unauthorized" in normalized or "token" in normalized:
            return CopilotProviderError(
                "GitHub Copilot authentication failed. Run /copilot login CLIENT_ID or set a new token."
            )
        return CopilotProviderError(f"GitHub Copilot request failed: {message}")

    async def _send_and_wait(self, session: Any, prompt: str, errors: list[str]) -> Any:
        """Normalize SDK lifecycle failures before they reach the CLI."""
        try:
            return await session.send_and_wait(prompt)
        except Exception as exc:
            detail = errors[-1] if errors else str(exc)
            raise self._friendly_error(detail) from exc

    @staticmethod
    async def _disconnect(session: Any) -> None:
        disconnect = getattr(session, "disconnect", None)
        if disconnect is not None:
            result = disconnect()
            if inspect.isawaitable(result):
                await result

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        session = await self._session(streaming=False)
        errors: list[str] = []

        def on_event(event: Any) -> None:
            event_type = self._event_type(event)
            if "error" in event_type or "failure" in event_type:
                if detail := self._event_error(event):
                    errors.append(detail)

        unsubscribe = session.on(on_event)
        try:
            response = await self._send_and_wait(session, _prompt_from_messages(messages, tools), errors)
            return {"content": str(getattr(getattr(response, "data", None), "content", "") or "")}
        finally:
            unsubscribe()
            await self._disconnect(session)

    async def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> AsyncIterator[dict[str, Any]]:
        session = await self._session(streaming=True)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        received_delta = False
        errors: list[str] = []

        def on_event(event: Any) -> None:
            event_type = self._event_type(event)
            if "error" in event_type or "failure" in event_type:
                if detail := self._event_error(event):
                    errors.append(detail)
            if event_type == "assistant.message.delta":
                delta = str(getattr(getattr(event, "data", None), "delta_content", "") or "")
                if delta:
                    queue.put_nowait(delta)

        unsubscribe = session.on(on_event)

        async def send() -> Any:
            try:
                return await self._send_and_wait(session, _prompt_from_messages(messages, tools), errors)
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(send())
        try:
            while True:
                delta = await queue.get()
                if delta is None:
                    break
                received_delta = True
                yield {"type": "content", "text": delta}
            response = await task
            if not received_delta:
                content = str(getattr(getattr(response, "data", None), "content", "") or "")
                if content:
                    yield {"type": "content", "text": content}
            yield {"type": "done"}
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            unsubscribe()
            await self._disconnect(session)

    async def close(self) -> None:
        if self._client is not None:
            stop = getattr(self._client, "stop", None)
            if stop is not None:
                result = stop()
                if inspect.isawaitable(result):
                    await result
            self._client = None


__all__ = ["CopilotConfigurationError", "CopilotLLMClient", "CopilotProviderError"]
