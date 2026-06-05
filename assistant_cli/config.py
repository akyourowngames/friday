from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class AssistantSettings:
    api_key: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    storage_dir: Path
    db_path: Path
    sessions_dir: Path
    memory_dir: Path
    knowledge_dir: Path
    rag_index_dir: Path
    rag_top_k: int
    last_messages: int
    agentic_rag_enabled: bool
    agentic_query_count: int
    auto_memory_enabled: bool
    sarvam_api_key: str
    voice_enabled: bool
    voice_speaker: str
    voice_language: str
    voice_model: str
    voice_output_dir: Path
    voice_sample_rate: int
    voice_codec: str
    voice_pace: float
    voice_temperature: float
    voice_max_chars: int


def load_settings() -> AssistantSettings:
    _load_env()
    storage_dir = _path(_env("FRIDAY_STORAGE_DIR", "storage"))
    api_key = _env("NVIDIA_API_KEY", "")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is missing. Add it to .env or your shell environment.")

    return AssistantSettings(
        api_key=api_key,
        base_url=_env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        model=_env("NVIDIA_MODEL", "mistralai/ministral-14b-instruct-2512"),
        temperature=_env_float("FRIDAY_TEMPERATURE", 0.35),
        max_tokens=_env_int("FRIDAY_MAX_TOKENS", 1200),
        storage_dir=storage_dir,
        db_path=_path(_env("FRIDAY_DB_PATH", "storage/friday_assistant.sqlite3")),
        sessions_dir=_path(_env("FRIDAY_SESSIONS_DIR", "storage/sessions")),
        memory_dir=_path(_env("FRIDAY_MEMORY_DIR", "memory")),
        knowledge_dir=_path(_env("FRIDAY_KNOWLEDGE_DIR", "knowledge")),
        rag_index_dir=_path(_env("FRIDAY_RAG_INDEX_DIR", "storage/assistant_rag_index")),
        rag_top_k=_env_int("FRIDAY_RAG_TOP_K", 5),
        last_messages=_env_int("FRIDAY_LAST_MESSAGES", 20),
        agentic_rag_enabled=_env_bool("FRIDAY_AGENTIC_RAG_ENABLED", False),
        agentic_query_count=_env_int("FRIDAY_AGENTIC_QUERY_COUNT", 3),
        auto_memory_enabled=_env_bool("FRIDAY_AUTO_MEMORY_ENABLED", True),
        sarvam_api_key=_env("SARVAM_API_KEY", ""),
        voice_enabled=_env_bool("FRIDAY_VOICE_ENABLED", False),
        voice_speaker=_env("FRIDAY_VOICE_SPEAKER", "priya"),
        voice_language=_env("FRIDAY_VOICE_LANGUAGE", "en-IN"),
        voice_model=_env("FRIDAY_VOICE_MODEL", "bulbul:v3"),
        voice_output_dir=_path(_env("FRIDAY_VOICE_OUTPUT_DIR", "storage/voice")),
        voice_sample_rate=_env_int("FRIDAY_VOICE_SAMPLE_RATE", 24000),
        voice_codec=_env("FRIDAY_VOICE_CODEC", "wav"),
        voice_pace=_env_float("FRIDAY_VOICE_PACE", 1.0),
        voice_temperature=_env_float("FRIDAY_VOICE_TEMPERATURE", 0.55),
        voice_max_chars=_env_int("FRIDAY_VOICE_MAX_CHARS", 900),
    )
