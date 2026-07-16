"""Evidence-first visual verification with conservative confidence thresholds."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ares.vision.models import SceneSnapshot, VerificationResult, VerificationStatus, VisionFrame


class VisionReasoner(Protocol):
    """Optional multimodal reasoning boundary used only for selected frames."""

    async def verify(
        self,
        expected_result: str,
        snapshot: SceneSnapshot,
        reference_snapshot: SceneSnapshot | None = None,
        frame: VisionFrame | None = None,
    ) -> VerificationResult | dict[str, Any]: ...


_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "at", "be", "been", "by", "correctly", "did", "do", "does",
    "for", "from", "has", "have", "i", "in", "is", "it", "of", "on", "or", "the", "that",
    "to", "was", "were", "whether", "with", "you", "your",
})


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", value)
        if len(token) > 1 and token.casefold() not in _STOP_WORDS
    }


class VisionVerifier:
    """Return pass/fail only when visible evidence clears the threshold.

    Object labels and OCR can provide a limited deterministic confirmation.  A
    physical relationship such as a cable being connected remains uncertain
    without a configured multimodal reasoner; this is intentional, because a
    weak visual cue must never be promoted to task completion.
    """

    def __init__(
        self,
        reasoner: VisionReasoner | Callable[..., Any] | None = None,
        *,
        confidence_threshold: float = 0.80,
    ) -> None:
        self.reasoner = reasoner
        self.confidence_threshold = max(0.0, min(1.0, float(confidence_threshold)))

    async def verify(
        self,
        expected_result: str,
        snapshot: SceneSnapshot,
        reference_snapshot: SceneSnapshot | None = None,
        *,
        frame: VisionFrame | None = None,
    ) -> VerificationResult:
        expected = str(expected_result or "").strip()
        if not expected:
            raise ValueError("expected_result is required")
        deterministic = self._deterministic(expected, snapshot)
        if deterministic.status is not VerificationStatus.UNCERTAIN:
            return deterministic
        if self.reasoner is None:
            return deterministic
        try:
            if hasattr(self.reasoner, "verify"):
                try:
                    response = self.reasoner.verify(expected, snapshot, reference_snapshot, frame=frame)
                except TypeError:
                    response = self.reasoner.verify(expected, snapshot, reference_snapshot)
            else:
                try:
                    response = self.reasoner(expected, snapshot, reference_snapshot, frame=frame)
                except TypeError:
                    response = self.reasoner(expected, snapshot, reference_snapshot)
            if inspect.isawaitable(response):
                response = await response
            result = (
                response
                if isinstance(response, VerificationResult)
                else VerificationResult.model_validate(response)
            )
        except Exception as exc:
            return VerificationResult(
                status=VerificationStatus.UNCERTAIN,
                confidence=0.0,
                evidence=deterministic.evidence,
                missing_evidence=deterministic.missing_evidence + [f"Visual reasoner unavailable: {exc}"],
            )
        # A reasoner cannot override the safety threshold.  It may express a
        # weak conclusion, but the public result stays uncertain below 0.80.
        if result.status in {VerificationStatus.PASSED, VerificationStatus.FAILED} and result.confidence < self.confidence_threshold:
            return result.model_copy(update={
                "status": VerificationStatus.UNCERTAIN,
                "missing_evidence": result.missing_evidence + [
                    f"Confidence {result.confidence:.2f} is below the {self.confidence_threshold:.2f} verification threshold."
                ],
            })
        return result

    def _deterministic(self, expected: str, snapshot: SceneSnapshot) -> VerificationResult:
        required = _tokens(expected)
        object_labels = {str(item.label).casefold() for item in snapshot.objects}
        visible_text = " ".join(snapshot.visible_text).casefold()
        observed_words = set(object_labels)
        observed_words.update(_tokens(visible_text))
        matches = sorted(required & observed_words)
        missing = sorted(required - observed_words)
        object_confidence = max((float(item.confidence) for item in snapshot.objects), default=0.0)
        has_negation = bool(re.search(r"\b(?:not|no|without|missing|disconnected)\b", expected, re.I))

        evidence = []
        if matches:
            evidence.append(f"Visible evidence matches: {', '.join(matches)}.")
        if snapshot.visible_text:
            evidence.append("OCR was evaluated on the current frame.")
        if not required:
            return VerificationResult(
                status=VerificationStatus.UNCERTAIN,
                confidence=0.0,
                evidence=evidence,
                missing_evidence=["The requested result did not contain verifiable visible evidence."],
            )
        # Only simple label/text presence can be proven without a semantic
        # reasoner. Relationship/action phrasing intentionally falls through.
        relational = bool(re.search(r"\b(?:connected|inside|beside|behind|under|over|plugged|attached|correctly)\b", expected, re.I))
        if not has_negation and not relational and not missing and object_confidence >= self.confidence_threshold:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                confidence=object_confidence,
                evidence=evidence,
                missing_evidence=[],
            )
        if has_negation and missing and object_confidence >= self.confidence_threshold:
            # Absence cannot usually be proven by one frame: the target may be
            # outside view or below detector confidence. Never claim failure.
            return VerificationResult(
                status=VerificationStatus.UNCERTAIN,
                confidence=0.0,
                evidence=evidence,
                missing_evidence=["A single frame cannot reliably prove absence of the requested item."],
            )
        missing_text = (
            f"Missing visible evidence: {', '.join(missing)}."
            if missing else "The required spatial or physical relationship is not deterministically observable."
        )
        return VerificationResult(
            status=VerificationStatus.UNCERTAIN,
            confidence=min(object_confidence, self.confidence_threshold - 0.01),
            evidence=evidence,
            missing_evidence=[missing_text],
        )


__all__ = ["VisionReasoner", "VisionVerifier"]
