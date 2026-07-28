"""MCP client integration for Ares.

This module manages configured Model Context Protocol servers, OAuth token
storage/refresh, tool discovery, and MCP tool execution through the official SDK.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib
import importlib.util
import json
import logging
import math
import os
import re
import secrets
import webbrowser
from contextlib import AsyncExitStack, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ares.tools.mcp_upgrades import (
    DEFAULT_MCP_TIMEOUT_SECONDS,
    MAX_MCP_TIMEOUT_SECONDS,
    MIN_MCP_TIMEOUT_SECONDS,
    MCPResponseCache,
    MCPUpgradeError,
    PreparedMCPCall,
    merge_paginated_responses,
    pagination_page_arguments,
    prepare_mcp_call,
    project_mcp_error,
    project_mcp_health,
    project_pagination_page,
)

logger = logging.getLogger(__name__)

DEFAULT_MCP_RECONNECT_INTERVAL_SECONDS = 10.0
DEFAULT_MCP_HEALTH_PROBE_INTERVAL_SECONDS = 30.0
MAX_MCP_RECONNECT_BACKOFF_SECONDS = 120.0

_SENSITIVE_MCP_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "id_token", "key",
    "password", "refresh_token", "secret", "token",
}
_SENSITIVE_MCP_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)(\b(?:api[_-]?key|token|secret|password)\b\s*[=:]\s*)([^\s,;&]+)"),
    re.compile(r"(?i)(--(?:api[-_]?key|token|secret|password)\s+)([^\s,;&]+)"),
)


def redact_mcp_text(value: object) -> str:
    """Redact credentials before MCP diagnostics reach a user-facing surface."""
    text = str(value or "")
    for pattern in _SENSITIVE_MCP_TEXT_PATTERNS:
        text = pattern.sub(r"\1[redacted]", text)
    try:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.netloc:
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            if "@" in parsed.netloc:
                host = f"[redacted]@{host}"
            query = urlencode(
                [
                    (key, "[redacted]" if key.lower() in _SENSITIVE_MCP_QUERY_KEYS else item)
                    for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                ]
            )
            text = urlunsplit((parsed.scheme, host or parsed.netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        pass
    return text


def _uncancel_task() -> None:
    """Clear asyncio cancellation state after intentionally handling CancelledError.

    Python 3.11+ keeps the task's cancellation flag active even after the
    CancelledError is caught.  Every subsequent await re-raises it until
    uncancel() is called the same number of times the task was cancelled.
    """
    task = asyncio.current_task()
    if task is not None and hasattr(task, "uncancel"):
        while task.cancelling():
            task.uncancel()


def _clear_current_task_cancellation() -> None:
    """Clear pending cancellation state after intentionally suppressing MCP cancellation."""
    current_task = asyncio.current_task()
    if current_task is None or not hasattr(current_task, "uncancel"):
        return
    while current_task.cancelling():
        current_task.uncancel()



def _optional_import(module_name: str, attr: str | None = None) -> Any:
    """Import an optional MCP SDK object without failing module import."""
    package = module_name.split(".", 1)[0]
    if (
        importlib.util.find_spec(package) is None
    ):  # pragma: no cover - optional dependency absent.
        return None
    if (
        importlib.util.find_spec(module_name) is None
    ):  # pragma: no cover - older optional dependency.
        return None
    module = importlib.import_module(module_name)
    return getattr(module, attr) if attr else module


ClientSession = _optional_import("mcp", "ClientSession")
streamable_http_client = _optional_import(
    "mcp.client.streamable_http", "streamable_http_client"
)
stdio_client = _optional_import("mcp.client.stdio", "stdio_client")
StdioServerParameters = _optional_import("mcp.client.stdio", "StdioServerParameters")
sse_client = _optional_import("mcp.client.sse", "sse_client")


class MCPServerConfig(BaseModel):
    """Configuration for one MCP server.

    Supports modern Streamable HTTP, local stdio, and legacy SSE transports. Ares
    also accepts common config shapes such as {"calendar": {...}} and command-only
    entries, so users can add more MCP tools without brittle config migrations.
    """

    name: str
    server_url: str = ""
    url: str = ""
    transport: Literal["streamable_http", "http", "stdio", "sse"] = "streamable_http"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scopes: list[str] = Field(default_factory=list)
    # ``timeout_seconds`` is the default for one request.  The separate cap
    # lets a caller opt in to a longer, still finite operation without making
    # every MCP call wait that long by default.
    timeout_seconds: float = DEFAULT_MCP_TIMEOUT_SECONDS
    max_timeout_seconds: float = MAX_MCP_TIMEOUT_SECONDS

    @field_validator("timeout_seconds", "max_timeout_seconds", mode="before")
    @classmethod
    def validate_timeout_seconds(cls, value: Any, info: Any) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be a number of seconds, not a boolean")
        try:
            seconds = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{info.field_name} must be a number of seconds") from exc
        if not math.isfinite(seconds):
            raise ValueError(f"{info.field_name} must be finite")
        if not MIN_MCP_TIMEOUT_SECONDS <= seconds <= MAX_MCP_TIMEOUT_SECONDS:
            raise ValueError(
                f"{info.field_name} must be between {MIN_MCP_TIMEOUT_SECONDS:g} and "
                f"{MAX_MCP_TIMEOUT_SECONDS:g} seconds"
            )
        return seconds

    @model_validator(mode="after")
    def normalize(self) -> "MCPServerConfig":
        if self.url and not self.server_url:
            self.server_url = self.url
        if self.command and not self.server_url and self.transport == "streamable_http":
            self.transport = "stdio"
        if self.transport == "http":
            self.transport = "streamable_http"
        if self.timeout_seconds > self.max_timeout_seconds:
            raise ValueError("timeout_seconds cannot exceed max_timeout_seconds")
        return self

    @property
    def endpoint(self) -> str:
        return self.server_url or self.url


class MCPAuthProvider:
    """OAuth 2.1 PKCE helper with token persistence and refresh support."""

    CALLBACK_HOST = "127.0.0.1"
    CALLBACK_PORT = 9865

    def __init__(self, data_dir: str = "~/.ares/data"):
        self.token_dir = Path(data_dir).expanduser() / "mcp_tokens"
        self.token_dir.mkdir(parents=True, exist_ok=True)

    async def ensure_token(self, config: MCPServerConfig) -> str:
        token = self._load_token(config.name)
        if token and not self._is_expired(token):
            return str(token["access_token"])
        if token and token.get("refresh_token"):
            refreshed = await self._refresh_token(config, token)
            if refreshed:
                return str(refreshed["access_token"])
        new_token = await self._run_pkce_flow(config)
        return str(new_token["access_token"])

    def _token_path(self, server_name: str) -> Path:
        safe_name = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in server_name
        )
        return self.token_dir / f"{safe_name}.json"

    def _load_token(self, server_name: str) -> dict[str, Any] | None:
        path = self._token_path(server_name)
        if not path.exists():
            return None
        with suppress(json.JSONDecodeError, OSError):
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _store_token(self, server_name: str, token: dict[str, Any]) -> dict[str, Any]:
        token = dict(token)
        if token.get("expires_in") and not token.get("expires_at"):
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(token["expires_in"])
            )
            token["expires_at"] = expires_at.isoformat()
        self._token_path(server_name).write_text(
            json.dumps(token, indent=2), encoding="utf-8"
        )
        return token

    def _is_expired(self, token: dict[str, Any]) -> bool:
        expires_at = token.get("expires_at")
        if not expires_at:
            return False
        try:
            parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            return True
        return parsed <= datetime.now(timezone.utc) + timedelta(minutes=2)

    async def _discover_endpoints(self, server_url: str) -> dict[str, str]:
        if "googleapis.com" in server_url or "google.com" in server_url:
            return {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
            }
        parsed = urlparse(server_url)
        issuer = f"{parsed.scheme}://{parsed.netloc}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{issuer}/.well-known/oauth-authorization-server"
            )
            response.raise_for_status()
            data = response.json()
        return {
            "authorization_endpoint": data["authorization_endpoint"],
            "token_endpoint": data["token_endpoint"],
        }

    async def _run_pkce_flow(self, config: MCPServerConfig) -> dict[str, Any]:
        endpoints = await self._discover_endpoints(config.endpoint)
        verifier = (
            base64.urlsafe_b64encode(secrets.token_bytes(32))
            .rstrip(b"=")
            .decode("ascii")
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        state = secrets.token_urlsafe(16)
        redirect_uri = f"http://{self.CALLBACK_HOST}:{self.CALLBACK_PORT}/callback"
        params = {
            "response_type": "code",
            "client_id": config.oauth_client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(config.oauth_scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        webbrowser.open(f"{endpoints['authorization_endpoint']}?{urlencode(params)}")
        code = await self._wait_for_callback(state)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.oauth_client_id,
            "code_verifier": verifier,
        }
        if config.oauth_client_secret:
            data["client_secret"] = config.oauth_client_secret
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(endpoints["token_endpoint"], data=data)
            response.raise_for_status()
            token = response.json()
        return self._store_token(config.name, token)

    async def _wait_for_callback(self, expected_state: str) -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                request_line = (await reader.readline()).decode(
                    "utf-8", errors="ignore"
                )
                target = request_line.split(" ")[1]
                params = parse_qs(urlparse(target).query)
                state = params.get("state", [""])[0]
                code = params.get("code", [""])[0]
                body = b"Authorization failed. You can close this tab."
                if state == expected_state and code:
                    if not future.done():
                        future.set_result(code)
                    body = b"Authorization complete. You can close this tab."
                elif not future.done():
                    future.set_exception(ValueError("Invalid MCP OAuth callback"))
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(
            handle, self.CALLBACK_HOST, self.CALLBACK_PORT
        )
        async with server:
            code = await future
            server.close()
            await server.wait_closed()
            return code

    async def _refresh_token(
        self, config: MCPServerConfig, token: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            endpoints = await self._discover_endpoints(config.endpoint)
            data = {
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
                "client_id": config.oauth_client_id,
            }
            if config.oauth_client_secret:
                data["client_secret"] = config.oauth_client_secret
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(endpoints["token_endpoint"], data=data)
                response.raise_for_status()
                refreshed = response.json()
            refreshed.setdefault("refresh_token", token.get("refresh_token"))
            return self._store_token(config.name, refreshed)
        except asyncio.CancelledError:
            _uncancel_task()
            logger.warning("MCP token refresh cancelled for %s", config.name)
            return None
        except Exception as exc:  # pragma: no cover - network failure path.
            logger.warning("MCP token refresh failed for %s: %s", config.name, exc)
            return None


class MCPClientManager:
    """Connect to configured MCP servers and expose their tools to Ares."""

    def __init__(
        self,
        server_configs: list[dict[str, Any] | MCPServerConfig] | dict[str, Any],
        data_dir: str = "~/.ares/data",
        reconnect_interval_seconds: float = DEFAULT_MCP_RECONNECT_INTERVAL_SECONDS,
        health_probe_interval_seconds: float = DEFAULT_MCP_HEALTH_PROBE_INTERVAL_SECONDS,
    ):
        configs = [
            self._coerce_config(name, value)
            for name, value in self._iter_config_entries(server_configs)
        ]
        self.servers = {c.name: c for c in configs if c is not None}
        self.auth = MCPAuthProvider(data_dir=data_dir)
        self.sessions: dict[str, Any] = {}
        self._exit_stacks: dict[str, AsyncExitStack] = {}
        self._http_clients: dict[str, httpx.AsyncClient] = {}
        # AnyIO transports used by the MCP SDK must be entered and exited by
        # the same asyncio task. Each server therefore gets a long-lived owner
        # task that opens the transport, publishes the session, and closes it.
        self._owner_tasks: dict[str, asyncio.Task[None]] = {}
        self._owner_stop_events: dict[str, asyncio.Event] = {}
        self.tool_definitions: list[dict[str, Any]] = []
        self.schema_cache: dict[str, list[dict[str, Any]]] = {}
        self.server_errors: dict[str, str] = {}
        self._reconnect_locks: dict[str, asyncio.Lock] = {}
        self._reconnect_interval_seconds = max(0.1, float(reconnect_interval_seconds))
        self._health_probe_interval_seconds = max(
            self._reconnect_interval_seconds,
            float(health_probe_interval_seconds),
        )
        self._last_health_probe_at = 0.0
        self._reconnect_failures: dict[str, int] = {}
        self._next_reconnect_at: dict[str, float] = {}
        self._reconnect_monitor_task: asyncio.Task[None] | None = None
        self._maintenance_lock = asyncio.Lock()
        self._active_calls: dict[str, int] = {}
        # This cache is used only by an explicit, read-only call policy. It
        # stays in memory so neither MCP output nor request details persist.
        self._response_cache = MCPResponseCache()

    def _iter_config_entries(
        self, server_configs: list[dict[str, Any] | MCPServerConfig] | dict[str, Any]
    ):
        if isinstance(server_configs, dict):
            yield from server_configs.items()
            return
        for item in server_configs or []:
            if isinstance(item, MCPServerConfig):
                yield item.name, item
            elif isinstance(item, dict) and "name" not in item and len(item) == 1:
                yield next(iter(item.items()))
            else:
                yield None, item

    def _coerce_config(self, name: str | None, value: Any) -> MCPServerConfig | None:
        if isinstance(value, MCPServerConfig):
            return value
        if not isinstance(value, dict):
            logger.warning("Ignoring invalid MCP server config %r", value)
            return None
        data = dict(value)
        if name and "name" not in data:
            data["name"] = name
        try:
            return MCPServerConfig(**data)
        except ValidationError as exc:
            logger.warning("Ignoring invalid MCP server config %r: %s", data, exc)
            return None

    @staticmethod
    def _connection_timeout(config: MCPServerConfig) -> float:
        """Bound one connection attempt without turning a call timeout into a retry."""

        return min(
            config.max_timeout_seconds,
            max(MIN_MCP_TIMEOUT_SECONDS, config.timeout_seconds * 2 + 1),
        )

    async def start(self) -> None:
        """Connect to all configured MCP servers.

        Failures are logged per server and do not block others.  The asyncio
        CancelledError that the MCP SDK can raise is fully consumed so it never
        leaks into the caller's event loop.
        """
        if self.sessions or self._exit_stacks or self._http_clients or self._owner_tasks:
            await self.close()
        self.tool_definitions = []
        self.server_errors = {}
        async def connect_one(name: str, config: MCPServerConfig) -> None:
            try:
                report = await self.reconnect_server(name, force=True)
                if not report.get("ready"):
                    raise ConnectionError(
                        str(report.get("error") or "connection completed without a live session")
                    )
            except asyncio.CancelledError as exc:
                _clear_current_task_cancellation()
                self.server_errors[name] = f"Connection cancelled: {exc or 'cancelled'}"
                await self.close_server(name)
            except Exception as exc:
                self.server_errors[name] = str(exc) or exc.__class__.__name__
                await self.close_server(name)
                logger.info(
                    "Optional MCP server '%s' is unavailable (%s); Ares will continue without it. "
                    "Run /mcp status for details.",
                    name,
                    exc.__class__.__name__,
                )
            finally:
                _uncancel_task()
        # Independent integrations must not queue behind one slow/unhealthy
        # server.  Each task owns a timeout and an explicit readiness result.
        await asyncio.gather(
            *(connect_one(name, config) for name, config in self.servers.items()),
            return_exceptions=True,
        )
        self._last_health_probe_at = asyncio.get_running_loop().time()
        self._start_reconnect_monitor()

    async def close(self) -> None:
        monitor = self._reconnect_monitor_task
        self._reconnect_monitor_task = None
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await monitor
        server_names = set(self.sessions) | set(self._exit_stacks) | set(self._http_clients)
        server_names.update(self._owner_tasks)
        await asyncio.gather(
            *(self.close_server(name) for name in server_names),
            return_exceptions=True,
        )
        self.sessions.clear()
        self._exit_stacks.clear()
        self._http_clients.clear()
        self._owner_tasks.clear()
        self._owner_stop_events.clear()
        self.tool_definitions.clear()
        self._response_cache.clear()

    def _start_reconnect_monitor(self) -> None:
        """Keep configured integrations alive for the lifetime of this manager."""
        current = self._reconnect_monitor_task
        if not self.servers or (current is not None and not current.done()):
            return
        self._reconnect_monitor_task = asyncio.create_task(
            self._reconnect_monitor(), name="ares-mcp-auto-reconnect"
        )

    async def _reconnect_monitor(self) -> None:
        """Retry disconnected servers in the background with bounded backoff."""
        while True:
            try:
                await asyncio.sleep(self._reconnect_interval_seconds)
                await self.maintain_connections_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One broken server or unexpected maintenance error must not
                # permanently kill reconnection for the lifetime of Ares.
                logger.exception("MCP auto-reconnect iteration failed; continuing")

    async def ensure_running(self) -> dict[str, Any]:
        """Start recovery in any Ares mode and reconnect missing transports now."""
        self._start_reconnect_monitor()
        await self.maintain_connections_once()
        return self.readiness_report()

    async def ensure_server_running(self, name: str) -> dict[str, Any]:
        """Lazily connect one routed server without touching unrelated MCPs.

        The reconnect deadline is a circuit breaker for unhealthy optional
        servers. A request that needs an offline server fails fast during the
        backoff window instead of paying the process-start timeout every turn.
        """
        self._start_reconnect_monitor()
        if name not in self.servers:
            return {
                "name": name,
                "ready": False,
                "status": "offline",
                "error": f"MCP server '{name}' is not configured.",
            }
        current = self.readiness_report()["servers"][name]
        if current["ready"]:
            return current
        loop = asyncio.get_running_loop()
        retry_at = self._next_reconnect_at.get(name, 0.0)
        if loop.time() < retry_at:
            current = dict(current)
            current["status"] = "degraded"
            current["retry_after_seconds"] = max(0.0, retry_at - loop.time())
            return current

        report = await self.reconnect_server(name, force=False)
        if report.get("ready"):
            self._reconnect_failures.pop(name, None)
            self._next_reconnect_at.pop(name, None)
            return report

        failures = self._reconnect_failures.get(name, 0) + 1
        self._reconnect_failures[name] = failures
        delay = min(
            MAX_MCP_RECONNECT_BACKOFF_SECONDS,
            self._reconnect_interval_seconds * (2 ** min(failures - 1, 6)),
        )
        self._next_reconnect_at[name] = loop.time() + delay
        report = dict(report)
        report["retry_after_seconds"] = delay
        return report

    async def maintain_connections_once(self) -> dict[str, Any]:
        """Reconnect every configured server that currently has no live session.

        This is safe to call before any user turn as well as from the background
        monitor. It never replays an MCP tool request; it only rebuilds missing
        transports and refreshes their schemas.
        """
        async with self._maintenance_lock:
            return await self._maintain_connections_unlocked()

    async def _maintain_connections_unlocked(self) -> dict[str, Any]:
        """Perform one serialized health-and-reconnect maintenance pass."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        if (
            self.sessions
            and now - self._last_health_probe_at >= self._health_probe_interval_seconds
        ):
            self._last_health_probe_at = now
            await self.health_probe()
            now = loop.time()

        async def reconnect_one(name: str) -> None:
            if name in self.sessions or now < self._next_reconnect_at.get(name, 0.0):
                return
            report = await self.reconnect_server(name, force=False)
            if report.get("ready"):
                self._reconnect_failures.pop(name, None)
                self._next_reconnect_at.pop(name, None)
                return
            failures = self._reconnect_failures.get(name, 0) + 1
            self._reconnect_failures[name] = failures
            delay = min(
                MAX_MCP_RECONNECT_BACKOFF_SECONDS,
                self._reconnect_interval_seconds * (2 ** min(failures - 1, 6)),
            )
            self._next_reconnect_at[name] = loop.time() + delay

        await asyncio.gather(
            *(reconnect_one(name) for name in sorted(self.servers)),
            return_exceptions=True,
        )
        return self.readiness_report()

    def _disable_server_tools(self, name: str) -> None:
        """Remove a server namespace from the live model-facing catalog."""
        prefix = f"mcp__{name}__"
        self.tool_definitions = [
            tool for tool in self.tool_definitions
            if not tool.get("function", {}).get("name", "").startswith(prefix)
        ]

    async def close_server(self, name: str, *, drop_schemas: bool = False) -> None:
        """Close one transport; cached schemas remain diagnostic-only."""
        owner = self._owner_tasks.get(name)
        stop_event = self._owner_stop_events.get(name)
        if stop_event is not None:
            stop_event.set()
        if owner is not None and owner is not asyncio.current_task():
            # The owner performs the actual exit so AnyIO sees the same task
            # that entered its task group. Direct cross-task aclose() calls
            # are the source of otherwise permanent stdio reconnect failures.
            with suppress(asyncio.CancelledError, Exception):
                await owner
        elif owner is None:
            # Compatibility for injected/test sessions that have no owner.
            stack = self._exit_stacks.pop(name, None)
            if stack is not None:
                with suppress(Exception):
                    await stack.aclose()
            client = self._http_clients.pop(name, None)
            if client is not None:
                with suppress(Exception):
                    await client.aclose()
        self.sessions.pop(name, None)
        self._owner_tasks.pop(name, None)
        self._owner_stop_events.pop(name, None)
        self._disable_server_tools(name)
        if drop_schemas:
            self.schema_cache.pop(name, None)
        # A server restart can change even a read-only result. Clearing this
        # small in-memory cache is conservative and avoids stale projections.
        self._response_cache.clear()

    async def reconnect_server(self, name: str, *, force: bool = True) -> dict[str, Any]:
        """Reconnect one configured MCP server and refresh its cached schemas."""
        config = self.servers.get(name)
        if config is None:
            return {"name": name, "ready": False, "error": f"MCP server '{name}' is not configured."}
        lock = self._reconnect_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if not force and name in self.sessions and not self.server_errors.get(name):
                return self.readiness_report()["servers"][name]
            await self.close_server(name)
            try:
                await asyncio.wait_for(
                    self._connect_server(name, config),
                    timeout=self._connection_timeout(config),
                )
                self.server_errors.pop(name, None)
                self._reconnect_failures.pop(name, None)
                self._next_reconnect_at.pop(name, None)
                return self.readiness_report()["servers"][name]
            except BaseException as exc:
                _uncancel_task()
                self.server_errors[name] = str(exc) or exc.__class__.__name__
                return self.readiness_report()["servers"][name]

    def readiness_report(self) -> dict[str, Any]:
        """Return a per-server readiness report for diagnostics and startup UI."""
        servers: dict[str, Any] = {}
        for name in sorted(self.servers):
            config = self.servers[name]
            cached = self.schema_cache.get(name, [])
            connected = name in self.sessions
            error = redact_mcp_text(self.server_errors.get(name, ""))
            servers[name] = {
                "name": name,
                "ready": connected and not error,
                "status": "ready" if connected and not error else ("degraded" if error else "disconnected"),
                "transport": config.transport,
                "endpoint": redact_mcp_text(config.endpoint),
                "command": config.command,
                "timeout_seconds": config.timeout_seconds,
                "max_timeout_seconds": config.max_timeout_seconds,
                "tools": len(cached),
                "schema_cached": bool(cached),
                "error": error,
            }
        errors = {
            name: details["error"]
            for name, details in servers.items()
            if details["error"]
        }
        report = {
            "ready": any(item["ready"] for item in servers.values()),
            "configured": len(self.servers),
            "connected": sum(1 for item in servers.values() if item["ready"]),
            "tools": sum(item["tools"] for item in servers.values()),
            "servers": servers,
            "errors": errors,
        }
        # Keep the established report fields while exposing a redacted,
        # stable health view for upgraded diagnostics and UI consumers.
        report["health"] = project_mcp_health(self)
        return report

    def tools_by_server(self, server_name: str | None = None) -> dict[str, list[dict[str, str]]]:
        """Return discovered MCP tools grouped into a stable CLI-friendly shape."""
        names = [server_name] if server_name else sorted(self.servers)
        groups: dict[str, list[dict[str, str]]] = {}
        for name in names:
            if name not in self.servers:
                continue
            tools: list[dict[str, str]] = []
            for schema in self.schema_cache.get(name, []):
                function = schema.get("function", {})
                full_name = str(function.get("name") or "")
                prefix = f"mcp__{name}__"
                if not full_name.startswith(prefix):
                    continue
                description = str(function.get("description") or "")
                description = description.removeprefix(f"[MCP:{name}]").strip()
                tools.append(
                    {
                        "name": full_name.removeprefix(prefix),
                        "full_name": full_name,
                        "description": description,
                    }
                )
            groups[name] = sorted(tools, key=lambda tool: tool["name"])
        return groups

    async def health_probe(self) -> dict[str, Any]:
        """Probe connected sessions and refresh schema cache when possible."""
        async def probe_one(name: str, session: Any) -> None:
            config = self.servers.get(name, MCPServerConfig(name=name))
            try:
                response = await asyncio.wait_for(
                    session.list_tools(), timeout=config.timeout_seconds
                )
                schemas = [
                    self._to_openai_schema(name, tool)
                    for tool in getattr(response, "tools", [])
                ]
                self.schema_cache[name] = schemas
                self.tool_definitions = [
                    tool for tool in self.tool_definitions
                    if not tool.get("function", {}).get("name", "").startswith(f"mcp__{name}__")
                ] + schemas
                self.server_errors.pop(name, None)
            except asyncio.TimeoutError:
                self.server_errors[name] = f"Health probe timed out after {config.timeout_seconds:g}s."
                await self.close_server(name)
            except asyncio.CancelledError as exc:
                _clear_current_task_cancellation()
                self.server_errors[name] = f"Health probe cancelled: {exc}"
                await self.close_server(name)
            except Exception as exc:
                self.server_errors[name] = str(exc)
                await self.close_server(name)
        await asyncio.gather(
            *(
                probe_one(name, session)
                for name, session in list(self.sessions.items())
                if self._active_calls.get(name, 0) <= 0
            ),
            return_exceptions=True,
        )
        return self.readiness_report()

    async def _connect_server(self, name: str, config: MCPServerConfig) -> None:
        """Start one task-affine transport owner and wait until it is ready."""
        if ClientSession is None:
            raise RuntimeError(
                "The 'mcp' package is required for MCP server connections"
            )
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        stop_event = asyncio.Event()
        owner = asyncio.create_task(
            self._server_owner(name, config, ready, stop_event),
            name=f"ares-mcp-{name}",
        )
        self._owner_tasks[name] = owner
        self._owner_stop_events[name] = stop_event
        try:
            # Shield the readiness future so an outer connection timeout can
            # cancel and cleanly join the owner without cancelling this signal.
            await asyncio.shield(ready)
        except asyncio.CancelledError:
            _clear_current_task_cancellation()
            stop_event.set()
            owner.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await owner
            # The owner reports an opening failure through this future. The
            # outer timeout already decided the result, so retrieve it only to
            # prevent an unobserved-future warning.
            if ready.done() and not ready.cancelled():
                with suppress(Exception):
                    ready.exception()
            raise
        except BaseException:
            stop_event.set()
            with suppress(asyncio.CancelledError, Exception):
                await owner
            raise

    async def _server_owner(
        self,
        name: str,
        config: MCPServerConfig,
        ready: asyncio.Future[None],
        stop_event: asyncio.Event,
    ) -> None:
        """Own one MCP transport for its full lifetime in a single task."""
        stack = AsyncExitStack()
        http_client = None
        session = None
        cancelled = False
        opened = False
        try:
            if config.transport in {"streamable_http", "sse"}:
                if not config.endpoint:
                    raise ValueError("HTTP MCP servers require server_url or url")
                headers = {"User-Agent": "ares-mcp-client/0.1"}
                if config.oauth_client_id or config.oauth_scopes:
                    headers["Authorization"] = (
                        f"Bearer {await self.auth.ensure_token(config)}"
                    )
                http_client = httpx.AsyncClient(
                    headers=headers, timeout=config.timeout_seconds
                )
                if config.transport == "sse":
                    if sse_client is None:
                        raise RuntimeError(
                            "Installed mcp package does not support SSE transport"
                        )
                    read_stream, write_stream = await stack.enter_async_context(
                        sse_client(config.endpoint, http_client=http_client)
                    )
                else:
                    if streamable_http_client is None:
                        raise RuntimeError(
                            "Installed mcp package does not support Streamable HTTP transport"
                        )
                    read_stream, write_stream, _ = await stack.enter_async_context(
                        streamable_http_client(config.endpoint, http_client=http_client)
                    )
            elif config.transport == "stdio":
                if stdio_client is None or StdioServerParameters is None:
                    raise RuntimeError(
                        "Installed mcp package does not support stdio transport"
                    )
                if not config.command:
                    raise ValueError("stdio MCP servers require command")
                env = {**os.environ, **config.env} if config.env else None
                params = StdioServerParameters(
                    command=config.command, args=config.args, env=env
                )
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(params)
                )
            else:
                raise ValueError(f"Unsupported MCP transport: {config.transport}")
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await asyncio.wait_for(session.initialize(), timeout=config.timeout_seconds)
            tools_response = await asyncio.wait_for(
                session.list_tools(), timeout=config.timeout_seconds
            )
            self.sessions[name] = session
            self._exit_stacks[name] = stack
            if http_client is not None:
                self._http_clients[name] = http_client
            schemas = [
                self._to_openai_schema(name, tool)
                for tool in getattr(tools_response, "tools", [])
            ]
            self.schema_cache[name] = schemas
            prefix = f"mcp__{name}__"
            self.tool_definitions = [
                tool for tool in self.tool_definitions
                if not tool.get("function", {}).get("name", "").startswith(prefix)
            ] + schemas
            opened = True
            if not ready.done():
                ready.set_result(None)
            await stop_event.wait()
        except asyncio.CancelledError as exc:
            cancelled = True
            _clear_current_task_cancellation()
            message = f"MCP transport owner stopped: {exc or 'cancelled'}"
            if not ready.done():
                ready.set_exception(RuntimeError(message))
            elif not stop_event.is_set():
                self.server_errors[name] = message
                self._next_reconnect_at[name] = 0.0
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                self.server_errors[name] = str(exc) or exc.__class__.__name__
                self._next_reconnect_at[name] = 0.0
                logger.warning("MCP transport owner for '%s' stopped: %s", name, exc)
        finally:
            current = asyncio.current_task()
            if session is not None and self.sessions.get(name) is session:
                self.sessions.pop(name, None)
            # An owner can terminate without close_server() being the caller.
            # In that case its schemas must disappear from the live registry
            # immediately; schema_cache is retained for status diagnostics.
            self._disable_server_tools(name)
            if self._exit_stacks.get(name) is stack:
                self._exit_stacks.pop(name, None)
            if http_client is not None and self._http_clients.get(name) is http_client:
                self._http_clients.pop(name, None)
            with suppress(asyncio.CancelledError, Exception):
                await stack.aclose()
            if http_client is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await http_client.aclose()
            # Keep the owner registered until cleanup is complete. A monitor
            # that observes the missing session will then join this owner
            # before it creates a replacement transport.
            if self._owner_tasks.get(name) is current:
                self._owner_tasks.pop(name, None)
                self._owner_stop_events.pop(name, None)
            if opened and not stop_event.is_set() and name not in self.server_errors:
                self.server_errors[name] = "MCP transport stopped unexpectedly."
                self._next_reconnect_at[name] = 0.0
        if cancelled:
            raise asyncio.CancelledError

    def _to_openai_schema(self, server_name: str, tool: Any) -> dict[str, Any]:
        schema = copy.deepcopy(
            getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
            or {"type": "object", "properties": {}}
        )
        description = str(getattr(tool, "description", "") or "")
        if server_name == "playwright":
            description = (
                "Preferred for browser and web-page automation. Use this before Windows MCP "
                "for websites, web apps, forms, browser navigation, and page inspection. "
                + description
            )
        elif server_name == "windows":
            schema.setdefault("type", "object")
            properties = schema.setdefault("properties", {})
            short_name = str(getattr(tool, "name", "") or "").casefold()
            is_type = "type" in short_name
            is_launch = "launch" in short_name
            is_semantic_action = any(
                token in short_name
                for token in (
                    "click",
                    "type",
                    "edit",
                    "select",
                    "key",
                    "drag",
                    "move",
                    "scroll",
                    "launch",
                    "resize",
                )
            )
            if is_semantic_action:
                semantic_required = [
                    "expected_app",
                    "expected_region",
                    "purpose",
                    "semantic_intent",
                    "phase",
                ]
                if not is_launch:
                    semantic_required.append("ui_generation")
                if is_type:
                    semantic_required.append("text_owner")
                properties["__ares"] = {
                    "type": "object",
                    "description": (
                        "Ares-only semantic target metadata. It is validated locally and "
                        "removed before the Windows MCP server call."
                    ),
                    "properties": {
                        "expected_app": {
                            "type": "string",
                            "description": "App from the latest compact computer state.",
                        },
                        "expected_region": {
                            "type": "string",
                            "description": (
                                "Semantic subtree such as global_search, message_composer, "
                                "chat_header, navigation, editor, dialog, or active_window "
                                "for a batch wholly scoped to the focused window. Use "
                                "application only for a bootstrap Launch."
                            ),
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Why this exact UI action is needed.",
                        },
                        "semantic_intent": {
                            "type": "string",
                            "description": (
                                "Stable intent used for loop detection, such as "
                                "search_contact, select_contact, focus_composer, or type_message."
                            ),
                        },
                        "phase": {
                            "type": "string",
                            "description": "Current phase from the compact computer state.",
                        },
                        "entity": {
                            "type": "string",
                            "description": "Entity this action acts on, when applicable.",
                        },
                        "text_owner": {
                            "type": "string",
                            "description": (
                                "For Type: semantic owner of text, exactly matching "
                                "expected_region (for example global_search or message_composer)."
                            ),
                        },
                        "ui_generation": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Exact ui_generation from the latest compact computer state. "
                                "Older generations are rejected."
                            ),
                        },
                        "postcondition": {
                            "type": "string",
                            "description": "Observable state required after the action.",
                        },
                    },
                    "required": semantic_required,
                    "additionalProperties": True,
                }
                required = list(schema.get("required") or [])
                if "__ares" not in required:
                    required.append("__ares")
                schema["required"] = required
            description = (
                "For native Windows desktop apps, OS dialogs, and non-browser UI only. "
                "Do not use for normal websites while Playwright MCP is available. "
                + (
                    "This action is guarded by app/region/purpose/UI-generation preconditions; "
                    + (
                        "for a bootstrap Launch use expected_region=application and phase=open_app. "
                        if is_launch
                        else "supply required __ares semantic metadata from the latest Snapshot. "
                    )
                    if is_semantic_action
                    else ""
                )
                + description
            )
        return {
            "type": "function",
            "function": {
                "name": f"mcp__{server_name}__{tool.name}",
                "description": f"[MCP:{server_name}] {description}",
                "parameters": schema,
            },
        }

    def operation_timeout_for(self, tool_name: str, arguments: dict[str, Any] | None) -> float:
        """Return a tool call's normalized timeout without connecting or calling MCP.

        Callers that impose an outer deadline (such as the agent runtime) can
        use this before dispatching :meth:`call_tool`, keeping that deadline in
        sync with the per-server and per-call MCP policy.  Invalid names or
        metadata raise the same normalization errors as a call would report.
        """

        return self._prepare_call(tool_name, arguments).timeout.timeout_seconds

    def _prepare_call(
        self, tool_name: str, arguments: dict[str, Any] | None
    ) -> PreparedMCPCall:
        """Normalize one call's metadata without touching connection state."""

        raw_server = str(tool_name).removeprefix("mcp__").partition("__")[0]
        config = self.servers.get(raw_server, MCPServerConfig(name=raw_server))
        is_windows_observation = (
            raw_server == "windows"
            and str(tool_name).rsplit("__", 1)[-1].casefold()
            in {"snapshot", "screenshot"}
        )
        default_timeout = (
            min(config.timeout_seconds, 15.0)
            if is_windows_observation
            else config.timeout_seconds
        )
        return prepare_mcp_call(
            tool_name,
            arguments,
            default_timeout_seconds=default_timeout,
            max_timeout_seconds=config.max_timeout_seconds,
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool, applying advanced policy only when explicitly requested.

        Reserved ``__ares``/``_ares`` metadata is stripped before a request is
        sent to the MCP server. Legacy callers continue to receive the same
        rendered string; a structured envelope requires an explicit
        ``response_format``/``result_format`` opt-in.
        """

        try:
            prepared = self._prepare_call(tool_name, arguments)
        except (MCPUpgradeError, TypeError, ValueError) as exc:
            message = redact_mcp_text(exc)
            if "Invalid MCP tool name" in message:
                return f"Error: {message}"
            return f"Error: Invalid MCP call: {message}"

        server_name = prepared.tool.server_name
        mcp_tool = prepared.tool.tool_name
        timeout = prepared.timeout.timeout_seconds
        structured = self._structured_result_requested(prepared.metadata)
        warnings: list[str] = []
        read_only = self._is_read_only_tool(mcp_tool)
        # Some embedders construct a manager without calling start(). A tool
        # call must still activate persistent recovery for the rest of the
        # process lifetime.
        self._start_reconnect_monitor()
        # Cache keys intentionally exclude execution metadata. Do not cache a
        # paginated aggregate under the same key as a one-shot read.
        cache_enabled = (
            prepared.cache_ttl_seconds > 0
            and read_only
            and not prepared.pagination.enabled
        )

        if prepared.cache_ttl_seconds > 0 and not read_only:
            warnings.append(
                "Response caching was ignored because this MCP tool is not classified as read-only."
            )
        elif prepared.cache_ttl_seconds > 0 and prepared.pagination.enabled:
            warnings.append(
                "Response caching was ignored for this paginated call to keep cache entries unambiguous."
            )
        if prepared.pagination.enabled and not read_only:
            warnings.append(
                "Pagination was ignored because this MCP tool is not classified as read-only."
            )

        if cache_enabled:
            cached = self._response_cache.lookup_call(
                prepared.tool.canonical_name, prepared.arguments
            )
            if cached.hit:
                return self._format_upgraded_result(
                    rendered=str(cached.value),
                    structured=structured,
                    ok=True,
                    server_name=server_name,
                    mcp_tool=mcp_tool,
                    timeout=timeout,
                    warnings=warnings,
                    cache={
                        "enabled": True,
                        "hit": True,
                        "age_seconds": cached.age_seconds,
                        "expires_in_seconds": cached.expires_in_seconds,
                    },
                )

        # A missing configured session is a connection-state failure, not a
        # reason to replay an operation.  Reconnect exactly once before the
        # first remote call; after a call starts its result is returned as-is.
        reconnect_error = await self._reconnect_before_first_call(server_name)
        if reconnect_error is not None:
            return self._format_upgraded_result(
                rendered=reconnect_error,
                structured=structured,
                ok=False,
                server_name=server_name,
                mcp_tool=mcp_tool,
                timeout=timeout,
                warnings=warnings,
            )

        async def invoke() -> tuple[str, bool, dict[str, Any] | None]:
            self._active_calls[server_name] = self._active_calls.get(server_name, 0) + 1
            try:
                if prepared.pagination.enabled and read_only:
                    return await self._call_paginated_tool(
                        server_name=server_name,
                        mcp_tool=mcp_tool,
                        base_arguments=prepared.arguments,
                        timeout=timeout,
                        policy=prepared.pagination,
                    )
                rendered_result, call_failed, _payload = await self._call_rendered_tool(
                    server_name=server_name,
                    mcp_tool=mcp_tool,
                    arguments=prepared.arguments,
                    timeout=timeout,
                )
                return rendered_result, call_failed, None
            finally:
                remaining = self._active_calls.get(server_name, 1) - 1
                if remaining > 0:
                    self._active_calls[server_name] = remaining
                else:
                    self._active_calls.pop(server_name, None)

        rendered, failed, pagination = await invoke()

        # A transport exception evicts the dead session. Reconnect immediately
        # so schemas and the next model iteration remain usable. Read-only
        # operations are safe to replay once; mutations are never replayed
        # because the remote side may have applied them before disconnecting.
        if failed and server_name not in self.sessions:
            recovery = await self.reconnect_server(server_name, force=True)
            if recovery.get("ready"):
                if read_only:
                    warnings.append(
                        "MCP transport reconnected and the read-only operation was retried once."
                    )
                    rendered, failed, pagination = await invoke()
                else:
                    rendered = (
                        f"{rendered}\n\nMCP connection recovered. The original mutation was not "
                        "replayed because its remote outcome is uncertain."
                    )
            else:
                recovery_error = str(recovery.get("error") or "reconnect did not become ready")
                rendered = (
                    f"{rendered}\n\nImmediate MCP reconnect failed: "
                    f"{redact_mcp_text(recovery_error)}. Background recovery will keep trying."
                )

        if failed:
            return self._format_upgraded_result(
                rendered=rendered,
                structured=structured,
                ok=False,
                server_name=server_name,
                mcp_tool=mcp_tool,
                timeout=timeout,
                warnings=warnings,
                pagination=pagination,
            )

        if cache_enabled:
            self._response_cache.put_call(
                prepared.tool.canonical_name,
                prepared.arguments,
                rendered,
                ttl_seconds=prepared.cache_ttl_seconds,
            )

        return self._format_upgraded_result(
            rendered=rendered,
            structured=structured,
            ok=True,
            server_name=server_name,
            mcp_tool=mcp_tool,
            timeout=timeout,
            warnings=warnings,
            cache={"enabled": True, "hit": False} if cache_enabled else None,
            pagination=pagination,
        )

    async def _reconnect_before_first_call(self, server_name: str) -> str | None:
        """Reconnect an explicitly configured server once before a tool call.

        This intentionally runs only while no call has been issued. Recovery
        after a started call follows the separate read-only replay policy in
        :meth:`call_tool`; mutation calls are never replayed.
        """

        if server_name not in self.servers or server_name in self.sessions:
            return None
        lock = self._reconnect_locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            if server_name in self.sessions:
                return None
            config = self.servers[server_name]
            # A prior failed connection can leave a transport context without
            # a usable session.  Release it before constructing the one retry.
            await self.close_server(server_name)
            try:
                await asyncio.wait_for(
                    self._connect_server(server_name, config),
                    timeout=self._connection_timeout(config),
                )
            except asyncio.CancelledError as exc:
                _clear_current_task_cancellation()
                reason = f"Connection cancelled: {exc or 'cancelled'}"
            except Exception as exc:
                reason = str(exc) or exc.__class__.__name__
            else:
                if server_name in self.sessions:
                    self.server_errors.pop(server_name, None)
                    return None
                reason = "Connection completed without an active session."

            safe_reason = redact_mcp_text(reason)
            self.server_errors[server_name] = safe_reason
            await self.close_server(server_name)
            return (
                f"Error: MCP server '{server_name}' is not connected and reconnect failed: "
                f"{safe_reason}"
            )

    async def _call_rendered_tool(
        self,
        *,
        server_name: str,
        mcp_tool: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> tuple[str, bool, Any | None]:
        """Run one MCP call without replaying a request that has started."""

        # A Windows snapshot is observational, so refreshing the local process
        # after failure is safe.  The snapshot itself is *not* replayed here;
        # callers can issue a new request after the reconnect completes.
        can_recover_windows_snapshot = (
            server_name == "windows" and mcp_tool.casefold() == "snapshot"
        )
        try:
            session = self.sessions.get(server_name)
            if session is None:
                return f"Error: MCP server '{server_name}' is not connected.", True, None
            result = await asyncio.wait_for(
                session.call_tool(mcp_tool, arguments=arguments), timeout=timeout
            )
            is_error = bool(getattr(result, "isError", getattr(result, "is_error", False)))
            rendered = self._render_result(result)
            if is_error:
                return (
                    f"Error: MCP tool '{mcp_tool}' on '{server_name}' reported failure: "
                    f"{redact_mcp_text(rendered)}",
                    True,
                    None,
                )
            return rendered, False, self._result_payload(result)
        except asyncio.TimeoutError:
            if can_recover_windows_snapshot:
                await self._recover_windows_server(
                    f"Snapshot timed out after {timeout:g}s"
                )
            else:
                await self._mark_transport_disconnected(
                    server_name, f"Tool call timed out after {timeout:g}s."
                )
            return (
                f"Error: MCP tool '{mcp_tool}' on '{server_name}' timed out after {timeout:g}s.",
                True,
                None,
            )
        except asyncio.CancelledError as exc:
            _clear_current_task_cancellation()
            logger.warning(
                "MCP tool call cancelled for %s on %s: %s", mcp_tool, server_name, exc
            )
            await self._mark_transport_disconnected(
                server_name, f"Tool call was cancelled: {exc or 'cancelled'}"
            )
            return (
                f"Error: MCP tool '{mcp_tool}' on '{server_name}' was cancelled. "
                "Check the MCP server logs or increase timeout_seconds.",
                True,
                None,
            )
        except Exception as exc:
            if can_recover_windows_snapshot:
                await self._recover_windows_server(str(exc) or exc.__class__.__name__)
            else:
                await self._mark_transport_disconnected(
                    server_name, str(exc) or exc.__class__.__name__
                )
            return (
                f"Error calling MCP tool '{mcp_tool}' on '{server_name}': "
                f"{redact_mcp_text(exc)}",
                True,
                None,
            )

    async def _mark_transport_disconnected(self, server_name: str, reason: str) -> None:
        """Evict a failed transport so reconnect logic does not trust a dead session."""
        self.server_errors[server_name] = redact_mcp_text(reason)
        self._next_reconnect_at[server_name] = 0.0
        await self.close_server(server_name)

    async def _call_paginated_tool(
        self,
        *,
        server_name: str,
        mcp_tool: str,
        base_arguments: dict[str, Any],
        timeout: float,
        policy: Any,
    ) -> tuple[str, bool, dict[str, Any] | None]:
        """Execute bounded pagination without speculating on unstructured output."""

        pages: list[dict[str, Any]] = []
        cursor = policy.initial_cursor
        for page_number in range(1, policy.max_pages + 1):
            page_arguments = pagination_page_arguments(policy, page_number, cursor=cursor)
            conflicts = {
                key
                for key, value in page_arguments.items()
                if key in base_arguments and base_arguments[key] != value
            }
            if conflicts:
                return (
                    "Error: Pagination metadata conflicts with MCP argument(s): "
                    f"{', '.join(sorted(conflicts))}.",
                    True,
                    None,
                )
            rendered, failed, raw_payload = await self._call_rendered_tool(
                server_name=server_name,
                mcp_tool=mcp_tool,
                arguments={**base_arguments, **page_arguments},
                timeout=timeout,
            )
            if failed:
                return rendered, True, None
            payload = raw_payload if raw_payload is not None else self._json_payload(rendered)
            if payload is None:
                return rendered, False, {
                    "enabled": True,
                    "applied": False,
                    "reason": "MCP response was not structured JSON; returned the first response only.",
                    "page_count": 1,
                }
            page = project_pagination_page(payload, policy, page_number, cursor=cursor)
            pages.append(page)
            if not page["has_more"]:
                break
            next_cursor = page.get("next_cursor")
            if policy.mode == "cursor" and not next_cursor:
                break
            cursor = str(next_cursor) if next_cursor is not None else None

        merged = merge_paginated_responses(pages, policy)
        return (
            json.dumps(merged, indent=2, default=str),
            False,
            {"enabled": True, "applied": True, **merged},
        )

    @staticmethod
    def _result_payload(result: Any) -> Any | None:
        structured = getattr(result, "structured_content", None)
        return structured if structured is not None else None

    @staticmethod
    def _json_payload(rendered: str) -> Any | None:
        try:
            return json.loads(rendered)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _structured_result_requested(metadata: dict[str, Any]) -> bool:
        value = metadata.get(
            "response_format", metadata.get("result_format", metadata.get("structured"))
        )
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"structured", "json", "envelope", "true", "on"}
        return False

    @staticmethod
    def _is_read_only_tool(mcp_tool: str) -> bool:
        """Conservatively classify dynamic MCP tools before local caching."""

        normalized = re.sub(r"[^a-z0-9]+", "_", mcp_tool.casefold()).strip("_")
        if not normalized:
            return False
        mutation_tokens = {
            "add", "approve", "apply", "click", "commit", "create", "delete", "deploy",
            "disable", "edit", "enable", "execute", "fill", "install", "launch", "merge",
            "move", "patch", "post", "publish", "put", "remove", "run", "save", "send",
            "set", "start", "stop", "submit", "type", "update", "upload", "write",
        }
        if set(normalized.split("_")) & mutation_tokens:
            return False
        read_prefixes = (
            "browse", "check", "describe", "fetch", "find", "get", "inspect", "list",
            "lookup", "query", "read", "retrieve", "search", "snapshot", "screenshot",
            "status", "view",
        )
        return normalized.startswith(read_prefixes) or normalized.endswith(
            ("_snapshot", "_screenshot")
        )

    @staticmethod
    def _format_upgraded_result(
        *,
        rendered: str,
        structured: bool,
        ok: bool,
        server_name: str,
        mcp_tool: str,
        timeout: float,
        warnings: list[str],
        cache: dict[str, Any] | None = None,
        pagination: dict[str, Any] | None = None,
    ) -> str:
        """Return legacy text or the shared opt-in structured response shape."""

        if not structured:
            return rendered
        error = (
            project_mcp_error(rendered, server_name=server_name, tool_name=mcp_tool)
            if not ok
            else None
        )
        data: dict[str, Any] = {"result": rendered}
        if pagination is not None:
            data["pagination"] = pagination
        return json.dumps(
            {
                "ok": ok,
                "status": "completed" if ok else "failed",
                "summary": (
                    f"MCP tool '{mcp_tool}' completed."
                    if ok
                    else f"MCP tool '{mcp_tool}' failed."
                ),
                "data": data,
                "artifacts": [],
                "warnings": warnings,
                "errors": [] if error is None else [error],
                "next_actions": (
                    []
                    if ok or error is None or not error["retryable"]
                    else ["Retry the operation when the MCP server is ready."]
                ),
                "provenance": {"server": server_name, "tool": mcp_tool},
                "metrics": {
                    "timeout_seconds": timeout,
                    "cache": cache or {"enabled": False, "hit": False},
                    "pagination": {
                        "requested": pagination is not None,
                        "applied": bool(pagination and pagination.get("applied")),
                    },
                },
                "undo_id": None,
            },
            indent=2,
            default=str,
        )

    async def _recover_windows_server(self, reason: str) -> None:
        """Replace a crashed Windows MCP process without touching other MCPs."""
        name = "windows"
        config = self.servers.get(name)
        if config is None:
            return
        logger.warning("Restarting Windows MCP after Snapshot failure: %s", redact_mcp_text(reason))
        self.server_errors[name] = "Restarting after a failed desktop snapshot."
        await self.close_server(name)
        try:
            await asyncio.wait_for(
                self._connect_server(name, config), timeout=min(config.timeout_seconds, 20.0)
            )
        except Exception as exc:
            self.server_errors[name] = f"Restart failed: {redact_mcp_text(exc)}"
            logger.warning("Windows MCP restart failed: %s", self.server_errors[name])
        else:
            self.server_errors.pop(name, None)

    def _render_result(self, result: Any) -> str:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return json.dumps(structured, indent=2, default=str)
        content = getattr(result, "content", None)
        if not content:
            return str(result)
        parts: list[str] = []
        for item in content:
            if hasattr(item, "text"):
                parts.append(str(item.text))
            elif hasattr(item, "data"):
                mime = getattr(item, "mimeType", None) or getattr(
                    item, "mime_type", "binary"
                )
                parts.append(f"[binary content: {mime}]")
            else:
                parts.append(str(item))
        return "\n".join(parts)
