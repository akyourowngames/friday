"""MCP client integration for Ares.

This module manages configured Model Context Protocol servers, OAuth token
storage/refresh, tool discovery, and MCP tool execution through the official SDK.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import importlib.util
import json
import logging
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
from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

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
    timeout_seconds: float = 60.0

    @model_validator(mode="after")
    def normalize(self) -> "MCPServerConfig":
        if self.url and not self.server_url:
            self.server_url = self.url
        if self.command and not self.server_url and self.transport == "streamable_http":
            self.transport = "stdio"
        if self.transport == "http":
            self.transport = "streamable_http"
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
        self.tool_definitions: list[dict[str, Any]] = []
        self.schema_cache: dict[str, list[dict[str, Any]]] = {}
        self.server_errors: dict[str, str] = {}

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

    async def start(self) -> None:
        """Connect to all configured MCP servers.

        Failures are logged per server and do not block others.  The asyncio
        CancelledError that the MCP SDK can raise is fully consumed so it never
        leaks into the caller's event loop.
        """
        if self.sessions or self._exit_stacks or self._http_clients:
            await self.close()
        self.tool_definitions = []
        self.server_errors = {}
        async def connect_one(name: str, config: MCPServerConfig) -> None:
            try:
                await asyncio.wait_for(
                    self._connect_server(name, config),
                    timeout=max(1.0, float(config.timeout_seconds) * 2 + 1),
                )
                self.server_errors.pop(name, None)
            except asyncio.CancelledError as exc:
                _clear_current_task_cancellation()
                self.server_errors[name] = f"Connection cancelled: {exc or 'cancelled'}"
                await self.close_server(name)
            except Exception as exc:
                self.server_errors[name] = str(exc) or exc.__class__.__name__
                await self.close_server(name)
                logger.warning(
                    "Failed to connect MCP server '%s' — check config and network",
                    name,
                )
            finally:
                _uncancel_task()
        # Independent integrations must not queue behind one slow/unhealthy
        # server.  Each task owns a timeout and an explicit readiness result.
        await asyncio.gather(
            *(connect_one(name, config) for name, config in self.servers.items()),
            return_exceptions=True,
        )

    async def close(self) -> None:
        for stack in list(self._exit_stacks.values()):
            with suppress(Exception):
                await stack.aclose()
        for client in list(self._http_clients.values()):
            with suppress(Exception):
                await client.aclose()
        self.sessions.clear()
        self._exit_stacks.clear()
        self._http_clients.clear()
        self.tool_definitions.clear()

    async def close_server(self, name: str) -> None:
        """Close one MCP server session and keep all other servers connected."""
        stack = self._exit_stacks.pop(name, None)
        if stack is not None:
            with suppress(Exception):
                await stack.aclose()
        client = self._http_clients.pop(name, None)
        if client is not None:
            with suppress(Exception):
                await client.aclose()
        self.sessions.pop(name, None)
        self.tool_definitions = [
            tool for tool in self.tool_definitions
            if not tool.get("function", {}).get("name", "").startswith(f"mcp__{name}__")
        ]

    async def reconnect_server(self, name: str) -> dict[str, Any]:
        """Reconnect one configured MCP server and refresh its cached schemas."""
        config = self.servers.get(name)
        if config is None:
            return {"name": name, "ready": False, "error": f"MCP server '{name}' is not configured."}
        await self.close_server(name)
        try:
            await self._connect_server(name, config)
            self.server_errors.pop(name, None)
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
                "tools": len(cached),
                "schema_cached": bool(cached),
                "error": error,
            }
        errors = {
            name: details["error"]
            for name, details in servers.items()
            if details["error"]
        }
        return {
            "ready": any(item["ready"] for item in servers.values()),
            "configured": len(self.servers),
            "connected": sum(1 for item in servers.values() if item["ready"]),
            "tools": sum(item["tools"] for item in servers.values()),
            "servers": servers,
            "errors": errors,
        }

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
            *(probe_one(name, session) for name, session in list(self.sessions.items())),
            return_exceptions=True,
        )
        return self.readiness_report()

    async def _connect_server(self, name: str, config: MCPServerConfig) -> None:
        if ClientSession is None:
            raise RuntimeError(
                "The 'mcp' package is required for MCP server connections"
            )
        stack = AsyncExitStack()
        http_client = None
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
            tools_response =             await asyncio.wait_for(
                session.list_tools(), timeout=config.timeout_seconds
            )
        except asyncio.CancelledError as exc:
            _uncancel_task()
            await stack.aclose()
            if http_client is not None:
                await http_client.aclose()
            raise RuntimeError(f"MCP connection cancelled: {exc or 'cancelled'}") from exc
        except Exception:
            await stack.aclose()
            if http_client is not None:
                await http_client.aclose()
            raise
        self.sessions[name] = session
        self._exit_stacks[name] = stack
        if http_client is not None:
            self._http_clients[name] = http_client
        schemas: list[dict[str, Any]] = []
        for tool in getattr(tools_response, "tools", []):
            schemas.append(self._to_openai_schema(name, tool))
        self.schema_cache[name] = schemas
        self.tool_definitions.extend(schemas)

    def _to_openai_schema(self, server_name: str, tool: Any) -> dict[str, Any]:
        schema = (
            getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
            or {"type": "object", "properties": {}}
        )
        return {
            "type": "function",
            "function": {
                "name": f"mcp__{server_name}__{tool.name}",
                "description": f"[MCP:{server_name}] {getattr(tool, 'description', '') or ''}",
                "parameters": schema,
            },
        }

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            _, server_name, mcp_tool = tool_name.split("__", 2)
        except ValueError:
            return f"Error: Invalid MCP tool name '{tool_name}'. Expected mcp__<server>__<tool>."
        session = self.sessions.get(server_name)
        if session is None:
            return f"Error: MCP server '{server_name}' is not connected."
        timeout = self.servers.get(
            server_name, MCPServerConfig(name=server_name)
        ).timeout_seconds
        try:
            result = await asyncio.wait_for(
                session.call_tool(mcp_tool, arguments=arguments), timeout=timeout
            )
            is_error = bool(getattr(result, "isError", getattr(result, "is_error", False)))
            rendered = self._render_result(result)
            if is_error:
                return f"Error: MCP tool '{mcp_tool}' on '{server_name}' reported failure: {rendered}"
            return rendered
        except asyncio.TimeoutError:
            return f"Error: MCP tool '{mcp_tool}' on '{server_name}' timed out after {timeout:g}s."
        except asyncio.CancelledError as exc:
            _clear_current_task_cancellation()
            logger.warning(
                "MCP tool call cancelled for %s on %s: %s", mcp_tool, server_name, exc
            )
            return f"Error: MCP tool '{mcp_tool}' on '{server_name}' was cancelled. Check the MCP server logs or increase timeout_seconds."
        except Exception as exc:
            return f"Error calling MCP tool '{mcp_tool}' on '{server_name}': {exc}"

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
