import asyncio
import os
from types import SimpleNamespace

import numpy as np
import pytest

from ares.desktop.agent import (
    DesktopAlreadyRunningError,
    DesktopVoiceAgent,
    _desktop_instance_guard,
)
from ares.models import DesktopConfig, VoiceConfig


def _bare_agent() -> DesktopVoiceAgent:
    agent = object.__new__(DesktopVoiceAgent)
    agent.desktop_config = DesktopConfig()
    agent.voice_config = VoiceConfig()
    agent._wake_detector = None
    agent._command_vad = None
    agent._ambient_rms_samples = __import__("collections").deque(
        [0.0002] * 20, maxlen=200
    )
    return agent


def test_wake_word_supports_command_and_follow_up_modes():
    agent = _bare_agent()
    assert agent._command_after_wake_word("Hey Jarvis, list files in this folder") == (
        "list files in this folder"
    )
    assert agent._command_after_wake_word("Jarvis!") == ""
    assert agent._command_after_wake_word("Jarvis, hey Jarvis") == ""
    assert agent._command_after_wake_word("Hey, Jarvis, list files") == "list files"
    assert agent._command_after_wake_word("Hey Jervis open downloads") == "open downloads"
    assert agent._command_after_wake_word(
        "Jarvis, hey Jarvis, Jarvis, list my files"
    ) == "list my files"
    assert agent._command_after_wake_word("please list files") is None


def test_wake_word_is_configurable():
    agent = _bare_agent()
    agent.desktop_config.wake_words = ["computer"]
    assert agent._command_after_wake_word("Computer open downloads") == "open downloads"
    assert agent._command_after_wake_word("Hey Jarvis open downloads") is None


def test_bluetooth_wake_gate_is_more_sensitive_than_normal_speech_gate():
    agent = _bare_agent()
    quiet_frame = np.full(480, 0.003, dtype=np.float32)

    assert agent._frame_is_speech(quiet_frame) is False
    assert agent._frame_is_speech(quiet_frame, wake_word=True) is True


def test_response_sanitizers_remove_emoji_and_tts_symbols():
    display = DesktopVoiceAgent._sanitize_display_text(
        "Done 😊 C:\\Users\\anime\\Desktop"
    )
    assert "😊" not in display
    assert "C:\\Users" in display

    spoken = DesktopVoiceAgent._sanitize_tts_text(
        "😊 **Files:** C:\\Users\\anime\\README.md / notes-old.txt | "
        "[Open](https://example.com/file)"
    )
    for unwanted in ("😊", "*", ":", "\\", "/", "-", "|", "https"):
        assert unwanted not in spoken
    assert "README md" in spoken
    assert "Open" in spoken


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_desktop_instance_guard_rejects_a_second_copy():
    mutex_name = f"Local\\Ares.DesktopVoice.test.{os.getpid()}"
    with _desktop_instance_guard(mutex_name):
        with pytest.raises(DesktopAlreadyRunningError):
            with _desktop_instance_guard(mutex_name):
                pass


@pytest.mark.asyncio
async def test_interrupt_stops_playback_and_cancels_active_turn():
    agent = _bare_agent()
    agent._speech_stop_event = asyncio.Event()
    agent._window = None
    agent._tray = None
    active = asyncio.create_task(asyncio.sleep(60))
    agent._active_turn_task = active

    await agent._interrupt_current_turn(show_listening=True)

    assert agent._speech_stop_event.is_set()
    assert active.cancelled()


@pytest.mark.asyncio
async def test_detected_wake_phrase_queues_trailing_command():
    agent = _bare_agent()
    agent._loop = asyncio.get_running_loop()
    agent._transcriber = SimpleNamespace(
        transcribe_samples=lambda audio, rate, **kwargs: "Hey Jarvis, list project files"
    )
    agent._command_queue = asyncio.Queue()
    agent._active_turn_task = None
    agent._speech_stop_event = None
    agent._window = None
    agent._tray = None
    agent._wake_transcribing = True
    agent._wake_task = asyncio.current_task()
    agent._wake_armed_until = 0.0

    await agent._handle_wake_audio(np.ones(640, dtype=np.float32))

    assert await agent._command_queue.get() == "list project files"
    assert agent._wake_armed_until == 0.0
    assert agent._wake_transcribing is False


@pytest.mark.asyncio
async def test_completed_post_wake_audio_is_not_dropped_after_deadline():
    agent = _bare_agent()
    agent._loop = asyncio.get_running_loop()
    agent._transcriber = SimpleNamespace(
        transcribe_samples=lambda audio, rate, **kwargs: "list project files"
    )
    agent._command_queue = asyncio.Queue()
    agent._active_turn_task = None
    agent._speech_stop_event = None
    agent._window = None
    agent._tray = None
    agent._wake_transcribing = True
    agent._wake_task = asyncio.current_task()
    agent._wake_armed_until = 0.0

    await agent._handle_wake_audio(np.ones(640, dtype=np.float32))

    assert await agent._command_queue.get() == "list project files"


@pytest.mark.asyncio
async def test_empty_post_wake_transcript_rearms_listening():
    agent = _bare_agent()
    agent._loop = asyncio.get_running_loop()
    agent._transcriber = SimpleNamespace(
        transcribe_samples=lambda audio, rate, **kwargs: ""
    )
    agent._command_queue = asyncio.Queue()
    agent._active_turn_task = None
    agent._speech_stop_event = None
    agent._window = None
    agent._tray = None
    agent._wake_transcribing = True
    agent._wake_task = asyncio.current_task()
    agent._wake_armed_until = 0.0
    agent._wake_pre_roll = __import__("collections").deque(maxlen=20)
    agent._wake_speech = []
    agent._wake_consecutive_speech = 0
    agent._wake_voiced_frames = 0
    agent._wake_silence_frames = 0

    await agent._handle_wake_audio(np.ones(640, dtype=np.float32))

    assert agent._wake_armed_until > agent._loop.time()
    assert agent._command_queue.empty()


def test_post_wake_command_gate_accepts_quiet_bluetooth_speech():
    agent = _bare_agent()
    quiet_speech = np.full(480, 0.001, dtype=np.float32)

    assert agent._frame_is_command_speech(quiet_speech) is True
