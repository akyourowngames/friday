"""Provider-backed telephony regression tests with no external calls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from io import BytesIO

import httpx

from ares.context.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.telephony.call_session import TelephonyStore
from ares.telephony.media_gateway import TwilioMediaGateway, _StreamState, decode_twilio_media, encode_twilio_media, pcm16_rms, pcm16_to_twilio_chunks, pcm8k_to_float16k
from ares.telephony.manager import TelephonyManager
from ares.telephony.models import CallStatus
from ares.telephony.twilio_client import TwilioClient
from ares.telephony.webhook import TwilioWebhookApp, telephony_readiness, validate_twilio_signature
from ares.tools import ToolExecutor


class FakeTwilio:
    phone_number = "+15555550000"
    configured = True

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.hung_up: list[str] = []

    def close(self) -> None:
        pass

    def make_call(self, number: str, *, voice_url: str, status_callback: str = "") -> dict:
        self.calls.append({"number": number, "voice_url": voice_url, "status_callback": status_callback})
        return {"sid": f"CA-test-{len(self.calls)}", "status": "queued"}

    def hangup(self, call_sid: str) -> dict:
        self.hung_up.append(call_sid)
        return {"sid": call_sid, "status": "completed"}

    def mute(self, call_sid: str, muted: bool) -> dict:
        return {"ok": True, "call_sid": call_sid, "muted": muted}

    def transfer(self, call_sid: str, *, destination: str) -> dict:
        return {"sid": call_sid, "destination": destination}


class FakeVoiceAgent:
    def __init__(self) -> None:
        self.interrupted: set[str] = set()

    def interrupt(self, call_id: str) -> None:
        self.interrupted.add(call_id)

    async def respond(self, call_id: str, transcript: str, history: list[dict]) -> str:
        assert history and history[-1]["content"] == transcript
        return "Certainly. I created the reminder."


class FakeMemory:
    def __init__(self) -> None:
        self.stored: list[tuple[str, dict]] = []

    def store(self, text: str, **kwargs) -> int:
        self.stored.append((text, kwargs))
        return 1


def telephony_config(tmp_path):
    cfg = AppConfig(data_dir=str(tmp_path))
    cfg.telephony.enabled = True
    cfg.telephony.account_sid = "AC-test"
    cfg.telephony.auth_token = "token-test"
    cfg.telephony.phone_number = "+15555550000"
    cfg.telephony.public_base_url = "https://ares.example"
    cfg.telephony.media_stream_url = "wss://media.example/twilio"
    return cfg


def test_contacts_are_encrypted_and_resolve_by_name_or_nickname(tmp_path):
    store = TelephonyStore(tmp_path / "ares.db", data_dir=tmp_path)
    try:
        contact = store.upsert_contact("Maya Singh", "+15555550111", nickname="mom", notes="Call after work")
        assert store.find_contact("mom").phone_number == "+15555550111"
        assert store.find_contact("Maya Singh").contact_id == contact.contact_id
        raw = (tmp_path / "ares.db").read_bytes()
        assert b"+15555550111" not in raw
        assert (tmp_path / "telephony.key").exists()
    finally:
        store.close()


def test_outbound_call_requires_confirmation_only_for_unknown_raw_number(tmp_path):
    cfg = telephony_config(tmp_path)
    fake_twilio = FakeTwilio()
    store = TelephonyStore(tmp_path / "ares.db", data_dir=tmp_path)
    manager = TelephonyManager(cfg, store=store, twilio_client=fake_twilio)
    try:
        manager.add_contact("Mom", "+15555550111", nickname="mom")
        call = manager.place_call("mom")
        assert call.call_sid == "CA-test-1"
        assert fake_twilio.calls[0]["number"] == "+15555550111"
        assert "call_id=" in fake_twilio.calls[0]["voice_url"]

        # Confirmation guardrails removed: unknown numbers are allowed directly.
        call2 = manager.place_call("+15555550112")
        assert call2.call_sid == "CA-test-2"
    finally:
        manager.close()


def test_incoming_transcript_is_persisted_summarized_and_mirrored_to_memory(tmp_path):
    cfg = telephony_config(tmp_path)
    store = TelephonyStore(tmp_path / "ares.db", data_dir=tmp_path)
    memory = FakeMemory()
    conversations = ConversationStore(tmp_path / "ares.db")
    manager = TelephonyManager(cfg, store=store, twilio_client=FakeTwilio(), memory_store=memory, conversation_store=conversations)
    try:
        call = manager.receive_incoming_call("+15555550111", "+15555550000", call_sid="CA-inbound")
        assert call.direction.value == "inbound"
        reply = asyncio.run(manager.respond_to_transcript(call.call_id, "Remind me tomorrow to buy milk."))
        assert reply == "Certainly. I created the reminder."
        complete = manager.handle_provider_status(call.call_id, {"CallSid": "CA-inbound", "CallStatus": "completed"})
        assert complete.status == CallStatus.COMPLETED
        assert "buy milk" in complete.summary
        assert memory.stored and "Phone call" in memory.stored[0][0]
        loaded = store.get_call(call.call_id)
        assert [item["role"] for item in loaded.transcript] == ["caller", "assistant"]
        assert any(event["event_type"] == "call_ended" for event in store.list_events(call.call_id))
    finally:
        manager.close()
        conversations.close()


def test_twilio_client_uses_rest_api_and_builds_media_stream_twiml(tmp_path):
    cfg = telephony_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/Calls.json")
        assert request.method == "POST"
        return httpx.Response(201, json={"sid": "CA-http", "status": "queued"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    twilio = TwilioClient(cfg.telephony, client=client)
    try:
        call = twilio.make_call("+15555550111", voice_url="https://ares.example/voice")
        assert call["sid"] == "CA-http"
    finally:
        twilio.close()
        client.close()


def test_webhook_validates_signature_and_rejects_tampering(tmp_path):
    cfg = telephony_config(tmp_path)
    manager = TelephonyManager(cfg, store=TelephonyStore(tmp_path / "ares.db", data_dir=tmp_path), twilio_client=FakeTwilio())
    app = TwilioWebhookApp(manager)
    values = {"From": "+15555550111", "To": "+15555550000", "CallSid": "CA-hook"}
    url = "https://ares.example/telephony/twilio/voice"
    signed = url + "".join(f"{key}{values[key]}" for key in sorted(values))
    signature = base64.b64encode(hmac.new(b"token-test", signed.encode(), hashlib.sha1).digest()).decode()
    assert validate_twilio_signature(url, values, signature, "token-test") is True
    body = "&".join(f"{key}={value.replace('+', '%2B')}" for key, value in values.items()).encode()
    captured: dict[str, object] = {}
    try:
        response = app({"REQUEST_METHOD": "POST", "PATH_INFO": "/telephony/twilio/voice", "QUERY_STRING": "", "CONTENT_LENGTH": str(len(body)), "wsgi.input": BytesIO(body), "HTTP_X_TWILIO_SIGNATURE": signature}, lambda status, headers: captured.update(status=status, headers=headers))
        assert captured["status"] == "200 OK"
        assert b"<Response>" in b"".join(response)
    finally:
        manager.close()


def test_telephony_readiness_is_redacted_and_lists_only_missing_requirements(tmp_path):
    cfg = telephony_config(tmp_path)
    report = telephony_readiness(cfg)
    assert report["ready"] is True
    assert report["twilio_credentials_configured"] is True
    assert report["missing"] == []
    assert "token-test" not in repr(report)

    cfg.telephony.media_stream_url = ""
    report = telephony_readiness(cfg)
    assert report["ready"] is False
    assert "public WSS media gateway URL" in report["missing"]


def test_twilio_media_codec_round_trip_and_resampling():
    original = (b"\x00\x00\x00\x10\x00\xf0" * 80)
    payload = encode_twilio_media(original)
    decoded = decode_twilio_media(payload)
    assert len(decoded) == len(original)
    samples = pcm8k_to_float16k(decoded)
    assert samples.dtype == "float32"
    assert len(samples) == len(decoded)
    assert pcm16_rms(decoded) > 0
    chunks = pcm16_to_twilio_chunks(decoded, chunk_bytes=160)
    assert len(chunks) == len(decoded) // 160
    assert b"".join(decode_twilio_media(chunk) for chunk in chunks) == decoded


def test_first_caller_audio_does_not_interrupt_its_own_response():
    class GatewayManager:
        def __init__(self):
            self.interrupts: list[str] = []

        def interrupt(self, call_id: str) -> None:
            self.interrupts.append(call_id)

    class Socket:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, message: str) -> None:
            self.sent.append(message)

    manager = GatewayManager()
    gateway = TwilioMediaGateway(manager)  # type: ignore[arg-type]
    state = _StreamState(stream_sid="MZ-test", call_id="call-test")
    payload = encode_twilio_media(b"\x00\x10" * 160)
    asyncio.run(gateway._handle_media(Socket(), state, {"media": {"payload": payload}}))
    assert manager.interrupts == []


def test_telephony_tools_expose_calls_contacts_and_unknown_confirmation(tmp_path, fake_embedding_provider):
    cfg = telephony_config(tmp_path)
    memory = MemoryStore(db_path=tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    manager = TelephonyManager(cfg, store=TelephonyStore(tmp_path / "ares.db", data_dir=tmp_path), twilio_client=FakeTwilio(), memory_store=memory)
    executor = ToolExecutor(memory, config=cfg, telephony_manager=manager)
    try:
        saved = json.loads(executor.execute("telephony_save_contact", {"name": "Mom", "phone_number": "+15555550111", "nickname": "mom"}))
        assert saved["ok"] and saved["contact"]["phone_number"] == "+15555550111"
        placed = json.loads(executor.execute("telephony_call", {"recipient": "mom"}))
        assert placed["ok"] and placed["call"]["call_sid"] == "CA-test-1"
        # Confirmation guardrails removed: unknown numbers are placed directly.
        unknown = json.loads(executor.execute("telephony_call", {"recipient": "+15555550112"}))
        assert unknown["ok"] and unknown["call"]["call_sid"] == "CA-test-2"
        assert json.loads(executor.execute("telephony_list_calls", {}))["calls"]
    finally:
        executor.close()
        manager.close()
        memory.close()
