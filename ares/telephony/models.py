"""Domain models for Ares telephony."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no-answer"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.BUSY, self.NO_ANSWER, self.FAILED, self.CANCELED}


@dataclass(slots=True)
class CallContact:
    contact_id: int
    name: str
    nickname: str = ""
    phone_number: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CallSession:
    call_id: str
    call_sid: str | None
    caller: str
    callee: str
    direction: CallDirection
    status: CallStatus
    started_at: str
    answered_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation_id: int | None = None
    error: str = ""

    def to_dict(self, *, include_transcript: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        payload["direction"] = self.direction.value
        payload["status"] = self.status.value
        if not include_transcript:
            payload.pop("transcript", None)
        return payload
