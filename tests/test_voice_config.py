import os

import pytest

from ares.models import AppConfig, VoiceConfig
from ares.voice.tts import EdgeTTS, SarvamTTS, create_tts_provider, voice_config_from_env


def test_app_config_has_voice_defaults():
    config = AppConfig()

    assert config.voice.enabled is False
    assert config.voice.tts_provider == "edge_tts"
    assert config.voice.hotkey == "space"
    assert config.voice.stt_model == "tiny"


def test_voice_env_overrides(monkeypatch):
    monkeypatch.setenv("ARES_VOICE_ENABLED", "true")
    monkeypatch.setenv("ARES_TTS_PROVIDER", "sarvam")
    monkeypatch.setenv("ARES_TTS_VOICE", "arya")
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")
    monkeypatch.setenv("SARVAM_LANGUAGE_CODE", "en-IN")

    resolved = voice_config_from_env(VoiceConfig())

    assert resolved.enabled is True
    assert resolved.tts_provider == "sarvam"
    assert resolved.tts_voice == "arya"
    assert resolved.sarvam_api_key == "test-key"
    assert resolved.sarvam_language_code == "en-IN"


def test_create_edge_provider():
    provider = create_tts_provider(VoiceConfig(tts_provider="edge_tts", tts_voice="en-US-GuyNeural"))

    assert isinstance(provider, EdgeTTS)
    assert provider.default_voice == "en-US-GuyNeural"


def test_create_sarvam_provider_requires_key():
    with pytest.raises(ValueError, match="Sarvam TTS requires"):
        create_tts_provider(VoiceConfig(tts_provider="sarvam"))


def test_create_sarvam_provider_with_env_key(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")

    provider = create_tts_provider(VoiceConfig(tts_provider="sarvam", tts_voice="arya"))

    assert isinstance(provider, SarvamTTS)
    assert provider.api_key == "test-key"
    assert provider.default_voice == "arya"
