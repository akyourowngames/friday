"""Situational Awareness Layer.

Fuses passive activity signals into two continuous estimates:
- `cognitive_load`  (0..1): how heads-down the user is right now
- `availability`    (0..1): how reachable/idle-and-present the user is

These gate every proactive utterance. Default posture is silence: when load is
high or availability is low, KING stays quiet.

Signals are timestamps fed in by whatever is already producing them (the
folder_watcher bus, gesture activity, conversation turns). This module never
imports those subsystems directly; it just consumes timestamps, so it stays
modular and testable offline. No regex, no keyword logic, pure arithmetic.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from .config import section_values
from .util import clamp01, half_life_decay

_DEFAULTS = {
    "busy_event_window_seconds": 120,
    "busy_event_count_for_full_load": 8,
    "idle_seconds_for_available": 600,
    "rapid_turn_window_seconds": 90,
    "rapid_turn_count_for_engaged": 4,
    "load_decay_half_life_seconds": 300,
    "min_availability_to_speak": 0.45,
    "max_load_to_speak": 0.6,
}


class SituationModel:
    def __init__(self, config: dict | None = None, max_signals: int = 256):
        self._cfg = dict(_DEFAULTS)
        if config:
            self._cfg.update({k: v for k, v in config.items() if k in self._cfg})
        else:
            self._cfg = section_values("situation", _DEFAULTS)
        self._max_signals = max(8, int(max_signals))
        self._events: deque[datetime] = deque(maxlen=self._max_signals)
        self._turns: deque[datetime] = deque(maxlen=self._max_signals)

    def record_event(self, when: datetime | None = None) -> None:
        """Record ambient activity (file change, gesture, etc.)."""
        self._events.append(when or datetime.now())

    def record_turn(self, when: datetime | None = None) -> None:
        """Record a conversational turn (user spoke / KING replied)."""
        self._turns.append(when or datetime.now())

    def _count_within(self, stamps: deque[datetime], window_seconds: float, now: datetime) -> int:
        if window_seconds <= 0:
            return 0
        count = 0
        for stamp in reversed(stamps):
            if (now - stamp).total_seconds() <= window_seconds:
                count += 1
            else:
                break
        return count

    def _seconds_since_last(self, stamps: deque[datetime], now: datetime) -> float | None:
        if not stamps:
            return None
        return max(0.0, (now - stamps[-1]).total_seconds())

    def cognitive_load(self, now: datetime | None = None) -> float:
        now = now or datetime.now()
        cfg = self._cfg
        recent_events = self._count_within(self._events, cfg["busy_event_window_seconds"], now)
        recent_turns = self._count_within(self._turns, cfg["rapid_turn_window_seconds"], now)

        event_load = clamp01(recent_events / max(1, cfg["busy_event_count_for_full_load"]))
        turn_load = clamp01(recent_turns / max(1, cfg["rapid_turn_count_for_engaged"]))
        raw_load = max(event_load, turn_load)

        # Decay load toward zero as the most recent signal ages.
        last_event = self._seconds_since_last(self._events, now)
        last_turn = self._seconds_since_last(self._turns, now)
        ages = [age for age in (last_event, last_turn) if age is not None]
        if not ages:
            return 0.0
        freshness = half_life_decay(min(ages), cfg["load_decay_half_life_seconds"])
        return clamp01(raw_load * freshness)

    def availability(self, now: datetime | None = None) -> float:
        now = now or datetime.now()
        cfg = self._cfg
        last_event = self._seconds_since_last(self._events, now)
        last_turn = self._seconds_since_last(self._turns, now)
        ages = [age for age in (last_event, last_turn) if age is not None]
        if not ages:
            # No signal at all: treat as quietly available (present but idle).
            return 1.0
        idle_seconds = min(ages)
        idle_target = max(1.0, float(cfg["idle_seconds_for_available"]))
        idle_ratio = clamp01(idle_seconds / idle_target)
        # Lower current load also raises availability.
        load = self.cognitive_load(now)
        return clamp01(0.5 * idle_ratio + 0.5 * (1.0 - load))

    def can_interrupt(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        cfg = self._cfg
        return (
            self.availability(now) >= cfg["min_availability_to_speak"]
            and self.cognitive_load(now) <= cfg["max_load_to_speak"]
        )

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        return {
            "cognitive_load": round(self.cognitive_load(now), 3),
            "availability": round(self.availability(now), 3),
            "can_interrupt": self.can_interrupt(now),
            "event_signals": len(self._events),
            "turn_signals": len(self._turns),
        }
