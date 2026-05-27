from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import settings

from .configuration import WatcherConfig
from .index import FolderIndex
from .understanding import file_understanding


@dataclass
class LLMPolicy:
    path: Path
    provider_enabled: bool = True
    summaries_enabled: bool = True
    queries_enabled: bool = True
    chat_enabled: bool = True
    deep_dive_enabled: bool = True
    max_file_chars: int = 12000
    max_summary_chars: int = 700
    max_chat_chars: int = 3000
    max_chat_history: int = 8
    chat_context_files: int = 8
    chat_response_tokens: int = 1200
    max_deep_dive_chars: int = 20000
    max_tags: int = 8
    query_row_limit: int = 25
    query_context_files: int = 12
    max_sql_chars: int = 2000
    allowed_tables: list[str] = field(default_factory=list)
    allowed_functions: list[str] = field(default_factory=list)
    summary_prompt: str = ""
    sql_prompt: str = ""
    chat_prompt: str = ""
    deep_dive_prompt: str = ""

    def public_dict(self) -> dict:
        return {
            "path": str(self.path),
            "provider_enabled": self.provider_enabled,
            "summaries_enabled": self.summaries_enabled,
            "queries_enabled": self.queries_enabled,
            "chat_enabled": self.chat_enabled,
            "deep_dive_enabled": self.deep_dive_enabled,
            "max_file_chars": self.max_file_chars,
            "max_summary_chars": self.max_summary_chars,
            "max_chat_chars": self.max_chat_chars,
            "max_chat_history": self.max_chat_history,
            "chat_context_files": self.chat_context_files,
            "chat_response_tokens": self.chat_response_tokens,
            "max_deep_dive_chars": self.max_deep_dive_chars,
            "max_tags": self.max_tags,
            "query_row_limit": self.query_row_limit,
            "query_context_files": self.query_context_files,
            "max_sql_chars": self.max_sql_chars,
            "allowed_tables": list(self.allowed_tables),
            "allowed_functions": list(self.allowed_functions),
        }


class FolderWatcherLLM:
    def __init__(
        self,
        config: WatcherConfig,
        index: FolderIndex,
        client_factory: Callable[[], Any] | None = None,
        policy: LLMPolicy | None = None,
    ):
        self.config = config
        self.index = index
        self.policy = policy or load_llm_policy(config.repo_root, config.llm_policy_path)
        self._client_factory = client_factory

    def status(self) -> dict:
        provider_ready = bool(settings.nim_api_key.strip())
        return {
            "provider": "nvidia_openai_compatible",
            "provider_ready": provider_ready,
            "model": settings.model_name,
            "base_url": settings.nim_base_url,
            "summaries_enabled": self.config.ai_summaries_enabled and self.policy.summaries_enabled,
            "queries_enabled": self.config.llm_queries_enabled and self.policy.queries_enabled,
            "chat_enabled": self.policy.chat_enabled,
            "deep_dive_enabled": self.policy.deep_dive_enabled,
            "policy": self.policy.public_dict(),
        }

    def query_available(self) -> bool:
        return bool(
            self.policy.provider_enabled
            and self.policy.queries_enabled
            and self.config.llm_queries_enabled
            and settings.nim_api_key.strip()
        )

    def chat_available(self) -> bool:
        return bool(
            self.policy.provider_enabled
            and self.policy.chat_enabled
            and settings.nim_api_key.strip()
        )

    def deep_dive_available(self) -> bool:
        return bool(
            self.policy.provider_enabled
            and self.policy.deep_dive_enabled
            and settings.nim_api_key.strip()
        )

    def summaries_available(self) -> bool:
        return bool(
            self.policy.provider_enabled
            and self.policy.summaries_enabled
            and self.config.ai_summaries_enabled
            and settings.nim_api_key.strip()
        )

    def generate_sql(self, query: str, limit: int | None = None) -> dict:
        row_limit = _bounded_int(limit or self.policy.query_row_limit, 1, self.policy.query_row_limit)
        context = {
            "question": query,
            "row_limit": row_limit,
            "allowed_tables": self.policy.allowed_tables,
            "allowed_functions": self.policy.allowed_functions,
            "schema": self.index.public_schema(self.policy.allowed_tables),
            "recent_files": self.index.latest(self.policy.query_context_files),
        }
        result = self._complete_json(
            self.policy.sql_prompt,
            json.dumps(context, ensure_ascii=False, sort_keys=True),
            max_tokens=700,
        )
        sql = str(result.get("sql", "")).strip()
        if len(sql) > self.policy.max_sql_chars:
            sql = sql[: self.policy.max_sql_chars]
        return {
            "sql": sql,
            "explanation": str(result.get("explanation", "")),
            "row_limit": row_limit,
            "provider": "llm",
        }

    def summarize_file(self, file_record: dict, content: str) -> dict:
        request = {
            "file": {
                "id": file_record.get("id"),
                "path": file_record.get("path"),
                "filename": file_record.get("filename"),
                "extension": file_record.get("extension"),
                "mime_type": file_record.get("mime_type"),
                "size_bytes": file_record.get("size_bytes"),
                "metadata": file_record.get("metadata", {}),
                "tags": file_record.get("tags", []),
            },
            "content": str(content or "")[: self.policy.max_file_chars],
            "max_summary_chars": self.policy.max_summary_chars,
            "max_tags": self.policy.max_tags,
        }
        result = self._complete_json(
            self.policy.summary_prompt,
            json.dumps(request, ensure_ascii=False, sort_keys=True),
            max_tokens=400,
        )
        summary = str(result.get("summary", "")).strip()[: self.policy.max_summary_chars]
        tags = result.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        clean_tags = []
        for item in tags:
            tag = str(item).strip()
            if tag and tag not in clean_tags:
                clean_tags.append(tag)
            if len(clean_tags) >= self.policy.max_tags:
                break
        return {"summary": summary, "tags": clean_tags, "provider": "llm"}

    def chat(self, message: str, history: list[dict] | None = None, file_id: str | None = None, limit: int | None = None) -> dict:
        context = self._chat_context(message, file_id, limit)
        answer = self._complete_text(
            self.policy.chat_prompt,
            context,
            history or [],
            max_tokens=max(256, int(self.policy.chat_response_tokens or 1200)),
        )
        return {
            "answer": answer[: self.policy.max_chat_chars],
            "provider": "llm",
            "context_mode": context.get("context_mode"),
            "selected_file": context.get("selected_file"),
            "files": context.get("relevant_files", []),
            "stats": context.get("stats", {}),
        }

    def deep_dive_file(self, file_id: str) -> dict:
        file_record = self.index.get_file(file_id)
        if file_record is None:
            raise ValueError("file not found")
        context = self._deep_dive_context(file_record)
        answer = self._complete_text(
            self.policy.deep_dive_prompt,
            context,
            [],
            max_tokens=1100,
        )
        return {
            "answer": answer[: self.policy.max_chat_chars],
            "provider": "llm",
            "file": context["file"],
            "understanding": context["understanding"],
            "dependencies": context["dependencies"],
            "dependents": context["dependents"],
            "events": context["events"],
        }

    def _complete_json(self, system_prompt: str, user_payload: str, max_tokens: int) -> dict:
        client = self._client()
        response = client.client.chat.completions.create(
            model=settings.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content
        parsed = _parse_json_object(str(content or ""))
        if not isinstance(parsed, dict):
            raise ValueError("LLM response did not contain a JSON object")
        return parsed

    def _complete_text(self, system_prompt: str, payload: dict, history: list[dict], max_tokens: int) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        for item in _clean_history(history, self.policy.max_chat_history, self.policy.max_chat_chars):
            messages.append(item)
        messages.append(
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            }
        )
        client = self._client()
        response = client.client.chat.completions.create(
            model=settings.model_name,
            messages=messages,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return str(response.choices[0].message.content or "").strip()

    def _chat_context(self, message: str, file_id: str | None, limit: int | None) -> dict:
        bounded = _bounded_int(limit or self.policy.chat_context_files, 1, self.policy.chat_context_files)
        selected_file = self.index.get_file(file_id) if file_id else None
        relevant_files = self.index.search(message, bounded) if _has_search_signal(message) else []
        if selected_file and all(item["id"] != selected_file["id"] for item in relevant_files):
            relevant_files.insert(0, selected_file)
            relevant_files = relevant_files[:bounded]
        stats = self.index.stats()
        context_mode = "selected_file" if selected_file else ("search_results" if relevant_files else "chat_only")
        return {
            "user_message": message,
            "context_mode": context_mode,
            "file_feature": {
                "available": True,
                "selected_file_attached": bool(selected_file),
                "search_result_count": len(relevant_files),
                "active_files": stats.get("active_files", 0),
                "events": stats.get("events", 0),
            },
            "selected_file": self._file_context(selected_file, self.policy.max_deep_dive_chars) if selected_file else None,
            "relevant_files": [
                self._file_context(item, self.policy.max_file_chars)
                for item in relevant_files
            ],
            "hot_files": self.index.hot_files(
                threshold=self.config.hot_file_event_threshold,
                window_seconds=self.config.hot_file_window_seconds,
                limit=5,
            ) if relevant_files or selected_file else [],
            "recent_anomalies": self.index.anomalies(5) if relevant_files or selected_file else [],
            "duplicate_suggestions": self.index.duplicate_symlink_suggestions()[:5] if relevant_files or selected_file else [],
            "stats": {
                "active_files": stats.get("active_files", 0),
                "total_size_bytes": stats.get("total_size_bytes", 0),
                "events": stats.get("events", 0),
                "summary_coverage": stats.get("summary_coverage", 0),
                "fts_enabled": stats.get("fts_enabled", False),
                "by_extension": stats.get("by_extension", {}),
                "by_mime_type": stats.get("by_mime_type", {}),
                "by_extension_details": stats.get("by_extension_details", {}),
                "by_mime_type_details": stats.get("by_mime_type_details", {}),
                "largest_files": [
                    self._file_context(item, 0)
                    for item in stats.get("largest_files", [])[:5]
                ],
            },
        }

    def _deep_dive_context(self, file_record: dict) -> dict:
        file_id = file_record["id"]
        events = [
            item
            for item in self.index.diff(since=0, limit=1000)
            if item.get("file_id") == file_id
        ][-20:]
        duplicate_groups = []
        for suggestion in self.index.duplicate_symlink_suggestions():
            canonical = suggestion.get("canonical", {})
            duplicates = suggestion.get("duplicates", [])
            if canonical.get("id") == file_id or any(item.get("id") == file_id for item in duplicates):
                duplicate_groups.append(suggestion)
        content = self.index.get_content(file_id) or ""
        return {
            "file": self._file_context(file_record, self.policy.max_deep_dive_chars),
            "understanding": file_understanding(file_record, content),
            "dependencies": self.index.dependencies(file_id),
            "dependents": self.index.dependents(file_id),
            "events": events,
            "duplicate_suggestions": duplicate_groups,
            "stats": self.index.stats(),
        }

    def _file_context(self, file_record: dict | None, max_chars: int) -> dict | None:
        if file_record is None:
            return None
        content = self.index.get_content(file_record["id"]) or ""
        return {
            "id": file_record.get("id"),
            "path": file_record.get("path"),
            "filename": file_record.get("filename"),
            "extension": file_record.get("extension"),
            "mime_type": file_record.get("mime_type"),
            "size_bytes": file_record.get("size_bytes"),
            "sha256": file_record.get("sha256"),
            "metadata": file_record.get("metadata", {}),
            "summary": file_record.get("summary", ""),
            "tags": file_record.get("tags", []),
            "status": file_record.get("status"),
            "content_excerpt": content[: max(0, int(max_chars or 0))],
        }

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from agent.llm import NIMClient

        return NIMClient()


def load_llm_policy(repo_root: str | Path = ".", policy_path: str | Path | None = None) -> LLMPolicy:
    root = Path(repo_root).expanduser().resolve()
    if policy_path is None:
        path = root / settings.folder_watcher_llm_policy_file
    else:
        path = Path(policy_path).expanduser()
        if not path.is_absolute():
            path = root / path
    path = path.resolve()

    values: dict[str, str] = {}
    allowed_tables: list[str] = []
    allowed_functions: list[str] = []
    summary_lines: list[str] = []
    sql_lines: list[str] = []
    chat_lines: list[str] = []
    deep_dive_lines: list[str] = []

    if path.exists():
        section = ""
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                section = line[3:].strip().lower().replace(" ", "_")
                continue
            if section == "runtime" and line.startswith("- "):
                key, found, value = line[2:].partition(":")
                if found:
                    values[key.strip()] = value.strip()
            elif section == "allowed_sql_tables" and line.startswith("- "):
                allowed_tables.append(line[2:].strip())
            elif section == "allowed_sql_functions" and line.startswith("- "):
                allowed_functions.append(line[2:].strip().lower())
            elif section == "summary_system_prompt":
                if line:
                    summary_lines.append(raw_line)
            elif section == "sql_system_prompt":
                if line:
                    sql_lines.append(raw_line)
            elif section == "chat_system_prompt":
                if line:
                    chat_lines.append(raw_line)
            elif section == "deep_dive_system_prompt":
                if line:
                    deep_dive_lines.append(raw_line)

    return LLMPolicy(
        path=path,
        provider_enabled=_bool_value(values.get("provider_enabled"), True),
        summaries_enabled=_bool_value(values.get("summaries_enabled"), True),
        queries_enabled=_bool_value(values.get("queries_enabled"), True),
        chat_enabled=_bool_value(values.get("chat_enabled"), True),
        deep_dive_enabled=_bool_value(values.get("deep_dive_enabled"), True),
        max_file_chars=_int_value(values.get("max_file_chars"), 12000),
        max_summary_chars=_int_value(values.get("max_summary_chars"), 700),
        max_chat_chars=_int_value(values.get("max_chat_chars"), 3000),
        max_chat_history=_int_value(values.get("max_chat_history"), 8),
        chat_context_files=_int_value(values.get("chat_context_files"), 8),
        chat_response_tokens=_int_value(values.get("chat_response_tokens"), 1200),
        max_deep_dive_chars=_int_value(values.get("max_deep_dive_chars"), 20000),
        max_tags=_int_value(values.get("max_tags"), 8),
        query_row_limit=_int_value(values.get("query_row_limit"), 25),
        query_context_files=_int_value(values.get("query_context_files"), 12),
        max_sql_chars=_int_value(values.get("max_sql_chars"), 2000),
        allowed_tables=[item for item in allowed_tables if _safe_identifier(item)],
        allowed_functions=[item for item in allowed_functions if _safe_identifier(item)],
        summary_prompt="\n".join(summary_lines).strip(),
        sql_prompt="\n".join(sql_lines).strip(),
        chat_prompt="\n".join(chat_lines).strip(),
        deep_dive_prompt="\n".join(deep_dive_lines).strip(),
    )


def _parse_json_object(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = _first_json_start(text)
    if start < 0:
        raise ValueError("No JSON object found")
    end = _matching_json_end(text, start)
    if end < 0:
        raise ValueError("JSON object was not complete")
    return json.loads(text[start : end + 1])


def _first_json_start(text: str) -> int:
    positions = [pos for pos in (text.find("{"), text.find("[")) if pos >= 0]
    return min(positions) if positions else -1


def _matching_json_end(text: str, start: int) -> int:
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    stack = [closing]
    in_string = False
    escaped = False
    index = start + 1
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif not in_string:
            if char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif stack and char == stack[-1]:
                stack.pop()
                if not stack:
                    return index
        index += 1
    return -1


def _bool_value(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _int_value(value: object, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _bounded_int(value: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _has_search_signal(value: str) -> bool:
    alpha = 0
    alnum = 0
    for char in str(value or "").strip():
        if char.isalpha():
            alpha += 1
        if char.isalnum():
            alnum += 1
    return alpha > 0 and alnum >= 3


def _clean_history(history: list[dict], max_items: int, max_chars: int) -> list[dict]:
    clean: list[dict] = []
    for item in history[-max(0, int(max_items or 0)) :]:
        role = str(item.get("role", "")).strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        clean.append({"role": role, "content": content[: max(1, int(max_chars or 1))]})
    return clean


def _safe_identifier(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    for char in text:
        if not (char.isalnum() or char == "_"):
            return False
    return True
