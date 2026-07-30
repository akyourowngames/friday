"""Tests for the Telegram medical report classifier."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ares.channels.medical_classifier import (
    MedicalClassifier,
    MedicalReportClassification,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_llm(content: str | dict | None, *, raise_error: bool = False):
    """Return a mock LLMClient whose chat() returns the given content."""
    if raise_error:
        response: dict = {"content": ""}
        side_effect = Exception("LLM API down")
    elif isinstance(content, dict):
        response = {"content": json.dumps(content)}
        side_effect = None
    else:
        response = {"content": content or ""}
        side_effect = None

    mock = AsyncMock()
    mock.chat = AsyncMock(return_value=response)
    if side_effect:
        mock.chat.side_effect = side_effect
    return mock


# ---------------------------------------------------------------------------
# Keyword classification tests
# ---------------------------------------------------------------------------

class TestKeywordClassification:
    """Test the keyword fast-path — no LLM calls."""

    @pytest.mark.asyncio
    async def test_keyword_high_confidence_positive(self):
        """Strong medical keywords → classified immediately, no LLM call."""
        classifier = MedicalClassifier(llm_client=_fake_llm("should not be called"))
        result = await classifier.classify(
            "Lab report for patient #1234 — blood work attached",
        )
        assert result.is_medical is True
        assert result.confidence >= 0.7
        assert result.source == "keyword"
        assert result.category in {
            "lab_report", "imaging", "prescription", "discharge_summary",
            "consultation", "vitals", "other_medical",
        }

    @pytest.mark.asyncio
    async def test_keyword_high_confidence_negative(self):
        """Casual conversation → classified as not medical, no LLM call."""
        classifier = MedicalClassifier(llm_client=_fake_llm("should not be called"))
        result = await classifier.classify("hey what's up lol thanks")
        assert result.is_medical is False
        assert result.source == "keyword"
        assert result.confidence >= 0.8

    @pytest.mark.asyncio
    async def test_keyword_strong_positive_with_attachment(self):
        """Medical filename + text → strong positive."""
        classifier = MedicalClassifier(llm_client=_fake_llm("should not be called"))
        result = await classifier.classify(
            "Here are the results",
            attachment_info={"name": "blood_work_report.pdf", "type": "application/pdf", "size": 50000, "kind": "file"},
        )
        assert result.is_medical is True
        assert result.source == "keyword"


# ---------------------------------------------------------------------------
# LLM fallback tests
# ---------------------------------------------------------------------------

class TestLLMFallback:
    """Test the LLM classification path."""

    @pytest.mark.asyncio
    async def test_llm_fallback_ambiguous(self):
        """Ambiguous message → LLM called, JSON parsed correctly."""
        llm_response = {
            "is_medical": True,
            "category": "consultation",
            "confidence": 0.85,
            "summary": "Dr. Patel sent an update on patient condition",
            "urgency": "routine",
        }
        classifier = MedicalClassifier(llm_client=_fake_llm(llm_response))
        result = await classifier.classify("Here's the update from Dr. Patel")
        assert result.is_medical is True
        assert result.category == "consultation"
        assert result.confidence == 0.85
        assert result.source == "llm"
        assert result.summary is not None
        assert result.urgency == "routine"

    @pytest.mark.asyncio
    async def test_llm_fallback_not_medical(self):
        """LLM says not medical for ambiguous message."""
        llm_response = {
            "is_medical": False,
            "category": "not_medical",
            "confidence": 0.9,
            "summary": None,
            "urgency": "none",
        }
        classifier = MedicalClassifier(llm_client=_fake_llm(llm_response))
        result = await classifier.classify("Can you look into this for me?")
        assert result.is_medical is False
        assert result.category == "not_medical"
        assert result.source == "llm"

    @pytest.mark.asyncio
    async def test_llm_fallback_error_handling(self):
        """LLM error → defaults to not_medical, no crash."""
        classifier = MedicalClassifier(llm_client=_fake_llm(None, raise_error=True))
        result = await classifier.classify("Ambiguous message about health")
        assert result.is_medical is False
        assert result.source == "llm_fallback"
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# File pattern tests
# ---------------------------------------------------------------------------

class TestFilePatterns:
    """Test filename-based medical detection."""

    @pytest.mark.asyncio
    async def test_file_pattern_medical_pdf(self):
        """Medical PDF filename → detected."""
        classifier = MedicalClassifier(llm_client=_fake_llm("should not be called"))
        result = await classifier.classify(
            "",
            attachment_info={"name": "mri_report_knee.pdf", "type": "application/pdf", "size": 100000, "kind": "file"},
        )
        assert result.is_medical is True
        assert result.source == "keyword"

    @pytest.mark.asyncio
    async def test_file_pattern_non_medical(self):
        """Non-medical filename → not detected by file pattern alone."""
        classifier = MedicalClassifier(llm_client=_fake_llm("should not be called"))
        result = await classifier.classify(
            "Check this out",
            attachment_info={"name": "vacation_photo.jpg", "type": "image/jpeg", "size": 2000000, "kind": "image"},
        )
        # "Check this out" has no medical keywords, no medical filename → ambiguous → LLM
        # But with the casual text, it should be classified as not medical by keyword gate
        assert result.is_medical is False


# ---------------------------------------------------------------------------
# Classification result tests
# ---------------------------------------------------------------------------

class TestClassificationResult:
    """Test the MedicalReportClassification dataclass."""

    def test_all_fields_populated(self):
        """Result has all expected fields."""
        result = MedicalReportClassification(
            is_medical=True, confidence=0.9, category="lab_report",
            summary="Blood glucose elevated", urgency="urgent", source="llm",
        )
        assert result.is_medical is True
        assert result.confidence == 0.9
        assert result.category == "lab_report"
        assert result.summary == "Blood glucose elevated"
        assert result.urgency == "urgent"
        assert result.source == "llm"

    def test_to_dict(self):
        """to_dict returns a JSON-serializable dict."""
        result = MedicalReportClassification(
            is_medical=True, confidence=0.8, category="imaging",
            summary=None, urgency="critical", source="keyword",
        )
        d = result.to_dict()
        assert d["is_medical"] is True
        assert d["category"] == "imaging"
        assert d["urgency"] == "critical"
        assert d["summary"] is None
        # Ensure it's JSON-serializable
        json.dumps(d)


# ---------------------------------------------------------------------------
# Urgency detection tests
# ---------------------------------------------------------------------------

class TestUrgencyDetection:
    """Test urgency signals in keyword classification."""

    @pytest.mark.asyncio
    async def test_critical_urgency(self):
        """Critical keywords → urgency=critical."""
        classifier = MedicalClassifier(llm_client=_fake_llm("should not be called"))
        result = await classifier.classify(
            "Critical lab report — ICU patient blood work stat"
        )
        assert result.is_medical is True
        assert result.urgency == "critical"

    @pytest.mark.asyncio
    async def test_routine_urgency(self):
        """Routine keywords → urgency=routine."""
        classifier = MedicalClassifier(llm_client=_fake_llm("should not be called"))
        result = await classifier.classify(
            "Routine follow-up lab results for annual checkup"
        )
        assert result.is_medical is True
        assert result.urgency == "routine"
