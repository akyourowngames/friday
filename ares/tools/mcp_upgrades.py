"""Pure helpers for the upgraded MCP execution boundary.

The live :mod:`ares.tools.mcp_client` deliberately remains a small, backwards
compatible transport wrapper.  This module contains the opt-in policy pieces
that a caller can compose around it: reserved Ares metadata handling, bounded
timeouts, pagination, cache keys, and safe diagnostic projections.

Nothing in this module sends a request or imports the MCP SDK.  That makes the
helpers safe to use from the CLI, agent runtime, watcher workflows, and tests
without changing legacy MCP calls until the caller explicitly opts in.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_MCP_TIMEOUT_SECONDS = 60.0
MAX_MCP_TIMEOUT_SECONDS = 600.0
MIN_MCP_TIMEOUT_SECONDS = 1.0
DEFAULT_MCP_CACHE_TTL_SECONDS = 0.0
MAX_MCP_CACHE_TTL_SECONDS = 3_600.0
DEFAULT_PAGINATION_MAX_PAGES = 10
MAX_PAGINATION_MAX_PAGES = 100

_METADATA_PREFIXES = ("__ares_", "_ares_")
_METADATA_CONTAINERS = {"__ares", "_ares"}
_METADATA_ALIASES = {
    "timeout": "timeout_seconds",
    "timeout_seconds": "timeout_seconds",
    "cache_ttl": "cache_ttl_seconds",
    "cache_ttl_seconds": "cache_ttl_seconds",
    "paginate": "pagination",
    "pagination": "pagination",
}
_TOOL_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_PARAMETER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$|^\d+$")

_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "id_token",
    "key",
    "password",
    "refresh_token",
    "secret",
    "token",
    "x_api_key",
    "x-api-key",
}
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|id[_-]?token|"
        r"refresh[_-]?token|token|secret|password)\b\s*[=:]\s*)([^\s,;&]+)"
    ),
    re.compile(r"(?i)(--(?:api[-_]?key|token|secret|password)\s+)([^\s,;&]+)"),
    re.compile(r"\b(sk|pk)_[A-Za-z0-9_-]{12,}\b"),
)
_URL_PATTERN = re.compile(r"https?://[^\s,;]+", re.IGNORECASE)


class MCPUpgradeError(ValueError):
    """Raised when opt-in MCP policy input cannot be normalized safely."""


@dataclass(frozen=True)
class ParsedMCPToolName:
    """A validated dynamic MCP tool identifier."""

    server_name: str
    tool_name: str

    @property
    def canonical_name(self) -> str:
        return f"mcp__{self.server_name}__{self.tool_name}"


@dataclass(frozen=True)
class TimeoutPolicy:
    """The effective timeout and whether normalization changed the request."""

    timeout_seconds: float
    requested_seconds: float | None
    used_default: bool
    capped: bool


@dataclass(frozen=True)
class PaginationPolicy:
    """Validated client-side pagination settings for one MCP tool call."""

    enabled: bool = False
    mode: Literal["page", "cursor"] = "page"
    max_pages: int = 1
    page_param: str = "page"
    initial_page: int = 1
    cursor_param: str = "cursor"
    initial_cursor: str | None = None
    items_path: str = "items"
    next_cursor_path: str = "next_cursor"
    has_more_path: str = "has_more"
    merge_mode: Literal["items", "pages", "last"] = "items"
    stop_on_empty: bool = True


@dataclass(frozen=True)
class PreparedMCPCall:
    """MCP-safe arguments plus opt-in Ares execution policy.

    ``arguments`` can be passed directly to an MCP server.  ``metadata`` is
    intentionally separate and never contains a value destined for the server.
    """

    tool: ParsedMCPToolName
    arguments: dict[str, Any]
    metadata: dict[str, Any]
    timeout: TimeoutPolicy
    cache_ttl_seconds: float
    pagination: PaginationPolicy


@dataclass(frozen=True)
class CacheLookup:
    """A cache lookup result that distinguishes a miss from a cached ``None``."""

    key: str
    hit: bool
    value: Any | None = None
    age_seconds: float | None = None
    expires_in_seconds: float | None = None


@dataclass
class _CacheEntry:
    value: Any
    created_at: float
    expires_at: float


def parse_mcp_tool_name(tool_name: object) -> ParsedMCPToolName:
    """Validate and split ``mcp__<server>__<tool>`` without guessing.

    Dynamic tool names are protocol identifiers rather than shell fragments.
    Rejecting whitespace, extra separators in the server portion, and control
    characters here keeps downstream transports from receiving ambiguous names.
    """

    if not isinstance(tool_name, str):
        raise MCPUpgradeError("MCP tool name must be a string.")
    value = tool_name.strip()
    if value != tool_name or not value.startswith("mcp__"):
        raise MCPUpgradeError(
            "Invalid MCP tool name. Expected mcp__<server>__<tool>."
        )
    remainder = value.removeprefix("mcp__")
    server_name, separator, remote_name = remainder.partition("__")
    if not separator or not server_name or not remote_name:
        raise MCPUpgradeError(
            "Invalid MCP tool name. Expected mcp__<server>__<tool>."
        )
    if not _TOOL_SEGMENT_PATTERN.fullmatch(server_name):
        raise MCPUpgradeError("MCP server name contains unsupported characters.")
    if not _TOOL_NAME_PATTERN.fullmatch(remote_name):
        raise MCPUpgradeError("MCP tool name contains unsupported characters.")
    return ParsedMCPToolName(server_name=server_name, tool_name=remote_name)


def split_mcp_arguments(
    arguments: Mapping[str, Any] | None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split reserved ``__ares``/``_ares`` metadata from server arguments.

    Both container forms (``{"__ares": {...}}``) and flattened forms
    (``{"__ares_timeout_seconds": 15}``) are accepted.  Reserved keys are
    *always* removed from the returned server argument mapping.  Conflicting
    aliases are rejected instead of silently choosing one policy value.
    """

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise MCPUpgradeError("MCP arguments must be an object.")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise MCPUpgradeError("MCP metadata must be an object when provided.")

    server_arguments: dict[str, Any] = {}
    metadata_sources: list[tuple[str, Mapping[str, Any]]] = []
    flattened_metadata: list[tuple[str, Any]] = []
    if metadata:
        _collect_metadata_source("metadata", metadata, metadata_sources, flattened_metadata)

    for raw_key, value in arguments.items():
        if not isinstance(raw_key, str):
            raise MCPUpgradeError("MCP argument keys must be strings.")
        if raw_key in _METADATA_CONTAINERS:
            if not isinstance(value, Mapping):
                raise MCPUpgradeError(f"{raw_key} metadata must be an object.")
            metadata_sources.append((raw_key, value))
            continue
        prefix = next((candidate for candidate in _METADATA_PREFIXES if raw_key.startswith(candidate)), None)
        if prefix is not None:
            metadata_key = raw_key.removeprefix(prefix)
            if not metadata_key:
                raise MCPUpgradeError(f"{raw_key} is not a valid Ares metadata key.")
            flattened_metadata.append((metadata_key, value))
            continue
        if raw_key.startswith("__ares") or raw_key.startswith("_ares"):
            raise MCPUpgradeError(
                f"{raw_key} is reserved for Ares metadata; use __ares_<key> or __ares."
            )
        server_arguments[raw_key] = copy.deepcopy(value)

    normalized_metadata: dict[str, Any] = {}
    for source_name, source in metadata_sources:
        for raw_key, value in source.items():
            if not isinstance(raw_key, str):
                raise MCPUpgradeError(f"{source_name} metadata keys must be strings.")
            _merge_metadata_value(normalized_metadata, raw_key, value)
    for raw_key, value in flattened_metadata:
        _merge_metadata_value(normalized_metadata, raw_key, value)
    return server_arguments, normalized_metadata


def normalize_mcp_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a standalone Ares metadata mapping using the same strict rules."""

    _arguments, normalized = split_mcp_arguments({}, metadata=metadata)
    return normalized


def _merge_metadata_value(target: dict[str, Any], raw_key: str, value: Any) -> None:
    key = _METADATA_ALIASES.get(raw_key.strip().casefold().replace("-", "_"), raw_key)
    if not key:
        raise MCPUpgradeError("Ares metadata key cannot be blank.")
    copied_value = copy.deepcopy(value)
    if key in target and not _values_equivalent(target[key], copied_value):
        raise MCPUpgradeError(f"Conflicting values supplied for Ares metadata '{key}'.")
    target[key] = copied_value


def _collect_metadata_source(
    source_name: str,
    source: Mapping[str, Any],
    metadata_sources: list[tuple[str, Mapping[str, Any]]],
    flattened_metadata: list[tuple[str, Any]],
) -> None:
    """Collect explicit metadata using the same reserved syntax as arguments."""

    direct_values: dict[str, Any] = {}
    for raw_key, value in source.items():
        if not isinstance(raw_key, str):
            raise MCPUpgradeError(f"{source_name} metadata keys must be strings.")
        if raw_key in _METADATA_CONTAINERS:
            if not isinstance(value, Mapping):
                raise MCPUpgradeError(f"{raw_key} metadata must be an object.")
            metadata_sources.append((raw_key, value))
            continue
        prefix = next((candidate for candidate in _METADATA_PREFIXES if raw_key.startswith(candidate)), None)
        if prefix is not None:
            key = raw_key.removeprefix(prefix)
            if not key:
                raise MCPUpgradeError(f"{raw_key} is not a valid Ares metadata key.")
            flattened_metadata.append((key, value))
            continue
        if raw_key.startswith("__ares") or raw_key.startswith("_ares"):
            raise MCPUpgradeError(
                f"{raw_key} is reserved for Ares metadata; use __ares_<key> or __ares."
            )
        direct_values[raw_key] = value
    if direct_values:
        metadata_sources.append((source_name, direct_values))


def _values_equivalent(left: Any, right: Any) -> bool:
    try:
        return _canonical_json_value(left) == _canonical_json_value(right)
    except MCPUpgradeError:
        return left == right


def normalize_timeout_policy(
    value: object | None,
    *,
    default_seconds: float = DEFAULT_MCP_TIMEOUT_SECONDS,
    max_seconds: float = MAX_MCP_TIMEOUT_SECONDS,
    min_seconds: float = MIN_MCP_TIMEOUT_SECONDS,
) -> TimeoutPolicy:
    """Return a positive, finite timeout capped to the caller's hard limit."""

    default = _positive_number(default_seconds, name="default timeout")
    maximum = _positive_number(max_seconds, name="maximum timeout")
    minimum = _positive_number(min_seconds, name="minimum timeout")
    if minimum > maximum:
        raise MCPUpgradeError("Minimum timeout cannot exceed maximum timeout.")
    if value is None or value == "":
        effective = min(max(default, minimum), maximum)
        return TimeoutPolicy(
            timeout_seconds=effective,
            requested_seconds=None,
            used_default=True,
            capped=default > maximum,
        )
    requested = _positive_number(value, name="timeout")
    if requested < minimum:
        raise MCPUpgradeError(f"Timeout must be at least {minimum:g} seconds.")
    return TimeoutPolicy(
        timeout_seconds=min(requested, maximum),
        requested_seconds=requested,
        used_default=False,
        capped=requested > maximum,
    )


def normalize_timeout_seconds(
    value: object | None,
    *,
    default_seconds: float = DEFAULT_MCP_TIMEOUT_SECONDS,
    max_seconds: float = MAX_MCP_TIMEOUT_SECONDS,
    min_seconds: float = MIN_MCP_TIMEOUT_SECONDS,
) -> float:
    """Compatibility-friendly shortcut for callers that only need the value."""

    return normalize_timeout_policy(
        value,
        default_seconds=default_seconds,
        max_seconds=max_seconds,
        min_seconds=min_seconds,
    ).timeout_seconds


def normalize_cache_ttl_seconds(
    value: object | None,
    *,
    default_seconds: float = DEFAULT_MCP_CACHE_TTL_SECONDS,
    max_seconds: float = MAX_MCP_CACHE_TTL_SECONDS,
) -> float:
    """Normalize an optional cache TTL; zero means that caching is disabled."""

    try:
        default = float(default_seconds)
        maximum = float(max_seconds)
    except (TypeError, ValueError) as exc:
        raise MCPUpgradeError("Cache TTL defaults must be numeric.") from exc
    if not math.isfinite(default) or not math.isfinite(maximum) or default < 0 or maximum < 0:
        raise MCPUpgradeError("Cache TTL defaults must be finite non-negative values.")
    if default > maximum:
        raise MCPUpgradeError("Default cache TTL cannot exceed maximum cache TTL.")
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise MCPUpgradeError("Cache TTL must be a number of seconds, not a boolean.")
    try:
        ttl = float(value)
    except (TypeError, ValueError) as exc:
        raise MCPUpgradeError("Cache TTL must be numeric.") from exc
    if not math.isfinite(ttl) or ttl < 0:
        raise MCPUpgradeError("Cache TTL must be a finite non-negative number.")
    return min(ttl, maximum)


def normalize_pagination_policy(value: object | None) -> PaginationPolicy:
    """Normalize an opt-in page or cursor pagination policy.

    ``None`` and falsey string forms retain legacy one-shot behavior.  ``True``
    and ``"auto"`` enable bounded page-number pagination.  Mapping forms
    support either ``mode: page`` or ``mode: cursor`` and never allow more than
    :data:`MAX_PAGINATION_MAX_PAGES` requests per caller invocation.
    """

    if value is None or value is False:
        return PaginationPolicy()
    if isinstance(value, str):
        mode = value.strip().casefold()
        if mode in {"", "off", "false", "none", "disabled"}:
            return PaginationPolicy()
        if mode in {"auto", "on", "true", "page", "pages"}:
            return PaginationPolicy(enabled=True, max_pages=DEFAULT_PAGINATION_MAX_PAGES)
        if mode in {"cursor", "cursors"}:
            return PaginationPolicy(
                enabled=True,
                mode="cursor",
                max_pages=DEFAULT_PAGINATION_MAX_PAGES,
            )
        raise MCPUpgradeError("Unknown pagination mode.")
    if value is True:
        return PaginationPolicy(enabled=True, max_pages=DEFAULT_PAGINATION_MAX_PAGES)
    if not isinstance(value, Mapping):
        raise MCPUpgradeError("Pagination policy must be a boolean, mode, or object.")

    raw_enabled = value.get("enabled", True)
    if not isinstance(raw_enabled, bool):
        raise MCPUpgradeError("Pagination enabled must be a boolean.")
    if not raw_enabled:
        return PaginationPolicy()
    raw_mode = str(value.get("mode") or value.get("type") or "page").strip().casefold()
    if raw_mode in {"pages", "page_number", "page-number"}:
        raw_mode = "page"
    if raw_mode in {"cursors", "token"}:
        raw_mode = "cursor"
    if raw_mode not in {"page", "cursor"}:
        raise MCPUpgradeError("Pagination mode must be page or cursor.")

    max_pages = _bounded_int(
        value.get("max_pages", DEFAULT_PAGINATION_MAX_PAGES),
        name="Pagination max_pages",
        minimum=1,
        maximum=MAX_PAGINATION_MAX_PAGES,
    )
    initial_page = _bounded_int(
        value.get("initial_page", 1),
        name="Pagination initial_page",
        minimum=1,
        maximum=2_147_483_647,
    )
    page_param = _safe_parameter_name(value.get("page_param", "page"), "page_param")
    cursor_param = _safe_parameter_name(value.get("cursor_param", "cursor"), "cursor_param")
    items_path = _safe_projection_path(value.get("items_path", "items"), "items_path")
    next_cursor_path = _safe_projection_path(
        value.get("next_cursor_path", value.get("response_cursor_path", "next_cursor")),
        "next_cursor_path",
    )
    has_more_path = _safe_projection_path(value.get("has_more_path", "has_more"), "has_more_path")
    merge_mode = str(value.get("merge_mode", value.get("merge", "items"))).strip().casefold()
    if merge_mode not in {"items", "pages", "last"}:
        raise MCPUpgradeError("Pagination merge_mode must be items, pages, or last.")
    stop_on_empty = value.get("stop_on_empty", True)
    if not isinstance(stop_on_empty, bool):
        raise MCPUpgradeError("Pagination stop_on_empty must be a boolean.")
    initial_cursor = value.get("initial_cursor")
    if initial_cursor is not None and not isinstance(initial_cursor, (str, int, float)):
        raise MCPUpgradeError("Pagination initial_cursor must be a scalar value.")
    return PaginationPolicy(
        enabled=True,
        mode=raw_mode,  # type: ignore[arg-type]
        max_pages=max_pages,
        page_param=page_param,
        initial_page=initial_page,
        cursor_param=cursor_param,
        initial_cursor=None if initial_cursor is None else str(initial_cursor),
        items_path=items_path,
        next_cursor_path=next_cursor_path,
        has_more_path=has_more_path,
        merge_mode=merge_mode,  # type: ignore[arg-type]
        stop_on_empty=stop_on_empty,
    )


def pagination_page_arguments(
    policy: PaginationPolicy,
    page_number: int,
    *,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return only the per-page arguments to merge into a sanitized MCP call."""

    if not policy.enabled:
        return {}
    if page_number < 1 or page_number > policy.max_pages:
        raise MCPUpgradeError(
            f"Page number must be between 1 and {policy.max_pages} for this policy."
        )
    if policy.mode == "cursor":
        value = cursor if cursor is not None else policy.initial_cursor
        return {} if value in (None, "") else {policy.cursor_param: value}
    return {policy.page_param: policy.initial_page + page_number - 1}


def project_pagination_page(
    response: Any,
    policy: PaginationPolicy,
    page_number: int,
    *,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Extract items and continuation state from one untrusted MCP response."""

    items = _extract_projection_path(response, policy.items_path)
    if items is _MISSING:
        items = response if isinstance(response, list) else []
    if isinstance(items, tuple):
        items = list(items)
    if not isinstance(items, list):
        items = [items] if items is not None else []
    next_cursor = _extract_projection_path(response, policy.next_cursor_path)
    if next_cursor is _MISSING or next_cursor in (None, ""):
        next_cursor = None
    else:
        next_cursor = str(next_cursor)
    explicit_has_more = _extract_projection_path(response, policy.has_more_path)
    if explicit_has_more is _MISSING:
        has_more = bool(next_cursor) if policy.mode == "cursor" else len(items) > 0
    else:
        has_more = bool(explicit_has_more)
    if policy.stop_on_empty and not items:
        has_more = False
    if page_number >= policy.max_pages:
        has_more = False
    next_page = page_number + 1 if has_more and page_number < policy.max_pages else None
    next_arguments = (
        pagination_page_arguments(policy, next_page, cursor=next_cursor)
        if next_page is not None
        else {}
    )
    return {
        "page": page_number,
        "cursor": cursor,
        "items": copy.deepcopy(items),
        "item_count": len(items),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "next_arguments": next_arguments,
        "response": copy.deepcopy(response),
    }


def pagination_page_projection(
    response: Any,
    policy: PaginationPolicy,
    page_number: int,
    *,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Alias with a noun-first name for UI/result projection callers."""

    return project_pagination_page(response, policy, page_number, cursor=cursor)


def merge_paginated_responses(
    pages: Sequence[Mapping[str, Any]],
    policy: PaginationPolicy,
) -> dict[str, Any]:
    """Merge page projections into a stable, non-destructive result shape."""

    projected_pages = [copy.deepcopy(dict(page)) for page in pages]
    if not projected_pages:
        return {
            "items": [],
            "pages": [],
            "page_count": 0,
            "item_count": 0,
            "has_more": False,
            "next_cursor": None,
            "next_arguments": {},
            "merge_mode": policy.merge_mode,
        }
    last_page = projected_pages[-1]
    if policy.merge_mode == "last":
        response = copy.deepcopy(last_page.get("response"))
        return {
            "result": response,
            "pages": projected_pages,
            "page_count": len(projected_pages),
            "item_count": int(last_page.get("item_count") or 0),
            "has_more": bool(last_page.get("has_more")),
            "next_cursor": last_page.get("next_cursor"),
            "next_arguments": copy.deepcopy(last_page.get("next_arguments") or {}),
            "merge_mode": policy.merge_mode,
        }
    all_items = [
        item
        for page in projected_pages
        for item in (page.get("items") if isinstance(page.get("items"), list) else [])
    ]
    result: dict[str, Any] = {
        "items": all_items,
        "page_count": len(projected_pages),
        "item_count": len(all_items),
        "has_more": bool(last_page.get("has_more")),
        "next_cursor": last_page.get("next_cursor"),
        "next_arguments": copy.deepcopy(last_page.get("next_arguments") or {}),
        "merge_mode": policy.merge_mode,
    }
    if policy.merge_mode == "pages":
        result["pages"] = projected_pages
    return result


def prepare_mcp_call(
    tool_name: object,
    arguments: Mapping[str, Any] | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    default_timeout_seconds: float = DEFAULT_MCP_TIMEOUT_SECONDS,
    max_timeout_seconds: float = MAX_MCP_TIMEOUT_SECONDS,
    min_timeout_seconds: float = MIN_MCP_TIMEOUT_SECONDS,
    default_cache_ttl_seconds: float = DEFAULT_MCP_CACHE_TTL_SECONDS,
    max_cache_ttl_seconds: float = MAX_MCP_CACHE_TTL_SECONDS,
) -> PreparedMCPCall:
    """Prepare a dynamic MCP call without leaking Ares control metadata.

    This is the primary integration point for a future execution wrapper.  A
    caller passes only ``prepared.arguments`` to ``session.call_tool`` and uses
    the remaining fields locally.
    """

    parsed = parse_mcp_tool_name(tool_name)
    server_arguments, normalized_metadata = split_mcp_arguments(arguments, metadata=metadata)
    timeout = normalize_timeout_policy(
        normalized_metadata.get("timeout_seconds"),
        default_seconds=default_timeout_seconds,
        max_seconds=max_timeout_seconds,
        min_seconds=min_timeout_seconds,
    )
    cache_ttl = normalize_cache_ttl_seconds(
        normalized_metadata.get("cache_ttl_seconds"),
        default_seconds=default_cache_ttl_seconds,
        max_seconds=max_cache_ttl_seconds,
    )
    pagination = normalize_pagination_policy(normalized_metadata.get("pagination"))
    return PreparedMCPCall(
        tool=parsed,
        arguments=server_arguments,
        metadata=normalized_metadata,
        timeout=timeout,
        cache_ttl_seconds=cache_ttl,
        pagination=pagination,
    )


def make_mcp_cache_key(tool_name: object, arguments: Mapping[str, Any] | None) -> str:
    """Create an opaque, stable cache key from sanitized tool arguments.

    Metadata is intentionally excluded, so secrets/control values never appear
    in the key and incidental policy changes cannot create a data-leaking key.
    Type tags prevent collisions such as ``1`` versus ``"1"``.
    """

    parsed = parse_mcp_tool_name(tool_name)
    sanitized_arguments, _metadata = split_mcp_arguments(arguments)
    payload = {
        "version": 1,
        "tool": parsed.canonical_name,
        "arguments": _canonical_json_value(sanitized_arguments),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"mcp:v1:{parsed.server_name}:{digest}"


class MCPResponseCache:
    """Small in-memory TTL cache with safe keys and defensive copies."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise MCPUpgradeError("max_entries must be a positive integer.")
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}

    def lookup(self, key: str, *, now: float | None = None) -> CacheLookup:
        """Return a defensive copy plus hit/age metadata for an opaque key."""

        current = self._now(now)
        entry = self._entries.get(key)
        if entry is None:
            return CacheLookup(key=key, hit=False)
        if current >= entry.expires_at:
            self._entries.pop(key, None)
            return CacheLookup(key=key, hit=False)
        return CacheLookup(
            key=key,
            hit=True,
            value=copy.deepcopy(entry.value),
            age_seconds=max(0.0, current - entry.created_at),
            expires_in_seconds=max(0.0, entry.expires_at - current),
        )

    def get(self, key: str, default: Any = None, *, now: float | None = None) -> Any:
        """Return a cached value or ``default``; use ``lookup`` for cached None."""

        lookup = self.lookup(key, now=now)
        return lookup.value if lookup.hit else default

    def put(
        self,
        key: str,
        response: Any,
        *,
        ttl_seconds: object,
        now: float | None = None,
    ) -> bool:
        """Store a response when TTL is positive; returns whether it was cached."""

        ttl = normalize_cache_ttl_seconds(ttl_seconds)
        if ttl <= 0:
            self._entries.pop(key, None)
            return False
        current = self._now(now)
        self._purge_expired(current)
        if key not in self._entries and len(self._entries) >= self.max_entries:
            oldest_key = min(self._entries, key=lambda item: self._entries[item].created_at)
            self._entries.pop(oldest_key, None)
        self._entries[key] = _CacheEntry(
            value=copy.deepcopy(response),
            created_at=current,
            expires_at=current + ttl,
        )
        return True

    def lookup_call(
        self,
        tool_name: object,
        arguments: Mapping[str, Any] | None,
        *,
        now: float | None = None,
    ) -> CacheLookup:
        return self.lookup(make_mcp_cache_key(tool_name, arguments), now=now)

    def put_call(
        self,
        tool_name: object,
        arguments: Mapping[str, Any] | None,
        response: Any,
        *,
        ttl_seconds: object,
        now: float | None = None,
    ) -> bool:
        return self.put(
            make_mcp_cache_key(tool_name, arguments),
            response,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def invalidate(self, key: str) -> bool:
        """Remove one entry and report whether it existed."""

        return self._entries.pop(key, None) is not None

    def clear(self) -> int:
        """Remove every cached result and return the number removed."""

        count = len(self._entries)
        self._entries.clear()
        return count

    def stats(self, *, now: float | None = None) -> dict[str, int]:
        """Return a non-sensitive cache health projection."""

        self._purge_expired(self._now(now))
        return {"entries": len(self._entries), "max_entries": self.max_entries}

    def _now(self, supplied: float | None) -> float:
        current = self._clock() if supplied is None else supplied
        try:
            current = float(current)
        except (TypeError, ValueError) as exc:
            raise MCPUpgradeError("Cache clock must return a numeric timestamp.") from exc
        if not math.isfinite(current):
            raise MCPUpgradeError("Cache clock must return a finite timestamp.")
        return current

    def _purge_expired(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if now >= entry.expires_at:
                self._entries.pop(key, None)


def redact_mcp_error(value: object, *, limit: int = 1_000) -> str:
    """Return a bounded diagnostic with common credential forms redacted."""

    text = str(value or "")
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(r"\1[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    text = _URL_PATTERN.sub(_redact_url, text)
    bounded_limit = max(64, int(limit))
    return text[:bounded_limit]


def classify_mcp_error(error: object) -> dict[str, Any]:
    """Classify an MCP error without returning its original text."""

    text = str(error or "")
    lowered = text.casefold()
    category: str
    retryable: bool
    if any(token in lowered for token in ("timeout", "timed out", "deadline exceeded")):
        category, retryable = "timeout", True
    elif any(token in lowered for token in ("cancelled", "canceled", "cancellederror")):
        category, retryable = "cancelled", False
    elif any(token in lowered for token in ("401", "unauthenticated", "authentication", "token expired")):
        category, retryable = "authentication", False
    elif any(token in lowered for token in ("403", "forbidden", "permission denied", "not authorized")):
        category, retryable = "authorization", False
    elif any(token in lowered for token in ("not configured", "not connected", "no session")):
        category, retryable = "not_ready", True
    elif any(token in lowered for token in ("404", "not found", "unknown tool", "method not found")):
        category, retryable = "not_found", False
    elif any(token in lowered for token in ("429", "rate limit", "too many requests", "throttl")):
        category, retryable = "rate_limited", True
    elif any(token in lowered for token in ("invalid", "validation", "schema", "bad request", "argument")):
        category, retryable = "validation", False
    elif any(
        token in lowered
        for token in ("connection", "network", "dns", "socket", "reset by peer", "unreachable")
    ):
        category, retryable = "network", True
    elif any(token in lowered for token in ("500", "502", "503", "504", "internal server")):
        category, retryable = "server", True
    else:
        category, retryable = "unknown", False
    return {"category": category, "retryable": retryable}


def project_mcp_error(
    error: object,
    *,
    server_name: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """Produce a UI/API-safe error projection with redacted diagnostics."""

    classification = classify_mcp_error(error)
    category = str(classification["category"])
    safe_server = _safe_display_identifier(server_name)
    safe_tool = _safe_display_identifier(tool_name)
    subject = "MCP call"
    if safe_server and safe_tool:
        subject = f"MCP tool {safe_tool} on {safe_server}"
    elif safe_server:
        subject = f"MCP server {safe_server}"
    messages = {
        "timeout": f"{subject} timed out.",
        "cancelled": f"{subject} was cancelled.",
        "authentication": f"{subject} needs authentication.",
        "authorization": f"{subject} is not authorized for this action.",
        "not_ready": f"{subject} is not ready.",
        "not_found": f"{subject} was not found.",
        "rate_limited": f"{subject} is temporarily rate limited.",
        "validation": f"{subject} rejected the supplied arguments.",
        "network": f"{subject} could not be reached.",
        "server": f"{subject} reported a server error.",
        "unknown": f"{subject} failed.",
    }
    return {
        "ok": False,
        "status": "failed",
        "code": f"mcp_{category}",
        "category": category,
        "retryable": bool(classification["retryable"]),
        "message": messages[category],
        "diagnostic": redact_mcp_error(error),
        "server": safe_server,
        "tool": safe_tool,
    }


def project_mcp_readiness(
    server_name: object,
    *,
    configured: bool,
    connected: bool,
    tool_count: int = 0,
    error: object | None = None,
    transport: object | None = None,
) -> dict[str, Any]:
    """Project one server's readiness without exposing configuration secrets."""

    name = _safe_display_identifier(server_name) or "unknown"
    tools = max(0, int(tool_count or 0))
    error_projection = project_mcp_error(error, server_name=name) if error else None
    if not configured:
        status = "unconfigured"
        ready = False
        next_action = "Configure the MCP server before calling its tools."
    elif connected and error_projection is None:
        status = "ready"
        ready = True
        next_action = None
    elif connected:
        status = "degraded"
        ready = False
        next_action = "Reconnect the MCP server and retry the failed operation."
    else:
        status = "offline"
        ready = False
        next_action = "Reconnect the MCP server before calling its tools."
    return {
        "name": name,
        "status": status,
        "ready": ready,
        "configured": bool(configured),
        "connected": bool(connected),
        "tool_count": tools,
        "transport": _safe_display_identifier(transport),
        "error": error_projection,
        "next_action": next_action,
    }


def project_mcp_health(
    manager: Any | None = None,
    *,
    servers: Mapping[str, Any] | None = None,
    sessions: Mapping[str, Any] | None = None,
    schema_cache: Mapping[str, Any] | None = None,
    server_errors: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an aggregate health view from an MCP manager or its maps.

    The function only inspects public-ish manager attributes and works with
    simple fake mappings, which keeps it suitable for the CLI and tests.
    """

    server_map = _mapping_or_empty(servers if servers is not None else getattr(manager, "servers", {}))
    session_map = _mapping_or_empty(sessions if sessions is not None else getattr(manager, "sessions", {}))
    schema_map = _mapping_or_empty(
        schema_cache if schema_cache is not None else getattr(manager, "schema_cache", {})
    )
    error_map = _mapping_or_empty(
        server_errors if server_errors is not None else getattr(manager, "server_errors", {})
    )
    projections: list[dict[str, Any]] = []
    for name, config in sorted(server_map.items(), key=lambda item: str(item[0]).casefold()):
        name_text = str(name)
        schema = schema_map.get(name_text, [])
        try:
            tool_count = len(schema)  # type: ignore[arg-type]
        except TypeError:
            tool_count = 0
        transport = _lookup_config_value(config, "transport")
        projections.append(
            project_mcp_readiness(
                name_text,
                configured=True,
                connected=name_text in session_map and session_map.get(name_text) is not None,
                tool_count=tool_count,
                error=error_map.get(name_text),
                transport=transport,
            )
        )
    configured = len(projections)
    ready = sum(1 for item in projections if item["ready"])
    degraded = sum(1 for item in projections if item["status"] == "degraded")
    offline = sum(1 for item in projections if item["status"] == "offline")
    if configured == 0:
        status = "unconfigured"
    elif ready == configured:
        status = "ready"
    elif ready:
        status = "degraded"
    else:
        status = "offline"
    return {
        "status": status,
        "ready": status == "ready",
        "servers": projections,
        "metrics": {
            "configured": configured,
            "ready": ready,
            "degraded": degraded,
            "offline": offline,
            "tools": sum(int(item["tool_count"]) for item in projections),
        },
    }


def _positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise MCPUpgradeError(f"{name.capitalize()} must be numeric, not a boolean.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MCPUpgradeError(f"{name.capitalize()} must be numeric.") from exc
    if not math.isfinite(number) or number <= 0:
        raise MCPUpgradeError(f"{name.capitalize()} must be a finite positive number.")
    return number


def _bounded_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MCPUpgradeError(f"{name} must be an integer.")
    if isinstance(value, float) and (not value.is_integer() or not math.isfinite(value)):
        raise MCPUpgradeError(f"{name} must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MCPUpgradeError(f"{name} must be an integer.") from exc
    if isinstance(value, str):
        stripped = value.strip()
        if stripped not in {str(number), f"{number}.0"}:
            raise MCPUpgradeError(f"{name} must be an integer.")
    if number < minimum:
        raise MCPUpgradeError(f"{name} must be at least {minimum}.")
    return min(number, maximum)


def _safe_parameter_name(value: object, name: str) -> str:
    if not isinstance(value, str) or not _PARAMETER_PATTERN.fullmatch(value):
        raise MCPUpgradeError(f"Pagination {name} is not a valid parameter name.")
    return value


def _safe_projection_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPUpgradeError(f"Pagination {name} must be a non-empty dotted path.")
    path = value.strip()
    if not all(_PATH_SEGMENT_PATTERN.fullmatch(part) for part in path.split(".")):
        raise MCPUpgradeError(f"Pagination {name} contains an invalid path segment.")
    return path


_MISSING = object()


def _extract_projection_path(value: Any, path: str) -> Any:
    current = value
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if not segment.isdigit() or int(segment) >= len(current):
                return _MISSING
            current = current[int(segment)]
        else:
            return _MISSING
    return current


def _canonical_json_value(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Create a type-tagged JSON-compatible value for cache equality."""

    seen = _seen if _seen is not None else set()
    if value is None:
        return ["none"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MCPUpgradeError("MCP cache arguments cannot contain non-finite floats.")
        return ["float", repr(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", hashlib.sha256(value).hexdigest()]
    if isinstance(value, (datetime, date)):
        return ["datetime", value.isoformat()]
    object_id = id(value)
    if object_id in seen:
        raise MCPUpgradeError("MCP cache arguments cannot contain cyclic values.")
    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            pairs = []
            for key, item in value.items():
                if not isinstance(key, str):
                    raise MCPUpgradeError("MCP cache argument keys must be strings.")
                pairs.append([key, _canonical_json_value(item, _seen=seen)])
            return ["object", sorted(pairs, key=lambda item: item[0])]
        finally:
            seen.discard(object_id)
    if isinstance(value, (list, tuple)):
        seen.add(object_id)
        try:
            return ["array", [_canonical_json_value(item, _seen=seen) for item in value]]
        finally:
            seen.discard(object_id)
    if isinstance(value, (set, frozenset)):
        seen.add(object_id)
        try:
            items = [_canonical_json_value(item, _seen=seen) for item in value]
            return [
                "set",
                sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)),
            ]
        finally:
            seen.discard(object_id)
    raise MCPUpgradeError(
        f"MCP cache arguments do not support values of type {type(value).__name__}."
    )


def _redact_url(match: re.Match[str]) -> str:
    text = match.group(0)
    try:
        parsed = urlsplit(text)
        query = urlencode(
            [
                (key, "[redacted]" if key.casefold() in _SENSITIVE_QUERY_KEYS else value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        hostname = parsed.hostname or ""
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        if "@" in parsed.netloc:
            hostname = f"[redacted]@{hostname}"
        return urlunsplit((parsed.scheme, hostname or parsed.netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return "[redacted-url]"


def _safe_display_identifier(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return redact_mcp_error(text, limit=160)


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _lookup_config_value(config: Any, key: str) -> Any:
    if isinstance(config, Mapping):
        return config.get(key)
    return getattr(config, key, None)
