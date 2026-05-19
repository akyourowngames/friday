import os
from functools import lru_cache
from pathlib import Path


def _load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


_load_env()


@lru_cache(maxsize=128)
def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@lru_cache(maxsize=128)
def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=128)
def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


@lru_cache(maxsize=128)
def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


class Settings:
    nim_api_key: str = _env("NVIDIA_API_KEY", "")
    nim_base_url: str = _env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    model_name: str = _env("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")
    debug: bool = _env_bool("KING_DEBUG", False)
    tool_top_k: int = _env_int("KING_TOOL_TOP_K", 3)
    tool_similarity_threshold: float = _env_float("KING_TOOL_SIMILARITY_THRESHOLD", 0.23)
    tool_winner_margin: float = _env_float("KING_TOOL_WINNER_MARGIN", 0.15)
    tool_relative_floor: float = _env_float("KING_TOOL_RELATIVE_FLOOR", 0.72)
    max_tool_rounds: int = _env_int("KING_MAX_TOOL_ROUNDS", 6)
    llm_stream_attempts: int = _env_int("KING_LLM_STREAM_ATTEMPTS", 2)
    tool_call_retries: int = _env_int("KING_TOOL_CALL_RETRIES", 1)
    tool_argument_grounding_threshold: float = _env_float("KING_TOOL_ARGUMENT_GROUNDING_THRESHOLD", 0.35)
    backtick_tool_similarity_threshold: float = _env_float("KING_BACKTICK_TOOL_SIMILARITY_THRESHOLD", 0.65)
    direct_single_tool_result: bool = _env_bool("KING_DIRECT_SINGLE_TOOL_RESULT", False)
    terminal_default_timeout: int = _env_int("KING_TERMINAL_TIMEOUT", 30)
    terminal_max_timeout: int = _env_int("KING_TERMINAL_MAX_TIMEOUT", 300)
    external_request_attempts: int = _env_int("KING_EXTERNAL_REQUEST_ATTEMPTS", 2)
    external_retry_delay: float = _env_float("KING_EXTERNAL_RETRY_DELAY", 0.25)
    embedding_model: str = _env("NVIDIA_EMBEDDING_MODEL", "nvidia/nv-embed-v1")
    embedding_min_chars: int = _env_int("KING_EMBEDDING_MIN_CHARS", 7)

    storage_dir: str = _env("KING_STORAGE_DIR", "storage")
    memory_dir: str = _env("KING_MEMORY_DIR", "storage/memories")
    memory_similarity_threshold: float = _env_float("KING_MEMORY_SIMILARITY_THRESHOLD", 0.25)
    memory_winner_margin: float = _env_float("KING_MEMORY_WINNER_MARGIN", 0.08)
    sarvam_api_key: str = _env("SARVAM_API_KEY", "")
    tavily_api_key: str = _env("TAVILY_API_KEY", "")
    voice_enabled: bool = _env_bool("KING_VOICE_ENABLED", False)
    voice_language: str = _env("KING_VOICE_LANGUAGE", "en-IN")
    
    # File-generating tools that trigger terminal viewing
    file_generating_tools: str = _env(
        "KING_FILE_GENERATING_TOOLS",
        "imagine,web_scrape,download,generate,create,gallery"
    )


settings = Settings()
