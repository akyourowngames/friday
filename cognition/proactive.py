"""Proactive Intelligence Engine.

Holds a queue of proactive candidates and decides whether any of them earns the
right to be spoken right now. The pipeline every candidate passes through:

    Signal -> Candidate -> Relevance -> Situational Gate -> Confidence
           -> Attention Budget (daily cap + novelty) -> Speak

Key anti-annoyance mechanisms:
- Default is silence. A candidate must clear the adaptive threshold AND the
  situational gate AND the daily budget AND the novelty check.
- The confidence threshold is adaptive: it jumps up right after KING speaks and
  decays back over time, so KING never fires twice in a row.
- Novelty: a candidate semantically near something already delivered today is
  suppressed (cosine dedup).
- Annoyance learning: dismissals of a source raise that source's penalty.

Scoring and novelty use embeddings + arithmetic only. No regex, no keyword
tables, no hardcoded phrases. Every weight/threshold comes from the markdown
control surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import section_values
from .util import clamp01, cosine, half_life_decay, parse_iso, safe_float

_DEFAULTS = {
    "base_confidence_threshold": 0.55,
    "threshold_rise_after_speak": 0.25,
    "threshold_decay_half_life_seconds": 3600,
    "daily_budget": 3,
    "novelty_suppression_similarity": 0.8,
    "relevance_weight": 0.4,
    "freshness_weight": 0.2,
    "importance_weight": 0.2,
    "situational_weight": 0.2,
    "freshness_half_life_seconds": 86400,
    "annoyance_penalty_per_dismissal": 0.15,
    "max_queue_size": 50,
}


@dataclass
class Candidate:
    content: str
    source: str
    importance: float = 0.5
    relevance: float = 0.5
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    node: str = ""
    embedding: list | None = None

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "source": self.source,
            "importance": self.importance,
            "relevance": self.relevance,
            "created_at": self.created_at,
            "node": self.node,
        }


class ProactiveEngine:
    def __init__(self, state: dict | None = None, config: dict | None = None):
        self._cfg = section_values("proactive", _DEFAULTS) if config is None else {**_DEFAULTS, **config}
        self._queue: list[Candidate] = []
        self._delivered_today: list[dict] = []
        self._delivered_date: str = ""
        self._dismissals: dict[str, int] = {}
        self._last_spoke_at: str = ""
        if isinstance(state, dict):
            self._delivered_today = list(state.get("delivered_today") or [])
            self._delivered_date = str(state.get("delivered_date") or "")
            self._dismissals = {str(k): int(v) for k, v in (state.get("dismissals") or {}).items()}
            self._last_spoke_at = str(state.get("last_spoke_at") or "")

    # --- queue management ---

    def add_candidate(self, candidate: Candidate) -> None:
        if not str(candidate.content or "").strip():
            return
        self._queue.append(candidate)
        max_queue = int(self._cfg["max_queue_size"])
        if len(self._queue) > max_queue:
            self._queue = self._queue[-max_queue:]

    def queue_size(self) -> int:
        return len(self._queue)

    # --- threshold + budget ---

    def current_threshold(self, now: datetime | None = None) -> float:
        now = now or datetime.now()
        base = clamp01(self._cfg["base_confidence_threshold"])
        last = parse_iso(self._last_spoke_at)
        if last is None:
            return base
        age = max(0.0, (now - last).total_seconds())
        rise = clamp01(self._cfg["threshold_rise_after_speak"])
        decay = half_life_decay(age, self._cfg["threshold_decay_half_life_seconds"])
        return clamp01(base + rise * decay)

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._delivered_date != today:
            self._delivered_date = today
            self._delivered_today = []

    def budget_remaining(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        self._roll_day(now)
        return max(0, int(self._cfg["daily_budget"]) - len(self._delivered_today))

    # --- scoring ---

    def _freshness(self, candidate: Candidate, now: datetime) -> float:
        created = parse_iso(candidate.created_at)
        if created is None:
            return 1.0
        age = max(0.0, (now - created).total_seconds())
        return half_life_decay(age, self._cfg["freshness_half_life_seconds"])

    def score(self, candidate: Candidate, situational_fit: float, now: datetime | None = None) -> float:
        now = now or datetime.now()
        cfg = self._cfg
        relevance = clamp01(candidate.relevance)
        freshness = self._freshness(candidate, now)
        importance = clamp01(candidate.importance)
        fit = clamp01(situational_fit)

        weighted = (
            cfg["relevance_weight"] * relevance
            + cfg["freshness_weight"] * freshness
            + cfg["importance_weight"] * importance
            + cfg["situational_weight"] * fit
        )
        weight_total = (
            cfg["relevance_weight"]
            + cfg["freshness_weight"]
            + cfg["importance_weight"]
            + cfg["situational_weight"]
        )
        base = clamp01(weighted / weight_total) if weight_total > 0 else 0.0

        penalty = clamp01(self._dismissals.get(candidate.source, 0) * safe_float(cfg["annoyance_penalty_per_dismissal"]))
        return clamp01(base * (1.0 - penalty))

    def _is_novel(self, candidate: Candidate) -> bool:
        if candidate.embedding is None:
            return True
        threshold = float(self._cfg["novelty_suppression_similarity"])
        for delivered in self._delivered_today:
            prior = delivered.get("embedding")
            if prior is None:
                continue
            if cosine(candidate.embedding, prior) >= threshold:
                return False
        return True

    # --- decision ---

    def select(self, situational_fit: float, now: datetime | None = None) -> Candidate | None:
        """Return the single best candidate that clears every gate, or None.

        Does not mutate state; call `mark_delivered` after actually speaking.
        """
        now = now or datetime.now()
        if self.budget_remaining(now) <= 0 or not self._queue:
            return None

        threshold = self.current_threshold(now)
        scored = []
        for candidate in self._queue:
            value = self.score(candidate, situational_fit, now)
            if value >= threshold and self._is_novel(candidate):
                scored.append((value, candidate))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1]

    def mark_delivered(self, candidate: Candidate, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self._roll_day(now)
        self._delivered_today.append(
            {
                "content": candidate.content,
                "source": candidate.source,
                "embedding": candidate.embedding,
                "at": now.isoformat(timespec="seconds"),
            }
        )
        self._last_spoke_at = now.isoformat(timespec="seconds")
        self._queue = [c for c in self._queue if c is not candidate]

    def record_dismissal(self, source: str) -> None:
        source = str(source or "").strip()
        if source:
            self._dismissals[source] = self._dismissals.get(source, 0) + 1

    def to_dict(self) -> dict:
        return {
            "delivered_today": self._delivered_today,
            "delivered_date": self._delivered_date,
            "dismissals": self._dismissals,
            "last_spoke_at": self._last_spoke_at,
        }

    @classmethod
    def from_dict(cls, state: dict, config: dict | None = None) -> "ProactiveEngine":
        return cls(state=state, config=config)
