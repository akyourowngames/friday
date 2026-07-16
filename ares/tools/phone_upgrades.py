"""Privacy-preserving helpers for the existing Android phone tools.

This module intentionally does *not* invoke a bridge, write a database, or
send a message/call.  It prepares bounded, explainable data for the existing
``phone_*`` tool handlers.  That keeps discovery and previews safe to use on
untrusted notification/contact payloads while the executor remains the only
place that performs an external side effect.

Raw notification content and contact numbers are hidden by default.  Callers
must select the explicit ``full``/``reveal`` options immediately before a
user-confirmed action; this module never remembers either form of data.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping


MAX_NOTIFICATION_RESULTS = 100
MAX_CONTACT_RESULTS = 50
MAX_SMS_CHARACTERS = 10_000
MAX_POST_CALL_NOTE_CHARACTERS = 2_000

_SPACE = re.compile(r"\s+")
_NAME_TOKEN = re.compile(r"[^\w]+", re.UNICODE)
_TEMPLATE_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# GSM 03.38 basic alphabet and extension table.  The extension characters
# consume two septets, which matters around the 160/153-character boundary.
_GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ "
    "!\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENDED = set("^{}\\[~]|€")
_PHONE_SAFE = re.compile(r"^[+0-9 ()-]{7,40}$")


def _clean_text(value: Any, *, maximum: int = 4_000) -> str:
    """Normalize untrusted bridge text without preserving control characters."""
    text = _CONTROL.sub("", str(value or ""))
    text = _SPACE.sub(" ", text).strip()
    return text[:maximum]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(result, maximum))


def normalize_contact_name(value: Any) -> str:
    """Return a comparison key for names and aliases, retaining no display data."""
    text = unicodedata.normalize("NFKD", _clean_text(value, maximum=240)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SPACE.sub(" ", _NAME_TOKEN.sub(" ", text)).strip()


def normalize_phone_number(value: Any, *, default_country_code: str | None = None) -> str:
    """Normalize a user/contact number to an E.164-like value when possible.

    This is deliberately conservative: ambiguous local numbers require a
    ``default_country_code`` rather than guessing a country.  Extensions are
    not passed to Android/KDE Connect because they cannot be dialled reliably
    by those bridges.
    """
    raw = _clean_text(value, maximum=80)
    raw = re.sub(r"^(?:tel|phone):", "", raw, flags=re.IGNORECASE).strip()
    if not raw or not _PHONE_SAFE.fullmatch(raw):
        raise ValueError("A valid phone number is required.")
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits, has_plus = digits[2:], True
    if not has_plus:
        country = re.sub(r"\D", "", str(default_country_code or ""))
        if country:
            digits = country.lstrip("0") + digits.lstrip("0")
            has_plus = True
    if not 7 <= len(digits) <= 15:
        raise ValueError("Phone numbers must contain between 7 and 15 digits.")
    return f"+{digits}" if has_plus else digits


def phone_match_key(value: Any, *, default_country_code: str | None = None) -> str:
    """Return a stable matching key while tolerating an unqualified number."""
    try:
        normalized = normalize_phone_number(value, default_country_code=default_country_code)
    except ValueError:
        digits = re.sub(r"\D", "", _clean_text(value, maximum=80))
        return digits[-10:] if len(digits) >= 7 else ""
    return normalized.lstrip("+")[-10:]


def mask_phone_number(value: Any, *, visible: int = 4) -> str:
    """Mask a phone number for discovery/preflight responses."""
    raw = _clean_text(value, maximum=80)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    shown = max(0, min(int(visible), len(digits)))
    suffix = digits[-shown:] if shown else ""
    hidden = "•" * max(3, len(digits) - shown)
    return f"{hidden}{suffix}"


def mask_contact_value(value: Any, *, kind: str) -> str:
    """Mask contact values while retaining enough context for disambiguation."""
    raw = _clean_text(value, maximum=320)
    if kind == "phone":
        return mask_phone_number(raw)
    if kind == "email":
        local, at, domain = raw.partition("@")
        if not at:
            return "•••"
        return f"{(local[:1] or '•')}•••@{domain}"
    return "•••"


def _mask_text(value: Any) -> dict[str, Any]:
    text = _clean_text(value, maximum=4_000)
    return {
        "present": bool(text),
        "characters": len(text),
        "fingerprint": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else "",
    }


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    else:
        raw = _clean_text(value, maximum=80)
        if not raw:
            return None
        try:
            point = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                point = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                return None
    return point.replace(tzinfo=point.tzinfo or timezone.utc).astimezone(timezone.utc)


def _iso_time(value: Any) -> str:
    point = _parse_time(value)
    return point.isoformat(timespec="seconds").replace("+00:00", "Z") if point else ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "unread", "new"}


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [_clean_text(item, maximum=240).casefold() for item in values if _clean_text(item, maximum=240)]


def _notification_priority(record: Mapping[str, Any], now: datetime | None = None) -> tuple[int, str, list[str]]:
    """Calculate an explainable priority score without returning notification text."""
    app = _clean_text(record.get("app") or record.get("package"), maximum=240).casefold()
    content = " ".join(
        _clean_text(record.get(field), maximum=800).casefold()
        for field in ("title", "text", "conversation", "person")
    )
    score, reasons = 0, []
    if _as_bool(record.get("unread")):
        score += 20
        reasons.append("unread")
    if any(word in content for word in ("urgent", "emergency", "asap", "otp", "verification code")):
        score += 45
        reasons.append("urgent language")
    if any(word in app for word in ("phone", "dialer", "call", "calendar", "alarm")):
        score += 25
        reasons.append("time-sensitive application")
    elif any(word in app for word in ("message", "sms", "whatsapp", "telegram", "signal")):
        score += 12
        reasons.append("message application")
    timestamp = _parse_time(record.get("timestamp"))
    if timestamp and ((now or datetime.now(timezone.utc)) - timestamp).total_seconds() <= 3_600:
        score += 8
        reasons.append("recent")
    if score >= 45:
        label = "high"
    elif score >= 20:
        label = "normal"
    else:
        label = "low"
    return score, label, reasons


def _safe_notification_record(record: Mapping[str, Any], *, content_mode: str, now: datetime | None) -> tuple[dict[str, Any], str]:
    app = _clean_text(record.get("app") or record.get("application") or record.get("package") or "unknown", maximum=160)
    package = _clean_text(record.get("package"), maximum=240)
    title = _clean_text(record.get("title"), maximum=1_000)
    text = _clean_text(record.get("text") or record.get("body") or record.get("message"), maximum=4_000)
    conversation = _clean_text(record.get("conversation") or record.get("thread") or record.get("person") or record.get("sender"), maximum=320)
    score, priority, reasons = _notification_priority(record, now=now)
    fingerprint_input = "\x1f".join((app.casefold(), conversation.casefold(), title.casefold(), text.casefold()))
    fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:20]
    result: dict[str, Any] = {
        "notification_id": _clean_text(record.get("id") or record.get("notification_id"), maximum=160),
        "app": app,
        "package": package,
        "timestamp": _iso_time(record.get("timestamp") or record.get("time")),
        "unread": _as_bool(record.get("unread")),
        "priority": priority,
        "priority_score": score,
        "priority_reasons": reasons,
        "content": {"title": _mask_text(title), "text": _mask_text(text), "conversation": _mask_text(conversation)},
        "duplicate_fingerprint": fingerprint,
    }
    if content_mode == "redacted":
        result["content_preview"] = {
            "title": "[redacted]" if title else "",
            "text": "[redacted]" if text else "",
            "conversation": "[redacted]" if conversation else "",
        }
    elif content_mode == "full":
        result["content_preview"] = {"title": title, "text": text, "conversation": conversation}
    return result, " ".join((app, package, title, text, conversation)).casefold()


def _conversation_group_label(items: list[dict[str, Any]], *, content_mode: str) -> str:
    """Return a conversation group label without leaking it in metadata mode."""
    if content_mode != "full":
        return "private-conversation"
    preview = items[0].get("content_preview") if items else None
    if isinstance(preview, Mapping):
        return _clean_text(preview.get("conversation"), maximum=320) or "unknown"
    return "unknown"


def prepare_notifications(
    notifications: Iterable[Mapping[str, Any]] | None,
    *,
    applications: Any = None,
    person: str | None = None,
    keywords: Any = None,
    unread_only: bool = False,
    since: Any = None,
    until: Any = None,
    group_by: str = "none",
    collapse_duplicates: bool = True,
    content_mode: str = "metadata",
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Filter, rank, group, and redact one in-memory notification snapshot.

    ``content_mode`` is ``metadata`` by default.  ``redacted`` exposes only
    presence/length markers, while ``full`` is an explicit transient view for
    a user-facing read operation.  Nothing is cached or written to disk.
    """
    mode = str(content_mode or "metadata").casefold()
    if mode not in {"metadata", "redacted", "full"}:
        raise ValueError("content_mode must be metadata, redacted, or full")
    grouping = str(group_by or "none").casefold()
    if grouping not in {"none", "application", "conversation"}:
        raise ValueError("group_by must be none, application, or conversation")
    app_filters = _listify(applications)
    keyword_filters = _listify(keywords)
    person_filter = normalize_contact_name(person) if person else ""
    lower_bound, upper_bound = _parse_time(since), _parse_time(until)
    if lower_bound and upper_bound and lower_bound > upper_bound:
        raise ValueError("since must be before until")
    bounded_limit = _bounded_int(limit, default=20, minimum=1, maximum=MAX_NOTIFICATION_RESULTS)
    prepared: list[tuple[dict[str, Any], str]] = []
    inspected = 0
    for raw in notifications or []:
        if not isinstance(raw, Mapping):
            continue
        inspected += 1
        item, searchable = _safe_notification_record(raw, content_mode=mode, now=now)
        if app_filters and not any(token in f"{item['app']} {item['package']}".casefold() for token in app_filters):
            continue
        if unread_only and not item["unread"]:
            continue
        timestamp = _parse_time(item["timestamp"])
        if lower_bound and (timestamp is None or timestamp < lower_bound):
            continue
        if upper_bound and (timestamp is None or timestamp > upper_bound):
            continue
        normalized_searchable = normalize_contact_name(searchable)
        if person_filter and person_filter not in normalized_searchable:
            continue
        if keyword_filters and not all(token in searchable for token in keyword_filters):
            continue
        prepared.append((item, searchable))
    collapsed = 0
    if collapse_duplicates:
        unique: dict[str, tuple[dict[str, Any], str]] = {}
        for item, searchable in prepared:
            key = item["duplicate_fingerprint"]
            incumbent = unique.get(key)
            if incumbent is None or item["priority_score"] > incumbent[0]["priority_score"]:
                if incumbent is not None:
                    collapsed += 1
                unique[key] = (item, searchable)
            else:
                collapsed += 1
        prepared = list(unique.values())
    prepared.sort(key=lambda entry: (entry[0]["priority_score"], entry[0]["timestamp"]), reverse=True)
    prepared = prepared[:bounded_limit]
    records = [item for item, _ in prepared]
    groups: list[dict[str, Any]] = []
    if grouping != "none":
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in records:
            if grouping == "application":
                key = item["app"] or "unknown"
            else:
                # Deliberately do not expose a conversation label in metadata mode.
                key = item["content"]["conversation"]["fingerprint"] or "unknown"
            buckets[key].append(item)
        groups = [
            {
                "group": (
                    key
                    if grouping == "application"
                    else _conversation_group_label(items, content_mode=mode)
                ),
                "count": len(items),
                "highest_priority": max((item["priority"] for item in items), key={"low": 0, "normal": 1, "high": 2}.get),
                "notification_ids": [item["notification_id"] for item in items if item["notification_id"]],
            }
            for key, items in sorted(buckets.items(), key=lambda entry: len(entry[1]), reverse=True)
        ]
    app_counts = Counter(item["app"] for item in records)
    return {
        "ok": True,
        "summary": f"{len(records)} notification(s) matched from {inspected} inspected.",
        "privacy": {"content_mode": mode, "persisted": False, "raw_content_returned": mode == "full"},
        "filters": {
            "applications": app_filters,
            "person": bool(person_filter),
            "keywords": len(keyword_filters),
            "unread_only": bool(unread_only),
            "since": _iso_time(since),
            "until": _iso_time(until),
        },
        "notifications": records,
        "groups": groups,
        "metrics": {
            "inspected": inspected,
            "returned": len(records),
            "duplicates_collapsed": collapsed,
            "unread": sum(1 for item in records if item["unread"]),
            "applications": dict(app_counts),
        },
    }


def _contact_numbers(value: Mapping[str, Any]) -> list[str]:
    raw_values: list[Any] = []
    for key in ("numbers", "phones", "phone", "number", "mobile"):
        found = value.get(key)
        if isinstance(found, (list, tuple, set)):
            raw_values.extend(found)
        elif found:
            raw_values.append(found)
    output: list[str] = []
    for raw in raw_values:
        try:
            number = normalize_phone_number(raw)
        except ValueError:
            continue
        if number not in output:
            output.append(number)
    return output


def _contact_emails(value: Mapping[str, Any]) -> list[str]:
    raw_values: list[Any] = []
    for key in ("emails", "email"):
        found = value.get(key)
        if isinstance(found, (list, tuple, set)):
            raw_values.extend(found)
        elif found:
            raw_values.append(found)
    output: list[str] = []
    for raw in raw_values:
        email = _clean_text(raw, maximum=320).casefold()
        if "@" in email and email not in output:
            output.append(email)
    return output


def _contact_name(value: Mapping[str, Any]) -> str:
    return _clean_text(value.get("canonical_name") or value.get("name") or value.get("display_name") or "unknown", maximum=240)


def _contact_aliases(value: Mapping[str, Any], primary: str) -> list[str]:
    raw = value.get("aliases") or []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    aliases = [_clean_text(item, maximum=240) for item in raw]
    aliases = [item for item in aliases if item and normalize_contact_name(item) != normalize_contact_name(primary)]
    return list(dict.fromkeys(aliases))


def _candidate_from_source(value: Mapping[str, Any], *, source: str, index: int) -> dict[str, Any]:
    name = _contact_name(value)
    return {
        "id": str(value.get("id") or value.get("person_id") or f"{source}-{index}"),
        "name": name,
        "normalized_name": normalize_contact_name(name),
        "aliases": _contact_aliases(value, name),
        "numbers": _contact_numbers(value),
        "emails": _contact_emails(value),
        "preferred_contact_method": _clean_text(value.get("preferred_contact_method") or value.get("preferred_channel"), maximum=40).casefold(),
        "source": source,
        "record": value,
    }


def _name_similarity(query: str, candidate: dict[str, Any]) -> tuple[int, list[str]]:
    if not query:
        return 0, []
    options = [candidate["normalized_name"], *(normalize_contact_name(alias) for alias in candidate["aliases"])]
    scores = []
    for option in options:
        if not option:
            continue
        if option == query:
            scores.append((100, "exact name/alias"))
        elif query in option or option in query:
            scores.append((88, "partial name/alias"))
        else:
            scores.append((int(SequenceMatcher(None, query, option).ratio() * 75), "fuzzy name/alias"))
    return max(scores, default=(0, ""))


def _mergeable(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if set(left["numbers"]) & set(right["numbers"]):
        return True
    if left["normalized_name"] and left["normalized_name"] == right["normalized_name"]:
        return True
    if left["normalized_name"] and right["normalized_name"]:
        return SequenceMatcher(None, left["normalized_name"], right["normalized_name"]).ratio() >= 0.93
    return False


def _merge_contact_candidates(
    device_contacts: Iterable[Mapping[str, Any]] | None,
    saved_people: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Merge local People Store entries with transient device contacts in memory."""
    raw_candidates = [
        *[_candidate_from_source(item, source="saved_person", index=index) for index, item in enumerate(saved_people or []) if isinstance(item, Mapping)],
        *[_candidate_from_source(item, source="device", index=index) for index, item in enumerate(device_contacts or []) if isinstance(item, Mapping)],
    ]
    merged: list[dict[str, Any]] = []
    for candidate in raw_candidates:
        target = next((entry for entry in merged if _mergeable(entry, candidate)), None)
        if target is None:
            merged.append(
                {
                    "candidate_id": candidate["id"],
                    "name": candidate["name"],
                    "normalized_name": candidate["normalized_name"],
                    "aliases": list(candidate["aliases"]),
                    "numbers": list(candidate["numbers"]),
                    "emails": list(candidate["emails"]),
                    "preferred_contact_method": candidate["preferred_contact_method"],
                    "sources": [candidate["source"]],
                    "source_records": [candidate["record"]],
                    "person_id": candidate["id"] if candidate["source"] == "saved_person" else None,
                }
            )
            continue
        if candidate["source"] == "saved_person":
            target["person_id"] = candidate["id"]
            target["candidate_id"] = candidate["id"]
            target["name"] = candidate["name"]
        target["aliases"] = list(dict.fromkeys([*target["aliases"], candidate["name"], *candidate["aliases"]]))
        target["numbers"] = list(dict.fromkeys([*target["numbers"], *candidate["numbers"]]))
        target["emails"] = list(dict.fromkeys([*target["emails"], *candidate["emails"]]))
        if not target["preferred_contact_method"]:
            target["preferred_contact_method"] = candidate["preferred_contact_method"]
        target["sources"] = list(dict.fromkeys([*target["sources"], candidate["source"]]))
        target["source_records"].append(candidate["record"])
    return merged


def merge_contact_candidates(
    device_contacts: Iterable[Mapping[str, Any]] | None,
    saved_people: Iterable[Mapping[str, Any]] | None,
    *,
    reveal_contact_values: bool = False,
) -> list[dict[str, Any]]:
    """Merge contacts without exposing their channel values by default.

    ``reveal_contact_values=True`` is intentionally explicit and intended for
    a just-confirmed side-effecting executor path.  Discovery callers should
    use the default output or :func:`rank_contact_candidates`.
    """
    merged = _merge_contact_candidates(device_contacts, saved_people)
    output: list[dict[str, Any]] = []
    for candidate in merged:
        result = {
            "candidate_id": candidate["candidate_id"],
            "person_id": candidate["person_id"],
            "name": candidate["name"],
            "aliases": candidate["aliases"][:8],
            "sources": candidate["sources"],
            "preferred_contact_method": candidate["preferred_contact_method"],
            "channels": [
                {
                    "kind": "phone",
                    "value": number if reveal_contact_values else mask_contact_value(number, kind="phone"),
                }
                for number in candidate["numbers"]
            ] + [
                {
                    "kind": "email",
                    "value": email if reveal_contact_values else mask_contact_value(email, kind="email"),
                }
                for email in candidate["emails"]
            ],
        }
        if reveal_contact_values:
            # This private execution representation deliberately remains
            # transient; it is never stored or returned from discovery tools.
            result.update({
                "numbers": list(candidate["numbers"]),
                "emails": list(candidate["emails"]),
                "source_records": list(candidate["source_records"]),
            })
        output.append(result)
    return output


def _preferred_channel(candidate: Mapping[str, Any], action: str) -> str:
    action = str(action or "sms").casefold()
    configured = _clean_text(candidate.get("preferred_contact_method"), maximum=40).casefold()
    available = {"phone" if candidate.get("numbers") else "", "email" if candidate.get("emails") else ""} - {""}
    if action in {"call", "phone_call"}:
        return "phone" if "phone" in available else ""
    if action in {"email", "invite", "invitation"}:
        return "email" if "email" in available else ("phone" if "phone" in available else "")
    if configured in available:
        return configured
    return "phone" if "phone" in available else ("email" if "email" in available else "")


def rank_contact_candidates(
    query: Any,
    *,
    device_contacts: Iterable[Mapping[str, Any]] | None = None,
    saved_people: Iterable[Mapping[str, Any]] | None = None,
    action: str = "sms",
    limit: int = 20,
    reveal_contact_values: bool = False,
) -> dict[str, Any]:
    """Fuzzy-rank merged contacts and retain ambiguity instead of guessing."""
    raw_query = _clean_text(query, maximum=240)
    if not raw_query:
        raise ValueError("A contact query is required.")
    query_name = normalize_contact_name(raw_query)
    query_digits = re.sub(r"\D", "", raw_query)
    candidates = _merge_contact_candidates(device_contacts, saved_people)
    ranked: list[dict[str, Any]] = []
    duplicate_count = 0
    for candidate in candidates:
        score, reason = _name_similarity(query_name, candidate)
        reasons = [reason] if reason else []
        if query_digits:
            matching_numbers = [number for number in candidate["numbers"] if query_digits in re.sub(r"\D", "", number)]
            if matching_numbers:
                score = max(score, 98 if len(query_digits) >= 7 else 78)
                reasons.append("matching phone digits")
        if "saved_person" in candidate["sources"]:
            score += 3
            reasons.append("saved person record")
        if len(candidate["sources"]) > 1:
            duplicate_count += 1
            reasons.append("merged duplicate contact")
        channel = _preferred_channel(candidate, action)
        if channel:
            reasons.append(f"preferred channel for {action}: {channel}")
        if score <= 0:
            continue
        channels = [
            {"kind": "phone", "value": number if reveal_contact_values else mask_contact_value(number, kind="phone")}
            for number in candidate["numbers"]
        ] + [
            {"kind": "email", "value": email if reveal_contact_values else mask_contact_value(email, kind="email")}
            for email in candidate["emails"]
        ]
        ranked.append(
            {
                "candidate_id": candidate["candidate_id"],
                "person_id": candidate["person_id"],
                "name": candidate["name"],
                "aliases": candidate["aliases"][:8],
                "score": min(100, score),
                "match_reasons": reasons,
                "sources": candidate["sources"],
                "preferred_channel": channel,
                "channels": channels,
                "_raw": candidate,
            }
        )
    ranked.sort(key=lambda item: (item["score"], len(item["channels"]), item["name"].casefold()), reverse=True)
    bounded = _bounded_int(limit, default=20, minimum=1, maximum=MAX_CONTACT_RESULTS)
    ranked = ranked[:bounded]
    ambiguous = len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < 12
    public = [{key: value for key, value in item.items() if key != "_raw"} for item in ranked]
    return {
        "ok": True,
        "query": raw_query,
        "action": action,
        "candidates": public,
        "requires_disambiguation": ambiguous,
        "best_candidate_id": None if ambiguous or not public else public[0]["candidate_id"],
        "privacy": {"contact_values_revealed": bool(reveal_contact_values), "persisted": False},
        "metrics": {"merged_candidates": len(candidates), "returned": len(public), "duplicates_detected": duplicate_count},
    }


def resolve_contact_channel(
    candidate: Mapping[str, Any],
    *,
    action: str = "sms",
    reveal_contact_value: bool = False,
) -> dict[str, Any]:
    """Choose a channel from an already-selected candidate; never sends it."""
    raw = candidate.get("_raw") if isinstance(candidate.get("_raw"), Mapping) else candidate
    numbers = list(raw.get("numbers") or [])
    emails = list(raw.get("emails") or [])
    channel = _preferred_channel({"numbers": numbers, "emails": emails, "preferred_contact_method": raw.get("preferred_contact_method")}, action)
    values = numbers if channel == "phone" else emails
    if not channel or not values:
        return {"ok": False, "error": f"No usable contact channel is available for {action}.", "channel": "", "value": ""}
    ambiguous = len(values) > 1
    value = values[0]
    return {
        "ok": not ambiguous,
        "channel": channel,
        "value": value if reveal_contact_value else mask_contact_value(value, kind=channel),
        "requires_disambiguation": ambiguous,
        "warning": "Multiple values exist; ask the user to choose one." if ambiguous else "",
    }


def render_sms_template(template: Any, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Render simple named templates without evaluating arbitrary expressions."""
    source = _clean_text(template, maximum=MAX_SMS_CHARACTERS)
    if not source:
        raise ValueError("An SMS template is required.")
    values = {str(key): _clean_text(value, maximum=1_000) for key, value in (variables or {}).items()}
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2) or ""
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    rendered = _TEMPLATE_TOKEN.sub(replace, source)
    return {"ok": not missing, "message": rendered, "missing_variables": list(dict.fromkeys(missing)), "template_used": True}


def _sms_encoding_and_units(message: str) -> tuple[str, int]:
    if all(char in _GSM7_BASIC or char in _GSM7_EXTENDED for char in message):
        return "GSM-7", sum(2 if char in _GSM7_EXTENDED else 1 for char in message)
    # UCS-2 transport accounting uses UTF-16 code units; astral emoji use two.
    return "UCS-2", len(message.encode("utf-16-be")) // 2


def _segment_boundaries(message: str, *, encoding: str, units_per_segment: int) -> list[dict[str, int]]:
    if not message:
        return []
    segments: list[dict[str, int]] = []
    start, units = 0, 0
    for index, char in enumerate(message):
        char_units = 2 if encoding == "GSM-7" and char in _GSM7_EXTENDED else (len(char.encode("utf-16-be")) // 2 if encoding == "UCS-2" else 1)
        if units and units + char_units > units_per_segment:
            segments.append({"start": start, "end": index, "units": units})
            start, units = index, 0
        units += char_units
    segments.append({"start": start, "end": len(message), "units": units})
    return segments


def sms_segmentation(message: Any) -> dict[str, Any]:
    """Return carrier-neutral SMS segment accounting without exposing text."""
    text = _clean_text(message, maximum=MAX_SMS_CHARACTERS)
    encoding, units = _sms_encoding_and_units(text)
    single_limit = 160 if encoding == "GSM-7" else 70
    multi_limit = 153 if encoding == "GSM-7" else 67
    per_segment = single_limit if units <= single_limit else multi_limit
    segments = _segment_boundaries(text, encoding=encoding, units_per_segment=per_segment)
    return {
        "encoding": encoding,
        "characters": len(text),
        "transport_units": units,
        "segments": len(segments),
        "units_per_segment": per_segment,
        "boundaries": segments,
    }


def project_sms_delivery(*, segments: int, bridge_ready: bool | None = None, carrier_status_available: bool = False) -> dict[str, Any]:
    """Describe delivery observability without claiming carrier delivery."""
    if bridge_ready is False:
        state = "blocked"
        message = "The paired phone bridge is not ready; no message can be submitted."
    elif segments <= 0:
        state = "invalid"
        message = "An empty message cannot be submitted."
    else:
        state = "ready_to_submit"
        message = "Submission can be requested after explicit confirmation; carrier delivery is not yet known."
    return {
        "state": state,
        "segments": max(0, int(segments)),
        "carrier_status_available": bool(carrier_status_available),
        "message": message,
        "next_status": "carrier status" if carrier_status_available else "bridge submission result only",
    }


def preview_sms(
    number: Any,
    *,
    message: Any | None = None,
    template: Any | None = None,
    variables: Mapping[str, Any] | None = None,
    include_message: bool = False,
    bridge_ready: bool | None = None,
) -> dict[str, Any]:
    """Build a non-sending SMS preview with template and segmentation checks."""
    normalized_number = normalize_phone_number(number)
    if template is not None:
        rendered = render_sms_template(template, variables)
        if not rendered["ok"]:
            return {
                "ok": False,
                "recipient": mask_phone_number(normalized_number),
                "error": f"Missing template variables: {', '.join(rendered['missing_variables'])}",
                "missing_variables": rendered["missing_variables"],
                "confirmation_required": True,
            }
        body = rendered["message"]
    else:
        body = _clean_text(message, maximum=MAX_SMS_CHARACTERS)
    if not body:
        raise ValueError("An SMS message or template is required.")
    segmentation = sms_segmentation(body)
    return {
        "ok": True,
        "recipient": mask_phone_number(normalized_number),
        "message": body if include_message else _mask_text(body),
        "message_included": bool(include_message),
        "template_used": template is not None,
        "segmentation": segmentation,
        "delivery_projection": project_sms_delivery(segments=segmentation["segments"], bridge_ready=bridge_ready),
        "confirmation_required": True,
        "persisted": False,
    }


def sms_delivery_status(response: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize bridge/carrier delivery evidence and allow retry only for transport errors."""
    response = response or {}
    raw_state = _clean_text(response.get("status") or response.get("state"), maximum=80).casefold()
    ok = bool(response.get("ok") or response.get("sent") or raw_state in {"sent", "queued", "delivered"})
    error = _clean_text(response.get("error") or response.get("message"), maximum=500)
    if raw_state in {"delivered", "sent", "queued", "submitted"} or ok:
        status = "delivered" if raw_state == "delivered" else ("queued" if raw_state in {"queued", "submitted"} else "submitted")
        retryable = False
    elif any(token in error.casefold() for token in ("timeout", "temporar", "connection", "network", "unreachable", "transport")):
        status, retryable = "transport_failed", True
    elif error:
        status, retryable = "failed", False
    else:
        status, retryable = "unknown", False
    return {
        "ok": ok,
        "status": status,
        "retry_allowed": retryable,
        "retry_reason": "transport failure" if retryable else "retry is limited to transport failures",
        "error": error,
    }


def _call_devices(status: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize the legacy ADB and newer top-level device status shapes."""
    devices: list[dict[str, Any]] = []
    for item in status.get("devices") if isinstance(status.get("devices"), list) else []:
        if isinstance(item, Mapping):
            devices.append(dict(item))
        elif item:
            devices.append({"id": str(item), "reachable": True})
    adb = status.get("adb") if isinstance(status.get("adb"), Mapping) else {}
    adb_ready = bool(adb.get("connected", adb.get("ok", False)))
    for item in adb.get("devices") if isinstance(adb.get("devices"), list) else []:
        if isinstance(item, Mapping):
            devices.append(dict(item))
        elif item:
            devices.append({"id": str(item), "reachable": adb_ready})
    return devices


def call_preflight(
    number: Any,
    *,
    phone_status: Mapping[str, Any] | None,
    device_id: str | None = None,
    recipient: str | None = None,
    confirm: bool = False,
    reveal_number: bool = False,
) -> dict[str, Any]:
    """Validate a call request and live bridge state without placing a call."""
    normalized_number = normalize_phone_number(number)
    status = phone_status or {}
    devices = _call_devices(status)
    capabilities = status.get("capability_matrix") if isinstance(status.get("capability_matrix"), Mapping) else {}
    calls_available = bool(capabilities["calls"]) if "calls" in capabilities else bool(status.get("any_ready", status.get("ok", False)))
    selected = None
    if device_id:
        selected = next((item for item in devices if isinstance(item, Mapping) and str(item.get("id")) == str(device_id)), None)
        device_ready = bool(calls_available and selected and selected.get("reachable", selected.get("ready", False)))
    else:
        device_ready = calls_available
        if not device_ready:
            device_ready = calls_available and any(bool(item.get("reachable", item.get("ready", False))) for item in devices if isinstance(item, Mapping))
        selected = next((item for item in devices if isinstance(item, Mapping) and item.get("reachable", item.get("ready", False))), None)
    errors: list[str] = []
    warnings: list[str] = []
    if device_id and selected is None:
        errors.append("The requested phone device was not found.")
    elif not device_ready:
        errors.append(_clean_text(status.get("error"), maximum=500) or "No paired, reachable phone device is ready.")
    if not confirm:
        warnings.append("A call can only be placed after explicit confirmation.")
    return {
        "ok": not errors and bool(confirm),
        "ready": not errors,
        "confirmation_required": not bool(confirm),
        "recipient": _clean_text(recipient, maximum=240) or "",
        "number": normalized_number if reveal_number else mask_phone_number(normalized_number),
        "selected_device_id": str(selected.get("id")) if isinstance(selected, Mapping) and selected.get("id") else (str(device_id or "")),
        "errors": errors,
        "warnings": warnings,
        "next_action": "place_call" if not errors and confirm else "confirm_call" if not errors else "restore_phone_connectivity",
        "persisted": False,
    }


def normalize_call_status(event: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize caller/bridge call status updates without saving call history."""
    event = event or {}
    raw = _clean_text(event.get("status") or event.get("state"), maximum=80).casefold()
    aliases = {
        "dialing": "initiated", "initiated": "initiated", "ringing": "ringing", "active": "connected",
        "connected": "connected", "ended": "ended", "completed": "ended", "cancelled": "cancelled",
        "canceled": "cancelled", "failed": "failed", "busy": "failed", "no_answer": "failed",
    }
    status = aliases.get(raw, "unknown")
    terminal = status in {"ended", "cancelled", "failed"}
    return {
        "status": status,
        "terminal": terminal,
        "call_id": _clean_text(event.get("call_id") or event.get("id"), maximum=160),
        "timestamp": _iso_time(event.get("timestamp") or event.get("time")),
        "reason": _clean_text(event.get("reason") or event.get("error"), maximum=500),
        "persisted": False,
    }


def validate_post_call_note(note: Any, *, person_id: Any = None, call_id: Any = None) -> dict[str, Any]:
    """Validate an explicit user note before a caller chooses to attach it.

    The helper intentionally does not update a people record.  The executor
    must make that write explicit after the call completes and preserve its
    normal People Store revision/audit behavior.
    """
    raw = str(note or "")
    if _CONTROL.search(raw):
        return {"ok": False, "error": "Post-call notes cannot contain control characters.", "note": "", "persisted": False}
    cleaned = _SPACE.sub(" ", raw).strip()
    if not cleaned:
        return {"ok": False, "error": "A post-call note is required.", "note": "", "persisted": False}
    if len(cleaned) > MAX_POST_CALL_NOTE_CHARACTERS:
        return {
            "ok": False,
            "error": f"Post-call notes must be at most {MAX_POST_CALL_NOTE_CHARACTERS} characters.",
            "note": "",
            "persisted": False,
        }
    return {
        "ok": True,
        "note": cleaned,
        "person_id": _clean_text(person_id, maximum=160),
        "call_id": _clean_text(call_id, maximum=160),
        "requires_explicit_attachment": True,
        "persisted": False,
    }


__all__ = [
    "MAX_CONTACT_RESULTS",
    "MAX_NOTIFICATION_RESULTS",
    "MAX_POST_CALL_NOTE_CHARACTERS",
    "MAX_SMS_CHARACTERS",
    "call_preflight",
    "mask_contact_value",
    "mask_phone_number",
    "merge_contact_candidates",
    "normalize_call_status",
    "normalize_contact_name",
    "normalize_phone_number",
    "phone_match_key",
    "prepare_notifications",
    "preview_sms",
    "project_sms_delivery",
    "rank_contact_candidates",
    "render_sms_template",
    "resolve_contact_channel",
    "sms_delivery_status",
    "sms_segmentation",
    "validate_post_call_note",
]
