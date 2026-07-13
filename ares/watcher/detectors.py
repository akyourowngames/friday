"""Deterministic, noise-resistant watcher change detection."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DetectionResult:
    changed: bool
    method: str
    old_hash: str | None = None
    new_hash: str | None = None
    summary: str = ""
    old_value: Any = None
    new_value: Any = None
    severity: str = "info"
    details: dict[str, Any] = field(default_factory=dict)


def canonicalize(value: Any, ignore_patterns: list[str] | None = None) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern in ignore_patterns or []:
        try:
            text = re.sub(pattern, "", text, flags=re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"Invalid ignore pattern {pattern!r}: {exc}") from exc
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


class HashDetector:
    def _hash(self, content: Any, ignore_patterns: list[str] | None = None) -> str:
        return hashlib.sha256(canonicalize(content, ignore_patterns).encode("utf-8")).hexdigest()

    def detect(self, old_content: Any, new_content: Any, *, ignore_patterns: list[str] | None = None) -> DetectionResult:
        old_hash, new_hash = self._hash(old_content, ignore_patterns), self._hash(new_content, ignore_patterns)
        changed = old_hash != new_hash
        return DetectionResult(changed=changed, method="hash", old_hash=old_hash, new_hash=new_hash,
            summary="Content changed" if changed else "No content change", old_value=old_content, new_value=new_content)


class DiffDetector(HashDetector):
    def detect(self, old_content: Any, new_content: Any, *, ignore_patterns: list[str] | None = None, context_lines: int = 2) -> DetectionResult:
        old, new = canonicalize(old_content, ignore_patterns), canonicalize(new_content, ignore_patterns)
        base = super().detect(old, new)
        if not base.changed:
            base.method, base.summary = "diff", "No text change"
            return base
        old_lines, new_lines = old.splitlines(), new.splitlines()
        diff = list(difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after", n=context_lines, lineterm=""))
        matcher = difflib.SequenceMatcher(None, old, new)
        similarity = round(matcher.ratio(), 4)
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        return DetectionResult(True,"diff",base.old_hash,base.new_hash,
            f"Text changed: {added} added, {removed} removed",old,new,
            "warning" if similarity < .5 else "info",{"diff":"\n".join(diff[:300]),"similarity":similarity,"added":added,"removed":removed})


class ThresholdDetector:
    def detect(self, old_value: float | int | None, new_value: float | int | None, thresholds: dict[str, Any] | None = None) -> DetectionResult:
        if old_value is None or new_value is None:
            return DetectionResult(False,"threshold",summary="Insufficient numeric history",old_value=old_value,new_value=new_value)
        old, new, cfg = float(old_value), float(new_value), thresholds or {}
        delta, pct = new - old, ((new - old) / abs(old) * 100) if old else (100.0 if new else 0.0)
        reasons: list[str] = []
        if cfg.get("max_change_pct") is not None and abs(pct) >= float(cfg["max_change_pct"]): reasons.append(f"change {pct:+.2f}%")
        if cfg.get("max_change_abs") is not None and abs(delta) >= float(cfg["max_change_abs"]): reasons.append(f"delta {delta:+g}")
        if cfg.get("alert_below") is not None and new <= float(cfg["alert_below"]) and old > float(cfg["alert_below"]): reasons.append(f"crossed below {cfg['alert_below']}")
        if cfg.get("alert_above") is not None and new >= float(cfg["alert_above"]) and old < float(cfg["alert_above"]): reasons.append(f"crossed above {cfg['alert_above']}")
        if not cfg and old != new: reasons.append(f"changed {pct:+.2f}%")
        changed = bool(reasons)
        severity = "critical" if any("crossed" in reason for reason in reasons) else ("warning" if changed else "info")
        return DetectionResult(changed,"threshold",summary="; ".join(reasons) if reasons else "Threshold not crossed",
            old_value=old,new_value=new,severity=severity,details={"delta":delta,"percent_change":pct})
