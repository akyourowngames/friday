"""Shared arithmetic helpers for cognition modules.

Pure math and ISO timestamp handling. No regex, no keyword logic.
"""

from __future__ import annotations

import math
from datetime import datetime


def clamp01(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(result):
        return 0.0
    return min(1.0, max(0.0, result))


def safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def parse_iso(value) -> datetime | None:
    """Parse an ISO timestamp (optionally with a separate date+time) safely."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def combine_date_time(date_str: str, time_str: str) -> datetime | None:
    """Combine a memory's `_date` and `ts` fields into a datetime."""
    date_part = str(date_str or "").strip()
    time_part = str(time_str or "").strip() or "00:00:00"
    if not date_part:
        return None
    return parse_iso(f"{date_part}T{time_part}")


def half_life_decay(age_seconds: float, half_life_seconds: float) -> float:
    """Return a 0..1 multiplier that halves every `half_life_seconds`."""
    age = safe_float(age_seconds, 0.0)
    half_life = safe_float(half_life_seconds, 0.0)
    if half_life <= 0 or age <= 0:
        return 1.0 if age <= 0 else 0.0
    return float(2.0 ** (-(age / half_life)))


def cosine(vec_a, vec_b) -> float:
    """Cosine similarity for already-normalized embeddings falls out of a dot
    product, but we normalize defensively so callers cannot break it."""
    import numpy as np

    a = np.asarray(vec_a, dtype=np.float32).ravel()
    b = np.asarray(vec_b, dtype=np.float32).ravel()
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm <= 0:
        return 0.0
    return float(np.dot(a, b) / norm)
