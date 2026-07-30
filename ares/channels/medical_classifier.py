"""Lightweight medical report classifier for incoming Telegram messages.

Uses a keyword fast-path for obvious cases and falls back to LLM
classification for ambiguous messages.  Designed to sit in the Telegram
channel's message pipeline *before* the main agent turn so it can enrich
the prompt with medical context.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Keyword sets — free, instant classification
# ---------------------------------------------------------------------------

# Terms that strongly signal a medical report / document (case-insensitive).
MEDICAL_REPORT_KEYWORDS: frozenset[str] = frozenset({
    "lab report", "lab result", "lab results", "test result", "test results",
    "blood work", "blood test", "blood report",
    "scan result", "scan report", "x-ray report", "mri report",
    "ct scan", "ultrasound report",
    "discharge summary", "discharge report",
    "prescription", "rx", "medication list",
    "diagnosis report", "diagnostic report",
    "pathology", "biopsy report", "biopsy result",
    "ecg report", "echo report", "ekg report",
    "culture report", "culture result",
    "urine test", "urinalysis",
    "hba1c", "lipid panel", "cbc report", "complete blood count",
    "thyroid panel", "metabolic panel",
    "radiology", "radiologist",
    "clinical findings", "clinical report",
    "patient report", "medical report",
    "operative report", "surgical report",
    "referral letter", "consultation report",
    "vitals", "vital signs",
    "follow-up", "follow up",
    "icu", "sepsis", "blood culture", "patient",
    "metformin", "lisinopril", "atorvastatin",
    "hemoglobin", "wbc", "platelet", "creatinine",
})

# Casual / conversational terms — strong signal it's NOT a report.
NON_MEDICAL_KEYWORDS: frozenset[str] = frozenset({
    "hey", "hello", "hi", "thanks", "thank you", "thx", "lol", "haha",
    "okay", "ok", "sure", "cool", "nice", "great", "awesome",
    "dinner", "lunch", "coffee", "movie", "game",
    "call me", "busy", "later", "tomorrow", "monday", "friday",
    "birthday", "party", "weekend",
})

# Filename patterns that suggest medical documents.
MEDICAL_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:lab|blood|test|scan|xray|x-ray|mri|ct|echo|ecg|ekg|prescription|rx|discharge|pathology|biopsy|report|medical|clinical|vitals|referral|consult)", re.IGNORECASE),
)

# Map keyword fragments → classification category.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "lab_report": ("lab report", "lab result", "test result", "blood work", "blood test", "blood report", "hba1c", "lipid panel", "cbc", "culture", "urine"),
    "imaging": ("scan", "x-ray", "xray", "mri", "ct scan", "ultrasound", "radiology", "radiologist"),
    "prescription": ("prescription", "rx", "medication", "medication list"),
    "discharge_summary": ("discharge summary", "discharge report"),
    "consultation": ("consultation", "referral", "opinion"),
    "vitals": ("vitals", "vital signs"),
}

# Urgency signal keywords.
_URGENCY_CRITICAL: frozenset[str] = frozenset({
    "critical", "emergency", "icu", "code blue", "code red", "immediate",
    "life-threatening", "deteriorating", "acute", "stat",
})
_URGENCY_ROUTINE: frozenset[str] = frozenset({
    "routine", "follow-up", "follow up", "annual", "check-up", "checkup",
    "screening", "elective",
})


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MedicalReportClassification:
    """Result of classifying a Telegram message."""

    is_medical: bool = False
    confidence: float = 0.0
    category: str = "not_medical"
    summary: str | None = None
    urgency: str = "none"
    source: str = "keyword"  # "keyword" or "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_medical": self.is_medical,
            "confidence": self.confidence,
            "category": self.category,
            "summary": self.summary,
            "urgency": self.urgency,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class MedicalClassifier:
    """Classify Telegram messages as medical reports or not.

    Uses a keyword fast-path for obvious cases and falls back to LLM
    classification for ambiguous messages.
    """

    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client

    async def classify(
        self,
        text: str,
        attachment_info: dict[str, Any] | None = None,
    ) -> MedicalReportClassification:
        """Classify a message as a medical report or not.

        Parameters
        ----------
        text : the message text or caption.
        attachment_info : optional dict with keys like ``name``, ``type``,
            ``size``, ``kind`` from Telegram attachment metadata.

        Returns
        -------
        MedicalReportClassification with all fields populated.
        """
        combined = self._build_input(text, attachment_info)

        # Step 1: keyword gate
        keyword_result = self._keyword_classify(combined, attachment_info)
        if keyword_result is not None:
            return keyword_result

        # Step 2: LLM fallback
        return await self._llm_classify(text, attachment_info)

    # ------------------------------------------------------------------
    # Keyword fast-path
    # ------------------------------------------------------------------

    def _keyword_classify(
        self,
        combined: str,
        attachment_info: dict[str, Any] | None,
    ) -> MedicalReportClassification | None:
        """Try keyword classification.  Returns None if ambiguous."""
        lowered = combined.lower()

        # Check filename patterns first.
        filename = str((attachment_info or {}).get("name") or "").lower()
        has_medical_file = any(p.search(filename) for p in MEDICAL_FILE_PATTERNS)

        # Count positive medical keyword hits (flexible word-boundary match).
        # Allows dashes, colons, and punctuation between/around keywords.
        positive_hits = sum(
            1 for kw in MEDICAL_REPORT_KEYWORDS
            if re.search(
                r"(?:^|[\s\-:;,]+)" + re.escape(kw).replace(r"\ ", r"[\s\-:;,]+")
                + r"(?:[\s\-:;,]+|$)",
                lowered,
            )
        )

        # Count negative / casual keyword hits (flexible word-boundary match).
        negative_hits = sum(
            1 for kw in NON_MEDICAL_KEYWORDS
            if re.search(
                r"(?:^|[\s\-:;,]+)" + re.escape(kw).replace(r"\ ", r"[\s\-:;,]+")
                + r"(?:[\s\-:;,]+|$)",
                lowered,
            )
        )

        # Strong positive: multiple medical keywords, or medical filename + at
        # least one keyword, or a medical filename alone (strong signal).
        is_file = bool(attachment_info and attachment_info.get("kind") in {"file", "image"})
        strong_positive = (
            positive_hits >= 2
            or (has_medical_file and positive_hits >= 1)
            or (has_medical_file and is_file)
            or (has_medical_file and len(combined) > 5)
        )

        # Strong negative: casual conversation with no medical keywords.
        strong_negative = (
            negative_hits >= 2 and positive_hits == 0
            or (negative_hits >= 1 and positive_hits == 0 and not has_medical_file and not is_file)
        )

        if strong_positive:
            category = self._detect_category(lowered)
            urgency = self._detect_urgency(lowered)
            confidence = min(0.95, 0.7 + 0.05 * positive_hits)
            return MedicalReportClassification(
                is_medical=True,
                confidence=confidence,
                category=category,
                urgency=urgency,
                source="keyword",
            )

        if strong_negative:
            return MedicalReportClassification(
                is_medical=False,
                confidence=0.85,
                category="not_medical",
                urgency="none",
                source="keyword",
            )

        # Ambiguous — let LLM decide.
        return None

    # ------------------------------------------------------------------
    # LLM fallback
    # ------------------------------------------------------------------

    async def _llm_classify(
        self,
        text: str,
        attachment_info: dict[str, Any] | None,
    ) -> MedicalReportClassification:
        """Use LLM for ambiguous classification."""
        if self._llm is None:
            try:
                from ares.integrations.llm import LLMClient
                self._llm = LLMClient()
            except Exception:
                return MedicalReportClassification(
                    is_medical=False, confidence=0.0,
                    category="not_medical", urgency="none", source="llm_fallback",
                )

        user_content = self._build_llm_user_content(text, attachment_info)

        try:
            response = await self._llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a medical document classifier. "
                            "Determine if a Telegram message contains or references a medical report, "
                            "lab result, prescription, clinical finding, or health-related document.\n\n"
                            "Reply with ONLY a JSON object (no markdown, no explanation):\n"
                            '{"is_medical": true/false, "category": "lab_report|imaging|prescription|discharge_summary|consultation|vitals|other_medical|not_medical", '
                            '"confidence": 0.0-1.0, "summary": "brief one-line summary if medical", '
                            '"urgency": "critical|urgent|routine|none"}\n\n'
                            "Rules:\n"
                            "- A forwarded message from a doctor or hospital is likely medical.\n"
                            "- A PDF or image attachment with medical-sounding filename is likely medical.\n"
                            "- Casual health chat ('I have a headache') is NOT a report.\n"
                            "- If unsure, set is_medical to false."
                        ),
                    },
                    {"role": "user", "content": user_content},
                ],
                tool_choice="none",
                max_tokens=200,
                temperature=0.1,
            )

            content = str(
                response.get("content")
                or response.get("reasoning_content")
                or ""
            ).strip()
            return self._parse_llm_response(content)

        except Exception:
            return MedicalReportClassification(
                is_medical=False, confidence=0.0,
                category="not_medical", urgency="none", source="llm_fallback",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_input(text: str, attachment_info: dict[str, Any] | None) -> str:
        """Combine text + attachment metadata for keyword scanning."""
        parts = [text or ""]
        if attachment_info:
            name = str(attachment_info.get("name") or "")
            if name:
                parts.append(name)
        return " ".join(parts)

    @staticmethod
    def _build_llm_user_content(
        text: str, attachment_info: dict[str, Any] | None,
    ) -> str:
        """Build the user message for the LLM classifier."""
        parts = []
        if text:
            parts.append(f"Message text: {text}")
        if attachment_info:
            name = str(attachment_info.get("name") or "unknown")
            mime = str(attachment_info.get("type") or "unknown")
            size = int(attachment_info.get("size") or 0)
            kind = str(attachment_info.get("kind") or "unknown")
            parts.append(
                f"Attachment: {name} ({mime}, {kind}, {size} bytes)"
            )
        if not parts:
            parts.append("(empty message)")
        return "\n".join(parts)

    @staticmethod
    def _detect_category(text_lower: str) -> str:
        """Map text to a medical category based on keyword density."""
        best_category = "other_medical"
        best_count = 0
        for category, keywords in _CATEGORY_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw in text_lower)
            if count > best_count:
                best_count = count
                best_category = category
        return best_category

    @staticmethod
    def _detect_urgency(text_lower: str) -> str:
        """Detect urgency level from text."""
        if any(kw in text_lower for kw in _URGENCY_CRITICAL):
            return "critical"
        if any(kw in text_lower for kw in _URGENCY_ROUTINE):
            return "routine"
        # Check for numeric urgency signals (high BP, high glucose, etc.)
        if re.search(r"(?:bp|blood pressure)\s*[:=]?\s*(?:1[89]\d|2\d\d)", text_lower):
            return "urgent"
        if re.search(r"(?:glucose|sugar)\s*[:=]?\s*(?:[3-9]\d\d|1\d{3})", text_lower):
            return "urgent"
        return "none"

    @staticmethod
    def _parse_llm_response(content: str) -> MedicalReportClassification:
        """Parse the LLM's JSON response into a classification."""
        # Strip markdown code fences if present.
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            # Try to extract JSON from the response.
            match = re.search(r"\{[^}]+\}", cleaned, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except (json.JSONDecodeError, ValueError):
                    return MedicalReportClassification(
                        is_medical=False, confidence=0.0,
                        category="not_medical", urgency="none", source="llm",
                    )
            else:
                return MedicalReportClassification(
                    is_medical=False, confidence=0.0,
                    category="not_medical", urgency="none", source="llm",
                )

        valid_categories = {
            "lab_report", "imaging", "prescription", "discharge_summary",
            "consultation", "vitals", "other_medical", "not_medical",
        }
        valid_urgency = {"critical", "urgent", "routine", "none"}

        is_medical = bool(data.get("is_medical", False))
        confidence = float(data.get("confidence", 0.0))
        category = str(data.get("category", "not_medical"))
        if category not in valid_categories:
            category = "other_medical" if is_medical else "not_medical"
        urgency = str(data.get("urgency", "none"))
        if urgency not in valid_urgency:
            urgency = "none"
        summary = data.get("summary") or None
        if summary and not is_medical:
            summary = None

        return MedicalReportClassification(
            is_medical=is_medical,
            confidence=min(1.0, max(0.0, confidence)),
            category=category,
            summary=summary,
            urgency=urgency,
            source="llm",
        )
