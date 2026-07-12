"""Trusted-registry MCP discovery and safe configuration planning.

Registry metadata is useful, but it is not permission to execute a command.
This module returns a constrained configuration plan; callers must present it
to the user and obtain confirmation before adding it to ``mcp_servers``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlparse

import httpx

from ares.models import MCPRegistry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 12.0
SEARCH_CACHE_TTL_SECONDS = 300.0
_SAFE_SERVER_NAME = re.compile(r"[^a-z0-9_-]+")


class RegistryError(RuntimeError):
    """A display-safe MCP registry failure."""


class MCPInstallSafetyError(ValueError):
    """Registry metadata cannot be converted into a safe Ares MCP config."""


@dataclass(frozen=True)
class MCPResult:
    name: str
    description: str
    version: str
    repository: str
    registry: str
    title: str = ""
    canonical_url: str = ""
    verified: bool = False
    stars: int | None = None
    downloads: int | None = None


@dataclass(frozen=True)
class MCPServerDetail:
    name: str
    description: str
    version: str
    repository: str
    registry: str
    title: str = ""
    packages: list[dict[str, Any]] = field(default_factory=list)
    remotes: list[dict[str, Any]] = field(default_factory=list)
    canonical_url: str = ""
    verified: bool = False
    stars: int | None = None
    downloads: int | None = None


@dataclass(frozen=True)
class InstallCommand:
    """A reviewable, non-executing MCP configuration plan."""

    source_name: str
    server_name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    server_url: str = ""
    env_requirements: tuple[str, ...] = ()
    registry: str = ""
    repository: str = ""

    def as_config(self, *, existing_names: set[str] | None = None) -> dict[str, Any]:
        """Return a config shape accepted by ``MCPClientManager``.

        Required environment variables are deliberately listed to the user but
        never filled from registry content.  The user must set values locally.
        """
        name = derive_server_name(self.server_name, existing_names or set())
        payload: dict[str, Any] = {"name": name, "transport": self.transport}
        if self.command:
            payload["command"] = self.command
            payload["args"] = list(self.args)
        if self.server_url:
            payload["server_url"] = self.server_url
        if self.env_requirements:
            payload["env"] = {key: "" for key in self.env_requirements}
        return payload


ClientFactory = Callable[[], httpx.AsyncClient]


class MCPRegistryClient:
    """Discover MCP metadata from configured, trusted registries only."""

    # Registry catalog reads can occasionally stall at an upstream edge. Keep
    # a short process-local cache so a successful search remains useful while
    # the same registry recovers; config mutation is never cached.
    _search_cache: dict[tuple[str, str], tuple[float, list[MCPResult]]] = {}

    def __init__(
        self,
        registries: list[MCPRegistry],
        *,
        client_factory: ClientFactory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.registries = sorted(
            [registry for registry in registries if registry.enabled],
            key=lambda registry: (-registry.priority, registry.name),
        )
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)
        )
        self.timeout_seconds = timeout_seconds
        self.last_errors: dict[str, str] = {}

    def configured_registry(self, name: str) -> MCPRegistry | None:
        needle = str(name or "").strip().casefold()
        return next((registry for registry in self.registries if registry.name.casefold() == needle), None)

    async def search(self, query: str, registry: str | None = None) -> list[MCPResult]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("An MCP server search query is required.")
        targets = self._select_registries(registry)
        responses = await asyncio.gather(
            *(self._search_registry(target, query) for target in targets),
            return_exceptions=True,
        )
        results: list[MCPResult] = []
        for target, response in zip(targets, responses):
            if isinstance(response, Exception):
                self.last_errors[target.name] = str(response) or f"Registry '{target.name}' is unavailable."
                logger.info("MCP registry %s skipped: %s", target.name, self.last_errors[target.name])
                continue
            self.last_errors.pop(target.name, None)
            results.extend(response)
        priority = {item.name: item.priority for item in self.registries}
        return sorted(results, key=lambda item: (-priority.get(item.registry, 0), item.name.casefold()))

    async def get_server(self, name: str, registry: str | None = None) -> MCPServerDetail | None:
        for target in self._select_registries(registry):
            try:
                detail = await self._get_server_detail(target, name)
                self.last_errors.pop(target.name, None)
                return detail
            except RegistryError as exc:
                self.last_errors[target.name] = str(exc)
        return None

    async def get_install_command(self, name: str, registry: str | None = None) -> InstallCommand | None:
        """Build a safe configuration plan from one server's registry metadata."""
        detail = await self.get_server(name, registry)
        if detail is None:
            return None
        return installation_plan(detail)

    def _select_registries(self, name: str | None) -> list[MCPRegistry]:
        if not name:
            return list(self.registries)
        target = self.configured_registry(name)
        if target is None:
            raise ValueError(f"Registry '{name}' is not configured or enabled.")
        return [target]

    async def _search_registry(self, registry: MCPRegistry, query: str) -> list[MCPResult]:
        cache_key = (registry.api_base.rstrip("/").casefold(), query.casefold())
        try:
            if _is_official_registry(registry):
                # The official v0.1 catalog supports a name-substring ``search``
                # parameter. Asking the registry to filter avoids downloading a
                # huge catalog page just to search it locally.
                response = await self._request(
                    registry,
                    "/v0.1/servers",
                    params={"search": query, "limit": 25, "version": "latest"},
                )
                data = self._json(response, registry)
                entries = data.get("servers") or []
            elif _is_smithery(registry):
                response = await self._request(
                    registry,
                    "/servers",
                    params={"q": query, "page": 1, "pageSize": 25},
                )
                data = self._json(response, registry)
                entries = data.get("servers") or []
            else:
                response = await self._request(registry, "/search", params={"q": query, "limit": 25})
                data = self._json(response, registry)
                entries = data.get("results") or data.get("servers") or []
            if not isinstance(entries, list):
                raise RegistryError("Registry returned an invalid server list.")
            results = [result for entry in entries if (result := _to_result(registry, entry)) is not None]
            self._search_cache[cache_key] = (time.monotonic(), results)
            return results
        except RegistryError:
            cached = self._search_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] <= SEARCH_CACHE_TTL_SECONDS:
                logger.info("Using cached MCP registry results for %s after registry failure", registry.name)
                return list(cached[1])
            raise

    async def _get_server_detail(self, registry: MCPRegistry, name: str) -> MCPServerDetail:
        encoded = quote(str(name).strip(), safe="")
        if _is_official_registry(registry):
            response = await self._request(registry, f"/v0.1/servers/{encoded}/versions/latest")
        elif _is_smithery(registry):
            response = await self._request(registry, f"/servers/{encoded}")
        else:
            response = await self._request(registry, f"/servers/{encoded}")
        data = self._json(response, registry)
        raw = data.get("server") if isinstance(data.get("server"), dict) else data
        if not isinstance(raw, dict):
            raise RegistryError("Registry returned invalid server metadata.")
        # The official latest-version endpoint may wrap the server version in
        # a ``server`` property or put package fields at the root.
        packages = raw.get("packages") or data.get("packages") or []
        remotes = raw.get("remotes") or data.get("remotes") or []
        if not isinstance(packages, list):
            packages = []
        if not isinstance(remotes, list):
            remotes = []
        result = _to_result(registry, raw)
        if result is None:
            raise RegistryError("Registry response has no server name.")
        return MCPServerDetail(
            name=result.name,
            title=result.title,
            description=result.description,
            version=result.version,
            repository=result.repository,
            registry=registry.name,
            packages=[dict(item) for item in packages if isinstance(item, dict)],
            remotes=[dict(item) for item in remotes if isinstance(item, dict)],
            canonical_url=result.canonical_url,
            verified=result.verified,
            stars=result.stars,
            downloads=result.downloads,
        )

    async def _request(
        self,
        registry: MCPRegistry,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response: httpx.Response | None = None
        last_timeout: httpx.TimeoutException | None = None
        # Public registry reads are idempotent. A single retry prevents an
        # intermittent edge timeout from making marketplace discovery look
        # broken, without retrying any configuration-changing request.
        for attempt in range(2):
            try:
                async with self._client_factory() as client:
                    response = await client.get(
                        f"{registry.api_base.rstrip('/')}/{path.lstrip('/')}",
                        params=params,
                        headers=self._headers(registry),
                        # The official catalog can take longer than a normal
                        # lookup while it pages a large public registry. Keep a
                        # bounded, registry-specific allowance instead of making
                        # every marketplace request sluggish.
                        timeout=max(self.timeout_seconds, 20.0) if _is_official_registry(registry) else self.timeout_seconds,
                    )
                break
            except httpx.TimeoutException as exc:
                last_timeout = exc
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
            except httpx.HTTPError as exc:
                raise RegistryError(f"Registry '{registry.name}' could not be reached.") from exc
        if response is None:
            raise RegistryError(f"Registry '{registry.name}' timed out while loading server metadata.") from last_timeout
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            wait = f" Retry after {retry_after}s." if retry_after else ""
            raise RegistryError(f"Registry '{registry.name}' rate limited this request.{wait}")
        if response.status_code >= 400:
            if response.status_code == 401:
                detail = "an API token is required or invalid"
            elif response.status_code == 404:
                detail = "the requested server was not found"
            else:
                detail = f"returned HTTP {response.status_code}"
            raise RegistryError(f"Registry '{registry.name}': {detail}.")
        return response

    @staticmethod
    def _headers(registry: MCPRegistry) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "ares-marketplace/0.1"}
        if registry.auth_token:
            headers["Authorization"] = f"Bearer {registry.auth_token}"
        return headers

    @staticmethod
    def _json(response: httpx.Response, registry: MCPRegistry) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RegistryError(f"Registry '{registry.name}' returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise RegistryError(f"Registry '{registry.name}' returned an invalid JSON object.")
        return payload


def installation_plan(detail: MCPServerDetail) -> InstallCommand | None:
    """Return a constrained plan, never a shell string from a registry."""
    for remote in detail.remotes:
        transport = str(remote.get("type") or remote.get("transport") or "").casefold()
        url = str(remote.get("url") or remote.get("serverUrl") or "").strip()
        if transport in {"streamable-http", "streamable_http", "http"} and _safe_https_url(url):
            return InstallCommand(
                source_name=detail.name,
                server_name=detail.name,
                transport="streamable_http",
                server_url=url,
                registry=detail.registry,
                repository=detail.repository,
            )
        if transport == "sse" and _safe_https_url(url):
            return InstallCommand(
                source_name=detail.name,
                server_name=detail.name,
                transport="sse",
                server_url=url,
                registry=detail.registry,
                repository=detail.repository,
            )
    for package in detail.packages:
        registry_type = str(package.get("registryType") or package.get("registryName") or "").casefold()
        identifier = str(package.get("identifier") or package.get("packageName") or "").strip()
        transport = package.get("transport")
        transport_type = str(transport.get("type") if isinstance(transport, dict) else transport or "stdio").casefold()
        if transport_type not in {"stdio", ""} or not identifier:
            continue
        version = str(package.get("version") or "").strip()
        required_env = _required_env_names(package)
        package_args = _safe_package_arguments(package)
        if package_args is None:
            # A client would have to invent a secret or unresolved variable to
            # run this package. Make the user configure it manually instead.
            continue
        if registry_type == "npm" and _safe_package_identifier(identifier):
            target = f"{identifier}@{version}" if version else identifier
            return InstallCommand(
                source_name=detail.name,
                server_name=detail.name,
                transport="stdio",
                command="npx",
                args=("-y", target, *package_args),
                env_requirements=required_env,
                registry=detail.registry,
                repository=detail.repository,
            )
        if registry_type in {"pypi", "python"} and _safe_package_identifier(identifier):
            target = f"{identifier}=={version}" if version else identifier
            return InstallCommand(
                source_name=detail.name,
                server_name=detail.name,
                transport="stdio",
                command="uvx",
                args=(target, *package_args),
                env_requirements=required_env,
                registry=detail.registry,
                repository=detail.repository,
            )
    return None


def derive_server_name(source_name: str, existing_names: set[str]) -> str:
    """Create a stable config key without leaking registry path syntax."""
    base = str(source_name).casefold().rsplit("/", 1)[-1]
    base = _SAFE_SERVER_NAME.sub("-", base).strip("-_")[:48] or "marketplace-server"
    candidate = base
    suffix = 2
    folded_existing = {str(name).casefold() for name in existing_names}
    while candidate.casefold() in folded_existing:
        candidate = f"{base[:42]}-{suffix}"
        suffix += 1
    return candidate


def _to_result(registry: MCPRegistry, entry: Any) -> MCPResult | None:
    raw = entry.get("server", entry) if isinstance(entry, dict) else None
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("qualifiedName") or raw.get("slug") or "").strip()
    if not name:
        return None
    repository = raw.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("url") or repository.get("source")
    title = str(raw.get("title") or raw.get("displayName") or "")
    version = str(raw.get("version") or raw.get("latestVersion") or "unknown")
    canonical = raw.get("homepage") or raw.get("url") or ""
    if not canonical and _is_smithery(registry):
        canonical = f"https://smithery.ai/server/{name}"
    return MCPResult(
        name=name,
        title=title,
        description=str(raw.get("description") or raw.get("summary") or ""),
        version=version,
        repository=str(repository or ""),
        registry=registry.name,
        canonical_url=str(canonical),
        verified=bool(raw.get("verified") or raw.get("isVerified")),
        stars=_public_count(raw, names=("stars", "starCount", "stargazersCount")),
        downloads=_public_count(raw, names=("downloads", "downloadCount", "installs", "installCount")),
    )


def _is_official_registry(registry: MCPRegistry) -> bool:
    return registry.name.casefold() in {"mcp-registry", "mcp_registry"} or "registry.modelcontextprotocol.io" in registry.api_base


def _is_smithery(registry: MCPRegistry) -> bool:
    return registry.name.casefold() == "smithery" or "smithery.ai" in registry.api_base


def _public_count(value: Any, *, names: tuple[str, ...]) -> int | None:
    """Return registry-published popularity data only; never infer a count."""
    if not isinstance(value, dict):
        return None
    for container in (value, value.get("stats"), value.get("metrics"), value.get("metadata")):
        if not isinstance(container, dict):
            continue
        for name in names:
            raw = container.get(name)
            if isinstance(raw, bool):
                continue
            try:
                count = int(raw)
            except (TypeError, ValueError):
                continue
            if count >= 0:
                return count
    return None


def _safe_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and "{" not in value
        and "}" not in value
        and not parsed.username
        and not parsed.password
    )


def _safe_package_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"@?[a-zA-Z0-9][a-zA-Z0-9._/@-]{0,200}", value)) and ".." not in value


def _required_env_names(package: dict[str, Any]) -> tuple[str, ...]:
    raw = package.get("environmentVariables") or package.get("env") or package.get("envVars") or []
    names: list[str] = []
    if isinstance(raw, dict):
        raw = raw.keys()
    for item in raw:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            if item.get("required") is False:
                continue
            name = str(item.get("name") or item.get("key") or "")
        else:
            continue
        if re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) and name not in names:
            names.append(name)
    return tuple(names)


def _safe_package_arguments(package: dict[str, Any]) -> tuple[str, ...] | None:
    """Extract only fully specified, non-secret registry package arguments."""
    raw = package.get("packageArguments") or package.get("arguments") or []
    if not isinstance(raw, list):
        return ()
    result: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        if item.get("isSecret"):
            return None
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if value is None or "{" in str(value) or "}" in str(value):
            # Missing required values and template variables should never be
            # fabricated by an installer.
            if item.get("isRequired", True):
                return None
            continue
        value = str(value)
        argument_type = str(item.get("type") or "").casefold()
        if name:
            if not name.startswith("-") or "\x00" in name or "\x00" in value:
                return None
            result.extend((name, value))
        elif argument_type in {"", "positional"} and "\x00" not in value:
            result.append(value)
        else:
            return None
    return tuple(result)
