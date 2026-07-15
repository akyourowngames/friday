"""Structured evidence contracts for specialist research and synthesis."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


_EXACT_NUMBER = re.compile(
    r"(?<![A-Za-z])(?:\d+(?:\.\d+)?\s*(?:%|ms|s|seconds?|rps|req(?:uests?)?/s|x)|"
    r"\d{2,}(?:,\d{3})*(?:\.\d+)?)",
    re.IGNORECASE,
)
_BENCHMARK_NUMBER = re.compile(
    r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:%|ms|s|seconds?|rps|req(?:uests?)?/s|x)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    claim: str
    source_urls: tuple[str, ...]
    evidence: tuple[str, ...]
    confidence: float
    caveats: tuple[str, ...] = ()
    publication_dates: tuple[str, ...] = ()
    benchmark_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        claim = self.claim.strip()
        if not claim:
            raise ValueError("research claim cannot be empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("research confidence must be between 0 and 1")
        object.__setattr__(self, "claim", claim)
        for field_name in (
            "source_urls", "evidence", "caveats", "publication_dates", "benchmark_conditions"
        ):
            values = tuple(dict.fromkeys(
                str(value).strip() for value in getattr(self, field_name) if str(value).strip()
            ))
            object.__setattr__(self, field_name, values)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "source_urls": list(self.source_urls),
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "caveats": list(self.caveats),
            "publication_dates": list(self.publication_dates),
            "benchmark_conditions": list(self.benchmark_conditions),
        }


@dataclass(frozen=True, slots=True)
class ResearchValidation:
    valid: bool
    issues: tuple[str, ...]
    claims: tuple[ResearchClaim, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": list(self.issues),
            "claims": [claim.as_dict() for claim in self.claims],
        }


def _public_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_research_claim(claim: ResearchClaim) -> tuple[str, ...]:
    issues: list[str] = []
    invalid_urls = [url for url in claim.source_urls if not _public_http_url(url)]
    if invalid_urls:
        issues.append("claim contains invalid source URLs")
    if not claim.source_urls:
        issues.append("claim has no source URL")
    if not claim.evidence:
        issues.append("claim has no quoted or paraphrased evidence")
    if _EXACT_NUMBER.search(claim.claim):
        if not claim.source_urls or not claim.evidence:
            issues.append("exact numeric claim lacks source evidence")
    if _BENCHMARK_NUMBER.search(claim.claim):
        if not claim.benchmark_conditions:
            issues.append("exact benchmark/throughput claim lacks conditions")
    return tuple(dict.fromkeys(issues))


def validate_research_claims(claims: Iterable[ResearchClaim]) -> ResearchValidation:
    claims = tuple(claims)
    issues: list[str] = []
    for index, claim in enumerate(claims, 1):
        issues.extend(f"claim {index}: {issue}" for issue in validate_research_claim(claim))
    return ResearchValidation(not issues, tuple(issues), claims)


def validate_research_text(text: str, *, require_structured: bool = False) -> ResearchValidation:
    """Flag unstructured or locally unsupported exact figures."""
    text = str(text or "")
    issues: list[str] = []
    if require_structured:
        issues.append("research output must be a structured JSON claims object")
    segments = re.split(r"(?<=[.!?])\s+|\n+", text)
    for index, segment in enumerate(segments, 1):
        if _EXACT_NUMBER.search(segment) and not re.search(r"https?://[^\s)\]}>]+", segment):
            issues.append(
                f"unstructured research segment {index} contains an exact figure without a directly associated source URL"
            )
    return ResearchValidation(not issues, tuple(issues))


def research_claim_from_mapping(item: Mapping[str, Any]) -> ResearchClaim:
    def values(name: str) -> tuple[str, ...]:
        raw = item.get(name) or ()
        if isinstance(raw, str):
            raw = (raw,)
        return tuple(str(value) for value in raw)

    return ResearchClaim(
        claim=str(item.get("claim") or ""),
        source_urls=values("source_urls"),
        evidence=values("evidence"),
        confidence=float(item.get("confidence", 0.0)),
        caveats=values("caveats"),
        publication_dates=values("publication_dates"),
        benchmark_conditions=values("benchmark_conditions"),
    )


def parse_research_claims(
    content: str, *, require_structured: bool = False
) -> ResearchValidation:
    """Parse a JSON research result while retaining validation failures."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return validate_research_text(content, require_structured=require_structured)
    raw_claims = payload.get("claims") if isinstance(payload, dict) else payload
    if not isinstance(raw_claims, list):
        if require_structured:
            return ResearchValidation(False, ("research JSON must contain a claims array",))
        return validate_research_text(content)
    claims: list[ResearchClaim] = []
    issues: list[str] = []
    for index, item in enumerate(raw_claims, 1):
        if not isinstance(item, Mapping):
            issues.append(f"claim {index}: expected an object")
            continue
        try:
            claims.append(research_claim_from_mapping(item))
        except (TypeError, ValueError) as exc:
            issues.append(f"claim {index}: {exc}")
    validation = validate_research_claims(claims)
    issues.extend(validation.issues)
    return ResearchValidation(not issues, tuple(issues), tuple(claims))


def synthesis_confidence(
    source_claims: Iterable[ResearchClaim], proposed_confidence: float
) -> float:
    """A synthesizer can never exceed its strongest underlying evidence."""
    source_claims = tuple(source_claims)
    ceiling = max((claim.confidence for claim in source_claims), default=0.0)
    return max(0.0, min(float(proposed_confidence), ceiling))


def conflicting_claims(claims: Iterable[ResearchClaim]) -> tuple[tuple[int, int], ...]:
    """Surface simple explicit positive/negative contradictions for review."""
    claims = tuple(claims)
    conflicts: list[tuple[int, int]] = []
    normalized = [re.sub(r"\s+", " ", claim.claim.casefold()).strip() for claim in claims]
    for left in range(len(claims)):
        for right in range(left + 1, len(claims)):
            first = normalized[left]
            second = normalized[right]
            if first == second:
                continue
            first_plain = re.sub(
                r"\s+", " ", re.sub(r"\b(?:not|no|never)\b", "", first)
            ).strip()
            second_plain = re.sub(
                r"\s+", " ", re.sub(r"\b(?:not|no|never)\b", "", second)
            ).strip()
            if first_plain == second_plain and (
                bool(re.search(r"\b(?:not|no|never)\b", first))
                != bool(re.search(r"\b(?:not|no|never)\b", second))
            ):
                conflicts.append((left, right))
    return tuple(conflicts)
