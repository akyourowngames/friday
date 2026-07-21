"""Small, content-free request latency instrumentation helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Final


LATENCY_EVENTS: Final[tuple[str, ...]] = (
    "request_received",
    "reflection_check_finished",
    "context_build_finished",
    "provider_request_started",
    "provider_first_token_received",
    "first_token_sent",
    "tool_execution_finished",
    "response_finished",
)

LATENCY_METRICS: Final[tuple[str, ...]] = (
    "reflection_wait_ms",
    "context_build_ms",
    "provider_ttft_ms",
    "ares_ttft_ms",
    "tool_execution_ms",
    "generation_ms",
    "total_ms",
)


def _elapsed_ms(started_at: float, finished_at: float) -> float:
    """Return a rounded non-negative monotonic duration in milliseconds."""
    return round(max(0.0, finished_at - started_at) * 1000.0, 3)


@dataclass
class RequestLatency:
    """Capture lightweight, content-free timings for one agent request.

    Event timestamps stay relative to ``request_received`` in the published
    record.  This avoids exposing wall-clock timing data while retaining
    ordering and useful durations for diagnostics.
    """

    request_id: str
    session_id: str | None
    request_received: float = field(default_factory=time.monotonic)
    _events: dict[str, float] = field(default_factory=dict)
    _context_started: float | None = None
    _tool_execution_seconds: float = 0.0
    model: str = ""
    tool_schema_count: int = 0

    def __post_init__(self) -> None:
        self._events["request_received"] = self.request_received

    def mark(self, event: str, *, replace: bool = False) -> float:
        """Record an event once, unless a later occurrence is requested."""
        if event not in LATENCY_EVENTS:
            raise ValueError(f"Unknown latency event: {event}")
        now = time.monotonic()
        if replace or event not in self._events:
            self._events[event] = now
        return self._events[event]

    def begin_context_build(self) -> None:
        self._context_started = time.monotonic()

    def add_tool_execution(self, started_at: float) -> None:
        """Accumulate tool wall time while retaining the latest completion event."""
        self._tool_execution_seconds += max(0.0, time.monotonic() - started_at)
        self.mark("tool_execution_finished", replace=True)

    def finish(self) -> dict[str, object]:
        """Publish a complete event and metric record with non-negative data."""
        finished_at = self.mark("response_finished", replace=True)

        # Terminal routing, cancellation, or provider failure can skip a phase.
        # Keep the record schema stable without inventing negative durations.
        for event in LATENCY_EVENTS:
            self._events.setdefault(event, finished_at)

        context_started = self._context_started or self._events["reflection_check_finished"]
        provider_started = self._events["provider_request_started"]
        metrics = {
            "reflection_wait_ms": _elapsed_ms(
                self._events["request_received"], self._events["reflection_check_finished"]
            ),
            "context_build_ms": _elapsed_ms(context_started, self._events["context_build_finished"]),
            "provider_ttft_ms": _elapsed_ms(
                provider_started, self._events["provider_first_token_received"]
            ),
            "ares_ttft_ms": _elapsed_ms(
                self._events["request_received"], self._events["first_token_sent"]
            ),
            "tool_execution_ms": round(max(0.0, self._tool_execution_seconds) * 1000.0, 3),
            "generation_ms": _elapsed_ms(provider_started, finished_at),
            "total_ms": _elapsed_ms(self._events["request_received"], finished_at),
        }
        events = {
            event: _elapsed_ms(self._events["request_received"], self._events[event])
            for event in LATENCY_EVENTS
        }
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "model": self.model,
            "tool_schema_count": max(0, int(self.tool_schema_count)),
            "events": events,
            "metrics": metrics,
        }
