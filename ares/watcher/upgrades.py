"""Deterministic policy primitives for Ares watcher upgrades.

This module intentionally has no database, scheduler, network, or tool
dependencies.  It gives those layers a single, testable interpretation of
watcher conditions and alert suppression rules while leaving existing watcher
behaviour untouched until a caller elects to use it.

The public functions accept plain mappings so persisted watcher configuration
can be passed through without model migrations.  Their output is normalized,
JSON-friendly data or small dataclasses with ``to_dict`` methods.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class WatcherPolicyError(ValueError):
    """Raised when a watcher condition or alert policy is malformed."""


_MISSING = object()
_OPERATORS = {"all": "all", "and": "all", "any": "any", "or": "any"}
_KINDS = {
    "changed": "changed",
    "change": "changed",
    "regex": "regex",
    "matches": "regex",
    "threshold": "threshold",
    "numeric": "threshold",
    "semantic": "similarity",
    "similarity": "similarity",
    "token_similarity": "similarity",
    "token-similarity": "similarity",
}
_REGEX_FLAGS = {
    "ignorecase": re.IGNORECASE,
    "i": re.IGNORECASE,
    "multiline": re.MULTILINE,
    "m": re.MULTILINE,
    "dotall": re.DOTALL,
    "s": re.DOTALL,
}
_SEVERITY_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}


def _ensure_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WatcherPolicyError(f"{name} must be an object")
    return value


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    raise WatcherPolicyError(f"{name} must be true or false")


def _as_non_negative_number(value: Any, name: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WatcherPolicyError(f"{name} must be a number")
    if not math.isfinite(float(value)) or value < 0:
        raise WatcherPolicyError(f"{name} must be a finite non-negative number")
    if integer:
        if int(value) != value:
            raise WatcherPolicyError(f"{name} must be an integer")
        return int(value)
    return float(value)


def _as_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WatcherPolicyError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise WatcherPolicyError(f"{name} must be a finite number")
    return result


def _string_list(value: Any, name: str) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
    else:
        raise WatcherPolicyError(f"{name} must be a string or a list of strings")
    if not values:
        raise WatcherPolicyError(f"{name} cannot be empty")
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise WatcherPolicyError(f"{name} must contain only non-empty strings")
        if item.strip() not in normalized:
            normalized.append(item.strip())
    return normalized


def _normalize_operator(value: Any) -> str:
    if not isinstance(value, str) or value.casefold().strip() not in _OPERATORS:
        raise WatcherPolicyError("condition operator must be one of AND, OR, all, or any")
    return _OPERATORS[value.casefold().strip()]


def _normalize_regex_flags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        flags = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        flags = list(value)
    else:
        raise WatcherPolicyError("regex flags must be a string or a list of strings")
    normalized: list[str] = []
    for flag in flags:
        if not isinstance(flag, str) or not flag.strip():
            raise WatcherPolicyError("regex flags must contain only non-empty strings")
        canonical = flag.casefold().replace("_", "").replace("-", "")
        if canonical not in _REGEX_FLAGS:
            raise WatcherPolicyError(f"Unsupported regex flag: {flag}")
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _regex_flag_value(flags: Iterable[str]) -> re.RegexFlag:
    result = re.NOFLAG
    for flag in flags:
        result |= _REGEX_FLAGS[flag]
    return result


def _normalize_leaf(raw: Mapping[str, Any]) -> dict[str, Any]:
    raw_kind = raw.get("kind", raw.get("type", raw.get("condition")))
    if not isinstance(raw_kind, str):
        raise WatcherPolicyError("Every condition must declare a type")
    kind = _KINDS.get(raw_kind.casefold().strip())
    if kind is None:
        raise WatcherPolicyError(f"Unsupported watcher condition type: {raw_kind}")

    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise WatcherPolicyError("condition label must be a non-empty string")

    result: dict[str, Any] = {"kind": kind}
    if label is not None:
        result["label"] = label.strip()

    if kind == "changed":
        if "field" in raw:
            if not isinstance(raw["field"], str) or not raw["field"].strip():
                raise WatcherPolicyError("changed.field must be a non-empty string")
            result["field"] = raw["field"].strip()
        if "old_field" in raw:
            if not isinstance(raw["old_field"], str) or not raw["old_field"].strip():
                raise WatcherPolicyError("changed.old_field must be a non-empty string")
            result["old_field"] = raw["old_field"].strip()
        result["expected"] = _as_bool(raw.get("expected", raw.get("equals", True)), "changed.expected")
        return result

    if kind == "regex":
        pattern = raw.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise WatcherPolicyError("regex.pattern is required")
        if len(pattern) > 10_000:
            raise WatcherPolicyError("regex.pattern is too long")
        flags = _normalize_regex_flags(raw.get("flags"))
        try:
            re.compile(pattern, _regex_flag_value(flags))
        except re.error as exc:
            raise WatcherPolicyError(f"Invalid regex pattern: {exc}") from exc
        field_name = raw.get("field", "new_value")
        if not isinstance(field_name, str) or not field_name.strip():
            raise WatcherPolicyError("regex.field must be a non-empty string")
        min_matches = _as_non_negative_number(raw.get("min_matches", 1), "regex.min_matches", integer=True)
        if min_matches < 1:
            raise WatcherPolicyError("regex.min_matches must be at least 1")
        result.update(
            pattern=pattern,
            flags=flags,
            field=field_name.strip(),
            must_match=_as_bool(raw.get("must_match", True), "regex.must_match"),
            min_matches=min_matches,
        )
        return result

    if kind == "threshold":
        field_name = raw.get("field", "new_value")
        if not isinstance(field_name, str) or not field_name.strip():
            raise WatcherPolicyError("threshold.field must be a non-empty string")
        result["field"] = field_name.strip()
        if "old_field" in raw:
            old_field = raw["old_field"]
            if not isinstance(old_field, str) or not old_field.strip():
                raise WatcherPolicyError("threshold.old_field must be a non-empty string")
            result["old_field"] = old_field.strip()
        found = False
        for key in ("above", "below", "delta_abs", "delta_pct", "equals"):
            if key in raw:
                result[key] = _as_number(raw[key], f"threshold.{key}")
                found = True
        if not found:
            raise WatcherPolicyError("threshold requires above, below, delta_abs, delta_pct, or equals")
        direction = str(raw.get("direction", "any")).casefold().strip()
        if direction not in {"any", "increase", "decrease"}:
            raise WatcherPolicyError("threshold.direction must be any, increase, or decrease")
        result["direction"] = direction
        result["crossing"] = _as_bool(raw.get("crossing", False), "threshold.crossing")
        return result

    # ``semantic`` deliberately means a local token/sequence comparison.  It
    # is deterministic and does not send monitored content to an embedding API.
    field_name = raw.get("field", "new_value")
    old_field = raw.get("old_field", "old_value")
    if not isinstance(field_name, str) or not field_name.strip():
        raise WatcherPolicyError("similarity.field must be a non-empty string")
    if not isinstance(old_field, str) or not old_field.strip():
        raise WatcherPolicyError("similarity.old_field must be a non-empty string")
    algorithm = str(raw.get("algorithm", "hybrid")).casefold().strip()
    if algorithm not in {"jaccard", "cosine", "sequence", "hybrid"}:
        raise WatcherPolicyError("similarity.algorithm must be jaccard, cosine, sequence, or hybrid")
    has_max = "max_similarity" in raw or "below" in raw
    has_min = "min_similarity" in raw or "above" in raw
    max_similarity = raw.get("max_similarity", raw.get("below")) if has_max else (None if has_min else 0.85)
    min_similarity = raw.get("min_similarity", raw.get("above"))
    if max_similarity is not None:
        max_similarity = _as_number(max_similarity, "similarity.max_similarity")
        if not 0 <= max_similarity <= 1:
            raise WatcherPolicyError("similarity.max_similarity must be between 0 and 1")
    if min_similarity is not None:
        min_similarity = _as_number(min_similarity, "similarity.min_similarity")
        if not 0 <= min_similarity <= 1:
            raise WatcherPolicyError("similarity.min_similarity must be between 0 and 1")
    if min_similarity is not None and max_similarity is not None and min_similarity > max_similarity:
        raise WatcherPolicyError("similarity.min_similarity cannot exceed similarity.max_similarity")
    result.update(
        field=field_name.strip(),
        old_field=old_field.strip(),
        algorithm=algorithm,
        max_similarity=max_similarity,
        min_similarity=min_similarity,
        min_tokens=_as_non_negative_number(raw.get("min_tokens", 0), "similarity.min_tokens", integer=True),
    )
    return result


def _normalize_node(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"type": raw}
    mapping = _ensure_mapping(raw, "condition")
    children = mapping.get("conditions", mapping.get("children"))
    if children is not None:
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes, bytearray)) or not children:
            raise WatcherPolicyError("condition group must contain a non-empty conditions list")
        return {
            "operator": _normalize_operator(mapping.get("operator", mapping.get("op", mapping.get("logic", "all")))),
            "conditions": [_normalize_node(child) for child in children],
        }
    return _normalize_leaf(mapping)


def normalize_condition_policy(policy: Mapping[str, Any] | Sequence[Any] | str | None) -> dict[str, Any]:
    """Validate and canonicalize a condition tree.

    A missing policy intentionally retains the historic watcher meaning of
    "notify on change".  Group nodes use ``operator: all|any``; leaf nodes use
    ``kind: changed|regex|threshold|similarity``.
    """

    if policy is None:
        return {"operator": "all", "conditions": [{"kind": "changed", "expected": True}]}
    if isinstance(policy, Sequence) and not isinstance(policy, (str, bytes, bytearray, Mapping)):
        if not policy:
            raise WatcherPolicyError("condition policy list cannot be empty")
        return {"operator": "all", "conditions": [_normalize_node(item) for item in policy]}
    node = _normalize_node(policy)
    return node if "conditions" in node else {"operator": "all", "conditions": [node]}


def _parse_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WatcherPolicyError(f"{name} must be ISO-8601") from exc
    else:
        raise WatcherPolicyError(f"{name} must be an ISO-8601 timestamp")
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_clock(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise WatcherPolicyError(f"{name} must be HH:MM")
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise WatcherPolicyError(f"{name} must be HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise WatcherPolicyError(f"{name} must not include seconds")
    return parsed.strftime("%H:%M")


def _normalize_quiet_hours(value: Any) -> list[dict[str, str]]:
    if value in (None, []):
        return []
    entries = [value] if isinstance(value, Mapping) or isinstance(value, str) else value
    if not isinstance(entries, Sequence) or isinstance(entries, (bytes, bytearray)):
        raise WatcherPolicyError("quiet_hours must be an object, string, or list")
    normalized: list[dict[str, str]] = []
    for item in entries:
        if isinstance(item, str):
            try:
                start, end = (part.strip() for part in item.split("-", 1))
            except ValueError as exc:
                raise WatcherPolicyError("quiet hour strings must be start-end") from exc
            entry: Mapping[str, Any] = {"start": start, "end": end}
        else:
            entry = _ensure_mapping(item, "quiet_hours entry")
        start = _parse_clock(entry.get("start"), "quiet_hours.start")
        end = _parse_clock(entry.get("end"), "quiet_hours.end")
        zone = entry.get("timezone", "UTC")
        if not isinstance(zone, str) or not zone.strip():
            raise WatcherPolicyError("quiet_hours.timezone must be an IANA timezone")
        try:
            ZoneInfo(zone.strip())
        except ZoneInfoNotFoundError as exc:
            raise WatcherPolicyError(f"Unknown quiet_hours timezone: {zone}") from exc
        candidate = {"start": start, "end": end, "timezone": zone.strip()}
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _normalize_false_positive_signatures(value: Any) -> list[Any]:
    if value in (None, []):
        return []
    values = [value] if isinstance(value, (str, Mapping)) else value
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        raise WatcherPolicyError("false_positive_signatures must be a string, object, or list")
    result: list[Any] = []
    for item in values:
        if isinstance(item, str):
            if not item.strip():
                raise WatcherPolicyError("false_positive signatures cannot be empty")
            candidate: Any = item.strip()
        elif isinstance(item, Mapping):
            candidate = dict(item)
            field_name = candidate.get("field")
            if field_name is not None and (not isinstance(field_name, str) or not field_name.strip()):
                raise WatcherPolicyError("false_positive signature field must be a non-empty string")
            if not any(key in candidate for key in ("field", "signature", "contains", "equals", "pattern")):
                raise WatcherPolicyError("false_positive signature needs field, signature, contains, equals, or pattern")
            if "pattern" in candidate:
                if not isinstance(candidate["pattern"], str):
                    raise WatcherPolicyError("false_positive signature pattern must be a string")
                try:
                    re.compile(candidate["pattern"])
                except re.error as exc:
                    raise WatcherPolicyError(f"Invalid false_positive pattern: {exc}") from exc
        else:
            raise WatcherPolicyError("false_positive signatures must be strings or objects")
        if candidate not in result:
            result.append(candidate)
    return result


def normalize_alert_policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and canonicalize local alert-delivery policy.

    The result is intentionally persistence-friendly; callers can store it in
    a monitor config without importing this module during reads.
    """

    raw = {} if policy is None else dict(_ensure_mapping(policy, "alert policy"))
    fields = _string_list(raw.get("dedupe_fields", ["event_type", "new_value", "change_summary"]), "dedupe_fields")
    result: dict[str, Any] = {
        "cooldown_seconds": _as_non_negative_number(raw.get("cooldown_seconds", raw.get("cooldown", 0)), "cooldown_seconds", integer=True),
        "dedupe_window_seconds": _as_non_negative_number(raw.get("dedupe_window_seconds", raw.get("dedupe_window", 0)), "dedupe_window_seconds", integer=True),
        "dedupe_fields": fields,
        "quiet_hours": _normalize_quiet_hours(raw.get("quiet_hours")),
        "false_positive_signatures": _normalize_false_positive_signatures(raw.get("false_positive_signatures", raw.get("false_positives"))),
    }
    if raw.get("expires_at") is not None:
        result["expires_at"] = _iso_datetime(_parse_datetime(raw["expires_at"], "expires_at"))
    else:
        result["expires_at"] = None
    expires_after = raw.get("expires_after_seconds", raw.get("expiry_seconds"))
    result["expires_after_seconds"] = None if expires_after is None else _as_non_negative_number(expires_after, "expires_after_seconds", integer=True)
    if raw.get("suppress_if") is not None:
        result["suppress_if"] = normalize_condition_policy(raw["suppress_if"])
    else:
        result["suppress_if"] = None
    min_severity = str(raw.get("min_severity", "debug")).casefold().strip()
    if min_severity not in _SEVERITY_RANK:
        raise WatcherPolicyError(f"Unsupported min_severity: {min_severity}")
    result["min_severity"] = min_severity
    return result


def _mapping_get(value: Any, path: str, default: Any = _MISSING) -> Any:
    if path in {"", "$"}:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part, _MISSING)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else _MISSING
        else:
            current = _MISSING
        if current is _MISSING:
            return default
    return current


def _evaluation_context(observation: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(observation)
    current = raw.get("current") if isinstance(raw.get("current"), Mapping) else raw.get("new")
    previous = raw.get("previous") if isinstance(raw.get("previous"), Mapping) else raw.get("old")
    current = dict(current) if isinstance(current, Mapping) else {}
    previous = dict(previous) if isinstance(previous, Mapping) else {}
    if "new_value" in raw and "new_value" not in current:
        current["new_value"] = raw["new_value"]
    if "old_value" in raw and "old_value" not in previous:
        previous["old_value"] = raw["old_value"]
    return {"raw": raw, "current": current, "previous": previous}


def _resolve_current(context: Mapping[str, Any], path: str) -> Any:
    if path.startswith("raw."):
        return _mapping_get(context["raw"], path[4:])
    if path.startswith("current."):
        return _mapping_get(context["current"], path[8:])
    if path.startswith("previous."):
        return _mapping_get(context["previous"], path[9:])
    if path in {"new", "new_value"}:
        return _mapping_get(context["current"], "new_value", _mapping_get(context["raw"], "new_value"))
    value = _mapping_get(context["current"], path)
    if value is not _MISSING:
        return value
    return _mapping_get(context["raw"], path)


def _resolve_previous(context: Mapping[str, Any], field: str, old_field: str | None = None) -> Any:
    if old_field:
        if old_field.startswith("raw."):
            return _mapping_get(context["raw"], old_field[4:])
        if old_field.startswith("current."):
            return _mapping_get(context["current"], old_field[8:])
        if old_field.startswith("previous."):
            return _mapping_get(context["previous"], old_field[9:])
        if old_field in {"old", "old_value"}:
            return _mapping_get(context["previous"], "old_value", _mapping_get(context["raw"], "old_value"))
        value = _mapping_get(context["previous"], old_field)
        return value if value is not _MISSING else _mapping_get(context["raw"], old_field)
    if field in {"new", "new_value"}:
        return _mapping_get(context["previous"], "old_value", _mapping_get(context["raw"], "old_value"))
    value = _mapping_get(context["previous"], field)
    if value is not _MISSING:
        return value
    return _mapping_get(context["raw"], "old_value")


def _json_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, ensure_ascii=False, default=str) == json.dumps(right, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return left == right


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[\w]+", str(value if value is not None else "").casefold(), flags=re.UNICODE)


def token_similarity(left: Any, right: Any, algorithm: str = "hybrid") -> dict[str, float]:
    """Return deterministic lexical similarity metrics for two values."""

    old_tokens, new_tokens = _tokens(left), _tokens(right)
    old_set, new_set = set(old_tokens), set(new_tokens)
    union = old_set | new_set
    jaccard = len(old_set & new_set) / len(union) if union else 1.0
    old_counts, new_counts = Counter(old_tokens), Counter(new_tokens)
    dot = sum(old_counts[key] * new_counts[key] for key in old_counts.keys() & new_counts.keys())
    norm = math.sqrt(sum(value * value for value in old_counts.values())) * math.sqrt(sum(value * value for value in new_counts.values()))
    cosine = dot / norm if norm else (1.0 if not old_tokens and not new_tokens else 0.0)
    sequence = SequenceMatcher(None, str(left if left is not None else ""), str(right if right is not None else "")).ratio()
    values = {"jaccard": jaccard, "cosine": cosine, "sequence": sequence}
    values["hybrid"] = sum(values.values()) / len(values)
    values["score"] = values[algorithm]
    return {key: round(value, 6) for key, value in values.items()}


@dataclass(slots=True)
class ConditionEvaluation:
    matched: bool
    kind: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    children: tuple["ConditionEvaluation", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "kind": self.kind,
            "summary": self.summary,
            "details": self.details,
            "children": [child.to_dict() for child in self.children],
        }


def _evaluate_leaf(node: Mapping[str, Any], context: Mapping[str, Any]) -> ConditionEvaluation:
    kind = node["kind"]
    if kind == "changed":
        field_name = node.get("field")
        if field_name:
            old_value = _resolve_previous(context, field_name, node.get("old_field"))
            new_value = _resolve_current(context, field_name)
            changed = old_value is not _MISSING and new_value is not _MISSING and not _json_equal(old_value, new_value)
        elif "changed" in context["raw"]:
            changed = bool(context["raw"]["changed"])
            old_value, new_value = _resolve_previous(context, "new_value"), _resolve_current(context, "new_value")
        else:
            old_value, new_value = _resolve_previous(context, "new_value"), _resolve_current(context, "new_value")
            if old_value is _MISSING or new_value is _MISSING:
                old_value, new_value = context["previous"], context["current"]
            changed = not _json_equal(old_value, new_value)
        expected = node["expected"]
        matched = changed is expected
        return ConditionEvaluation(matched, kind, f"Change state is {changed}", {"field": field_name, "changed": changed, "expected": expected})

    if kind == "regex":
        value = _resolve_current(context, node["field"])
        if value is _MISSING or value is None:
            return ConditionEvaluation(False, kind, "Regex source value is missing", {"field": node["field"]})
        matches = list(re.finditer(node["pattern"], str(value), _regex_flag_value(node["flags"])))
        positive = len(matches) >= node["min_matches"]
        matched = positive if node["must_match"] else not positive
        return ConditionEvaluation(
            matched,
            kind,
            f"Regex {'matched' if positive else 'did not match'} {len(matches)} time(s)",
            {"field": node["field"], "match_count": len(matches), "min_matches": node["min_matches"], "must_match": node["must_match"]},
        )

    if kind == "threshold":
        new_value = _resolve_current(context, node["field"])
        old_value = _resolve_previous(context, node["field"], node.get("old_field"))
        try:
            new_number = _as_number(new_value, "observed value")
        except WatcherPolicyError:
            return ConditionEvaluation(False, kind, "Threshold source value is missing or non-numeric", {"field": node["field"]})
        old_number: float | None
        try:
            old_number = None if old_value is _MISSING else _as_number(old_value, "previous value")
        except WatcherPolicyError:
            old_number = None
        delta = None if old_number is None else new_number - old_number
        delta_pct = None if old_number in (None, 0) else (delta / abs(old_number)) * 100
        direction_ok = node["direction"] == "any" or (delta is not None and ((node["direction"] == "increase" and delta > 0) or (node["direction"] == "decrease" and delta < 0)))
        reasons: list[str] = []
        crossing = node["crossing"]
        if "above" in node and new_number >= node["above"] and (not crossing or (old_number is not None and old_number < node["above"])):
            reasons.append(f"value >= {node['above']:g}")
        if "below" in node and new_number <= node["below"] and (not crossing or (old_number is not None and old_number > node["below"])):
            reasons.append(f"value <= {node['below']:g}")
        if "equals" in node and new_number == node["equals"]:
            reasons.append(f"value == {node['equals']:g}")
        if delta is not None and "delta_abs" in node and abs(delta) >= node["delta_abs"]:
            reasons.append(f"absolute delta >= {node['delta_abs']:g}")
        if delta_pct is not None and "delta_pct" in node and abs(delta_pct) >= node["delta_pct"]:
            reasons.append(f"percent delta >= {node['delta_pct']:g}%")
        matched = bool(reasons) and direction_ok
        return ConditionEvaluation(
            matched,
            kind,
            "; ".join(reasons) if reasons else "No threshold was met",
            {"field": node["field"], "old_value": old_number, "new_value": new_number, "delta": delta, "delta_pct": delta_pct, "direction_ok": direction_ok, "crossing": crossing},
        )

    old_value = _resolve_previous(context, node["field"], node["old_field"])
    new_value = _resolve_current(context, node["field"])
    if old_value is _MISSING or new_value is _MISSING:
        return ConditionEvaluation(False, kind, "Similarity source value is missing", {"field": node["field"], "old_field": node["old_field"]})
    old_count, new_count = len(_tokens(old_value)), len(_tokens(new_value))
    if min(old_count, new_count) < node["min_tokens"]:
        return ConditionEvaluation(False, kind, "Similarity source does not contain enough tokens", {"old_tokens": old_count, "new_tokens": new_count, "min_tokens": node["min_tokens"]})
    scores = token_similarity(old_value, new_value, node["algorithm"])
    score = scores["score"]
    lower_ok = node["min_similarity"] is None or score >= node["min_similarity"]
    upper_ok = node["max_similarity"] is None or score <= node["max_similarity"]
    matched = lower_ok and upper_ok
    return ConditionEvaluation(
        matched,
        kind,
        f"Token similarity is {score:.3f}",
        {"field": node["field"], "old_field": node["old_field"], "algorithm": node["algorithm"], "scores": scores, "min_similarity": node["min_similarity"], "max_similarity": node["max_similarity"], "old_tokens": old_count, "new_tokens": new_count},
    )


def _evaluate_node(node: Mapping[str, Any], context: Mapping[str, Any]) -> ConditionEvaluation:
    if "conditions" not in node:
        return _evaluate_leaf(node, context)
    children = tuple(_evaluate_node(child, context) for child in node["conditions"])
    operator = node["operator"]
    matched = all(child.matched for child in children) if operator == "all" else any(child.matched for child in children)
    count = sum(child.matched for child in children)
    noun = "All" if operator == "all" else "At least one"
    return ConditionEvaluation(matched, operator, f"{noun} condition(s): {count}/{len(children)} matched", {"operator": operator, "matched_conditions": count, "total_conditions": len(children)}, children)


def evaluate_condition_policy(policy: Mapping[str, Any] | Sequence[Any] | str | None, observation: Mapping[str, Any]) -> ConditionEvaluation:
    """Evaluate a condition policy without I/O or nondeterministic services.

    ``observation`` may provide ``current`` and ``previous`` mappings, or the
    common ``old_value``/``new_value``/``changed`` fields used by the existing
    watcher pipeline.
    """

    return _evaluate_node(normalize_condition_policy(policy), _evaluation_context(_ensure_mapping(observation, "observation")))


@dataclass(frozen=True, slots=True)
class PolicyReason:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(slots=True)
class AlertDecision:
    deliver: bool
    dedupe_key: str
    reasons: tuple[PolicyReason, ...] = ()
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def suppressed(self) -> bool:
        return not self.deliver

    def to_dict(self) -> dict[str, Any]:
        return {
            "deliver": self.deliver,
            "suppressed": self.suppressed,
            "dedupe_key": self.dedupe_key,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "policy": self.policy,
        }


def _canonical_event_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso_datetime(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_event_value(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_event_value(child) for child in value]
    return value


def event_signature(event: Mapping[str, Any], fields: Sequence[str] | None = None) -> str:
    """Build a human-inspectable canonical event signature for deduping."""

    payload = _ensure_mapping(event, "event")
    selected = fields or ("event_type", "new_value", "change_summary")
    return json.dumps({field: _canonical_event_value(_mapping_get(payload, field, None)) for field in selected}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def event_fingerprint(event: Mapping[str, Any], fields: Sequence[str] | None = None) -> str:
    return hashlib.sha256(event_signature(event, fields).encode("utf-8")).hexdigest()


def _event_timestamp(event: Mapping[str, Any]) -> datetime | None:
    for key in ("delivered_at", "sent_at", "finished_at", "last_checked_at", "created_at", "observed_at", "timestamp", "time", "started_at"):
        value = event.get(key)
        if value is None:
            continue
        try:
            return _parse_datetime(value, key)
        except WatcherPolicyError:
            continue
    return None


def _same_scope(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    for field_name in ("monitor_id", "watcher_id"):
        left_value, right_value = left.get(field_name), right.get(field_name)
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    return True


def _is_quiet_at(now: datetime, periods: Sequence[Mapping[str, str]]) -> PolicyReason | None:
    for period in periods:
        local = now.astimezone(ZoneInfo(period["timezone"]))
        start = time.fromisoformat(period["start"])
        end = time.fromisoformat(period["end"])
        current = local.timetz().replace(tzinfo=None)
        if start == end:
            continue  # Avoid turning a harmless typo into a permanent blackout.
        quiet = start <= current < end if start < end else current >= start or current < end
        if quiet:
            return PolicyReason("quiet_hours", "Alert is inside configured quiet hours", {"start": period["start"], "end": period["end"], "timezone": period["timezone"], "local_time": local.strftime("%H:%M")})
    return None


def _matches_false_positive_signature(event: Mapping[str, Any], signatures: Sequence[Any], dedupe_key: str) -> PolicyReason | None:
    fingerprint = event_fingerprint(event)
    for signature in signatures:
        if isinstance(signature, str):
            candidate = signature.casefold()
            matched = (candidate.startswith("sha256:") and fingerprint == candidate[7:]) or fnmatch.fnmatchcase(dedupe_key.casefold(), candidate) or candidate in dedupe_key.casefold()
            if matched:
                return PolicyReason("false_positive", "Event matches a false-positive signature", {"signature": signature, "fingerprint": fingerprint})
            continue
        field_name = signature.get("field")
        value = _mapping_get(event, str(field_name), _MISSING) if field_name else dedupe_key
        if value is _MISSING:
            continue
        text = str(value)
        matched = False
        if "signature" in signature:
            needle = str(signature["signature"])
            matched = fnmatch.fnmatchcase(dedupe_key.casefold(), needle.casefold()) or needle.casefold() in dedupe_key.casefold()
        if "equals" in signature:
            matched = matched or _json_equal(value, signature["equals"])
        if "contains" in signature:
            matched = matched or str(signature["contains"]).casefold() in text.casefold()
        if "pattern" in signature:
            matched = matched or re.search(str(signature["pattern"]), text) is not None
        if field_name is not None and not any(key in signature for key in ("equals", "contains", "pattern", "signature")):
            matched = bool(value)
        if matched:
            return PolicyReason("false_positive", "Event matches a false-positive signature", {"signature": signature, "fingerprint": fingerprint})
    return None


def suppression_reasons(
    policy: Mapping[str, Any] | None,
    event: Mapping[str, Any],
    *,
    history: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> tuple[PolicyReason, ...]:
    """Return all deterministic reasons an event should not be delivered."""

    normalized = normalize_alert_policy(policy)
    event_map = _ensure_mapping(event, "event")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    dedupe_key = event_signature(event_map, normalized["dedupe_fields"])
    reasons: list[PolicyReason] = []
    expires_at = normalized["expires_at"]
    if expires_at is not None and current_time >= _parse_datetime(expires_at, "expires_at"):
        reasons.append(PolicyReason("expired", "Alert policy has expired", {"expires_at": expires_at}))
    lifetime = normalized["expires_after_seconds"]
    source_time = _event_timestamp(event_map)
    if lifetime is not None and source_time is not None and current_time - source_time >= timedelta(seconds=lifetime):
        reasons.append(PolicyReason("expired", "Event is older than the configured delivery lifetime", {"event_time": _iso_datetime(source_time), "expires_after_seconds": lifetime}))
    quiet_reason = _is_quiet_at(current_time, normalized["quiet_hours"])
    if quiet_reason is not None:
        reasons.append(quiet_reason)
    false_positive = _matches_false_positive_signature(event_map, normalized["false_positive_signatures"], dedupe_key)
    if false_positive is not None:
        reasons.append(false_positive)
    severity = str(event_map.get("severity", "info")).casefold()
    if _SEVERITY_RANK.get(severity, _SEVERITY_RANK["info"]) < _SEVERITY_RANK[normalized["min_severity"]]:
        reasons.append(PolicyReason("below_min_severity", "Event severity is below the configured delivery threshold", {"severity": severity, "min_severity": normalized["min_severity"]}))
    if normalized["suppress_if"] is not None:
        evaluated = evaluate_condition_policy(normalized["suppress_if"], {"current": event_map, **dict(event_map)})
        if evaluated.matched:
            reasons.append(PolicyReason("suppression_condition", "Event matches the configured suppression condition", {"evaluation": evaluated.to_dict()}))

    recent: list[tuple[datetime, Mapping[str, Any]]] = []
    for candidate in history:
        if not isinstance(candidate, Mapping) or not _same_scope(event_map, candidate):
            continue
        timestamp = _event_timestamp(candidate)
        if timestamp is not None and timestamp <= current_time:
            recent.append((timestamp, candidate))
    if recent:
        latest_time, _ = max(recent, key=lambda item: item[0])
        age_seconds = int((current_time - latest_time).total_seconds())
        cooldown = normalized["cooldown_seconds"]
        if cooldown and age_seconds < cooldown:
            reasons.append(PolicyReason("cooldown", "A recent watcher alert is still in its cooldown period", {"age_seconds": age_seconds, "cooldown_seconds": cooldown, "last_alert_at": _iso_datetime(latest_time)}))
        dedupe_window = normalized["dedupe_window_seconds"]
        if dedupe_window:
            for timestamp, candidate in sorted(recent, key=lambda item: item[0], reverse=True):
                age_seconds = int((current_time - timestamp).total_seconds())
                if age_seconds >= dedupe_window:
                    break
                if event_signature(candidate, normalized["dedupe_fields"]) == dedupe_key:
                    reasons.append(PolicyReason("duplicate", "An equivalent watcher event was delivered recently", {"age_seconds": age_seconds, "dedupe_window_seconds": dedupe_window, "last_alert_at": _iso_datetime(timestamp), "dedupe_key": dedupe_key}))
                    break
    return tuple(reasons)


def evaluate_alert_policy(
    policy: Mapping[str, Any] | None,
    event: Mapping[str, Any],
    *,
    history: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> AlertDecision:
    normalized = normalize_alert_policy(policy)
    event_map = _ensure_mapping(event, "event")
    reasons = suppression_reasons(normalized, event_map, history=history, now=now)
    return AlertDecision(not reasons, event_signature(event_map, normalized["dedupe_fields"]), reasons, normalized)


def normalize_watcher_policy(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return scheduler-ready condition and delivery policy from monitor config.

    The wrapper accepts both the upgrade names (``condition_policy`` and
    ``alert_policy``) and concise monitor-config names (``conditions``,
    ``operator``, and ``alerts``).  It deliberately does not mutate or strip
    any existing monitor configuration keys.
    """

    raw = {} if config is None else dict(_ensure_mapping(config, "watcher config"))
    condition_source = raw.get("condition_policy", raw.get("conditions", raw.get("alert_conditions")))
    if condition_source is None and "condition" in raw:
        condition_source = raw["condition"]
    if condition_source is None:
        condition_source = None
    elif isinstance(condition_source, Sequence) and not isinstance(condition_source, (str, bytes, bytearray, Mapping)):
        condition_source = {"operator": raw.get("condition_operator", raw.get("operator", "all")), "conditions": condition_source}
    elif isinstance(condition_source, Mapping) and "conditions" not in condition_source and "operator" in raw:
        # A single leaf combined with an operator is valid but semantically a
        # one-item group; normalizing it keeps caller output predictable.
        condition_source = {"operator": raw["operator"], "conditions": [condition_source]}
    conditions = normalize_condition_policy(condition_source)
    alerts = normalize_alert_policy(raw.get("alert_policy", raw.get("alerts")))
    return {
        "condition_policy": conditions,
        "conditions": conditions["conditions"],
        "operator": conditions["operator"],
        "alert_policy": alerts,
    }


def evaluate_conditions(
    previous: Mapping[str, Any] | Any | None,
    current: Mapping[str, Any] | Any | None,
    conditions: Mapping[str, Any] | Sequence[Any] | str | None = None,
    operator: str = "all",
    *,
    changed: bool | None = None,
) -> dict[str, Any]:
    """Evaluate scheduler inputs and return a JSON-friendly decision record.

    ``previous`` and ``current`` can be snapshots (mappings) or primitive
    values.  This wrapper is intentionally small so a scheduler can call it
    before deciding whether to create an event.
    """

    previous_map = dict(previous) if isinstance(previous, Mapping) else {"old_value": previous}
    current_map = dict(current) if isinstance(current, Mapping) else {"new_value": current}
    if "old_value" not in previous_map and not isinstance(previous, Mapping):
        previous_map["old_value"] = previous
    if "new_value" not in current_map and not isinstance(current, Mapping):
        current_map["new_value"] = current
    if conditions is None:
        policy_input: Any = None
    elif isinstance(conditions, Mapping) and "conditions" in conditions:
        policy_input = conditions
    elif isinstance(conditions, Sequence) and not isinstance(conditions, (str, bytes, bytearray, Mapping)):
        policy_input = {"operator": operator, "conditions": conditions}
    else:
        policy_input = {"operator": operator, "conditions": [conditions]}
    policy = normalize_condition_policy(policy_input)
    observation: dict[str, Any] = {"previous": previous_map, "current": current_map}
    observation["changed"] = bool(changed) if changed is not None else not _json_equal(previous_map, current_map)
    evaluation = evaluate_condition_policy(policy, observation)
    result = evaluation.to_dict()
    return {
        "matched": evaluation.matched,
        "operator": policy["operator"],
        "condition_policy": policy,
        "evaluation": result,
        "summary": evaluation.summary,
    }


def suppression_reason(
    policy: Mapping[str, Any] | None,
    recent_events: Iterable[Mapping[str, Any]],
    event: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the first suppression reason, or ``None`` when delivery is allowed."""

    decision = evaluate_alert_policy(policy, event, history=recent_events, now=now)
    return decision.reasons[0].to_dict() if decision.reasons else None


def project_watcher_event(
    event: Mapping[str, Any],
    decision: AlertDecision | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a stable, UI/API-friendly event projection without persistence."""

    source = _ensure_mapping(event, "event")
    created = _event_timestamp(source)
    result = {
        "id": source.get("id"),
        "monitor_id": source.get("monitor_id", source.get("watcher_id")),
        "event_type": source.get("event_type", "change"),
        "severity": str(source.get("severity", "info")).casefold(),
        "summary": source.get("change_summary", source.get("summary", "")),
        "created_at": _iso_datetime(created) if created else None,
        "age_seconds": None if created is None else max(0, int(((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - created).total_seconds())),
    }
    if decision is not None:
        result["delivery"] = decision.to_dict()
    return result


def watcher_health(
    checks: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 1_800,
) -> dict[str, Any]:
    """Summarize watcher run health from plain check-run records."""

    if stale_after_seconds < 1:
        raise WatcherPolicyError("stale_after_seconds must be at least 1")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records = [dict(item) for item in checks if isinstance(item, Mapping)]
    if not records:
        return {"status": "unknown", "total_checks": 0, "successful_checks": 0, "failed_checks": 0, "success_rate": None, "consecutive_failures": 0, "last_checked_at": None, "age_seconds": None, "stale": True}
    successes = [record for record in records if str(record.get("status", "")).casefold() in {"ok", "success", "healthy"}]
    failures = [record for record in records if str(record.get("status", "")).casefold() in {"error", "failed", "timeout", "failure"}]
    stamped = [(timestamp, record) for record in records if (timestamp := _event_timestamp(record)) is not None]
    stamped.sort(key=lambda item: item[0], reverse=True)
    last_checked = stamped[0][0] if stamped else None
    consecutive = 0
    for _, record in stamped:
        if str(record.get("status", "")).casefold() in {"error", "failed", "timeout", "failure"}:
            consecutive += 1
        else:
            break
    age = None if last_checked is None else max(0, int((current_time - last_checked).total_seconds()))
    stale = age is None or age > stale_after_seconds
    status = "failed" if consecutive >= 2 else "degraded" if stale or failures else "healthy"
    return {
        "status": status,
        "total_checks": len(records),
        "successful_checks": len(successes),
        "failed_checks": len(failures),
        "success_rate": round(len(successes) / len(records), 4),
        "consecutive_failures": consecutive,
        "last_checked_at": _iso_datetime(last_checked) if last_checked else None,
        "age_seconds": age,
        "stale": stale,
    }


def health_projection(
    monitor: Mapping[str, Any] | None,
    checks: Iterable[Mapping[str, Any]] = (),
    events: Iterable[Mapping[str, Any]] = (),
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 1_800,
) -> dict[str, Any]:
    """Attach monitor identity and recent event facts to ``watcher_health``."""

    source = {} if monitor is None else dict(_ensure_mapping(monitor, "monitor"))
    health = watcher_health(checks, now=now, stale_after_seconds=stale_after_seconds)
    monitor_id = source.get("id", source.get("monitor_id"))
    scoped = [dict(event) for event in events if isinstance(event, Mapping) and (monitor_id is None or event.get("monitor_id", event.get("watcher_id")) in {None, monitor_id})]
    event_times = [timestamp for event in scoped if (timestamp := _event_timestamp(event)) is not None]
    health.update(
        {
            "monitor_id": monitor_id,
            "monitor_name": source.get("name"),
            "enabled": bool(source.get("enabled", True)),
            "recent_events": len(scoped),
            "unacknowledged_events": sum(not bool(event.get("acknowledged", False)) for event in scoped),
            "last_event_at": _iso_datetime(max(event_times)) if event_times else None,
        }
    )
    if not health["enabled"]:
        health["status"] = "disabled"
    return health


__all__ = [
    "AlertDecision",
    "ConditionEvaluation",
    "PolicyReason",
    "WatcherPolicyError",
    "evaluate_alert_policy",
    "evaluate_condition_policy",
    "evaluate_conditions",
    "event_fingerprint",
    "event_signature",
    "normalize_alert_policy",
    "normalize_condition_policy",
    "normalize_watcher_policy",
    "project_watcher_event",
    "suppression_reasons",
    "suppression_reason",
    "token_similarity",
    "watcher_health",
    "health_projection",
]
