"""Pydantic data models for Ares."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class FactCategory(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    BELIEF = "belief"
    HABIT = "habit"
    RELATIONSHIP = "relationship"
    NOTE = "note"


class Memory(BaseModel):
    fact_id: Optional[int] = None
    fact_text: str
    category: FactCategory = FactCategory.NOTE
    confidence: float = 1.0
    importance: float = 0.5
    source: str = "conversation"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_accessed: Optional[str] = None
    access_count: int = 0
    superseded_by: Optional[int] = None


class ConversationMessage(BaseModel):
    id: Optional[int] = None
    conversation_id: Optional[int] = None
    role: str
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    timestamp: Optional[str] = None


class VoiceConfig(BaseModel):
    """Voice settings for continuous voice mode."""

    enabled: bool = False
    tts_provider: str = "edge_tts"
    tts_voice: str = ""
    hotkey: str = "space"
    stt_model: str = "tiny"
    sarvam_api_key: str = ""
    sarvam_tts_model: str = "bulbul:v2"
    sarvam_language_code: str = "hi-IN"


class AppConfig(BaseModel):
    model: str = "deepseek-v4-flash-free"
    api_key: str = ""
    api_base_url: str = "https://opencode.ai/zen/v1"
    max_context_messages: int = 20
    max_memory_retrieval: int = 5
    data_dir: str = "~/.ares/data"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_backend: str = "onnx"
    embedding_provider: str = "CPUExecutionProvider"
    embedding_file_name: str = ""
    reminder_poll_seconds: int = 30
    enable_desktop_notifications: bool = True
    session_summary_messages: int = 2
    web_search_provider: str = "auto"
    tavily_api_key: str = ""
    tavily_search_depth: str = "basic"
    soul_path: str = ""
    profile_path: str = ""
    project_context_enabled: bool = True
    context_token_budget: int = 2000
    project_context_max_files: int = 2
    agent_max_iterations: int = 20
    context_compact_threshold: float = 0.90
    context_protected_tail: int = 20
    tool_output_max_chars: int = 500
    memory_dedup_threshold: float = 0.3
    memory_stale_days: int = 90
    memory_extract_enabled: bool = True
    memory_cleanup_enabled: bool = True
    skills_enabled: bool = True
    skill_dirs: list[str] = Field(default_factory=lambda: ["~/.ares/skills"])
    skill_auto_suggest: bool = True
    mcp_servers: list[dict] = Field(
        default_factory=list,
        description="MCP server configurations for remote Model Context Protocol tools.",
    )
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    cron_enabled: bool = True
    cron_tick_seconds: int = 60
    cron_max_concurrent: int = 3
    cron_max_iterations: int = 10
    cron_log_retention_days: int = 90


# ── v2: Task States ──────────────────────────────────────────


class TaskState(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TRANSITIONS = {
    "queued":      ["planning", "cancelled"],
    "planning":    ["running", "failed", "cancelled"],
    "running":     ["completed", "retrying", "failed", "cancelled"],
    "retrying":    ["running", "failed", "cancelled"],
    "completed":   [],
    "failed":      ["queued"],
    "cancelled":   ["queued"],
}
