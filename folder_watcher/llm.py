from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import settings

from .configuration import WatcherConfig
from .index import FolderIndex


@dataclass
class LLMPolicy:
    path: Path
    provider_enabled: bool = True
    summaries_enabled: bool = True
    queries_enabled: bool = True
    max_file_chars: int = 12000
    max_summary_chars: int = 700
    max_tags: int = 8
    query_row_limit: int = 25
    query_context_files: int = 12
    max_sql_chars: int = 2000
    allowed_tables: list[str] = field(default_factory=list)
    allowed_functions: list[str] = field(default_factory=list)
    summary_prompt: str = ""
    sql_prompt: str = ""

    def public_dict(self) -> dict:
        return {
            "path": str(self.path),
            "provider_enabled": self.provider_enabled,
            "summaries_enabled": self.summaries_enabled,
            "queries_enabled": self.queries_enabled,
            "max_file_chars": self.max_file_chars,
            "max_summary_chars": self.max_summary_chars,
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
            "policy": self.policy.public_dict(),
        }

    def query_available(self) -> bool:
        return bool(
            self.policy.provider_enabled
            and self.policy.queries_enabled
            and self.config.llm_queries_enabled
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

    return LLMPolicy(
        path=path,
        provider_enabled=_bool_value(values.get("provider_enabled"), True),
        summaries_enabled=_bool_value(values.get("summaries_enabled"), True),
        queries_enabled=_bool_value(values.get("queries_enabled"), True),
        max_file_chars=_int_value(values.get("max_file_chars"), 12000),
        max_summary_chars=_int_value(values.get("max_summary_chars"), 700),
        max_tags=_int_value(values.get("max_tags"), 8),
        query_row_limit=_int_value(values.get("query_row_limit"), 25),
        query_context_files=_int_value(values.get("query_context_files"), 12),
        max_sql_chars=_int_value(values.get("max_sql_chars"), 2000),
        allowed_tables=[item for item in allowed_tables if _safe_identifier(item)],
        allowed_functions=[item for item in allowed_functions if _safe_identifier(item)],
        summary_prompt="\n".join(summary_lines).strip(),
        sql_prompt="\n".join(sql_lines).strip(),
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


def _safe_identifier(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    for char in text:
        if not (char.isalnum() or char == "_"):
            return False
    return True
