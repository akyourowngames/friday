"""Deterministic visual-watch rule parsing and evaluation.

The engine has no database dependency.  Persistence layers can load a
``VisionWatch`` and pass scene events/snapshots in, then save the resulting
status transition.  That keeps a watch usable for camera, screen, and uploaded
image flows alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .events import make_visual_event, value_for
from .models import SceneSnapshot, VisualEvent, VisionWatch
from .scene import normalise_visible_text


CONDITION_OBJECT_APPEARS = "object_appears"
CONDITION_OBJECT_DISAPPEARS = "object_disappears"
CONDITION_OBJECT_MOVES = "object_moves"
CONDITION_ENTERS_REGION = "enters_region"
CONDITION_LEAVES_REGION = "leaves_region"
CONDITION_TEXT_CONTAINS = "text_contains"
CONDITION_TEXT_CHANGED = "text_changed"
CONDITION_PROGRESS_REACHES = "progress_reaches"
CONDITION_SCENE_UNCHANGED = "scene_unchanged"
CONDITION_SEMANTIC = "semantic"

_ACTIVE_STATUSES = {"active", "pending", "running"}
_REGION_NAMES = (
    "top_left",
    "top_center",
    "top_right",
    "center_left",
    "center",
    "center_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
)


@dataclass(slots=True)
class WatchRule:
    """A parsed condition that can be evaluated without an LLM."""

    condition_type: str
    target_labels: list[str]
    region: str | None = None
    phrase: str | None = None
    threshold: float | None = None
    unchanged_seconds: float | None = None


@dataclass(slots=True)
class WatchEvaluation:
    """Detailed result for an individual watch evaluation."""

    watch: VisionWatch
    status: str
    triggered: bool = False
    event: VisualEvent | None = None
    reason: str | None = None
    rule: WatchRule | None = None


@dataclass(slots=True)
class _UnchangedState:
    fingerprint: str
    stable_since: datetime


def _as_text(value: object | None) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _normalise_label(value: object) -> str:
    text = " ".join(_as_text(value).casefold().replace("_", " ").split())
    return re.sub(r"^(?:the|a|an|this|that)\s+", "", text).strip(" .,!?:;")


def _normalise_region(value: object | None) -> str | None:
    text = _normalise_label(value or "").replace("-", " ")
    text = re.sub(r"\b(?:the|area|side|region)\b", "", text)
    text = " ".join(text.split())
    if text in {"middle", "centre"}:
        return "center"
    candidate = text.replace(" ", "_")
    if candidate in _REGION_NAMES:
        return candidate
    # A horizontal/vertical short form is intentionally mapped to the middle
    # cell, which gives a useful deterministic interpretation of "right side".
    if text in {"left", "right"}:
        return f"center_{text}"
    if text in {"top", "bottom"}:
        return f"{text}_center"
    return None


def _normalise_condition_type(value: object | None) -> str:
    text = _as_text(value).casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "appear": CONDITION_OBJECT_APPEARS,
        "appears": CONDITION_OBJECT_APPEARS,
        "object_appear": CONDITION_OBJECT_APPEARS,
        "object_appears": CONDITION_OBJECT_APPEARS,
        "object_appeared": CONDITION_OBJECT_APPEARS,
        "disappear": CONDITION_OBJECT_DISAPPEARS,
        "disappears": CONDITION_OBJECT_DISAPPEARS,
        "object_disappear": CONDITION_OBJECT_DISAPPEARS,
        "object_disappears": CONDITION_OBJECT_DISAPPEARS,
        "object_disappeared": CONDITION_OBJECT_DISAPPEARS,
        "move": CONDITION_OBJECT_MOVES,
        "moves": CONDITION_OBJECT_MOVES,
        "moved": CONDITION_OBJECT_MOVES,
        "object_move": CONDITION_OBJECT_MOVES,
        "object_moves": CONDITION_OBJECT_MOVES,
        "object_moved": CONDITION_OBJECT_MOVES,
        "enter": CONDITION_ENTERS_REGION,
        "enters": CONDITION_ENTERS_REGION,
        "enter_region": CONDITION_ENTERS_REGION,
        "enters_region": CONDITION_ENTERS_REGION,
        "leave": CONDITION_LEAVES_REGION,
        "leaves": CONDITION_LEAVES_REGION,
        "leave_region": CONDITION_LEAVES_REGION,
        "leaves_region": CONDITION_LEAVES_REGION,
        "text_contains": CONDITION_TEXT_CONTAINS,
        "contains_text": CONDITION_TEXT_CONTAINS,
        "text_change": CONDITION_TEXT_CHANGED,
        "text_changed": CONDITION_TEXT_CHANGED,
        "progress": CONDITION_PROGRESS_REACHES,
        "progress_reaches": CONDITION_PROGRESS_REACHES,
        "progress_reached": CONDITION_PROGRESS_REACHES,
        "unchanged": CONDITION_SCENE_UNCHANGED,
        "scene_unchanged": CONDITION_SCENE_UNCHANGED,
        "semantic": CONDITION_SEMANTIC,
    }
    return aliases.get(text, text if text in aliases.values() else "")


def _extract_region(text: str) -> str | None:
    lowered = text.casefold().replace("-", " ")
    for region in _REGION_NAMES:
        if region.replace("_", " ") in lowered:
            return region
    match = re.search(r"\b(?:into|in|to|from|out of|leaves?)\s+(?:the\s+)?(top|bottom|left|right|center|centre|middle)(?:\s+(?:side|area|region))?\b", lowered)
    return _normalise_region(match.group(1)) if match else None


def _extract_duration_seconds(text: str) -> float | None:
    match = re.search(r"\bfor\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", text.casefold())
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit.startswith(("minute", "min")):
        amount *= 60
    elif unit.startswith(("hour", "hr")):
        amount *= 3600
    return amount


def _extract_threshold(text: str, condition_type: str) -> float | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:%|percent)\b", text.casefold())
    if not match and condition_type == CONDITION_PROGRESS_REACHES:
        match = re.search(r"\b(?:reaches?|hits?|gets?\s+to)\s+(\d+(?:\.\d+)?)\b", text.casefold())
    return float(match.group(1)) if match else None


def _extract_phrase(text: str) -> str | None:
    quoted = re.search(r"[\"']([^\"']+)[\"']", text)
    if quoted:
        return " ".join(quoted.group(1).split())
    match = re.search(r"\b(?:text\s+)?(?:contains?|includes?|shows?|reads?)\s+(.+?)(?:[.?!]|$)", text, re.IGNORECASE)
    if not match:
        return None
    phrase = match.group(1)
    phrase = re.sub(r"\b(?:on (?:the )?screen|in (?:the )?text)\b.*$", "", phrase, flags=re.IGNORECASE)
    phrase = phrase.strip(" \"'.")
    return phrase or None


def _extract_labels(text: str, condition_type: str) -> list[str]:
    lowered = " ".join(text.casefold().replace("_", " ").split())
    patterns: list[str] = []
    if condition_type == CONDITION_OBJECT_APPEARS:
        patterns = [r"(?:when|if)\s+(?:the\s+)?(.+?)\s+(?:appears?|shows? up)\b", r"watch\s+(?:this\s+|the\s+)?(.+?)(?:\s+and\b|$)"]
    elif condition_type == CONDITION_OBJECT_DISAPPEARS:
        patterns = [r"(?:when|if)\s+(?:the\s+)?(.+?)\s+(?:disappears?|goes away)\b"]
    elif condition_type == CONDITION_OBJECT_MOVES:
        patterns = [r"(?:when|if)\s+(?:the\s+)?(.+?)\s+(?:moves?|is moved)\b", r"watch\s+(?:this\s+|the\s+)?(.+?)(?:\s+and\b|$)"]
    elif condition_type in {CONDITION_ENTERS_REGION, CONDITION_LEAVES_REGION}:
        patterns = [r"(?:when|if)\s+(?:the\s+)?(.+?)\s+(?:enters?|leaves?|moves?\s+(?:into|out of))\b"]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            candidate = _normalise_label(match.group(1))
            candidate = re.sub(r"\b(?:the|a|an)\b", "", candidate).strip()
            if candidate and candidate not in {"it", "object", "screen", "scene", "text"}:
                return [candidate]
    direct_patterns = {
        CONDITION_OBJECT_APPEARS: r"^(?:the\s+)?(.+?)\s+(?:appears?|shows? up)\b",
        CONDITION_OBJECT_DISAPPEARS: r"^(?:the\s+)?(.+?)\s+(?:disappears?|goes away)\b",
        CONDITION_OBJECT_MOVES: r"^(?:the\s+)?(.+?)\s+(?:moves?|is moved)\b",
        CONDITION_ENTERS_REGION: r"^(?:the\s+)?(.+?)\s+(?:enters?|moves?\s+into)\b",
        CONDITION_LEAVES_REGION: r"^(?:the\s+)?(.+?)\s+(?:leaves?|moves?\s+out of)\b",
    }
    direct = direct_patterns.get(condition_type)
    if direct:
        # Support terse persisted rules such as "cup moves" as well as a
        # full conversational phrase where the "when" clause was stripped.
        remainder = re.sub(r"^.*?\b(?:when|if)\s+", "", lowered)
        match = re.search(direct, remainder)
        if match:
            candidate = _normalise_label(match.group(1))
            if candidate and candidate not in {"it", "object", "screen", "scene", "text"}:
                return [candidate]
    return []


def parse_watch_condition(
    condition_text: str,
    *,
    target_labels: Sequence[str] | None = None,
    condition_type: str | None = None,
) -> WatchRule:
    """Parse V1's supported natural-language watch conditions.

    Unsupported wording is marked ``semantic`` rather than guessed.  A caller
    can then route it to a multimodal evaluator only after a candidate change.
    """

    text = " ".join((condition_text or "").split())
    lowered = text.casefold()
    kind = _normalise_condition_type(condition_type)
    if not kind:
        if re.search(r"\b(?:progress|download).*(?:reaches?|hits?|gets?\s+to)|\b\d+(?:\.\d+)?\s*(?:%|percent)\b", lowered):
            kind = CONDITION_PROGRESS_REACHES
        elif re.search(r"\b(?:download|upload|install(?:ation)?)\b.*\b(?:finishes?|finished|completes?|completed|done)\b", lowered):
            kind = CONDITION_PROGRESS_REACHES
        elif re.search(r"\b(?:remain(?:s)?|stay(?:s)?)\s+unchanged\b|\bunchanged\b", lowered):
            kind = CONDITION_SCENE_UNCHANGED
        elif re.search(r"\btext\s+(?:changes?|changed)\b", lowered):
            kind = CONDITION_TEXT_CHANGED
        elif re.search(r"\b(?:text\s+)?(?:contains?|includes?|shows?|reads?)\b", lowered) and ("text" in lowered or "screen" in lowered):
            kind = CONDITION_TEXT_CONTAINS
        elif re.search(r"\b(?:enters?|moves?\s+into)\b", lowered):
            kind = CONDITION_ENTERS_REGION
        elif re.search(r"\b(?:leaves?|moves?\s+out of)\b", lowered):
            kind = CONDITION_LEAVES_REGION
        elif re.search(r"\b(?:appears?|shows? up)\b", lowered):
            kind = CONDITION_OBJECT_APPEARS
        elif re.search(r"\b(?:disappears?|goes away)\b", lowered):
            kind = CONDITION_OBJECT_DISAPPEARS
        elif re.search(r"\b(?:moves?|is moved)\b", lowered):
            kind = CONDITION_OBJECT_MOVES
        else:
            kind = CONDITION_SEMANTIC
    labels = [_normalise_label(label) for label in (target_labels or []) if _normalise_label(label)]
    if not labels:
        labels = _extract_labels(text, kind)
    threshold = _extract_threshold(text, kind) if kind == CONDITION_PROGRESS_REACHES else None
    if threshold is None and kind == CONDITION_PROGRESS_REACHES and re.search(r"\b(?:finishes?|finished|completes?|completed|done)\b", lowered):
        threshold = 100.0
    return WatchRule(
        condition_type=kind,
        target_labels=labels,
        region=_extract_region(text) if kind in {CONDITION_ENTERS_REGION, CONDITION_LEAVES_REGION} else None,
        phrase=_extract_phrase(text) if kind == CONDITION_TEXT_CONTAINS else None,
        threshold=threshold,
        unchanged_seconds=_extract_duration_seconds(text) if kind == CONDITION_SCENE_UNCHANGED else None,
    )


# A concise alias that reads nicely in tests and integrations.
parse_condition = parse_watch_condition


def _aware(value: datetime | str | None, fallback: Callable[[], datetime]) -> datetime:
    if value is None:
        current = fallback()
    elif isinstance(value, str):
        current = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        current = value
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def is_watch_expired(watch: VisionWatch | object, now: datetime | None = None) -> bool:
    """Return whether a watch has reached its optional expiration timestamp."""

    expires_at = value_for(watch, "expires_at")
    if expires_at is None:
        return False
    current = _aware(now, lambda: datetime.now(timezone.utc))
    return _aware(expires_at, lambda: current) <= current


def _watch_id(watch: VisionWatch | object) -> str:
    return _as_text(value_for(watch, "watch_id") or value_for(watch, "id") or "anonymous-watch")


def _watch_status(watch: VisionWatch | object) -> str:
    return _as_text(value_for(watch, "status", "active")).casefold()


def _set_watch_status(watch: VisionWatch | object, status: str) -> None:
    if isinstance(watch, dict):
        watch["status"] = status
        return
    current = value_for(watch, "status")
    enum_type = type(current)
    try:
        replacement = enum_type(status) if hasattr(enum_type, "__members__") else status
    except (TypeError, ValueError):
        replacement = status
    try:
        setattr(watch, "status", replacement)
    except (AttributeError, TypeError):
        # A frozen model can still be used for evaluation; its persistence layer
        # receives the resulting status in WatchEvaluation.
        pass


def _event_type(event: VisualEvent | object) -> str:
    return _normalise_condition_type(value_for(event, "event_type")) or _as_text(value_for(event, "event_type")).casefold()


def _event_source_matches(watch: VisionWatch | object, event: VisualEvent | object) -> bool:
    source = _as_text(value_for(watch, "source_id"))
    return not source or source == _as_text(value_for(event, "source_id"))


def _snapshot_source_matches(watch: VisionWatch | object, snapshot: SceneSnapshot | None) -> bool:
    if snapshot is None:
        return False
    source = _as_text(value_for(watch, "source_id"))
    return not source or source == _as_text(value_for(snapshot, "source_id"))


def _event_state(event: VisualEvent | object, name: str) -> dict[str, Any]:
    raw = value_for(event, name, {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _labels_match(expected: Sequence[str], event: VisualEvent | object) -> bool:
    if not expected:
        return True
    state = _event_state(event, "current_state") or _event_state(event, "previous_state")
    candidates = [
        _normalise_label(value_for(event, "subject", "")),
        _normalise_label(state.get("label", "")),
    ]
    attributes = state.get("attributes")
    if isinstance(attributes, Mapping):
        candidates.extend(_normalise_label(attributes.get(key, "")) for key in ("label", "class", "name"))
    for wanted in expected:
        for candidate in candidates:
            if not candidate:
                continue
            if wanted == candidate or wanted in candidate.split() or candidate in wanted.split():
                return True
    return False


def _snapshot_text(snapshot: SceneSnapshot | None) -> list[str]:
    return [str(item) for item in (value_for(snapshot, "visible_text", []) or [])] if snapshot is not None else []


def _event_text(event: VisualEvent | object, *, include_previous: bool = False) -> list[str]:
    lines: list[str] = []
    names = ("current_state", "previous_state") if include_previous else ("current_state",)
    for name in names:
        state = _event_state(event, name)
        raw = state.get("visible_text") or state.get("text")
        if isinstance(raw, str):
            lines.append(raw)
        elif isinstance(raw, Sequence):
            lines.extend(str(item) for item in raw)
    return lines


_PERCENT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:%|percent\b)", re.IGNORECASE)


def _progress_values(value: object) -> list[float]:
    values: list[float] = []
    if isinstance(value, str):
        values.extend(float(match.group(1)) for match in _PERCENT_PATTERN.finditer(value))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        values.append(float(value))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in {"progress", "percent", "percentage", "value", "visible_text", "text"}:
                values.extend(_progress_values(item))
            elif isinstance(item, (Mapping, list, tuple)):
                values.extend(_progress_values(item))
    elif isinstance(value, Sequence):
        for item in value:
            values.extend(_progress_values(item))
    return values


class WatchEngine:
    """Evaluate local V1 watch rules and apply one-shot watch transitions."""

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime] | None = None,
        default_unchanged_seconds: float = 30.0,
    ) -> None:
        if default_unchanged_seconds < 0:
            raise ValueError("default_unchanged_seconds must be non-negative")
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.default_unchanged_seconds = float(default_unchanged_seconds)
        self._last_triggered: dict[str, datetime] = {}
        self._unchanged: dict[str, _UnchangedState] = {}

    def rule_for(self, watch: VisionWatch | object) -> WatchRule:
        declared_type = _as_text(value_for(watch, "condition_type"))
        # ``VisionWatch`` defaults to ``semantic`` before a service parser has
        # populated it.  Let a plainly deterministic condition promote itself
        # instead of making direct model construction unexpectedly inert.
        if declared_type.casefold() == CONDITION_SEMANTIC:
            declared_type = ""
        return parse_watch_condition(
            _as_text(value_for(watch, "condition_text")),
            target_labels=value_for(watch, "target_labels", []) or [],
            condition_type=declared_type or None,
        )

    parse = rule_for

    def evaluate(
        self,
        watch: VisionWatch,
        events: Iterable[VisualEvent] | VisualEvent | None = None,
        snapshot: SceneSnapshot | None = None,
        *,
        now: datetime | None = None,
    ) -> list[VisualEvent]:
        """Evaluate one watch and return zero or one ``watch_condition_met`` event."""

        result = self.evaluate_watch(watch, events=events, snapshot=snapshot, now=now)
        return [result.event] if result.event is not None else []

    def evaluate_watch(
        self,
        watch: VisionWatch,
        events: Iterable[VisualEvent] | VisualEvent | None = None,
        snapshot: SceneSnapshot | None = None,
        *,
        now: datetime | None = None,
    ) -> WatchEvaluation:
        """Evaluate one watch and return its transition/evidence in detail."""

        event_list = self._coerce_events(events)
        reference_time = now
        if reference_time is None and snapshot is not None:
            reference_time = value_for(snapshot, "captured_at")
        if reference_time is None and event_list:
            reference_time = value_for(event_list[-1], "occurred_at")
        current = _aware(reference_time, self.now_provider)
        status = _watch_status(watch)
        rule = self.rule_for(watch)
        watch_id = _watch_id(watch)
        if is_watch_expired(watch, current):
            _set_watch_status(watch, "expired")
            self._unchanged.pop(watch_id, None)
            return WatchEvaluation(watch=watch, status="expired", reason="watch expired", rule=rule)
        if status not in _ACTIVE_STATUSES:
            return WatchEvaluation(watch=watch, status=status, reason="watch is not active", rule=rule)
        if rule.condition_type == CONDITION_SEMANTIC:
            return WatchEvaluation(watch=watch, status=status, reason="condition requires semantic evaluation", rule=rule)

        matched_event, evidence, confidence = self._find_match(watch, rule, event_list, snapshot, current)
        if evidence is None:
            return WatchEvaluation(watch=watch, status=status, reason="condition not met", rule=rule)
        cooldown = max(0, int(value_for(watch, "cooldown_seconds", 0) or 0))
        last_triggered = self._last_triggered.get(watch_id)
        if last_triggered is not None and current < last_triggered + timedelta(seconds=cooldown):
            return WatchEvaluation(watch=watch, status=status, reason="watch cooldown active", rule=rule)

        trigger = self._make_trigger_event(watch, rule, matched_event, evidence, confidence, current)
        self._last_triggered[watch_id] = current
        self._unchanged.pop(watch_id, None)
        # V1 watches are intentionally one-shot.  This makes an active visual
        # condition deliver a single useful notification rather than a stream.
        _set_watch_status(watch, "completed")
        return WatchEvaluation(watch=watch, status="completed", triggered=True, event=trigger, rule=rule)

    def evaluate_all(
        self,
        watches: Iterable[VisionWatch],
        events: Iterable[VisualEvent] | VisualEvent | None = None,
        snapshot: SceneSnapshot | None = None,
        *,
        now: datetime | None = None,
    ) -> list[VisualEvent]:
        """Evaluate every active watch against one scene update."""

        triggered: list[VisualEvent] = []
        event_list = self._coerce_events(events)
        for watch in watches:
            triggered.extend(self.evaluate(watch, event_list, snapshot, now=now))
        return triggered

    process = evaluate_all

    def expire(self, watches: Iterable[VisionWatch], *, now: datetime | None = None) -> list[VisionWatch]:
        """Mark due watches expired and return the transitioned watches."""

        current = _aware(now, self.now_provider)
        expired: list[VisionWatch] = []
        for watch in watches:
            if _watch_status(watch) in _ACTIVE_STATUSES and is_watch_expired(watch, current):
                _set_watch_status(watch, "expired")
                self._unchanged.pop(_watch_id(watch), None)
                expired.append(watch)
        return expired

    @staticmethod
    def _coerce_events(events: Iterable[VisualEvent] | VisualEvent | None) -> list[VisualEvent]:
        if events is None:
            return []
        if isinstance(events, VisualEvent):
            return [events]
        return list(events)

    def _find_match(
        self,
        watch: VisionWatch,
        rule: WatchRule,
        events: Sequence[VisualEvent],
        snapshot: SceneSnapshot | None,
        current: datetime,
    ) -> tuple[VisualEvent | None, dict[str, Any] | None, float]:
        if rule.condition_type == CONDITION_TEXT_CONTAINS:
            phrase = (rule.phrase or "").casefold()
            if not phrase:
                return None, None, 0.0
            text = _snapshot_text(snapshot) if _snapshot_source_matches(watch, snapshot) else []
            for event in events:
                if _event_source_matches(watch, event):
                    text.extend(_event_text(event))
            haystack = "\n".join(normalise_visible_text(text))
            if phrase in haystack:
                return None, {"phrase": rule.phrase, "visible_text": text}, 0.92
            return None, None, 0.0

        if rule.condition_type == CONDITION_PROGRESS_REACHES:
            values = _progress_values(_snapshot_text(snapshot)) if _snapshot_source_matches(watch, snapshot) else []
            for event in events:
                if _event_source_matches(watch, event):
                    values.extend(_progress_values(_event_state(event, "current_state")))
            threshold = rule.threshold
            if threshold is None or not values:
                return None, None, 0.0
            reached = max(values)
            if reached >= threshold:
                return None, {"progress": reached, "threshold": threshold}, min(1.0, 0.85 + (reached - threshold) / 1000.0)
            return None, None, 0.0

        if rule.condition_type == CONDITION_SCENE_UNCHANGED:
            if not _snapshot_source_matches(watch, snapshot):
                return None, None, 0.0
            fingerprint = self._snapshot_fingerprint(snapshot)
            watch_id = _watch_id(watch)
            state = self._unchanged.get(watch_id)
            if state is None or state.fingerprint != fingerprint:
                self._unchanged[watch_id] = _UnchangedState(fingerprint=fingerprint, stable_since=current)
                return None, None, 0.0
            duration = rule.unchanged_seconds if rule.unchanged_seconds is not None else self.default_unchanged_seconds
            if current >= state.stable_since + timedelta(seconds=duration):
                return None, {"unchanged_seconds": (current - state.stable_since).total_seconds(), "required_seconds": duration}, 0.90
            return None, None, 0.0

        event_type_map = {
            CONDITION_OBJECT_APPEARS: "object_appeared",
            CONDITION_OBJECT_DISAPPEARS: "object_disappeared",
            CONDITION_OBJECT_MOVES: "object_moved",
            CONDITION_TEXT_CHANGED: "text_changed",
        }
        for event in events:
            if not _event_source_matches(watch, event):
                continue
            raw_type = _as_text(value_for(event, "event_type")).casefold()
            if rule.condition_type in event_type_map:
                needs_label_match = rule.condition_type != CONDITION_TEXT_CHANGED
                if raw_type != event_type_map[rule.condition_type] or (needs_label_match and not _labels_match(rule.target_labels, event)):
                    continue
                return event, {"matched_event": value_for(event, "event_id"), "event_type": raw_type}, float(value_for(event, "confidence", 0.8))
            if rule.condition_type == CONDITION_ENTERS_REGION:
                if raw_type not in {"object_moved", "object_appeared"} or not _labels_match(rule.target_labels, event):
                    continue
                previous, state = _event_state(event, "previous_state"), _event_state(event, "current_state")
                if rule.region and _normalise_region(state.get("region")) == rule.region and _normalise_region(previous.get("region")) != rule.region:
                    return event, {"region": rule.region, "matched_event": value_for(event, "event_id")}, float(value_for(event, "confidence", 0.8))
            if rule.condition_type == CONDITION_LEAVES_REGION:
                if raw_type not in {"object_moved", "object_disappeared"} or not _labels_match(rule.target_labels, event):
                    continue
                previous, state = _event_state(event, "previous_state"), _event_state(event, "current_state")
                if rule.region and _normalise_region(previous.get("region")) == rule.region and _normalise_region(state.get("region")) != rule.region:
                    return event, {"region": rule.region, "matched_event": value_for(event, "event_id")}, float(value_for(event, "confidence", 0.8))
        return None, None, 0.0

    @staticmethod
    def _snapshot_fingerprint(snapshot: SceneSnapshot) -> str:
        objects = []
        for item in value_for(snapshot, "objects", []) or []:
            state = value_for(item, "attributes", {}) or {}
            objects.append(
                (
                    _normalise_label(value_for(item, "label")),
                    _as_text(value_for(item, "tracker_id")),
                    tuple(value_for(item, "bounding_box")),
                    tuple(sorted((str(key), repr(value)) for key, value in state.items())) if isinstance(state, Mapping) else (),
                )
            )
        return repr((tuple(sorted(objects)), normalise_visible_text(_snapshot_text(snapshot))))

    def _make_trigger_event(
        self,
        watch: VisionWatch,
        rule: WatchRule,
        matched_event: VisualEvent | None,
        evidence: dict[str, Any],
        confidence: float,
        occurred_at: datetime,
    ) -> VisualEvent:
        source_id = _as_text(value_for(watch, "source_id"))
        if not source_id and matched_event is not None:
            source_id = _as_text(value_for(matched_event, "source_id"))
        subject = rule.target_labels[0] if rule.target_labels else (rule.phrase or None)
        condition_text = _as_text(value_for(watch, "condition_text"))
        matched_payload = {
            "event_id": value_for(matched_event, "event_id") if matched_event is not None else None,
            "event_type": value_for(matched_event, "event_type") if matched_event is not None else None,
            "previous_state": value_for(matched_event, "previous_state") if matched_event is not None else None,
            "current_state": value_for(matched_event, "current_state") if matched_event is not None else None,
        }
        return make_visual_event(
            event_type="watch_condition_met",
            source_id=source_id,
            subject=subject,
            description=f"Watch condition met: {condition_text}",
            confidence=confidence,
            previous_state={
                "watch_id": _watch_id(watch),
                "condition_type": rule.condition_type,
                "condition_text": condition_text,
            },
            current_state={
                "watch_id": _watch_id(watch),
                "notify": bool(value_for(watch, "notify", True)),
                "remember_event": bool(value_for(watch, "remember_event", False)),
                "evidence": evidence,
                "matched_event": matched_payload,
            },
            occurred_at=occurred_at,
            frame_reference=value_for(matched_event, "frame_reference") if matched_event is not None else None,
            # ``remember_event`` is a request.  The event is only marked
            # remembered after the service has passed its memory-permission
            # gate and persisted the corresponding visual memory.
            remembered=False,
        )


VisionWatchEngine = WatchEngine
