from types import SimpleNamespace

import numpy as np

from ares.voice.stt import WhisperTranscriber


def test_translate_task_is_forwarded_for_audio_samples(monkeypatch):
    transcriber = WhisperTranscriber("small")
    received = {}

    def fake_transcribe_file(path, *, task, multilingual):
        received.update(task=task, multilingual=multilingual)
        return "Open my downloads"

    monkeypatch.setattr(transcriber, "transcribe_file", fake_transcribe_file)

    text = transcriber.transcribe_samples(
        np.zeros(1600, dtype=np.float32),
        task="translate",
        multilingual=True,
    )

    assert text == "Open my downloads"
    assert received == {"task": "translate", "multilingual": True}


def test_translate_task_requests_english_output_from_whisper():
    calls = []

    class FakeModel:
        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(text=" Open my downloads ")], None

    transcriber = WhisperTranscriber("small")
    transcriber._model = FakeModel()

    text = transcriber.transcribe_file(
        "ignored.wav", task="translate", multilingual=True
    )

    assert text == "Open my downloads"
    assert calls[0]["task"] == "translate"
    assert calls[0]["multilingual"] is True
