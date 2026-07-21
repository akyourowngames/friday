"""Pydantic data models for Ares."""

from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


class FactCategory(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    BELIEF = "belief"
    HABIT = "habit"
    RELATIONSHIP = "relationship"
    NOTE = "note"


class TaskState(str, Enum):
    """Lifecycle states for durable multi-step workflows."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset({
        TaskState.AWAITING_CONFIRMATION,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    }),
    TaskState.AWAITING_CONFIRMATION: frozenset({TaskState.PENDING, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset({TaskState.PENDING, TaskState.CANCELLED}),
    TaskState.CANCELLED: frozenset(),
}


class TaskStep(BaseModel):
    """One explicit, serializable workflow step."""

    step_id: str
    tool_name: str
    arguments: dict = Field(default_factory=dict)
    description: str = ""
    verify: dict | None = None


class Task(BaseModel):
    """Public representation of a durable task persisted by ``TaskStore``."""

    task_id: str
    goal: str
    plan: list[TaskStep] = Field(default_factory=list)
    status: TaskState = TaskState.PENDING
    created_at: str
    updated_at: str
    result_summary: str = ""
    related_person_ids: list[int] = Field(default_factory=list)
    related_action_ids: list[int] = Field(default_factory=list)
    session_id: str | None = None
    revision: int = 1


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
            "@playwright/mcp@0.0.78",
            "--browser", "chrome",
            "--caps", "vision,devtools",
            # MCP arguments bypass the shell, so this must be a real absolute
            # path rather than a literal ``~`` directory.
            "--user-data-dir", str(Path("~/.ares/data/playwright-profile").expanduser()),
            "--viewport-size", "1280x720",
        ],
        "timeout_seconds": 90.0,  # Increased timeout for browser operations
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
            # `windows-mcp` receives UI Automation text directly from desktop
            # applications.  Load Ares' narrowly-scoped compatibility hook in
            # that subprocess so one malformed UTF-16 surrogate cannot crash
            # its stdio JSON writer (observed with Telegram's UI tree).
            "ARES_WINDOWS_MCP_COMPAT": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parent),
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
    # Keep the end-of-turn detector responsive without accepting tiny clicks.
    min_utterance_ms: int = 350
    silence_timeout_ms: int = 420
    max_utterance_seconds: float = 20.0
    start_speech_frames: int = 3
    min_voiced_ms: int = 180
    min_audio_rms: float = 0.004
    barge_in_enabled: bool = True
    barge_in_delay_ms: int = 350
    barge_in_min_voiced_ms: int = 300
    post_speech_cooldown_ms: int = 120
    tts_chunk_chars: int = 90
    tts_sample_rate: int = 24000
    tts_volume: float = 1.6
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


class DesktopConfig(BaseModel):
    """Configuration for the desktop voice assistant mode."""

    enabled: bool = False
    hotkey_ptt: str = "ctrl+space"
    hotkey_mute: str = "ctrl+shift+m"
    hotkey_window: str = "ctrl+shift+h"
    window_x: int = -1
    window_y: int = -1
    window_opacity: float = 0.85
    auto_hide_seconds: int = 3
    history_size: int = 5


class TelephonyConfig(BaseModel):
    """Provider-backed telephone voice settings.

    Values may be supplied through the local config file or environment
    variables (``TWILIO_*``).  Secret-bearing fields are automatically
    redacted by Ares exports and diagnostics.
    """

    enabled: bool = False
    provider: Literal["twilio"] = "twilio"
    account_sid: str = ""
    auth_token: str = ""
    phone_number: str = ""
    public_base_url: str = ""
    voice_webhook_path: str = "/telephony/twilio/voice"
    status_webhook_path: str = "/telephony/twilio/status"
    media_stream_url: str = ""
    realtime_model: str = ""
    voice: str = ""
    language: str = "en-US"
    microphone_device: str = ""
    speaker_device: str = ""
    voice_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    estimated_cost_per_minute_usd: float = Field(default=0.013, ge=0.0, le=100.0)
    store_recordings: bool = False
    require_confirmation_for_unknown_numbers: bool = True
    response_timeout_seconds: float = Field(default=20.0, ge=1.0, le=90.0)


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
    audio_stt_backend: str = "auto"  # auto: local Whisper
    audio_stt_model: str = "small"
    max_audio_duration_seconds: int = Field(default=600, ge=1, le=7200)


class WatcherDashboardConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class WorkspaceConfig(BaseModel):
    """Local web workspace served by the unified Ares runtime."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)


class WatcherDefaultsConfig(BaseModel):
    interval_seconds: int = Field(default=900, ge=20, le=31_536_000)
    ai_action: Literal["notify", "suggest", "auto"] = "notify"
    timeout: int = Field(default=30, ge=1, le=120)
    max_retries: int = Field(default=3, ge=0, le=20)


class WatcherConfig(BaseModel):
    """Shared watcher service configuration for CLI and dashboard runtimes."""

    enabled: bool = True
    database_path: str = "~/.ares/data/watchers.db"
    poll_seconds: float = Field(default=5.0, ge=0.5, le=300)
    max_concurrency: int = Field(default=8, ge=1, le=100)
    tool_monitors_enabled: bool = True
    allow_mutating_tool_steps: bool = False
    max_tool_steps: int = Field(default=8, ge=1, le=25)
    max_tool_output_chars: int = Field(default=2_000_000, ge=1_000, le=10_000_000)
    dashboard: WatcherDashboardConfig = Field(default_factory=WatcherDashboardConfig)
    notifications: dict[str, dict] = Field(default_factory=lambda: {
        "telegram": {"enabled": False, "chat_id": ""},
        "desktop": {"enabled": True},
        "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "to_address": ""},
        "webhook": {"enabled": False, "url": ""},
    })
    defaults: WatcherDefaultsConfig = Field(default_factory=WatcherDefaultsConfig)


class VisionConfig(BaseModel):
    """Local-first visual observation controls.

    Consent is still tracked per source by the vision service.  These defaults
    make a new installation conservative: camera and screen capture remain
    unavailable until the user explicitly grants observation permission.
    """

    enabled: bool = True
    camera_enabled: bool = False
    screen_enabled: bool = False
    detection_interval_frames: int = Field(default=5, ge=1, le=120)
    motion_threshold: float = Field(default=0.025, ge=0.0, le=1.0)
    max_frame_width: int = Field(default=1280, ge=160, le=7680)
    default_watch_interval_seconds: float = Field(default=3.0, ge=0.25, le=3600.0)
    event_cooldown_seconds: float = Field(default=5.0, ge=0.0, le=3600.0)
    snapshot_history: int = Field(default=2, ge=2, le=100)
    verification_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    retain_event_frames: bool = False
    frame_retention_minutes: int = Field(default=0, ge=0, le=43_200)
    detector_model: str = "yolo26n.pt"


class MemoryCaptureConfig(BaseModel):
    """Automatic durable-memory capture policy.

    The legacy session-end extractor remains importable for compatibility, but
    durable reflection is the only automatic writer by default.
    """

    legacy_extractor_enabled: bool = False
    explicit_remember_fast_path: bool = True


class MemoryRetrievalConfig(BaseModel):
    """Bounded hybrid retrieval and active-recall controls."""

    query_rewrite_enabled: bool = True
    active_judge_enabled: bool = True
    foreground_model_calls_enabled: bool = False
    background_embedding_warmup: bool = True
    vector_weight: float = Field(default=0.55, ge=0.0, le=1.0)
    keyword_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    metadata_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    mmr_enabled: bool = True
    mmr_lambda: float = Field(default=0.70, ge=0.0, le=1.0)
    temporal_decay_enabled: bool = True
    max_candidates: int = Field(default=40, ge=5, le=200)
    max_injected: int = Field(default=5, ge=1, le=10)
    timeout_seconds: float = Field(default=5.0, ge=0.1, le=30.0)


class MemoryPromotionConfig(BaseModel):
    """Automatic observation-to-durable promotion scoring controls."""

    enabled: bool = True
    min_occurrences: int = Field(default=2, ge=1, le=20)
    min_unique_sessions: int = Field(default=2, ge=1, le=20)
    reference_score: float = Field(default=0.72, ge=0.0, le=1.0)


class MemorySelfImprovementConfig(BaseModel):
    """Hermes-inspired reviewed procedural-learning controls."""

    enabled: bool = True
    approval_required: bool = True
    max_active: int = Field(default=100, ge=1, le=1_000)


class MemoryConfig(BaseModel):
    """Ares Memory V3 configuration."""

    enabled: bool = True
    capture: MemoryCaptureConfig = Field(default_factory=MemoryCaptureConfig)
    retrieval: MemoryRetrievalConfig = Field(default_factory=MemoryRetrievalConfig)
    promotion: MemoryPromotionConfig = Field(default_factory=MemoryPromotionConfig)
    self_improvement: MemorySelfImprovementConfig = Field(
        default_factory=MemorySelfImprovementConfig
    )


class ReflectionConfig(BaseModel):
    """Background conversation-to-state extraction controls."""

    enabled: bool = True
    model: str = "mimo-v2.5-free"
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    completion_min_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    timeout_seconds: int = Field(default=45, ge=5, le=180)
    max_attempts: int = Field(default=3, ge=1, le=10)
    idle_delay_seconds: float = Field(default=0.35, ge=0.0, le=10.0)
    follow_up_delay_hours: int = Field(default=24, ge=0, le=8_760)
    follow_up_cooldown_hours: int = Field(default=72, ge=1, le=8_760)
    local_timezone: str = Field(
        default="",
        description="IANA timezone for interpreting reflection follow-up schedules; empty uses the system timezone.",
    )


class ProactiveConfig(BaseModel):
    """Initiative worker preferences and anti-spam boundaries."""

    enabled: bool = True
    poll_seconds: int = Field(default=900, ge=30, le=86_400)
    inactive_goal_days: int = Field(default=3, ge=1, le=365)
    due_soon_days: int = Field(default=7, ge=0, le=365)
    inactive_commitment_days: int = Field(default=3, ge=1, le=365)
    min_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    reminder_cooldown_hours: int = Field(default=72, ge=1, le=8_760)
    decision_cooldown_hours: int = Field(default=24, ge=1, le=8_760)
    failed_delivery_retry_hours: int = Field(default=1, ge=1, le=168)
    initiative_context_token_budget: int = Field(default=1_800, ge=400, le=8_000)
    decision_timeout_seconds: int = Field(default=30, ge=5, le=180)
    max_messages_per_day: int = Field(default=1, ge=0, le=20)
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"
    workspace_enabled: bool = True
    desktop_enabled: bool = True
    telegram_enabled: bool = False


class MultiAgentRoleOverride(BaseModel):
    """Optional local override for one native specialist role."""

    enabled: bool = True
    model: str | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=200)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1000000)
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=3600.0)
    allowed_tools: list[str] | None = None
    can_mutate: bool | None = None
    can_delegate: bool | None = None
    capabilities: list[str] | None = None
    retry_limit: int | None = Field(default=None, ge=0, le=5)
    retry_backoff_seconds: float | None = Field(default=None, ge=0.0, le=30.0)
    fallback_models: list[str] | None = None


class MultiAgentConfig(BaseModel):
    """Conservative limits for Ares' native supervisor runtime."""

    enabled: bool = True
    max_parallel_agents: int = Field(default=3, ge=1, le=16)
    max_tasks_per_run: int = Field(default=8, ge=1, le=32)
    default_timeout_seconds: float = Field(default=120.0, ge=1.0, le=3600.0)
    max_timeout_seconds: float = Field(default=600.0, ge=1.0, le=3600.0)
    max_total_duration_seconds: float = Field(default=900.0, ge=1.0, le=14400.0)
    max_total_iterations: int = Field(default=80, ge=1, le=1000)
    max_total_tokens: int = Field(default=120000, ge=256, le=4000000)
    max_retries_per_task: int = Field(default=1, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=30.0)
    max_depth: int = Field(default=1, ge=0, le=4)
    allow_recursive_delegation: bool = False
    require_review_for_mutations: bool = True
    review_role: str = "reviewer"
    persist_runs: bool = True
    retention_days: int = Field(default=30, ge=1, le=3650)
    stream_progress: bool = True
    role_overrides: dict[str, MultiAgentRoleOverride] = Field(default_factory=dict)
    model_overrides_by_role: dict[str, str] = Field(default_factory=dict)
    fallback_models_by_role: dict[str, list[str]] = Field(default_factory=dict)
    partial_result_synthesis: bool = True
    checkpoint_runs: bool = True
    action_grant_ttl_seconds: float = Field(default=300.0, ge=1.0, le=3600.0)
    provider_max_concurrency: int = Field(default=0, ge=0, le=64)
    builder_worktree_isolation: bool = True
    builder_worktree_root: str = "~/.ares/agent-worktrees"
    # A review verdict is evidence, not authority to modify the caller's
    # checkout. A separately-issued exact patch grant is also required when
    # this opt-in is enabled.
    auto_apply_builder_patches: bool = False
    tool_operation_timeout_seconds: float = Field(default=120.0, ge=1.0, le=3600.0)
    tool_cancel_grace_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    cancel_active_on_disable: bool = False


class SkillRegistry(BaseModel):
    """A configured, trusted source of community SKILL.md bundles.

    Registry tokens intentionally live only in the shared local config.  The
    marketplace never reads tokens from downloaded skills or manifests.
    """

    name: str
    api_base: str
    enabled: bool = True
    auth_token: str = ""
    priority: int = 0
    search_limit: int = Field(default=10, ge=1, le=100)


class MCPRegistry(BaseModel):
    """A configured, trusted source of MCP server metadata."""

    name: str
    api_base: str
    enabled: bool = True
    auth_token: str = ""
    priority: int = 0


class SkillDependency(BaseModel):
    """One declared dependency discovered in a skill's frontmatter."""

    type: Literal["mcp_server", "tool", "skill"] = "mcp_server"
    name: str
    required: bool = True
    auto_install: bool = False


def default_skill_registries() -> list[SkillRegistry]:
    """Return the built-in community skill registry configuration.

    ``openclaw`` remains configurable for compatible/private deployments.  The
    public ClawHub registry is the primary OpenClaw skills catalog.
    """

    return [
        SkillRegistry(
            name="clawhub",
            api_base="https://clawhub.ai/api/v1",
            priority=10,
        ),
        SkillRegistry(
            name="openclaw",
            api_base="https://api.openclaw.ai/v1",
            priority=5,
        ),
    ]


def default_mcp_registries() -> list[MCPRegistry]:
    """Return safe defaults for the public MCP metadata registries."""

    return [
        MCPRegistry(
            name="mcp-registry",
            api_base="https://registry.modelcontextprotocol.io",
            priority=10,
        ),
        # Smithery's current public Registry API is served from this host.
        MCPRegistry(
            name="smithery",
            api_base="https://api.smithery.ai",
            priority=5,
        ),
    ]


class AppConfig(BaseModel):
    # This is deliberately stored beside the rest of the shared Ares config.
    # Every Ares surface reads this shared file, so completing setup once
    # never triggers another first-run flow.
    onboarding_completed: bool = False
    provider: str = "opencode"
    provider_api_keys: dict[str, str] = Field(default_factory=dict)
    model: str = "deepseek-v4-flash-free"
    fast_conversation_enabled: bool = True
    fast_conversation_model: str = "mimo-v2.5-free"
    api_key: str = ""
    api_base_url: str = "https://opencode.ai/zen/v1"
    copilot_github_token: str = ""
    copilot_oauth_client_id: str = ""
    copilot_oauth_callback_url: str = "http://127.0.0.1:8765/copilot/oauth/callback"
    max_context_messages: int = 20
    max_memory_retrieval: int = 5
    data_dir: str = "~/.ares/data"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_backend: str = "onnx"
    embedding_provider: str = "CPUExecutionProvider"
    embedding_file_name: str = ""
    # Interactive turns must not stall while Hugging Face retries a model
    # download. Set this true only when deliberately provisioning a model.
    embedding_allow_download: bool = False
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
    agent_max_iterations: int = 80
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
    skill_registries: list[SkillRegistry] = Field(default_factory=default_skill_registries)
    mcp_registries: list[MCPRegistry] = Field(default_factory=default_mcp_registries)
    mcp_servers: list[dict] = Field(
        default_factory=lambda: [s.copy() for s in DEFAULT_MCP_SERVERS],
        description="MCP server configurations for remote Model Context Protocol tools.",
    )
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    phone: PhoneConfig = Field(default_factory=PhoneConfig)
    telephony: TelephonyConfig = Field(default_factory=TelephonyConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)
    cron_enabled: bool = True
    cron_tick_seconds: int = 60
    cron_max_concurrent: int = 3
    cron_max_iterations: int = 10
    cron_log_retention_days: int = 90
    browser_mode: Literal["isolated", "system", "extension", "auto"] = "auto"
    browser_cdp_port: int = Field(default=9222, ge=1, le=65535)
    browser_chrome_path: str = ""
    browser_extension_token: str = ""
    windows_snapshot_timeout_seconds: float = Field(default=12.0, ge=2.0, le=90.0)
    windows_snapshot_cache_seconds: float = Field(default=1.5, ge=0.0, le=10.0)
    block_session_context: bool = False  # Block previous session summary from flowing into new sessions
