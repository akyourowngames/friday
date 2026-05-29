"""Life Cadence Engine.

Builds a per-node rhythm model from timestamped activity. For each tracked node
(a graph node id, project, or activity label) it keeps a 24-hour x 7-weekday
count histogram and an EMA of inter-arrival intervals. From that it derives a
"deviation" signal: the current hour/weekday bucket usually has activity but the
node has gone quiet (or vice versa).

Pure numpy + arithmetic. No ML, no regex, no keyword logic. The model is a plain
dict so it serializes to JSON next to the memory graph.
"""

from __future__ import annotations

from datetime import datetime

from .config import section_values
from .util import clamp01, parse_iso

_DEFAULTS = {
    "buckets_per_day": 24,
    "ema_alpha": 0.3,
    "min_observations_for_signal": 5,
    "deviation_min_strength": 0.4,
    "expected_activity_floor": 0.15,
    "max_tracked_nodes": 200,
}

_WEEKDAYS = 7


class CadenceModel:
    def __init__(self, state: dict | None = None, config: dict | None = None):
        self._cfg = section_values("cadence", _DEFAULTS) if config is None else {**_DEFAULTS, **config}
        self._buckets = max(1, int(self._cfg["buckets_per_day"]))
        self.nodes: dict[str, dict] = {}
        if isinstance(state, dict):
            loaded = state.get("nodes", state)
            if isinstance(loaded, dict):
                for node_id, payload in loaded.items():
                    if isinstance(payload, dict):
                        self.nodes[str(node_id)] = self._normalize_node(payload)

    def _empty_node(self) -> dict:
        return {
            "counts": [[0 for _ in range(self._buckets)] for _ in range(_WEEKDAYS)],
            "observations": 0,
            "last_seen": "",
            "ema_interval_seconds": 0.0,
        }

    def _normalize_node(self, payload: dict) -> dict:
        node = self._empty_node()
        counts = payload.get("counts")
        if isinstance(counts, list) and len(counts) == _WEEKDAYS:
            for weekday in range(_WEEKDAYS):
                row = counts[weekday]
                if isinstance(row, list) and len(row) == self._buckets:
                    node["counts"][weekday] = [int(value) for value in row]
        node["observations"] = int(payload.get("observations", 0))
        node["last_seen"] = str(payload.get("last_seen", ""))
        node["ema_interval_seconds"] = float(payload.get("ema_interval_seconds", 0.0) or 0.0)
        return node

    def _bucket_for(self, when: datetime) -> int:
        seconds_into_day = when.hour * 3600 + when.minute * 60 + when.second
        bucket = int(seconds_into_day * self._buckets / 86400)
        return min(self._buckets - 1, max(0, bucket))

    def observe(self, node_id: str, when: datetime | None = None) -> None:
        node_id = str(node_id or "").strip()
        if not node_id:
            return
        when = when or datetime.now()
        node = self.nodes.get(node_id)
        if node is None:
            if len(self.nodes) >= int(self._cfg["max_tracked_nodes"]):
                self._evict_least_active()
            node = self._empty_node()
            self.nodes[node_id] = node

        weekday = when.weekday()
        bucket = self._bucket_for(when)
        node["counts"][weekday][bucket] += 1
        node["observations"] += 1

        previous = parse_iso(node.get("last_seen"))
        if previous is not None:
            interval = max(0.0, (when - previous).total_seconds())
            alpha = clamp01(self._cfg["ema_alpha"])
            current = node["ema_interval_seconds"]
            node["ema_interval_seconds"] = interval if current <= 0 else (alpha * interval + (1 - alpha) * current)
        node["last_seen"] = when.isoformat(timespec="seconds")

    def _evict_least_active(self) -> None:
        if not self.nodes:
            return
        victim = min(self.nodes.items(), key=lambda item: item[1].get("observations", 0))
        self.nodes.pop(victim[0], None)

    def _expected_activity(self, node: dict, when: datetime) -> float:
        """Normalized expected activity for the current bucket vs the node's own
        peak. 0 means this node is never active now; 1 means this is its peak."""
        counts = node["counts"]
        peak = max((max(row) for row in counts), default=0)
        if peak <= 0:
            return 0.0
        weekday = when.weekday()
        bucket = self._bucket_for(when)
        return clamp01(counts[weekday][bucket] / peak)

    def _recent_activity(self, node: dict, when: datetime) -> float:
        """1.0 if the node was seen within roughly one expected interval, else
        decays toward 0. Uses the EMA interval as the node's own clock."""
        last = parse_iso(node.get("last_seen"))
        if last is None:
            return 0.0
        elapsed = max(0.0, (when - last).total_seconds())
        interval = node.get("ema_interval_seconds", 0.0)
        if interval <= 0:
            return 1.0 if elapsed <= 0 else 0.0
        return clamp01(1.0 - (elapsed / (interval * 2.0)))

    def deviation(self, node_id: str, now: datetime | None = None) -> dict:
        """Return a deviation report for a node at a given moment.

        kind is one of: "none", "missing_expected", "unexpected_active".
        strength is 0..1. Only deviations at or above `deviation_min_strength`
        with enough observations are considered actionable (`actionable`).
        """
        now = now or datetime.now()
        node = self.nodes.get(str(node_id or "").strip())
        if node is None:
            return {"node": node_id, "kind": "none", "strength": 0.0, "actionable": False}

        expected = self._expected_activity(node, now)
        recent = self._recent_activity(node, now)
        floor = clamp01(self._cfg["expected_activity_floor"])
        min_obs = int(self._cfg["min_observations_for_signal"])
        enough = node.get("observations", 0) >= min_obs

        kind = "none"
        strength = 0.0
        if expected >= floor and recent <= (1.0 - expected):
            kind = "missing_expected"
            strength = clamp01(expected * (1.0 - recent))
        elif expected < floor and recent >= 0.8:
            kind = "unexpected_active"
            strength = clamp01((1.0 - expected) * recent)

        actionable = bool(enough and kind != "none" and strength >= clamp01(self._cfg["deviation_min_strength"]))
        return {
            "node": node_id,
            "kind": kind,
            "strength": round(strength, 3),
            "expected": round(expected, 3),
            "recent": round(recent, 3),
            "observations": node.get("observations", 0),
            "actionable": actionable,
        }

    def deviations(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        reports = [self.deviation(node_id, now=now) for node_id in self.nodes]
        actionable = [report for report in reports if report["actionable"]]
        actionable.sort(key=lambda report: report["strength"], reverse=True)
        return actionable

    def to_dict(self) -> dict:
        return {
            "buckets_per_day": self._buckets,
            "nodes": self.nodes,
        }

    @classmethod
    def from_dict(cls, state: dict, config: dict | None = None) -> "CadenceModel":
        return cls(state=state, config=config)
