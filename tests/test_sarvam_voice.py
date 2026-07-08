import asyncio
import base64
import sys
import types

import numpy as np

from ares.voice.sarvam import SarvamTTS, SarvamTranscriber


def test_sarvam_transcriber_posts_audio(monkeypatch):
    calls = {}

    class FakeResponse:
        is_error = False

        def raise_for_status(self):
            pass

        def json(self):
            return {"transcript": "hello sarvam"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, data=None, files=None, json=None):
            calls["url"] = url
            calls["headers"] = headers
            calls["data"] = data
            calls["files"] = files
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=FakeAsyncClient))

    transcriber = SarvamTranscriber(api_key="secret")

    assert transcriber.transcribe_samples(np.ones(160, dtype=np.float32) * 0.1) == "hello sarvam"
    assert calls["headers"]["api-subscription-key"] == "secret"
    assert calls["data"]["mode"] == "transcribe"
    assert calls["files"]["file"][2] == "audio/wav"


def test_sarvam_tts_decodes_base64_audio(monkeypatch):
    audio = b"RIFFaudio"

    class FakeResponse:
        is_error = False

        def raise_for_status(self):
            pass

        def json(self):
            return {"audios": [base64.b64encode(audio).decode("ascii")]}

    class FakeAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None, data=None, files=None):
            assert headers["api-subscription-key"] == "secret"
            assert json["speaker"] == "shubh"
            assert json["model"] == "bulbul:v3"
            assert json["speech_sample_rate"] == 24000
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=FakeAsyncClient))

    tts = SarvamTTS(api_key="secret")

    assert asyncio.run(tts.synthesize("hello")) == audio
    assert tts.audio_format == "encoded"


def test_sarvam_tts_error_includes_response_body(monkeypatch):
    class FakeResponse:
        is_error = True
        status_code = 422
        text = "speaker is not supported"

    class FakeAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None, data=None, files=None):
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=FakeAsyncClient))

    tts = SarvamTTS(api_key="secret")

    try:
        asyncio.run(tts.synthesize("hello"))
    except RuntimeError as exc:
        assert "Sarvam TTS HTTP 422" in str(exc)
        assert "speaker is not supported" in str(exc)
    else:
        raise AssertionError("Sarvam TTS HTTP errors should include response body")
