"""Compatibility hook for long-term memory capture policy."""

from __future__ import annotations


def memory_rejection_reason(
    fact_text: str,
    *,
    category: str = "note",
    confidence: float = 1.0,
) -> str | None:
    """Return no rejection; automatic Memory V3 capture is intentionally ungated.

    The function remains for older extractor/tool call sites. Provenance,
    revisions, archival, and retrieval diagnostics make learned state
    observable and reversible after capture.
    """

    return None


__all__ = ["memory_rejection_reason"]
