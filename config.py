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
                os.environ.setdefault(_clean_env_key(key), val.strip())


def _clean_env_key(value: str) -> str:
    return str(value or "").lstrip("\ufeff").strip()


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
    nim_timeout_seconds: float = _env_float("KING_NIM_TIMEOUT_SECONDS", 45.0)
    nim_max_retries: int = _env_int("KING_NIM_MAX_RETRIES", 0)
    model_name: str = _env("NVIDIA_MODEL", "mistralai/ministral-14b-instruct-2512")
    model_fallbacks: str = _env("NVIDIA_FALLBACK_MODELS", "qwen/qwen3-next-80b-a3b-instruct,deepseek-ai/deepseek-v4-flash,meta/llama-4-maverick-17b-128e-instruct")
    llm_max_tokens: int = _env_int("KING_LLM_MAX_TOKENS", 700)
    llm_streaming_enabled: bool = _env_bool("KING_LLM_STREAMING_ENABLED", True)
    llm_streaming_hedge_enabled: bool = _env_bool("KING_LLM_STREAMING_HEDGE_ENABLED", False)
    llm_streaming_hedge_model: str = _env("KING_LLM_STREAMING_HEDGE_MODEL", "qwen/qwen3-next-80b-a3b-instruct")
    llm_streaming_hedge_mode: str = _env("KING_LLM_STREAMING_HEDGE_MODE", "stream")
    llm_streaming_hedge_delay_seconds: float = _env_float("KING_LLM_STREAMING_HEDGE_DELAY_SECONDS", 0.0)
    debug: bool = _env_bool("KING_DEBUG", False)
    tool_top_k: int = _env_int("KING_TOOL_TOP_K", 3)
    tool_similarity_threshold: float = _env_float("KING_TOOL_SIMILARITY_THRESHOLD", 0.23)
    tool_category_threshold: float = _env_float("KING_TOOL_CATEGORY_THRESHOLD", 0.30)
    # Utterance-based tool routing. Each tool's similarity is the max over a
    # bank of example user phrasings (utterances) instead of a single
    # description vector. The bank is markdown-owned so collisions are fixed by
    # editing utterances, not code.
    tool_utterances_file: str = _env("KING_TOOL_UTTERANCES_FILE", "tools/TOOL_UTTERANCES.md")
    tool_routing_categories_file: str = _env("KING_TOOL_ROUTING_CATEGORIES_FILE", "tools/TOOL_ROUTING_CATEGORIES.md")
    max_tool_rounds: int = _env_int("KING_MAX_TOOL_ROUNDS", 6)
    llm_stream_attempts: int = _env_int("KING_LLM_STREAM_ATTEMPTS", 2)
    typing_speed_seconds: float = _env_float("KING_TYPING_SPEED_SECONDS", 0.0)
    tool_call_retries: int = _env_int("KING_TOOL_CALL_RETRIES", 1)
    tool_argument_grounding_threshold: float = _env_float("KING_TOOL_ARGUMENT_GROUNDING_THRESHOLD", 0.35)
    backtick_tool_similarity_threshold: float = _env_float("KING_BACKTICK_TOOL_SIMILARITY_THRESHOLD", 0.65)
    direct_single_tool_result: bool = _env_bool("KING_DIRECT_SINGLE_TOOL_RESULT", True)
    terminal_default_timeout: int = _env_int("KING_TERMINAL_TIMEOUT", 30)
    terminal_max_timeout: int = _env_int("KING_TERMINAL_MAX_TIMEOUT", 300)
    external_request_attempts: int = _env_int("KING_EXTERNAL_REQUEST_ATTEMPTS", 2)
    external_retry_delay: float = _env_float("KING_EXTERNAL_RETRY_DELAY", 0.25)
    embedding_model: str = _env("NVIDIA_EMBEDDING_MODEL", "nvidia/nv-embed-v1")
    embedding_min_chars: int = _env_int("KING_EMBEDDING_MIN_CHARS", 7)
    query_embedding_cache_size: int = _env_int("KING_QUERY_EMBEDDING_CACHE_SIZE", 512)
    query_embedding_cache_file: str = _env("KING_QUERY_EMBEDDING_CACHE_FILE", "storage/query_embeddings.npy")
    query_embedding_texts_file: str = _env("KING_QUERY_EMBEDDING_TEXTS_FILE", "storage/query_texts.json")

    notes_file: str = _env("KING_NOTES_FILE", "storage/notes.json")
    storage_dir: str = _env("KING_STORAGE_DIR", "storage")
    memory_dir: str = _env("KING_MEMORY_DIR", "storage/memories")
    memory_backup_dir: str = _env("KING_MEMORY_BACKUP_DIR", "storage/memory_backups")
    memory_index_file: str = _env("KING_MEMORY_INDEX_FILE", "memory_index.json")
    memory_embeddings_file: str = _env("KING_MEMORY_EMBEDDINGS_FILE", "memory_embeddings.npy")
    memory_archive_file: str = _env("KING_MEMORY_ARCHIVE_FILE", "memory_archive.jsonl")
    memory_graph_file: str = _env("KING_MEMORY_GRAPH_FILE", "memory_graph.json")
    memory_graph_relations_file: str = _env("KING_MEMORY_GRAPH_RELATIONS_FILE", "memory/MEMORY_GRAPH_RELATIONS.md")
    memory_auto_relations_file: str = _env("KING_MEMORY_AUTO_RELATIONS_FILE", "memory/MEMORY_AUTO_RELATIONS.md")
    memory_filter_policy_file: str = _env("KING_MEMORY_FILTER_POLICY_FILE", "memory/MEMORY_FILTER_POLICY.md")
    memory_max_entries: int = _env_int("KING_MEMORY_MAX_ENTRIES", 2000)
    memory_importance_min: float = _env_float("KING_MEMORY_IMPORTANCE_MIN", 0.0)
    memory_importance_max: float = _env_float("KING_MEMORY_IMPORTANCE_MAX", 1.0)
    memory_similarity_threshold: float = _env_float("KING_MEMORY_SIMILARITY_THRESHOLD", 0.25)
    memory_winner_margin: float = _env_float("KING_MEMORY_WINNER_MARGIN", 0.08)
    memory_tier_min_coverage: float = _env_float("KING_MEMORY_TIER_MIN_COVERAGE", 1.0)
    memory_query_cache_size: int = _env_int("KING_MEMORY_QUERY_CACHE_SIZE", 32)
    memory_rebuild_batch_size: int = _env_int("KING_MEMORY_REBUILD_BATCH_SIZE", 64)
    memory_recall_k: int = _env_int("KING_MEMORY_RECALL_K", 5)
    memory_profile_limit: int = _env_int("KING_MEMORY_PROFILE_LIMIT", 8)
    memory_broad_recall_limit: int = _env_int("KING_MEMORY_BROAD_RECALL_LIMIT", 50)
    memory_rank_semantic_weight: float = _env_float("KING_MEMORY_RANK_SEMANTIC_WEIGHT", 0.7)
    memory_rank_importance_weight: float = _env_float("KING_MEMORY_RANK_IMPORTANCE_WEIGHT", 0.2)
    memory_rank_overlap_weight: float = _env_float("KING_MEMORY_RANK_OVERLAP_WEIGHT", 0.1)
    memory_unified_graph_weight: float = _env_float("KING_MEMORY_UNIFIED_GRAPH_WEIGHT", 0.35)
    memory_unified_expansion_hops: int = _env_int("KING_MEMORY_UNIFIED_EXPANSION_HOPS", 1)
    memory_inference_bridge_relations: str = _env("KING_MEMORY_INFERENCE_BRIDGE_RELATIONS", "crush")
    memory_inference_confidence_factor: float = _env_float("KING_MEMORY_INFERENCE_CONFIDENCE_FACTOR", 0.85)
    memory_profile_relation_priority: str = _env("KING_MEMORY_PROFILE_RELATION_PRIORITY", "name,lives_in,in_class,age,health_status,crush,likes,prefers,building,working_on,remembers")
    memory_graph_fallback_source: str = _env("KING_MEMORY_GRAPH_FALLBACK_SOURCE", "User")
    memory_graph_fallback_relation: str = _env("KING_MEMORY_GRAPH_FALLBACK_RELATION", "remembers")
    memory_graph_fallback_tier: str = _env("KING_MEMORY_GRAPH_FALLBACK_TIER", "semantic")
    memory_auto_relations_enabled: bool = _env_bool("KING_MEMORY_AUTO_RELATIONS_ENABLED", True)
    memory_obsidian_sync_enabled: bool = _env_bool("KING_MEMORY_OBSIDIAN_SYNC_ENABLED", True)
    memory_obsidian_sync_on_startup: bool = _env_bool("KING_MEMORY_OBSIDIAN_SYNC_ON_STARTUP", False)
    memory_obsidian_llm_pages_enabled: bool = _env_bool("KING_MEMORY_OBSIDIAN_LLM_PAGES_ENABLED", False)
    memory_obsidian_vault_dir: str = _env("KING_MEMORY_OBSIDIAN_VAULT_DIR", "C:/Users/anime/Documents/King Memory")
    memory_obsidian_graph_dir: str = _env("KING_MEMORY_OBSIDIAN_GRAPH_DIR", "Generated Memory Graph")
    memory_store_enabled: bool = _env_bool("KING_MEMORY_STORE_ENABLED", True)
    memory_store_notify: bool = _env_bool("KING_MEMORY_STORE_NOTIFY", False)
    memory_extraction_context_messages: int = _env_int("KING_MEMORY_EXTRACTION_CONTEXT_MESSAGES", 8)
    proactive_memory_context_enabled: bool = _env_bool("KING_PROACTIVE_MEMORY_CONTEXT_ENABLED", True)
    proactive_memory_context_margin: float = _env_float("KING_PROACTIVE_MEMORY_CONTEXT_MARGIN", 0.0)

    # Memory consolidation worker (nightly "god tier" maintenance)
    memory_consolidation_enabled: bool = _env_bool("KING_MEMORY_CONSOLIDATION_ENABLED", True)
    memory_consolidation_policy_file: str = _env("KING_MEMORY_CONSOLIDATION_POLICY_FILE", "memory/MEMORY_CONSOLIDATION.md")
    memory_consolidation_dedup_enabled: bool = _env_bool("KING_MEMORY_CONSOLIDATION_DEDUP_ENABLED", True)
    memory_consolidation_dedup_similarity: float = _env_float("KING_MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.86)
    memory_consolidation_dedup_max_pairs: int = _env_int("KING_MEMORY_CONSOLIDATION_DEDUP_MAX_PAIRS", 20)
    memory_consolidation_insights_enabled: bool = _env_bool("KING_MEMORY_CONSOLIDATION_INSIGHTS_ENABLED", True)
    memory_consolidation_insight_min_cluster: int = _env_int("KING_MEMORY_CONSOLIDATION_INSIGHT_MIN_CLUSTER", 3)
    memory_consolidation_insight_max: int = _env_int("KING_MEMORY_CONSOLIDATION_INSIGHT_MAX", 5)
    memory_consolidation_insight_importance: float = _env_float("KING_MEMORY_CONSOLIDATION_INSIGHT_IMPORTANCE", 0.75)
    memory_consolidation_decay_enabled: bool = _env_bool("KING_MEMORY_CONSOLIDATION_DECAY_ENABLED", True)
    memory_consolidation_decay_after_days: int = _env_int("KING_MEMORY_CONSOLIDATION_DECAY_AFTER_DAYS", 45)
    memory_consolidation_decay_rate: float = _env_float("KING_MEMORY_CONSOLIDATION_DECAY_RATE", 0.05)
    memory_consolidation_decay_floor: float = _env_float("KING_MEMORY_CONSOLIDATION_DECAY_FLOOR", 0.1)
    memory_consolidation_max_tokens: int = _env_int("KING_MEMORY_CONSOLIDATION_MAX_TOKENS", 600)

    # Memory-driven scheduler bridge (nightly): surface time-bound intentions as nudges
    memory_scheduler_bridge_enabled: bool = _env_bool("KING_MEMORY_SCHEDULER_BRIDGE_ENABLED", True)
    memory_scheduler_bridge_lookback: int = _env_int("KING_MEMORY_SCHEDULER_BRIDGE_LOOKBACK", 40)
    memory_scheduler_bridge_recent_days: int = _env_int("KING_MEMORY_SCHEDULER_BRIDGE_RECENT_DAYS", 21)
    memory_scheduler_bridge_max_nudges: int = _env_int("KING_MEMORY_SCHEDULER_BRIDGE_MAX_NUDGES", 5)
    memory_scheduler_bridge_max_tokens: int = _env_int("KING_MEMORY_SCHEDULER_BRIDGE_MAX_TOKENS", 600)
    memory_scheduler_bridge_default_hour: int = _env_int("KING_MEMORY_SCHEDULER_BRIDGE_DEFAULT_HOUR", 9)

    # Vector store (FAISS) settings
    vector_store_index_path: str = _env("KING_VECTOR_STORE_INDEX_PATH", "storage/memories/vector_index.faiss")
    vector_store_metadata_path: str = _env("KING_VECTOR_STORE_METADATA_PATH", "storage/memories/vector_metadata.json")
    vector_store_dim: int = _env_int("KING_VECTOR_STORE_DIM", 384)

    # Conversation summary persistence
    summaries_path: str = _env("KING_SUMMARIES_PATH", "storage/summaries.json")
    summaries_max_count: int = _env_int("KING_SUMMARIES_MAX_COUNT", 10)
    summaries_max_context: int = _env_int("KING_SUMMARIES_MAX_CONTEXT", 3)

    # Session-based memory: full per-session transcripts + cross-session context
    session_store_enabled: bool = _env_bool("KING_SESSION_STORE_ENABLED", True)
    session_store_dir: str = _env("KING_SESSION_STORE_DIR", "storage/sessions")
    session_index_file: str = _env("KING_SESSION_INDEX_FILE", "storage/sessions/index.json")
    session_keep_count: int = _env_int("KING_SESSION_KEEP_COUNT", 200)
    session_context_count: int = _env_int("KING_SESSION_CONTEXT_COUNT", 2)
    session_context_max_chars: int = _env_int("KING_SESSION_CONTEXT_MAX_CHARS", 1800)
    session_digest_enabled: bool = _env_bool("KING_SESSION_DIGEST_ENABLED", True)
    session_digest_max_tokens: int = _env_int("KING_SESSION_DIGEST_MAX_TOKENS", 900)
    session_digest_min_turns: int = _env_int("KING_SESSION_DIGEST_MIN_TURNS", 2)
    session_digest_relations_file: str = _env("KING_SESSION_DIGEST_RELATIONS_FILE", "memory/MEMORY_SESSION_RELATIONS.md")

    sarvam_api_key: str = _env("SARVAM_API_KEY", "")
    tavily_api_key: str = _env("TAVILY_API_KEY", "")
    voice_enabled: bool = _env_bool("KING_VOICE_ENABLED", False)
    voice_language: str = _env("KING_VOICE_LANGUAGE", "en-IN")
    chat_polish_policy_file: str = _env("KING_CHAT_POLISH_POLICY_FILE", "memory/CHAT_POLISH_POLICY.md")
    tool_grounding_policy_file: str = _env("KING_TOOL_GROUNDING_POLICY_FILE", "tools/TOOL_GROUNDING_POLICY.md")
    context_followup_tools: str = _env("KING_CONTEXT_FOLLOWUP_TOOLS", "web_search,web_fetch,hackernews,reddit")
    context_followup_expansion_enabled: bool = _env_bool("KING_CONTEXT_FOLLOWUP_EXPANSION_ENABLED", False)
    capability_routing_file: str = _env("KING_CAPABILITY_ROUTING_FILE", "tools/CAPABILITY_ROUTING.md")
    progressive_disclosure_enabled: bool = _env_bool("KING_PROGRESSIVE_DISCLOSURE_ENABLED", True)
    progressive_disclosure_tools: str = _env("KING_PROGRESSIVE_DISCLOSURE_TOOLS", "find_tools,load_tool")
    tool_result_max_chars: int = _env_int("KING_TOOL_RESULT_MAX_CHARS", 8000)
    grounding_retry_without_tools: bool = _env_bool("KING_GROUNDING_RETRY_WITHOUT_TOOLS", True)
    local_system_action_min_score: float = _env_float("KING_LOCAL_SYSTEM_ACTION_MIN_SCORE", 0.05)
    incomplete_utterance_max_terms: int = _env_int("KING_INCOMPLETE_UTTERANCE_MAX_TERMS", 2)
    finalize_tool_results_with_llm: bool = _env_bool("KING_FINALIZE_TOOL_RESULTS_WITH_LLM", False)
    direct_single_tool_result: bool = _env_bool("KING_DIRECT_SINGLE_TOOL_RESULT", True)
    file_generating_tools: str = _env("KING_FILE_GENERATING_TOOLS", "imagine,web_scrape,download,generate,create,gallery")
    
    images_dir: str = _env("KING_IMAGES_DIR", "storage/images")
    browser_targets_file: str = _env("KING_BROWSER_TARGETS_FILE", "tools/BROWSER_TARGETS.md")
    browser_dom_policy_file: str = _env("KING_BROWSER_DOM_POLICY_FILE", "tools/BROWSER_DOM_POLICY.md")
    system_controls_file: str = _env("KING_SYSTEM_CONTROLS_FILE", "tools/SYSTEM_CONTROLS.md")
    keyboard_shortcuts_file: str = _env("KING_KEYBOARD_SHORTCUTS_FILE", "tools/KEYBOARD_SHORTCUTS.md")
    browser_auth_dir: str = _env("KING_BROWSER_AUTH_DIR", "storage/browser_auth")
    browser_default_timeout_ms: int = _env_int("KING_BROWSER_DEFAULT_TIMEOUT_MS", 15000)
    browser_login_timeout_ms: int = _env_int("KING_BROWSER_LOGIN_TIMEOUT_MS", 180000)
    browser_login_timeout_max_ms: int = _env_int("KING_BROWSER_LOGIN_TIMEOUT_MAX_MS", 600000)
    browser_max_text_chars: int = _env_int("KING_BROWSER_MAX_TEXT_CHARS", 12000)
    navigator_geocode_url: str = _env("KING_NAVIGATOR_GEOCODE_URL", "https://nominatim.openstreetmap.org/search")
    navigator_reverse_url: str = _env("KING_NAVIGATOR_REVERSE_URL", "https://nominatim.openstreetmap.org/reverse")
    navigator_route_url: str = _env("KING_NAVIGATOR_ROUTE_URL", "https://router.project-osrm.org")
    navigator_user_agent: str = _env("KING_NAVIGATOR_USER_AGENT", "KING Navigator Tool")
    navigator_default_mode: str = _env("KING_NAVIGATOR_DEFAULT_MODE", "driving")
    navigator_default_timeout_ms: int = _env_int("KING_NAVIGATOR_DEFAULT_TIMEOUT_MS", 12000)
    navigator_route_place_samples: int = _env_int("KING_NAVIGATOR_ROUTE_PLACE_SAMPLES", 4)
    camera_vision_model: str = _env("KING_CAMERA_VISION_MODEL", "nvidia/nemotron-nano-12b-v2-vl")
    camera_vision_fallback_models: str = _env(
        "KING_CAMERA_VISION_FALLBACK_MODELS",
        "nvidia/llama-3.1-nemotron-nano-vl-8b-v1,meta/llama-3.2-11b-vision-instruct,microsoft/phi-4-multimodal-instruct",
    )
    camera_vision_max_tokens: int = _env_int("KING_CAMERA_VISION_MAX_TOKENS", 260)
    camera_max_image_bytes: int = _env_int("KING_CAMERA_MAX_IMAGE_BYTES", 4500000)
    camera_default_timeout_ms: int = _env_int("KING_CAMERA_DEFAULT_TIMEOUT_MS", 25000)
    composio_policy_file: str = _env("KING_COMPOSIO_POLICY_FILE", "tools/COMPOSIO_GATEWAY.md")
    composio_api_key: str = _env("COMPOSIO_API_KEY", "")
    composio_base_url: str = _env("KING_COMPOSIO_BASE_URL", "https://backend.composio.dev/api/v3.1")
    composio_user_id: str = _env("KING_COMPOSIO_USER_ID", "king-local-user")
    composio_session_id: str = _env("KING_COMPOSIO_SESSION_ID", "")
    composio_default_timeout_ms: int = _env_int("KING_COMPOSIO_DEFAULT_TIMEOUT_MS", 20000)
    verification_pipeline_file: str = _env("KING_VERIFICATION_PIPELINE_FILE", "tools/TOOL_VERIFICATION_PIPELINE.md")
    verification_pipeline_max_steps: int = _env_int("KING_VERIFICATION_PIPELINE_MAX_STEPS", 8)
    verification_pipeline_timeout_ms: int = _env_int("KING_VERIFICATION_PIPELINE_TIMEOUT_MS", 180000)
    verification_pipeline_timeout_max_ms: int = _env_int("KING_VERIFICATION_PIPELINE_TIMEOUT_MAX_MS", 600000)
    verification_pipeline_output_chars: int = _env_int("KING_VERIFICATION_PIPELINE_OUTPUT_CHARS", 4000)
    folder_watcher_config_file: str = _env("KING_FOLDER_WATCHER_CONFIG_FILE", "tools/FOLDER_WATCHER_CONFIG.md")
    folder_watcher_llm_policy_file: str = _env("KING_FOLDER_WATCHER_LLM_POLICY_FILE", "tools/FOLDER_WATCHER_LLM_POLICY.md")
    folder_watcher_client_file: str = _env("KING_FOLDER_WATCHER_CLIENT_FILE", "tools/FOLDER_WATCHER_CLIENT.md")
    folder_watcher_target: str = _env("KING_FOLDER_WATCHER_TARGET", "")
    folder_watcher_base_url: str = _env("KING_FOLDER_WATCHER_BASE_URL", "")
    folder_watcher_auth_token: str = _env("KING_FOLDER_WATCHER_AUTH_TOKEN", "")
    folder_watcher_timeout_ms: int = _env_int("KING_FOLDER_WATCHER_TIMEOUT_MS", 0)
    telegram_watcher_config_file: str = _env("KING_TELEGRAM_CONFIG_FILE", "tools/TELEGRAM_WATCHER_CONFIG.md")
    browser_user_agent: str = _env("KING_BROWSER_USER_AGENT", "Mozilla/5.0 KING Browser Extractor")

    # Daily maintenance + scheduler
    maintenance_config_file: str = _env("KING_MAINTENANCE_CONFIG_FILE", "tools/MAINTENANCE_DAILY.md")
    maintenance_state_path: str = _env("KING_MAINTENANCE_STATE_PATH", "storage/maintenance_state.json")
    maintenance_log_path: str = _env("KING_MAINTENANCE_LOG_PATH", "storage/maintenance_log.jsonl")
    maintenance_log_max_runs: int = _env_int("KING_MAINTENANCE_LOG_MAX_RUNS", 90)
    scheduler_config_file: str = _env("KING_SCHEDULER_CONFIG_FILE", "tools/SCHEDULER_CONFIG.md")
    scheduler_store_path: str = _env("KING_SCHEDULER_STORE_PATH", "storage/scheduler_store.json")
    scheduler_log_path: str = _env("KING_SCHEDULER_LOG_PATH", "storage/scheduler_log.jsonl")
    scheduler_check_interval_seconds: int = _env_int("KING_SCHEDULER_CHECK_INTERVAL_SECONDS", 30)

    # Cognition substrate (situation, cadence, episodes, proactive)
    cognition_config_file: str = _env("KING_COGNITION_CONFIG_FILE", "cognition/COGNITION_CONFIG.md")
    cognition_state_path: str = _env("KING_COGNITION_STATE_PATH", "storage/cognition_state.json")

    # Autonomous project manager
    project_manager_config_file: str = _env("KING_PROJECT_MANAGER_CONFIG_FILE", "tools/PROJECT_MANAGER_CONFIG.md")
    project_store_path: str = _env("KING_PROJECT_STORE_PATH", "storage/projects.json")
    project_log_path: str = _env("KING_PROJECT_LOG_PATH", "storage/project_log.jsonl")


settings = Settings()
