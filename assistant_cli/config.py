from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    embed_model: str
    persona_file: str
    temperature: float
    max_tokens: int
    memory_dir: str
    memory_index_dir: str
    memory_top_k: int
    session_dir: str
    last_messages: int
    auto_llm_memory: bool
    sarvam_api_key: str
    voice_enabled: bool
    voice_speaker: str
    voice_language: str
    voice_model: str
    voice_output_dir: str
    voice_sample_rate: int
    voice_codec: str
    voice_pace: float
    voice_temperature: float
    voice_max_chars: int
    voice_input_enabled: bool
    voice_hotkey: str
    voice_hold_seconds: float
    stt_model: str
    stt_mode: str
    stt_language: str
    stt_sample_rate: int
    stt_max_seconds: float
    stt_min_seconds: float
    stt_output_dir: str
    tavily_api_key: str
    tools_enabled: bool
    auto_tools_enabled: bool
    tool_timeout_seconds: float
    tool_planner_timeout_seconds: float
    tool_min_confidence: float
    tool_result_max_chars: int


def load_settings() -> Settings:
    load_dotenv()

    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is missing. Add it to .env or your shell environment.")

    return Settings(
        api_key=api_key,
        base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").strip(),
        model=os.getenv("NVIDIA_MODEL", "mistralai/ministral-14b-instruct-2512").strip(),
        embed_model=os.getenv("NVIDIA_EMBED_MODEL", "nvidia/nv-embedqa-e5-v5").strip(),
        persona_file=os.getenv("PERSONA_FILE", "persona.md").strip(),
        temperature=float(os.getenv("ASSISTANT_TEMPERATURE", "0.35")),
        max_tokens=int(os.getenv("ASSISTANT_MAX_TOKENS", "1200")),
        memory_dir=os.getenv("MEMORY_DIR", "memory").strip(),
        memory_index_dir=os.getenv("MEMORY_INDEX_DIR", ".memory_index").strip(),
        memory_top_k=int(os.getenv("MEMORY_TOP_K", "4")),
        session_dir=os.getenv("SESSION_DIR", "sessions").strip(),
        last_messages=int(os.getenv("LAST_MESSAGES", "20")),
        auto_llm_memory=os.getenv("AUTO_LLM_MEMORY", "true").strip().lower() in {"1", "true", "yes", "on"},
        sarvam_api_key=os.getenv("SARVAM_API_KEY", "").strip(),
        voice_enabled=os.getenv("FRIDAY_VOICE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        voice_speaker=os.getenv("FRIDAY_VOICE_SPEAKER", "priya").strip(),
        voice_language=os.getenv("FRIDAY_VOICE_LANGUAGE", "en-IN").strip(),
        voice_model=os.getenv("FRIDAY_VOICE_MODEL", "bulbul:v3").strip(),
        voice_output_dir=os.getenv("FRIDAY_VOICE_OUTPUT_DIR", "storage/voice").strip(),
        voice_sample_rate=int(os.getenv("FRIDAY_VOICE_SAMPLE_RATE", "24000")),
        voice_codec=os.getenv("FRIDAY_VOICE_CODEC", "wav").strip(),
        voice_pace=float(os.getenv("FRIDAY_VOICE_PACE", "1.25")),
        voice_temperature=float(os.getenv("FRIDAY_VOICE_TEMPERATURE", "0.55")),
        voice_max_chars=int(os.getenv("FRIDAY_VOICE_MAX_CHARS", "900")),
        voice_input_enabled=os.getenv("FRIDAY_VOICE_INPUT_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"},
        voice_hotkey=os.getenv("FRIDAY_VOICE_HOTKEY", "ctrl+space").strip(),
        voice_hold_seconds=float(os.getenv("FRIDAY_VOICE_HOLD_SECONDS", "0.30")),
        stt_model=os.getenv("FRIDAY_STT_MODEL", "saaras:v3").strip(),
        stt_mode=os.getenv("FRIDAY_STT_MODE", "transcribe").strip(),
        stt_language=os.getenv("FRIDAY_STT_LANGUAGE", "en-IN").strip(),
        stt_sample_rate=int(os.getenv("FRIDAY_STT_SAMPLE_RATE", "16000")),
        stt_max_seconds=float(os.getenv("FRIDAY_STT_MAX_SECONDS", "30.0")),
        stt_min_seconds=float(os.getenv("FRIDAY_STT_MIN_SECONDS", "0.35")),
        stt_output_dir=os.getenv("FRIDAY_STT_OUTPUT_DIR", "storage/voice_input").strip(),
        tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
        tools_enabled=os.getenv("FRIDAY_TOOLS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        auto_tools_enabled=os.getenv("FRIDAY_AUTO_TOOLS_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"},
        tool_timeout_seconds=float(os.getenv("FRIDAY_TOOL_TIMEOUT_SECONDS", "8.0")),
        tool_planner_timeout_seconds=float(os.getenv("FRIDAY_TOOL_PLANNER_TIMEOUT_SECONDS", "8.0")),
        tool_min_confidence=float(os.getenv("FRIDAY_TOOL_MIN_CONFIDENCE", "0.45")),
        tool_result_max_chars=int(os.getenv("FRIDAY_TOOL_RESULT_MAX_CHARS", "6000")),
    )
