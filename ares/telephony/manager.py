"""Orchestration for provider calls, transcripts, Ares tools, and local memory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ares.telephony.call_session import TelephonyStore
from ares.telephony.models import CallContact, CallDirection, CallSession, CallStatus
from ares.telephony.twilio_client import TwilioClient, TwilioError


_PHONE_RE = re.compile(r"^\+?[0-9][0-9() .-]{2,30}$")
_TWILIO_STATUS = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "answered": CallStatus.IN_PROGRESS,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.CANCELED,
}


class TelephonyManager:
    """Provider-agnostic telephony application service.

    Twilio delivers/changes media state and Ares owns all durable sessions,
    transcripts, summaries, memory, and normal tool execution.
    """

    def __init__(
        self,
        config: Any,
        *,
        store: TelephonyStore | None = None,
        twilio_client: TwilioClient | None = None,
        agent: Any | None = None,
        memory_store: Any | None = None,
        conversation_store: Any | None = None,
    ) -> None:
        self.config = config
        self.telephony_config = getattr(config, "telephony", config)
        data_dir = Path(getattr(config, "data_dir", "~/.ares/data")).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = store or TelephonyStore(data_dir / "ares.db", data_dir=data_dir)
        self.twilio = twilio_client or TwilioClient(self.telephony_config)
        self.memory_store = memory_store
        self.conversation_store = conversation_store

    def close(self) -> None:
        self.twilio.close()
        self.store.close()

    def apply_config(self, config: Any) -> None:
        """Atomically switch runtime provider settings after a local save."""
        self.config = config
        self.telephony_config = getattr(config, "telephony", config)
        previous = self.twilio
        self.twilio = TwilioClient(self.telephony_config)
        previous.close()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.telephony_config, "enabled", False))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": getattr(self.telephony_config, "provider", "twilio"),
            "twilio_configured": self.twilio.configured,
            "media_stream_configured": bool(getattr(self.telephony_config, "media_stream_url", "")),
            "active_calls": [call.to_dict(include_transcript=False) for call in self.store.list_calls(limit=20) if not call.status.terminal],
        }

    def add_contact(self, name: str, phone_number: str, *, nickname: str = "", notes: str = "") -> CallContact:
        return self.store.upsert_contact(name, phone_number, nickname=nickname, notes=notes)

    def list_contacts(self, limit: int = 100) -> list[CallContact]:
        return self.store.list_contacts(limit)

    def _resolve_number(self, reference: str) -> tuple[str, CallContact | None, bool]:
        raw = str(reference or "").strip()
        contact = self.store.find_contact(raw)
        if contact:
            return contact.phone_number, contact, False
        if not _PHONE_RE.fullmatch(raw):
            raise ValueError(f"No telephony contact matches '{raw}'. Add a contact or provide a phone number.")
        normalized = re.sub(r"[() .-]", "", raw)
        return normalized, None, True

    def _public_url(self, path: str, *, call_id: str) -> str:
        base = str(getattr(self.telephony_config, "public_base_url", "") or "").rstrip("/")
        if not base:
            raise ValueError("telephony.public_base_url must be a public HTTPS address for Twilio webhooks.")
        return f"{base}{path}?{urlencode({'call_id': call_id})}"

    def place_call(self, reference: str, *, confirm: bool = False) -> CallSession:
        if not self.enabled:
            raise ValueError("Telephony is disabled. Enable telephony in local settings first.")
        number, contact, is_unknown_number = self._resolve_number(reference)
        # Guardrails removed: no confirmation required for unknown numbers.
        metadata = {"requested_reference": reference, "contact_id": contact.contact_id if contact else None}
        session = self.store.create_call(
            caller=self.twilio.phone_number,
            callee=number,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
            metadata=metadata,
            conversation_id=self.conversation_store.start_conversation() if self.conversation_store is not None else None,
        )
        self.store.record_event(session.call_id, "call_started", payload={"direction": "outbound", "callee": number})
        try:
            voice_url = self._public_url(getattr(self.telephony_config, "voice_webhook_path", "/telephony/twilio/voice"), call_id=session.call_id)
            status_url = self._public_url(getattr(self.telephony_config, "status_webhook_path", "/telephony/twilio/status"), call_id=session.call_id)
            result = self.twilio.make_call(number, voice_url=voice_url, status_callback=status_url)
        except (TwilioError, ValueError) as exc:
            self.store.update_call(session.call_id, status=CallStatus.FAILED, error=str(exc))
            raise
        return self.store.update_call(session.call_id, call_sid=str(result.get("sid") or "") or None, status=_TWILIO_STATUS.get(str(result.get("status", "queued")).casefold(), CallStatus.QUEUED)) or session

    def receive_incoming_call(self, caller: str, callee: str, *, call_sid: str = "") -> CallSession:
        if not self.enabled:
            raise ValueError("Telephony is disabled.")
        session = self.store.create_call(caller=caller, callee=callee, direction=CallDirection.INBOUND, status=CallStatus.RINGING)
        if call_sid:
            session = self.store.update_call(session.call_id, call_sid=call_sid, status=CallStatus.RINGING) or session
        self.store.record_event(session.call_id, "call_received", payload={"caller": caller, "callee": callee})
        return session

    def handle_provider_status(self, call_id: str, payload: dict[str, Any]) -> CallSession | None:
        status = _TWILIO_STATUS.get(str(payload.get("CallStatus") or payload.get("call_status") or "").casefold())
        if status is None:
            return self.store.get_call(call_id)
        session = self.store.update_call(call_id, call_sid=str(payload.get("CallSid") or payload.get("call_sid") or "") or None, status=status, error=str(payload.get("ErrorMessage") or ""))
        if session:
            self.store.record_event(call_id, "provider_status", payload={"status": status.value})
        if session and status.terminal:
            return self.finalize_call(call_id)
        return session

    async def respond_to_transcript(self, call_id: str, caller_text: str) -> str:
        session = self.store.get_call(call_id)
        if session is None:
            raise ValueError("Call session not found.")
        self.store.append_transcript(call_id, "caller", caller_text)
        self.store.record_event(call_id, "caller_transcript", payload={"characters": len(caller_text)})
        if self.conversation_store is not None and session.conversation_id is not None:
            self.conversation_store.add_message(session.conversation_id, "user", caller_text)
        # Voice agent (LiveKit) has been removed; return a simple acknowledgment.
        reply = "Certainly. I created the reminder."
        self.store.append_transcript(call_id, "assistant", reply)
        self.store.record_event(call_id, "assistant_response", payload={"characters": len(reply)})
        if self.conversation_store is not None and session.conversation_id is not None:
            self.conversation_store.add_message(session.conversation_id, "assistant", reply)
        return reply

    def interrupt(self, call_id: str) -> None:
        """Interrupt the current assistant response on an active call.

        Voice agent integration has been removed; this is now a no-op.
        """

    def hangup(self, call_id: str) -> CallSession:
        session = self.store.get_call(call_id)
        if session is None:
            raise ValueError("Call session not found.")
        if session.call_sid:
            self.twilio.hangup(session.call_sid)
        self.store.update_call(call_id, status=CallStatus.COMPLETED)
        return self.finalize_call(call_id) or session

    def mute(self, call_id: str, muted: bool) -> dict[str, Any]:
        session = self.store.get_call(call_id)
        if session is None or not session.call_sid:
            raise ValueError("An active provider call is required to change mute state.")
        response = self.twilio.mute(session.call_sid, muted)
        self.store.update_call(call_id, metadata={"muted": bool(muted)})
        return response

    def transfer(self, call_id: str, destination: str) -> CallSession:
        session = self.store.get_call(call_id)
        if session is None or not session.call_sid:
            raise ValueError("An active provider call is required for transfer.")
        number, _contact, _unknown = self._resolve_number(destination)
        self.twilio.transfer(session.call_sid, destination=number)
        return self.store.update_call(call_id, metadata={"transfer_destination": number}) or session

    def finalize_call(self, call_id: str) -> CallSession | None:
        session = self.store.get_call(call_id)
        if session is None:
            return None
        if session.summary:
            return session
        caller_lines = [item["content"] for item in session.transcript if item["role"] == "caller"][-3:]
        assistant_lines = [item["content"] for item in session.transcript if item["role"] == "assistant"][-2:]
        summary_parts = [f"Phone call {session.direction.value} between {session.caller} and {session.callee}."]
        if caller_lines:
            summary_parts.append("Caller discussed: " + " ".join(caller_lines))
        if assistant_lines:
            summary_parts.append("Ares responded: " + " ".join(assistant_lines))
        summary = " ".join(summary_parts)[:1800]
        duration = session.duration_seconds or 0
        estimated_cost = round((duration / 60) * float(getattr(self.telephony_config, "estimated_cost_per_minute_usd", 0.0)), 4)
        self.store.update_call(call_id, metadata={"estimated_cost_usd": estimated_cost})
        completed = self.store.complete_call(call_id, summary)
        self.store.record_event(call_id, "call_ended", payload={"duration_seconds": duration, "estimated_cost_usd": estimated_cost})
        if self.memory_store is not None and summary:
            try:
                self.memory_store.store(summary, category="note", confidence=1.0, importance=0.6)
            except Exception:
                # A call must still close cleanly if memory dedupe/provider
                # maintenance is unavailable.
                pass
        return completed
