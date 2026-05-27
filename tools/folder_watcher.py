from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

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


_TOOL_NAME = "folder_watcher"
_FOLDER_WATCHER_VERSION = "1.0.0"
_ACTIONS = ("ask", "query", "stats", "details", "search", "latest", "content", "deep_dive", "status")
_QUERY_ACTIONS = {"ask", "query", "search"}
_FILE_ID_ACTIONS = {"content", "deep_dive"}


@dataclass
class FolderWatcherTarget:
    name: str
    base_url: str
    auth_env: str = ""


@dataclass
class FolderWatcherClientConfig:
    path: Path
    active_target: str = "local"
    default_timeout_ms: int = 12000
    max_limit: int = 500
    targets: dict[str, FolderWatcherTarget] = field(default_factory=dict)
    enabled_actions: set[str] = field(default_factory=set)
    action_semantics: dict[str, str] = field(default_factory=dict)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _clean_action(value: str) -> str:
    return str(value or "").strip().lower()


def _clean_extension(value: str) -> str:
    extension = str(value or "").strip().lower()
    if extension and not extension.startswith("."):
        extension = "." + extension
    return extension


def _split_config_item(line: str) -> tuple[str, str]:
    body = line.strip()[2:].strip()
    key, separator, value = body.partition(":")
    if not separator:
        return "", ""
    return key.strip(), value.strip()


def _parse_int(value: str, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def _load_client_config(root: Path | None = None, path: str | Path | None = None) -> FolderWatcherClientConfig:
    repo_root = Path(root or _repo_root()).resolve()
    requested = Path(path or settings.folder_watcher_client_file)
    config_path = requested if requested.is_absolute() else repo_root / requested
    config_path = config_path.resolve()
    if not config_path.exists():
        raise FileNotFoundError(str(config_path))

    config = FolderWatcherClientConfig(path=config_path)
    section = ""
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            section = line[3:].strip().casefold()
            continue
        if not line.startswith("- "):
            continue

        if section == "runtime":
            key, value = _split_config_item(line)
            if key == "active_target":
                config.active_target = value
            elif key == "default_timeout_ms":
                config.default_timeout_ms = _parse_int(value, config.default_timeout_ms)
            elif key == "max_limit":
                config.max_limit = _parse_int(value, config.max_limit)
            continue

        if section == "targets":
            key, value = _split_config_item(line)
            if not key or not value:
                continue
            pieces = [piece.strip() for piece in value.split("|") if piece.strip()]
            base_url = pieces[0].rstrip("/") if pieces else ""
            auth_env = ""
            for piece in pieces[1:]:
                env_key, separator, env_value = piece.partition(":")
                if separator and env_key.strip() == "auth_env":
                    auth_env = env_value.strip()
            if base_url:
                config.targets[key] = FolderWatcherTarget(name=key, base_url=base_url, auth_env=auth_env)
            continue

        if section == "enabled actions":
            action = _clean_action(line[2:])
            if action:
                config.enabled_actions.add(action)
            continue

        if section == "action semantics":
            key, value = _split_config_item(line)
            action = _clean_action(key)
            if action in _ACTIONS and value:
                config.action_semantics[action] = value

    if not config.enabled_actions:
        config.enabled_actions = set(_ACTIONS)
    return config


def _enabled_actions(config: FolderWatcherClientConfig) -> set[str]:
    return {action for action in config.enabled_actions if action in _ACTIONS}


def _fallback_action(enabled_actions: set[str]) -> str:
    if "ask" in enabled_actions:
        return "ask"
    for action in _ACTIONS:
        if action in enabled_actions:
            return action
    return ""


def _score_action_semantics(user_input: str, candidates: list[tuple[str, str]]) -> list[tuple[str, float]]:
    if not candidates:
        return []
    try:
        import numpy as np
        from agent.embedder import embed

        query_emb = embed(str(user_input or ""))
        candidate_embs = embed([action + ": " + text for action, text in candidates])
        if getattr(candidate_embs, "ndim", 1) == 1:
            candidate_embs = candidate_embs.reshape(1, -1)
        scores = np.dot(candidate_embs, query_emb)
        return [
            (action, float(scores[index]))
            for index, (action, _) in enumerate(candidates)
        ]
    except Exception:
        return []


def _semantic_action_for_request(user_input: str, config: FolderWatcherClientConfig) -> str:
    enabled = _enabled_actions(config)
    fallback = _fallback_action(enabled)
    if not str(user_input or "").strip():
        return ""

    candidates = [
        (action, config.action_semantics[action])
        for action in _ACTIONS
        if action in enabled and config.action_semantics.get(action)
    ]
    scored = _score_action_semantics(user_input, candidates)
    if not scored:
        return fallback

    best_action, best_score = max(scored, key=lambda item: item[1])
    rounded_scores = {round(score, 6) for _, score in scored}
    if len(rounded_scores) <= 1:
        return fallback
    if best_score < settings.tool_argument_grounding_threshold:
        return fallback
    return best_action


def _recent_folder_watcher_files(recent_result: dict | None) -> list[dict[str, Any]]:
    if not isinstance(recent_result, dict):
        return []
    result = recent_result.get("result")
    if not isinstance(result, dict):
        return []

    files: list[dict[str, Any]] = []

    def add_file(item: Any) -> None:
        if isinstance(item, dict):
            files.append(item)

    for item in result.get("files") or []:
        add_file(item)

    data = result.get("data")
    if isinstance(data, dict):
        for item in data.get("files") or []:
            add_file(item)
        add_file(data.get("file"))

    add_file(result.get("file"))
    if result.get("file_id"):
        files.append({"id": result.get("file_id")})
    return files


def _single_recent_file_id(recent_result: dict | None) -> str:
    unique_ids: dict[str, bool] = {}
    for item in _recent_folder_watcher_files(recent_result):
        file_id = str(item.get("id") or item.get("file_id") or "").strip()
        if file_id:
            unique_ids[file_id] = True
    if len(unique_ids) != 1:
        return ""
    return next(iter(unique_ids))


def build_natural_folder_watcher_args(
    user_input: str,
    recent_result: dict | None = None,
    response_format: str = "structured",
) -> dict[str, Any]:
    query = str(user_input or "").strip()
    if not query:
        return {}

    try:
        config = _load_client_config()
        enabled = _enabled_actions(config)
        action = _semantic_action_for_request(query, config)
    except FileNotFoundError:
        enabled = set(_ACTIONS)
        action = "ask"

    if not action:
        return {}

    args: dict[str, Any] = {"action": action, "query": query}
    if action in _FILE_ID_ACTIONS:
        file_id = _single_recent_file_id(recent_result)
        if file_id:
            args["file_id"] = file_id
        else:
            fallback = _fallback_action({item for item in enabled if item not in _FILE_ID_ACTIONS})
            if not fallback:
                return {}
            args["action"] = fallback

    if response_format:
        args["response_format"] = response_format
    return args


def _target_from_config(config: FolderWatcherClientConfig, requested_target: str) -> tuple[str, FolderWatcherTarget | None]:
    env_base_url = str(settings.folder_watcher_base_url or "").strip().rstrip("/")
    target_name = str(requested_target or settings.folder_watcher_target or config.active_target or "local").strip()
    if env_base_url:
        return target_name or "env", FolderWatcherTarget(target_name or "env", env_base_url, "KING_FOLDER_WATCHER_AUTH_TOKEN")
    return target_name, config.targets.get(target_name)


def _auth_token(target: FolderWatcherTarget) -> str:
    configured = str(settings.folder_watcher_auth_token or "").strip()
    if configured:
        return configured
    if target.auth_env:
        return str(os.getenv(target.auth_env, "") or "").strip()
    return ""


def _trace(
    started_at: str,
    started: float,
    inputs_received: int,
    schema_valid: bool,
    path: str,
    status: str,
    fields: int,
    external_count: int = 0,
    error_code: str | None = None,
) -> dict:
    return make_trace(
        _TOOL_NAME,
        _FOLDER_WATCHER_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        path,
        status,
        fields,
        {"count": external_count, "systems": ["folder_watcher"] if external_count else []},
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
    path: str = "input_validation",
    external_count: int = 0,
):
    trace = _trace(started_at, started, inputs_received, False, path, "FAILED", 1, external_count, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(_TOOL_NAME, _FOLDER_WATCHER_VERSION, error, started, trace)
    return legacy


def _finish_success(
    result: dict,
    response_format: str,
    trace_enabled: bool,
    started: float,
    started_at: str,
    inputs_received: int,
    legacy: str,
):
    status = "SUCCESS"
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if data.get("status") in {"pending", "partial"} or data.get("provider_sql_generation") == "unavailable":
        status = "PARTIAL"
    trace = _trace(started_at, started, inputs_received, True, "folder_watcher_http", status, len(result), 1)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success(_TOOL_NAME, _FOLDER_WATCHER_VERSION, result, started, trace)
    return legacy


def _status_error_payload(code: str, message: str, action: str, base_url: str, endpoint: str, method: str, retryable: bool, suggestion: str) -> dict:
    error = error_payload(code, message, "service", base_url, "folder watcher HTTP JSON response", retryable, suggestion)
    error["action"] = action
    error["base_url"] = base_url
    error["endpoint"] = endpoint
    error["method"] = method
    return error


def _json_or_error(response: httpx.Response, action: str, base_url: str, endpoint: str, method: str) -> tuple[dict[str, Any] | list[Any], dict | None]:
    try:
        payload = response.json()
    except ValueError:
        return {}, _status_error_payload(
            "INVALID_UPSTREAM_JSON",
            "Folder watcher returned a non-JSON response.",
            action,
            base_url,
            endpoint,
            method,
            True,
            "Verify the folder watcher service URL and endpoint.",
        )
    if isinstance(payload, (dict, list)):
        return payload, None
    return {"value": payload}, None


def _safe_upstream_detail(payload: dict[str, Any] | list[Any], response: httpx.Response) -> Any:
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return str(getattr(response, "text", "") or "")[:500]


def _request_spec(
    action: str,
    query: str,
    file_id: str,
    extension: str,
    directory: str,
    limit: int,
    include_content: bool,
    max_content_chars: int,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if action == "ask":
        return "POST", "/chat", {}, {"message": query, "limit": limit}
    if action == "query":
        return "POST", "/files/query", {}, {"query": query, "limit": limit}
    if action == "stats":
        return "GET", "/files/stats", {}, {}
    if action == "details":
        params: dict[str, Any] = {
            "limit": limit,
            "include_content": include_content,
            "max_content_chars": max_content_chars,
        }
        if extension:
            params["ext"] = extension
        if directory:
            params["dir"] = directory
        return "GET", "/files/details", params, {}
    if action == "search":
        return "GET", "/files/search", {"q": query, "limit": limit}, {}
    if action == "latest":
        params = {"n": limit}
        if extension:
            params["ext"] = extension
        if directory:
            params["dir"] = directory
        return "GET", "/files/latest", params, {}
    if action == "content":
        return "GET", "/files/" + quote(file_id, safe="") + "/content", {"max_chars": max_content_chars}, {}
    if action == "deep_dive":
        return "GET", "/files/" + quote(file_id, safe="") + "/deep-dive", {}, {}
    return "GET", "/status", {}, {}


def _extract_files(action: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = data.get("files")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    if action == "deep_dive" and isinstance(data.get("file"), dict):
        return [data["file"]]
    return []


def _extract_count(action: str, data: dict[str, Any], files: list[dict[str, Any]]) -> int:
    count = data.get("count")
    if isinstance(count, int):
        return count
    if action == "stats":
        active = data.get("active_files")
        if isinstance(active, int):
            return active
    return len(files)


def _legacy_summary(result: dict[str, Any]) -> str:
    action = result.get("action")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if result.get("answer"):
        return str(result["answer"])
    if action == "stats":
        lines = [
            "Folder watcher stats",
            "Active files: " + str(data.get("active_files", "unknown")),
            "Total size bytes: " + str(data.get("total_size_bytes", "unknown")),
        ]
        details = data.get("by_extension_details")
        if isinstance(details, dict) and details:
            for extension, item in list(details.items())[:8]:
                if isinstance(item, dict):
                    lines.append(f"{extension}: {item.get('count', 0)} file(s), {item.get('size_bytes', 0)} bytes")
        return "\n".join(lines)
    if action in {"details", "search", "latest", "query"}:
        files = result.get("files") if isinstance(result.get("files"), list) else []
        lines = ["Folder watcher returned " + str(result.get("count", len(files))) + " file(s)."]
        for item in files[:10]:
            title = str(item.get("filename") or item.get("path") or item.get("id") or "file")
            path = str(item.get("path") or "")
            size = item.get("size_bytes")
            suffix = f" ({size} bytes)" if size is not None else ""
            lines.append(title + suffix + (" - " + path if path else ""))
        return "\n".join(lines)
    if action == "content":
        return str(data.get("content") or "")[:4000] or "Folder watcher returned empty content."
    if action == "status":
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        return "Folder watcher status: " + str(data.get("status") or runtime.get("watch_path") or "available")
    return "Folder watcher returned JSON data for action " + str(action) + "."


@tool(
    name=_TOOL_NAME,
    description=(
        "Read-only bridge to the KING folder watcher HTTP service for natural folder chat, "
        "file queries, stats, latest files, search, content, and deep dives. Use it when a "
        "user asks about indexed folder contents, file counts, image or media counts, file "
        "details, total size by extension, recent watcher files, or wants an LLM-backed "
        "answer from watcher evidence."
    ),
    examples=[
        "ask the folder watcher what is in this folder",
        "what is in the current folder",
        "how many python files and images are there in this folder",
        "count files by type in the current folder",
        "what is the total size for python files in the folder watcher",
        "show latest indexed audio files from the watcher",
        "deep dive this watcher file id",
        "search folder watcher for api_server",
    ],
    param_descriptions={
        "action": "Read-only action: ask, query, stats, details, search, latest, content, deep_dive, or status.",
        "query": "Natural-language question or search text for ask, query, and search actions.",
        "file_id": "Indexed folder watcher file id for content and deep_dive.",
        "extension": "Optional extension filter such as .py, py, .md, or wav.",
        "directory": "Optional directory filter passed to the watcher service.",
        "limit": "Maximum rows or files to return, bounded by markdown config.",
        "include_content": "Whether details should include content excerpts.",
        "max_content_chars": "Maximum content excerpt size for details.",
        "target": "Named target from tools/FOLDER_WATCHER_CLIENT.md.",
        "timeout_ms": "HTTP timeout in milliseconds.",
        "response_format": "legacy or structured.",
        "trace_enabled": "Emit machine-readable trace when true.",
    },
)
def folder_watcher(
    action: str = "ask",
    query: str = "",
    file_id: str = "",
    extension: str = "",
    directory: str = "",
    limit: int = 20,
    include_content: bool = False,
    max_content_chars: int = 2000,
    target: str = "",
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 12
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    include_content = coerce_bool(include_content)

    try:
        client_config = _load_client_config()
    except FileNotFoundError as exc:
        error = error_payload(
            "CONFIG_NOT_FOUND",
            "Folder watcher client markdown config was not found.",
            "config_path",
            str(exc),
            "existing markdown client config",
            False,
            "Create tools/FOLDER_WATCHER_CLIENT.md or set KING_FOLDER_WATCHER_CLIENT_FILE.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher client config is missing.")

    normalized_action = _clean_action(action or "ask")
    if normalized_action not in _ACTIONS:
        error = error_payload(
            "INVALID_ACTION",
            "folder_watcher action is not supported.",
            "action",
            normalized_action,
            ", ".join(_ACTIONS),
            False,
            "Use one of the read-only folder watcher actions.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher action is not supported.")
    if normalized_action not in client_config.enabled_actions:
        error = error_payload(
            "ACTION_DISABLED",
            "folder_watcher action is disabled by markdown config.",
            "action",
            normalized_action,
            ", ".join(sorted(client_config.enabled_actions)),
            False,
            "Enable the read-only action in tools/FOLDER_WATCHER_CLIENT.md.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher action is disabled.")

    clean_query = str(query or "").strip()
    clean_file_id = str(file_id or "").strip()
    if normalized_action in _QUERY_ACTIONS and not clean_query:
        error = error_payload("MISSING_QUERY", "query is required for this folder watcher action.", "query", "", "non-empty query", False, "Pass query for ask, query, or search.")
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher needs a query.")
    if normalized_action in _FILE_ID_ACTIONS and not clean_file_id:
        error = error_payload("MISSING_FILE_ID", "file_id is required for this folder watcher action.", "file_id", "", "indexed watcher file id", False, "Pass a file_id from folder watcher search, latest, details, or query results.")
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher needs a file id.")

    max_limit = max(1, int(client_config.max_limit or 500))
    clean_limit, limit_error = normalize_int(limit, "limit", 20, 1, max_limit, "Use a limit within the markdown-configured maximum.", "INVALID_LIMIT")
    if limit_error is not None:
        return _finish_error(limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher received an invalid limit.")
    clean_max_content, content_error = normalize_int(max_content_chars, "max_content_chars", 2000, 1, 50000, "Use a content excerpt limit between 1 and 50000.", "INVALID_MAX_CONTENT_CHARS")
    if content_error is not None:
        return _finish_error(content_error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher received an invalid content limit.")
    default_timeout = int(settings.folder_watcher_timeout_ms or client_config.default_timeout_ms or 12000)
    clean_timeout, timeout_error = normalize_timeout_ms(timeout_ms, default_timeout)
    if timeout_error is not None:
        return _finish_error(timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher received an invalid timeout.")

    target_name, target_config = _target_from_config(client_config, target)
    if target_config is None:
        error = error_payload(
            "TARGET_NOT_FOUND",
            "Folder watcher target was not found in markdown config.",
            "target",
            target_name,
            ", ".join(sorted(client_config.targets)),
            False,
            "Use a configured target or set KING_FOLDER_WATCHER_BASE_URL.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher target is not configured.")

    method, endpoint, params, json_payload = _request_spec(
        normalized_action,
        clean_query,
        clean_file_id,
        _clean_extension(extension),
        str(directory or "").strip(),
        int(clean_limit),
        include_content,
        int(clean_max_content),
    )
    headers = {"Accept": "application/json"}
    token = _auth_token(target_config)
    if token:
        headers["Authorization"] = "Bearer " + token

    url = target_config.base_url.rstrip("/") + endpoint
    try:
        response = httpx.request(
            method,
            url,
            params=params or None,
            json=json_payload or None,
            headers=headers,
            timeout=int(clean_timeout) / 1000,
        )
    except (httpx.TimeoutException, httpx.RequestError):
        error = _status_error_payload(
            "SERVICE_UNAVAILABLE",
            "Folder watcher service could not be reached.",
            normalized_action,
            target_config.base_url,
            endpoint,
            method,
            True,
            "Start the folder watcher service or choose another configured target.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher service is unavailable.", "folder_watcher_http", 1)

    payload, json_error = _json_or_error(response, normalized_action, target_config.base_url, endpoint, method)
    if json_error is not None:
        return _finish_error(json_error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher returned invalid JSON.", "folder_watcher_http", 1)
    if response.status_code in (401, 403):
        error = _status_error_payload(
            "AUTH_FAILED",
            "Folder watcher rejected the request authentication.",
            normalized_action,
            target_config.base_url,
            endpoint,
            method,
            False,
            "Check the configured auth token source; the token value is not returned here.",
        )
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher authentication failed.", "folder_watcher_http", 1)
    if response.status_code >= 400:
        error = _status_error_payload(
            "UPSTREAM_ERROR",
            "Folder watcher returned an HTTP error.",
            normalized_action,
            target_config.base_url,
            endpoint,
            method,
            response.status_code >= 500,
            "Inspect the upstream detail and retry only if the operation is safe.",
        )
        error["status_code"] = response.status_code
        error["upstream_detail"] = _safe_upstream_detail(payload, response)
        return _finish_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Folder watcher returned an HTTP error.", "folder_watcher_http", 1)

    data = payload if isinstance(payload, dict) else {"items": payload}
    files = _extract_files(normalized_action, data)
    result = {
        "action": normalized_action,
        "target": target_name,
        "base_url": target_config.base_url,
        "method": method,
        "endpoint": endpoint,
        "status_code": response.status_code,
        "query": clean_query,
        "file_id": clean_file_id,
        "extension": _clean_extension(extension),
        "directory": str(directory or "").strip(),
        "limit": int(clean_limit),
        "include_content": include_content,
        "max_content_chars": int(clean_max_content),
        "mode": str(data.get("mode") or ""),
        "answer": str(data.get("answer") or ""),
        "data": data,
        "files": files,
        "stats": data if normalized_action == "stats" else data.get("stats", {}),
        "count": _extract_count(normalized_action, data, files),
    }
    return _finish_success(result, response_format, trace_enabled, started, started_at, inputs_received, _legacy_summary(result))
