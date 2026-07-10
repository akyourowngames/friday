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


DEFAULT_MCP_SERVERS: list[dict] = [
    {
        "name": "playwright",
        "transport": "stdio",
        "command": "npx",
        "args": [
            "@playwright/mcp@latest",
            "--browser", "chrome",
            "--caps", "vision,devtools",
            "--user-data-dir", "~/.ares/data/playwright-profile",
            "--viewport-size", "1280x720",
        ],
    },
    {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
    },
    {
        "name": "fetch",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    },
    {
        "name": "windows",
        "transport": "stdio",
        "command": "uvx",
        "args": [
            "windows-mcp",
            "serve",
            "--tools",
            "Screenshot,Snapshot,Click,Type,Scroll,Move,Shortcut,Wait,WaitFor,App,Clipboard,Notification",
        ],
        "env": {
            "ANONYMIZED_TELEMETRY": "false",
            "WINDOWS_MCP_SCREENSHOT_SCALE": "0.5",
            "WINDOWS_MCP_DISABLE_FLASH": "true",
        },
        "timeout_seconds": 90.0,
    },
]


class VoiceConfig(BaseModel):
    """Voice settings for continuous voice mode."""

    enabled: bool = False
    stt_backend: str = "auto"
    tts_backend: str = "auto"
    tts_voice: str = "en-US-JennyNeural"
    stt_model: str = "small"
    stt_language: str = ""
    mic_device: int | str | None = None
    min_utterance_ms: int = 650
    silence_timeout_ms: int = 700
    max_utterance_seconds: float = 20.0
    start_speech_frames: int = 5
    min_voiced_ms: int = 250
    min_audio_rms: float = 0.004
    barge_in_enabled: bool = False
    post_speech_cooldown_ms: int = 1200
    tts_sample_rate: int = 24000
    tts_volume: float = 1.6
    sarvam_stt_model: str = "saaras:v3"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_language_code: str = "en-IN"
    sarvam_speaker: str = "shubh"
    sarvam_pace: float = 1.0
    voice_max_history: int = 10
    voice_max_memories: int = 3


class PhoneConfig(BaseModel):
    """Android phone bridge settings."""

    enabled: bool = False
    kdeconnect_device_id: str = ""
    adb_device_address: str = ""
    store_notification_content: bool = False
    kdeconnect_cli_path: str = ""   # auto-detected if empty
    adb_path: str = ""              # auto-detected if empty


class TelegramConfig(BaseModel):
    """Configuration for Ares' local Telegram channel.

    The channel is intentionally disabled and locked down by default.  A bot
    token is best supplied through ``ARES_TELEGRAM_BOT_TOKEN`` so it does not
    have to live in the shared config file.
    """

    enabled: bool = False
    bot_token: str = ""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    allow_group_chats: bool = False
    poll_timeout_seconds: int = Field(default=30, ge=1, le=50)
    show_tool_progress: bool = True
    max_attachment_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    max_outbound_file_bytes: int = Field(default=50 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    audio_transcription_enabled: bool = True
    audio_stt_backend: str = "auto"  # auto: Sarvam when configured, otherwise local Whisper
    audio_stt_model: str = "small"
    max_audio_duration_seconds: int = Field(default=600, ge=1, le=7200)


class AppConfig(BaseModel):
    # This is deliberately stored beside the rest of the shared Ares config.
    # Both the Electron app and CLI read this file, so completing setup in one
    # surface never triggers the first-run flow in the other.
    onboarding_completed: bool = False
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
    # Desktop control needs an observe-act-verify loop. Twenty turns is too
    # small for common multi-step tasks such as launching an app and saving.
    agent_max_iterations: int = 40
    context_compact_threshold: float = 0.90
    context_protected_tail: int = 20
    tool_output_max_chars: int = 500
    memory_dedup_threshold: float = 0.3
    memory_stale_days: int = 90
    memory_session_scope: int = 3  # Search current + N recent sessions
    memory_extract_enabled: bool = True
    memory_cleanup_enabled: bool = True
    skills_enabled: bool = True
    skill_dirs: list[str] = Field(default_factory=lambda: ["~/.ares/skills"])
    skill_auto_suggest: bool = True
    mcp_servers: list[dict] = Field(
        default_factory=lambda: [s.copy() for s in DEFAULT_MCP_SERVERS],
        description="MCP server configurations for remote Model Context Protocol tools.",
    )
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    phone: PhoneConfig = Field(default_factory=PhoneConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    cron_enabled: bool = True
    cron_tick_seconds: int = 60
    cron_max_concurrent: int = 3
    cron_max_iterations: int = 10
    cron_log_retention_days: int = 90
