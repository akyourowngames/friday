from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)


_TOOL_NAME = "composio"
_VERSION = "1.0.0"
_ACTIONS = (
    "status",
    "catalog",
    "toolkits",
    "tools",
    "create_session",
    "session_tools",
    "session_toolkits",
    "link",
    "search",
    "schema",
    "execute",
)
_NETWORK_ACTIONS = {"catalog", "toolkits", "tools", "create_session", "session_tools", "session_toolkits", "link", "search", "schema", "execute"}
_CONFIRM_RISKS = {"write", "destructive", "auth"}


@dataclass
class ComposioToolRule:
    slug: str
    toolkit: str
    risk: str = "read"
    enabled: bool = True
    note: str = ""


@dataclass
class ComposioPolicy:
    path: Path
    enabled: bool = True
    base_url: str = "https://backend.composio.dev/api/v3.1"
    api_key_env: str = "COMPOSIO_API_KEY"
    user_id_env: str = "KING_COMPOSIO_USER_ID"
    session_id_env: str = "KING_COMPOSIO_SESSION_ID"
    default_timeout_ms: int = 20000
    max_response_chars: int = 12000
    semantic_slug_resolution: bool = True
    semantic_slug_min_score: float = 0.35
    semantic_slug_min_margin: float = 0.03
    create_sessions_with_search: bool = True
    create_sessions_with_manage_connections: bool = True
    create_sessions_with_workbench: bool = False
    enabled_toolkits: set[str] = field(default_factory=set)
    tools: dict[str, ComposioToolRule] = field(default_factory=dict)
    argument_defaults: dict[str, dict[str, str]] = field(default_factory=dict)
    argument_default_placeholders: set[str] = field(default_factory=set)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _github_owner_repo_from_remote(remote_url: str) -> dict[str, str]:
    clean = str(remote_url or "").strip()
    if not clean:
        return {}

    path = ""
    if clean.startswith("git@"):
        separator = clean.find(":")
        if separator >= 0:
            path = clean[separator + 1 :]
    else:
        marker = "github.com/"
        marker_index = clean.lower().find(marker)
        if marker_index >= 0:
            path = clean[marker_index + len(marker) :]

    if not path:
        return {}

    path = path.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return {}

    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not parts[0] or not repo:
        return {}

    return {"owner": parts[0], "repo": repo, "remote_url": clean}


def _local_repository_hint() -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_repo_root()), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    return _github_owner_repo_from_remote(completed.stdout)


def _split_item(line: str) -> tuple[str, str]:
    body = line.strip()[2:].strip()
    key, separator, value = body.partition(":")
    if not separator:
        return "", ""
    return key.strip(), value.strip()


def _parse_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return default


def _parse_int(value: str, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def _parse_float(value: str, default: float) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def _parse_tool_line(value: str) -> ComposioToolRule | None:
    pieces = [piece.strip() for piece in value.split("|") if piece.strip()]
    if not pieces:
        return None
    slug = pieces[0].strip().upper()
    details = {"toolkit": "", "risk": "read", "enabled": "true", "note": ""}
    for piece in pieces[1:]:
        key, separator, item_value = piece.partition(":")
        if separator:
            clean_key = key.strip().lower()
            if clean_key in details:
                details[clean_key] = item_value.strip()
    toolkit = details["toolkit"].strip().lower()
    if not slug or not toolkit:
        return None
    return ComposioToolRule(
        slug=slug,
        toolkit=toolkit,
        risk=details["risk"].strip().lower() or "read",
        enabled=_parse_bool(details["enabled"], True),
        note=details["note"].strip(),
    )


def _parse_argument_defaults_line(value: str) -> tuple[str, dict[str, str]]:
    pieces = [piece.strip() for piece in value.split("|") if piece.strip()]
    if not pieces:
        return "", {}
    slug = pieces[0].strip().upper()
    defaults: dict[str, str] = {}
    for piece in pieces[1:]:
        key, separator, item_value = piece.partition(":")
        if separator:
            clean_key = key.strip()
            clean_value = item_value.strip()
            if clean_key and clean_value:
                defaults[clean_key] = clean_value
    return slug, defaults


def _parse_placeholder_line(value: str) -> set[str]:
    body = str(value or "").strip()
    if not body:
        return set()
    if body.lower().startswith("values:"):
        body = body.split(":", 1)[1].strip()
    return {piece.strip().casefold() for piece in body.split(",") if piece.strip()}


def _load_policy(path: str | Path | None = None) -> ComposioPolicy:
    root = _repo_root()
    requested = Path(path or settings.composio_policy_file)
    policy_path = requested if requested.is_absolute() else root / requested
    policy_path = policy_path.resolve()
    if not policy_path.exists():
        raise FileNotFoundError(str(policy_path))

    policy = ComposioPolicy(path=policy_path)
    section = ""
    for raw_line in policy_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            section = line[3:].strip().casefold()
            continue
        if not line.startswith("- "):
            continue

        if section == "runtime":
            key, value = _split_item(line)
            if key == "enabled":
                policy.enabled = _parse_bool(value, policy.enabled)
            elif key == "base_url":
                policy.base_url = value.rstrip("/") or policy.base_url
            elif key == "api_key_env":
                policy.api_key_env = value or policy.api_key_env
            elif key == "user_id_env":
                policy.user_id_env = value or policy.user_id_env
            elif key == "session_id_env":
                policy.session_id_env = value or policy.session_id_env
            elif key == "default_timeout_ms":
                policy.default_timeout_ms = _parse_int(value, policy.default_timeout_ms)
            elif key == "max_response_chars":
                policy.max_response_chars = _parse_int(value, policy.max_response_chars)
            elif key == "semantic_slug_resolution":
                policy.semantic_slug_resolution = _parse_bool(value, policy.semantic_slug_resolution)
            elif key == "semantic_slug_min_score":
                policy.semantic_slug_min_score = _parse_float(value, policy.semantic_slug_min_score)
            elif key == "semantic_slug_min_margin":
                policy.semantic_slug_min_margin = _parse_float(value, policy.semantic_slug_min_margin)
            elif key == "create_sessions_with_search":
                policy.create_sessions_with_search = _parse_bool(value, policy.create_sessions_with_search)
            elif key == "create_sessions_with_manage_connections":
                policy.create_sessions_with_manage_connections = _parse_bool(value, policy.create_sessions_with_manage_connections)
            elif key == "create_sessions_with_workbench":
                policy.create_sessions_with_workbench = _parse_bool(value, policy.create_sessions_with_workbench)
            continue

        if section == "enabled toolkits":
            toolkit = line[2:].strip().lower()
            if toolkit:
                policy.enabled_toolkits.add(toolkit)
            continue

        if section == "enabled tools":
            rule = _parse_tool_line(line[2:].strip())
            if rule is not None and rule.enabled:
                policy.tools[rule.slug] = rule
                policy.enabled_toolkits.add(rule.toolkit)
            continue

        if section == "argument defaults":
            slug, defaults = _parse_argument_defaults_line(line[2:].strip())
            if slug and defaults:
                policy.argument_defaults[slug] = defaults
            continue

        if section == "argument default placeholders":
            policy.argument_default_placeholders.update(_parse_placeholder_line(line[2:].strip()))

    return policy


def _api_key(policy: ComposioPolicy) -> str:
    configured = str(settings.composio_api_key or "").strip()
    if configured:
        return configured
    return str(os.getenv(policy.api_key_env, "") or "").strip()


def _user_id(policy: ComposioPolicy, user_id: str) -> str:
    requested = str(user_id or "").strip()
    if requested:
        return requested
    from_env = str(os.getenv(policy.user_id_env, "") or "").strip()
    if from_env:
        return from_env
    configured = str(settings.composio_user_id or "").strip()
    return configured or "king-local-user"


def _session_id(policy: ComposioPolicy, session_id: str) -> str:
    requested = str(session_id or "").strip()
    if requested:
        return requested
    from_env = str(os.getenv(policy.session_id_env, "") or "").strip()
    if from_env:
        return from_env
    return str(settings.composio_session_id or "").strip()


def _normalize_slug(value: str) -> str:
    return str(value or "").strip().upper()


def _normalize_toolkit(value: str) -> str:
    return str(value or "").strip().lower()


def _safe_json_loads(value) -> tuple[dict[str, Any], dict | None]:
    if value in (None, ""):
        return {}, None
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}, error_payload(
                "INVALID_ARGUMENTS_JSON",
                "Composio arguments must be a JSON object when passed as text.",
                "arguments",
                value[:200],
                "JSON object",
                False,
                "Pass arguments as an object or valid JSON object text.",
            )
        if isinstance(parsed, dict):
            return parsed, None
    return {}, error_payload(
        "INVALID_ARGUMENTS",
        "Composio arguments must be an object.",
        "arguments",
        type(value).__name__,
        "object",
        False,
        "Pass a JSON object with the selected Composio tool inputs.",
    )


def _trace(
    started_at: str,
    started: float,
    inputs_received: int,
    schema_valid: bool,
    path: str,
    status: str,
    output_fields: int,
    external_count: int = 0,
    error_code: str | None = None,
) -> dict:
    return make_trace(
        _TOOL_NAME,
        _VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        path,
        status,
        output_fields,
        {"count": external_count, "systems": ["composio"] if external_count else []},
        error_code,
    )


def _finish_error(
    error: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
    path: str,
    external_count: int = 0,
):
    trace = _trace(started_at, started, inputs_received, False, path, "FAILED", 1, external_count, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(_TOOL_NAME, _VERSION, error, started, trace)
    return legacy


def _finish_success(
    result: dict[str, Any],
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
    external_count: int = 0,
):
    trace = _trace(started_at, started, inputs_received, True, str(result.get("action") or "composio"), "SUCCESS", len(result), external_count)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success(_TOOL_NAME, _VERSION, result, started, trace)
    return legacy


def _safe_error_detail(payload: Any, response: httpx.Response) -> Any:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = {}
            for key in ("message", "slug", "status", "suggested_fix", "errors"):
                if key in error:
                    detail[key] = error[key]
            return detail
        if "message" in payload:
            return payload["message"]
    return str(getattr(response, "text", "") or "")[:500]


def _request(
    policy: ComposioPolicy,
    method: str,
    path: str,
    api_key: str,
    timeout_ms: int,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> tuple[Any, dict | None, int]:
    url = policy.base_url.rstrip("/") + path
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    try:
        response = httpx.request(
            method,
            url,
            headers=headers,
            params=params or None,
            json=json_payload or None,
            timeout=timeout_ms / 1000,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        return {}, error_payload(
            "COMPOSIO_UNAVAILABLE",
            "Composio could not be reached.",
            "service",
            policy.base_url,
            "reachable Composio API",
            True,
            "Check network access, the Composio base URL, and retry.",
        ) | {"detail": exc.__class__.__name__, "method": method, "endpoint": path}, 1

    try:
        payload = response.json()
    except ValueError:
        payload = {}
        json_error = error_payload(
            "INVALID_UPSTREAM_JSON",
            "Composio returned a non-JSON response.",
            "service",
            policy.base_url,
            "Composio JSON response",
            True,
            "Retry later or inspect the configured Composio endpoint.",
        )
        json_error["status_code"] = response.status_code
        json_error["method"] = method
        json_error["endpoint"] = path
        return payload, json_error, 1

    if response.status_code in (401, 403):
        error = error_payload(
            "COMPOSIO_AUTH_FAILED",
            "Composio rejected the configured API key or project authorization.",
            "api_key_env",
            policy.api_key_env,
            "valid Composio project API key",
            False,
            "Set COMPOSIO_API_KEY in .env or the process environment.",
        )
        error["status_code"] = response.status_code
        error["upstream_detail"] = _safe_error_detail(payload, response)
        error["method"] = method
        error["endpoint"] = path
        return payload, error, 1
    if response.status_code >= 400:
        error = error_payload(
            "COMPOSIO_UPSTREAM_ERROR",
            "Composio returned an HTTP error.",
            "service",
            policy.base_url,
            "successful Composio response",
            response.status_code >= 500,
            "Inspect the upstream detail and retry only if the operation is safe.",
        )
        error["status_code"] = response.status_code
        error["upstream_detail"] = _safe_error_detail(payload, response)
        error["method"] = method
        error["endpoint"] = path
        return payload, error, 1
    return payload, None, 1


def _session_tools(policy: ComposioPolicy) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for rule in policy.tools.values():
        result.setdefault(rule.toolkit, {"enable": []})["enable"].append(rule.slug)
    for item in result.values():
        item["enable"] = sorted(item["enable"])
    return result


def _create_session_body(policy: ComposioPolicy, user_id: str) -> dict[str, Any]:
    tool_slugs = sorted(policy.tools)
    body: dict[str, Any] = {
        "user_id": user_id,
        "toolkits": {"enable": sorted(policy.enabled_toolkits)},
        "tools": _session_tools(policy),
        "manage_connections": {
            "enable": policy.create_sessions_with_manage_connections,
            "enable_wait_for_connections": False,
            "enable_connection_removal": False,
        },
        "search": {"enable": policy.create_sessions_with_search},
        "execute": {"enable_multi_execute": False},
        "workbench": {"enable": policy.create_sessions_with_workbench},
    }
    if tool_slugs:
        body["preload"] = {"tools": tool_slugs}
    return body


def _create_session(policy: ComposioPolicy, api_key: str, user_id: str, timeout_ms: int) -> tuple[dict[str, Any], dict | None, int]:
    body = _create_session_body(policy, user_id)
    payload, error, external_count = _request(policy, "POST", "/tool_router/session", api_key, timeout_ms, json_payload=body)
    if error is not None:
        error["session_body"] = {
            "user_id": user_id,
            "toolkits": body.get("toolkits"),
            "tools": body.get("tools"),
            "preload": body.get("preload"),
        }
        return {}, error, external_count
    if not isinstance(payload, dict) or not payload.get("session_id"):
        error = error_payload(
            "SESSION_NOT_RETURNED",
            "Composio did not return a tool router session id.",
            "response",
            type(payload).__name__,
            "session_id field",
            True,
            "Retry session creation or inspect the Composio project configuration.",
        )
        return {}, error, external_count
    return payload, None, external_count


def _ensure_session(policy: ComposioPolicy, api_key: str, user_id: str, session_id: str, timeout_ms: int) -> tuple[str, dict[str, Any], dict | None, int]:
    existing = _session_id(policy, session_id)
    if existing:
        return existing, {}, None, 0
    payload, error, external_count = _create_session(policy, api_key, user_id, timeout_ms)
    if error is not None:
        return "", {}, error, external_count
    return str(payload.get("session_id") or ""), payload, None, external_count


def _truncate_payload(payload: Any, limit: int) -> Any:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(payload)
    if len(text) <= limit:
        return payload
    return {
        "truncated": True,
        "max_chars": limit,
        "preview": text[:limit],
    }


def _schema_input_parameters(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"properties": {}, "required": []}
    candidates = (
        payload.get("input_parameters"),
        payload.get("inputSchema"),
        payload.get("input_schema"),
        payload.get("parameters"),
        payload.get("schema"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("properties"), dict):
            return candidate
    return {"properties": {}, "required": []}


def _compact_catalog_items(payload: Any, max_items: int = 20) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []
    compact = []
    for item in raw_items[:max(1, max_items)]:
        if not isinstance(item, dict):
            continue
        toolkit = item.get("toolkit") if isinstance(item.get("toolkit"), dict) else {}
        input_schema = _schema_input_parameters(item)
        compact.append(
            {
                "slug": item.get("slug") or item.get("name") or toolkit.get("slug") or "",
                "name": item.get("name") or item.get("display_name") or "",
                "description": item.get("human_description") or item.get("description") or "",
                "toolkit": toolkit.get("slug") or item.get("toolkit_slug") or item.get("slug") or "",
                "no_auth": bool(item.get("no_auth", False)),
                "version": item.get("version") or "",
                "required_arguments": list(input_schema.get("required") or []),
                "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
            }
        )
    return compact


def _resolve_default_value(value: str, local_repository: dict[str, str]) -> str:
    if value == "local.owner":
        return str(local_repository.get("owner") or "")
    if value == "local.repo":
        return str(local_repository.get("repo") or "")
    if value == "local.remote_url":
        return str(local_repository.get("remote_url") or "")
    return str(value or "")


def _argument_defaults(policy: ComposioPolicy, tool_slug: str) -> dict[str, str]:
    defaults = policy.argument_defaults.get(tool_slug, {})
    if not defaults:
        return {}
    local_repository = _local_repository_hint()
    resolved: dict[str, str] = {}
    for key, value in defaults.items():
        clean_value = _resolve_default_value(value, local_repository).strip()
        if clean_value:
            resolved[key] = clean_value
    return resolved


def _is_placeholder_argument(value: Any, placeholders: set[str]) -> bool:
    if not placeholders:
        return False
    return str(value or "").strip().casefold() in placeholders


def _apply_argument_defaults(arguments: dict[str, Any], defaults: dict[str, str], placeholders: set[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if not defaults:
        return arguments, {}
    applied: dict[str, Any] = {}
    merged = dict(arguments)
    placeholder_values = placeholders or set()
    for key, value in defaults.items():
        existing = merged.get(key)
        if existing is None or str(existing).strip() == "" or _is_placeholder_argument(existing, placeholder_values):
            merged[key] = value
            applied[key] = value
    return merged, applied


def _semantic_resolution_query(requested_slug: str, query: str = "") -> str:
    pieces = []
    raw_slug = str(requested_slug or "").strip()
    if raw_slug:
        pieces.append(raw_slug)
        expanded = raw_slug.replace("_", " ").replace("-", " ")
        if expanded != raw_slug:
            pieces.append(expanded)
    clean_query = str(query or "").strip()
    if clean_query:
        pieces.append(clean_query)
    return " ".join(piece for piece in pieces if piece).strip()


def _semantic_tool_text(rule: ComposioToolRule) -> str:
    return " ".join(
        piece
        for piece in (
            rule.slug,
            rule.toolkit,
            rule.note,
        )
        if str(piece or "").strip()
    )


def _embed_texts_local(texts: list[str]):
    try:
        from agent.embedder import _local_embed

        return _local_embed(texts)
    except Exception:
        return None


def _resolve_tool_rule(policy: ComposioPolicy, requested_slug: str, query: str = "") -> tuple[str, ComposioToolRule | None, dict[str, Any]]:
    clean_slug = _normalize_slug(requested_slug)
    exact = policy.tools.get(clean_slug)
    if exact is not None:
        return clean_slug, exact, {"resolved": False, "method": "exact", "requested": clean_slug, "selected": clean_slug}

    resolution: dict[str, Any] = {
        "resolved": False,
        "method": "semantic_policy",
        "requested": clean_slug,
        "selected": "",
        "score": 0.0,
        "margin": 0.0,
        "candidates": [],
    }
    if not policy.semantic_slug_resolution or not policy.tools:
        return clean_slug, None, resolution

    resolution_query = _semantic_resolution_query(clean_slug, query)
    if not resolution_query:
        return clean_slug, None, resolution

    ordered_rules = sorted(policy.tools.values(), key=lambda item: item.slug)
    texts = [resolution_query] + [_semantic_tool_text(rule) for rule in ordered_rules]
    embeddings = _embed_texts_local(texts)
    if embeddings is None:
        resolution["method"] = "semantic_policy_unavailable"
        return clean_slug, None, resolution

    try:
        import numpy as np

        similarities = np.dot(embeddings[1:], embeddings[0])
    except Exception:
        resolution["method"] = "semantic_policy_failed"
        return clean_slug, None, resolution

    ranked = sorted(
        (
            {
                "slug": ordered_rules[index].slug,
                "score": float(similarities[index]),
            }
            for index in range(len(ordered_rules))
        ),
        key=lambda item: item["score"],
        reverse=True,
    )
    resolution["candidates"] = ranked[:3]
    if not ranked:
        return clean_slug, None, resolution

    best = ranked[0]
    second_score = float(ranked[1]["score"]) if len(ranked) > 1 else -1.0
    best_score = float(best["score"])
    margin = best_score - second_score
    resolution["score"] = best_score
    resolution["margin"] = margin
    resolution["selected"] = str(best["slug"])

    if best_score >= policy.semantic_slug_min_score and margin >= policy.semantic_slug_min_margin:
        resolved_slug = str(best["slug"])
        resolution["resolved"] = True
        return resolved_slug, policy.tools.get(resolved_slug), resolution

    return clean_slug, None, resolution


def _status_result(policy: ComposioPolicy) -> dict[str, Any]:
    local_repository = _local_repository_hint()
    return {
        "action": "status",
        "enabled": policy.enabled,
        "policy_path": str(policy.path),
        "base_url": policy.base_url,
        "api_key_env": policy.api_key_env,
        "api_key_present": bool(_api_key(policy)),
        "user_id": _user_id(policy, ""),
        "session_id_present": bool(_session_id(policy, "")),
        "semantic_slug_resolution": policy.semantic_slug_resolution,
        "semantic_slug_min_score": policy.semantic_slug_min_score,
        "semantic_slug_min_margin": policy.semantic_slug_min_margin,
        "enabled_toolkits": sorted(policy.enabled_toolkits),
        "enabled_tools": [
            {
                "slug": rule.slug,
                "toolkit": rule.toolkit,
                "risk": rule.risk,
                "note": rule.note,
            }
            for rule in sorted(policy.tools.values(), key=lambda item: item.slug)
        ],
        "network_actions": sorted(_NETWORK_ACTIONS),
        "local_repository": local_repository,
        "argument_defaults": {
            slug: {
                key: _resolve_default_value(value, local_repository)
                for key, value in defaults.items()
                if _resolve_default_value(value, local_repository).strip()
            }
            for slug, defaults in sorted(policy.argument_defaults.items())
        },
        "argument_default_placeholders": sorted(policy.argument_default_placeholders),
    }


def _legacy_summary(result: dict[str, Any]) -> str:
    action = result.get("action")
    if action == "status":
        return (
            "Composio gateway is installed.\n"
            "Enabled: " + str(result.get("enabled")) + "\n"
            "API key present: " + str(result.get("api_key_present")) + "\n"
            "Enabled toolkits: " + (", ".join(result.get("enabled_toolkits", [])) or "none") + "\n"
            "Enabled tools: " + (", ".join(item["slug"] for item in result.get("enabled_tools", [])) or "none")
        )
    if action == "create_session":
        return "Composio session created: " + str(result.get("session_id")) + "\nMCP URL: " + str(result.get("mcp_url") or "")
    if action == "link":
        return "Open this Composio auth link to connect " + str(result.get("toolkit")) + ": " + str(result.get("redirect_url") or "")
    if action == "execute":
        return "Composio executed " + str(result.get("tool_slug")) + ". Result: " + json.dumps(result.get("data"), ensure_ascii=False)[:3000]
    if action == "schema":
        return "Composio schema for " + str(result.get("tool_slug")) + ": " + json.dumps(result.get("data"), ensure_ascii=False)[:3000]
    if action == "catalog":
        return "Composio catalog result: " + json.dumps(result.get("data"), ensure_ascii=False)[:3000]
    if action in {"toolkits", "tools"}:
        return "Composio " + str(action) + " result: " + json.dumps(result.get("data"), ensure_ascii=False)[:3000]
    if action in {"session_tools", "session_toolkits"}:
        return "Composio session " + str(action).replace("_", " ") + " result: " + json.dumps(result.get("data"), ensure_ascii=False)[:3000]
    if action == "search":
        return "Composio search result: " + json.dumps(result.get("data"), ensure_ascii=False)[:3000]
    return "Composio gateway completed action " + str(action)


@tool(
    name=_TOOL_NAME,
    description=(
        "Gateway to approved Composio external app toolkits. Use for Composio status, "
        "browsing Composio toolkits and tools, creating limited Composio sessions, "
        "generating Composio auth links, searching approved Composio session tools, "
        "inspecting approved tool schemas, and executing only markdown-enabled "
        "Composio tool slugs."
    ),
    examples=[
        "composio status",
        "create a composio session",
        "connect github through composio",
        "list composio github tools",
        "search approved composio tools for listing repository issues",
        "use composio to get repo details for this repo",
        "use composio tool GITHUB_LIST_STARGAZERS with owner and repo",
    ],
    param_descriptions={
        "action": "status, catalog, toolkits, tools, create_session, session_tools, session_toolkits, link, search, schema, or execute.",
        "toolkit": "Composio toolkit slug, such as github, allowed only when listed in tools/COMPOSIO_GATEWAY.md.",
        "tool_slug": "Exact enabled Composio tool slug when known. If the model provides an imprecise slug, the gateway may semantically resolve it only to a markdown-enabled tool.",
        "query": "Catalog or tool-router search query.",
        "arguments": "Object arguments for the selected Composio tool.",
        "session_id": "Optional Composio tool router session id. Falls back to KING_COMPOSIO_SESSION_ID.",
        "user_id": "Optional Composio user id. Falls back to KING_COMPOSIO_USER_ID.",
        "account": "Optional Composio connected account alias or id for execution.",
        "confirm": "Required true for markdown tools marked write, destructive, or auth.",
        "limit": "Catalog result limit from 1 to 100.",
        "timeout_ms": "Composio request timeout in milliseconds.",
        "response_format": "legacy or structured.",
        "trace_enabled": "Emit machine-readable trace when true.",
    },
)
def composio(
    action: str = "status",
    toolkit: str = "",
    tool_slug: str = "",
    query: str = "",
    arguments: dict = None,
    session_id: str = "",
    user_id: str = "",
    account: str = "",
    confirm: bool = False,
    limit: int = 20,
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 13
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    confirm = coerce_bool(confirm)

    try:
        policy = _load_policy()
    except FileNotFoundError as exc:
        error = error_payload(
            "POLICY_NOT_FOUND",
            "Composio markdown policy was not found.",
            "policy_path",
            str(exc),
            "existing tools/COMPOSIO_GATEWAY.md file",
            False,
            "Create tools/COMPOSIO_GATEWAY.md or set KING_COMPOSIO_POLICY_FILE.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio policy is missing.", "policy")

    normalized_action = str(action or "status").strip().lower()
    if normalized_action not in _ACTIONS:
        error = error_payload(
            "INVALID_ACTION",
            "Composio action is not supported.",
            "action",
            normalized_action,
            ", ".join(_ACTIONS),
            False,
            "Use one of the documented Composio gateway actions.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio action is not supported.", "input_validation")

    if normalized_action == "status":
        result = _status_result(policy)
        return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result))

    if not policy.enabled:
        error = error_payload(
            "COMPOSIO_DISABLED",
            "Composio gateway is disabled by markdown policy.",
            "enabled",
            policy.enabled,
            "enabled: true",
            False,
            "Set enabled: true in tools/COMPOSIO_GATEWAY.md.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio gateway is disabled.", "policy")

    api_key = _api_key(policy)
    if not api_key:
        error = error_payload(
            "MISSING_API_KEY",
            "Composio API key is not configured.",
            "api_key_env",
            policy.api_key_env,
            "COMPOSIO_API_KEY",
            False,
            "Add COMPOSIO_API_KEY to .env or the process environment.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio API key is missing.", "config")

    clean_timeout, timeout_error = normalize_timeout_ms(timeout_ms, int(settings.composio_default_timeout_ms or policy.default_timeout_ms))
    if timeout_error is not None:
        return _finish_error(timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "Composio received an invalid timeout.", "input_validation")
    timeout_value = int(clean_timeout or policy.default_timeout_ms)
    clean_user_id = _user_id(policy, user_id)
    clean_toolkit = _normalize_toolkit(toolkit)
    clean_slug = _normalize_slug(tool_slug)
    max_chars = max(1000, int(policy.max_response_chars or 12000))
    external_count = 0

    if normalized_action == "create_session":
        payload, error, count = _create_session(policy, api_key, clean_user_id, timeout_value)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio session creation failed.", "composio_http", external_count)
        result = {
            "action": "create_session",
            "session_id": payload.get("session_id"),
            "mcp_url": (payload.get("mcp") or {}).get("url") if isinstance(payload.get("mcp"), dict) else "",
            "tool_router_tools": payload.get("tool_router_tools", []),
            "config": payload.get("config", {}),
            "enabled_toolkits": sorted(policy.enabled_toolkits),
            "enabled_tools": sorted(policy.tools),
        }
        return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)

    if normalized_action == "link":
        if not clean_toolkit:
            error = error_payload("MISSING_TOOLKIT", "Composio link action needs a toolkit.", "toolkit", "", "enabled toolkit slug", False, "Pass toolkit with a value from Enabled Toolkits.")
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio link needs a toolkit.", "input_validation")
        if clean_toolkit not in policy.enabled_toolkits:
            error = error_payload("TOOLKIT_NOT_ALLOWED", "Composio toolkit is not enabled by markdown policy.", "toolkit", clean_toolkit, ", ".join(sorted(policy.enabled_toolkits)), False, "Add the toolkit to Enabled Toolkits before connecting it.")
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio toolkit is not enabled.", "policy")
        clean_session, session_payload, error, count = _ensure_session(policy, api_key, clean_user_id, session_id, timeout_value)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio session creation failed.", "composio_http", external_count)
        payload, error, count = _request(policy, "POST", "/tool_router/session/" + clean_session + "/link", api_key, timeout_value, json_payload={"toolkit": clean_toolkit})
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio auth link failed.", "composio_http", external_count)
        result = {
            "action": "link",
            "toolkit": clean_toolkit,
            "session_id": clean_session,
            "session_created": bool(session_payload),
            "redirect_url": payload.get("redirect_url") if isinstance(payload, dict) else "",
            "connected_account_id": payload.get("connected_account_id") if isinstance(payload, dict) else "",
            "data": _truncate_payload(payload, max_chars),
        }
        return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)

    if normalized_action == "catalog":
        clean_limit, limit_error = normalize_int(limit, "limit", 20, 1, 100, "Use a catalog limit from 1 to 100.", "INVALID_LIMIT")
        if limit_error is not None:
            return _finish_error(limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Composio received an invalid limit.", "input_validation")
        params: dict[str, Any] = {"limit": clean_limit}
        if clean_toolkit:
            if clean_toolkit not in policy.enabled_toolkits:
                error = error_payload("TOOLKIT_NOT_ALLOWED", "Composio toolkit is not enabled by markdown policy.", "toolkit", clean_toolkit, ", ".join(sorted(policy.enabled_toolkits)), False, "Add the toolkit to Enabled Toolkits before browsing it.")
                return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio toolkit is not enabled.", "policy")
            params["toolkit_slug"] = clean_toolkit
            if query:
                params["query"] = str(query).strip()
            path = "/tools"
        else:
            if query:
                params["search"] = str(query).strip()
            path = "/toolkits"
        payload, error, count = _request(policy, "GET", path, api_key, timeout_value, params=params)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio catalog lookup failed.", "composio_http", external_count)
        result = {
            "action": "catalog",
            "toolkit": clean_toolkit,
            "query": str(query or "").strip(),
            "limit": clean_limit,
            "items": _compact_catalog_items(payload, clean_limit),
            "next_cursor": payload.get("next_cursor") if isinstance(payload, dict) else None,
            "total_items": payload.get("total_items") if isinstance(payload, dict) else None,
            "data": _truncate_payload(payload, max_chars),
        }
        return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)

    if normalized_action in {"toolkits", "tools"}:
        clean_limit, limit_error = normalize_int(limit, "limit", 20, 1, 100, "Use a catalog limit from 1 to 100.", "INVALID_LIMIT")
        if limit_error is not None:
            return _finish_error(limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Composio received an invalid limit.", "input_validation")
        params: dict[str, Any] = {"limit": clean_limit}
        path = "/toolkits"
        if normalized_action == "tools":
            path = "/tools"
            if clean_toolkit:
                params["toolkit_slug"] = clean_toolkit
            if query:
                params["query"] = str(query).strip()
        else:
            if query:
                params["search"] = str(query).strip()
        payload, error, count = _request(policy, "GET", path, api_key, timeout_value, params=params)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio catalog lookup failed.", "composio_http", external_count)
        result = {
            "action": normalized_action,
            "toolkit": clean_toolkit,
            "query": str(query or "").strip(),
            "limit": clean_limit,
            "items": _compact_catalog_items(payload, clean_limit),
            "next_cursor": payload.get("next_cursor") if isinstance(payload, dict) else None,
            "total_items": payload.get("total_items") if isinstance(payload, dict) else None,
            "data": _truncate_payload(payload, max_chars),
        }
        return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)

    if normalized_action in {"session_tools", "session_toolkits"}:
        clean_session, session_payload, error, count = _ensure_session(policy, api_key, clean_user_id, session_id, timeout_value)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio session creation failed.", "composio_http", external_count)
        path = "/tool_router/session/" + clean_session + ("/tools" if normalized_action == "session_tools" else "/toolkits")
        payload, error, count = _request(policy, "GET", path, api_key, timeout_value)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio session inspection failed.", "composio_http", external_count)
        result = {
            "action": normalized_action,
            "session_id": clean_session,
            "session_created": bool(session_payload),
            "data": _truncate_payload(payload, max_chars),
        }
        return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)

    if normalized_action in {"schema", "execute"}:
        if not clean_slug:
            error = error_payload("MISSING_TOOL_SLUG", "Composio action needs a tool_slug.", "tool_slug", "", "enabled Composio tool slug", False, "Pass a slug listed under Enabled Tools.")
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio needs a tool slug.", "input_validation")
        clean_slug, rule, tool_resolution = _resolve_tool_rule(policy, clean_slug, query)
        if rule is None:
            error = error_payload("TOOL_NOT_ALLOWED", "Composio tool slug is not enabled by markdown policy.", "tool_slug", clean_slug, ", ".join(sorted(policy.tools)), False, "Add the exact tool slug to Enabled Tools before using it.")
            error["tool_resolution"] = tool_resolution
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio tool is not enabled.", "policy")
        if rule.risk in _CONFIRM_RISKS and not confirm:
            error = error_payload("CONFIRMATION_REQUIRED", "Composio tool risk requires explicit confirmation.", "confirm", confirm, "confirm=true", False, "Re-run with confirm=true after reviewing the action and arguments.")
            error["risk"] = rule.risk
            error["tool_slug"] = clean_slug
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio action needs confirmation.", "risk_policy")
        if normalized_action == "schema":
            defaults = _argument_defaults(policy, clean_slug)
            payload, error, count = _request(policy, "GET", "/tools/" + clean_slug, api_key, timeout_value)
            external_count += count
            if error is not None:
                return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio schema lookup failed.", "composio_http", external_count)
            input_schema = _schema_input_parameters(payload)
            result = {
                "action": "schema",
                "tool_slug": clean_slug,
                "toolkit": rule.toolkit,
                "risk": rule.risk,
                "tool_resolution": tool_resolution,
                "argument_defaults": defaults,
                "input_schema": input_schema,
                "required_arguments": list(input_schema.get("required") or []),
                "data": _truncate_payload(payload, max_chars),
            }
            return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)

        clean_arguments, args_error = _safe_json_loads(arguments)
        if args_error is not None:
            return _finish_error(args_error, response_format, trace_enabled, started, started_at, inputs_received, "Composio received invalid arguments.", "input_validation")
        argument_defaults = _argument_defaults(policy, clean_slug)
        clean_arguments, defaults_applied = _apply_argument_defaults(clean_arguments, argument_defaults, policy.argument_default_placeholders)
        clean_session, session_payload, error, count = _ensure_session(policy, api_key, clean_user_id, session_id, timeout_value)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio session creation failed.", "composio_http", external_count)
        body: dict[str, Any] = {
            "tool_slug": clean_slug,
            "arguments": clean_arguments,
        }
        clean_account = str(account or "").strip()
        if clean_account:
            body["account"] = clean_account
        payload, error, count = _request(policy, "POST", "/tool_router/session/" + clean_session + "/execute", api_key, timeout_value, json_payload=body)
        external_count += count
        if error is not None:
            return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio execution failed.", "composio_http", external_count)
        result = {
            "action": "execute",
            "tool_slug": clean_slug,
            "toolkit": rule.toolkit,
            "risk": rule.risk,
            "session_id": clean_session,
            "session_created": bool(session_payload),
            "tool_resolution": tool_resolution,
            "arguments": clean_arguments,
            "argument_defaults_applied": defaults_applied,
            "data": _truncate_payload(payload, max_chars),
        }
        return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)

    clean_session, session_payload, error, count = _ensure_session(policy, api_key, clean_user_id, session_id, timeout_value)
    external_count += count
    if error is not None:
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio session creation failed.", "composio_http", external_count)
    clean_query = str(query or "").strip()
    if not clean_query:
        error = error_payload("MISSING_QUERY", "Composio search needs a query.", "query", "", "non-empty tool search use case", False, "Pass a natural-language use case to search the approved Composio session.")
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio search needs a query.", "input_validation", external_count)
    payload, error, count = _request(
        policy,
        "POST",
        "/tool_router/session/" + clean_session + "/search",
        api_key,
        timeout_value,
        json_payload={"queries": [{"use_case": clean_query}]},
    )
    external_count += count
    if error is not None:
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Composio search failed.", "composio_http", external_count)
    result = {
        "action": "search",
        "query": clean_query,
        "session_id": clean_session,
        "session_created": bool(session_payload),
        "enabled_toolkits": sorted(policy.enabled_toolkits),
        "enabled_tools": sorted(policy.tools),
        "data": _truncate_payload(payload, max_chars),
    }
    return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result), external_count)
