from ares.models import AppConfig, VoiceConfig
from ares.voice.agent import friendly_input_device_name, resolve_input_device, voice_config_from_env
from ares.voice.tts import DEFAULT_EDGE_VOICE, EdgeTTS


def test_app_config_has_voice_defaults():
    config = AppConfig()

    assert config.voice.enabled is False
    assert config.voice.stt_backend == "auto"
    assert config.voice.tts_backend == "auto"
    assert config.voice.tts_voice == DEFAULT_EDGE_VOICE
    assert config.voice.stt_model == "small"
    assert config.voice.min_utterance_ms == 350
    assert config.voice.silence_timeout_ms == 420
    assert config.voice.barge_in_enabled is True
    assert config.voice.barge_in_delay_ms == 350
    assert config.voice.barge_in_min_voiced_ms == 300
    assert config.voice.post_speech_cooldown_ms == 120
    assert config.voice.tts_chunk_chars == 90
    assert config.voice.tts_volume == 1.6


def test_voice_env_overrides(monkeypatch):
    monkeypatch.setenv("ARES_VOICE_ENABLED", "true")
    monkeypatch.setenv("ARES_STT_BACKEND", "whisper")
    monkeypatch.setenv("ARES_TTS_BACKEND", "edge")
    monkeypatch.setenv("ARES_TTS_VOICE", "en-US-GuyNeural")
    monkeypatch.setenv("ARES_STT_MODEL", "tiny")
    monkeypatch.setenv("ARES_STT_LANGUAGE", "en")
    monkeypatch.setenv("ARES_MIC_DEVICE", "2")
    monkeypatch.setenv("ARES_TTS_VOLUME", "1.8")
    monkeypatch.setenv("ARES_VOICE_BARGE_IN", "false")
    monkeypatch.setenv("ARES_VOICE_BARGE_IN_DELAY_MS", "480")
    monkeypatch.setenv("ARES_VOICE_BARGE_IN_MIN_VOICED_MS", "420")
    monkeypatch.setenv("ARES_VOICE_TTS_CHUNK_CHARS", "72")

    resolved = voice_config_from_env(VoiceConfig())

    assert resolved.enabled is True
    assert resolved.stt_backend == "whisper"
    assert resolved.tts_backend == "edge"
    assert resolved.tts_voice == "en-US-GuyNeural"
    assert resolved.stt_model == "tiny"
    assert resolved.stt_language == "en"
    assert resolved.mic_device == 2
    assert resolved.tts_volume == 1.8
    assert resolved.barge_in_enabled is False
    assert resolved.barge_in_delay_ms == 480
    assert resolved.barge_in_min_voiced_ms == 420
    assert resolved.tts_chunk_chars == 72


def test_edge_tts_uses_configured_voice():
    provider = EdgeTTS(voice="en-US-GuyNeural")

    assert isinstance(provider, EdgeTTS)
    assert provider.default_voice == "en-US-GuyNeural"


def test_voice_config_has_history_defaults():
    config = VoiceConfig()

    assert config.voice_max_history == 10
    assert config.voice_max_memories == 3


def test_voice_config_custom_history():
    config = VoiceConfig(voice_max_history=5, voice_max_memories=2)

    assert config.voice_max_history == 5
    assert config.voice_max_memories == 2


def test_bluetooth_microphone_is_preferred_over_windows_default(monkeypatch):
    devices = [
        {
            "name": "Microphone Array (Realtek Audio)",
            "max_input_channels": 2,
            "default_samplerate": 44100.0,
            "hostapi": 0,
        },
        {
            "name": "Headset (Airdopes 181 Pro Hands-Free AG Audio)",
            "max_input_channels": 1,
            "default_samplerate": 16000.0,
            "hostapi": 1,
        },
    ]

    class FakeSoundDevice:
        default = type("Default", (), {"device": [0, 3]})()

        @staticmethod
        def query_devices(device=None, _kind=None):
            if device is None and _kind == "input":
                return devices[0]
            return devices if device is None else devices[device]

        @staticmethod
        def query_hostapis(index):
            return {"name": "Windows WASAPI" if index == 1 else "MME"}

        @staticmethod
        def check_input_settings(**_kwargs):
            return None

    monkeypatch.setitem(__import__("sys").modules, "sounddevice", FakeSoundDevice)

    selected, name = resolve_input_device(None, prefer_bluetooth=True)

    assert selected == 1
    assert "Airdopes" in name
    assert friendly_input_device_name(name) == "Airdopes 181 Pro"


def test_system_default_microphone_is_not_pinned_to_numeric_device(monkeypatch):
    default_info = {
        "name": "Microphone Array (Realtek Audio)",
        "max_input_channels": 2,
        "default_samplerate": 48000.0,
        "hostapi": 0,
    }

    class FakeSoundDevice:
        default = type("Default", (), {"device": [7, 3]})()

        @staticmethod
        def query_devices(device=None, kind=None):
            if device is None and kind == "input":
                return default_info
            return [default_info]

        @staticmethod
        def query_hostapis(_index):
            return {"name": "Windows WASAPI"}

        @staticmethod
        def check_input_settings(**kwargs):
            assert kwargs["device"] is None

    monkeypatch.setitem(__import__("sys").modules, "sounddevice", FakeSoundDevice)

    selected, name = resolve_input_device(None, prefer_bluetooth=False)

    assert selected is None
    assert name == "Microphone Array (Realtek Audio)"
    assert friendly_input_device_name(name) == "Realtek Audio Microphone Array"


def test_bluetooth_default_falls_back_to_non_bluetooth_input(monkeypatch):
    devices = [
        {
            "name": "Headset (Airdopes Hands-Free AG Audio)",
            "max_input_channels": 1,
            "default_samplerate": 16000.0,
            "hostapi": 0,
        },
        {
            "name": "Microphone Array (Realtek Audio)",
            "max_input_channels": 2,
            "default_samplerate": 48000.0,
            "hostapi": 1,
        },
    ]

    class FakeSoundDevice:
        @staticmethod
        def query_devices(device=None, kind=None):
            if device is None and kind == "input":
                return devices[0]
            return devices if device is None else devices[device]

        @staticmethod
        def query_hostapis(index):
            return {"name": "Windows WASAPI" if index == 1 else "MME"}

        @staticmethod
        def check_input_settings(**_kwargs):
            return None

    monkeypatch.setitem(__import__("sys").modules, "sounddevice", FakeSoundDevice)

    selected, name = resolve_input_device(None, avoid_bluetooth=True)

    assert selected == 1
    assert "Realtek" in name
