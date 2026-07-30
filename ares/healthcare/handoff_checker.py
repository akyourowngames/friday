"""Handoff Safety Checker — compares spoken handoff against patient records.

Core engine:
1. Parses a handoff transcript to extract medical facts
2. Loads the relevant patient file
3. Compares each claim against real data
4. Returns a structured safety report
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ares.healthcare.patient_store import PatientStore

logger = logging.getLogger(__name__)

# ── Lab test aliases (spoken → canonical) ────────────────────────────

LAB_ALIASES: dict[str, str] = {
    "potassium": "potassium", "k": "potassium",
    "sodium": "sodium", "na": "sodium",
    "creatinine": "creatinine", "crea": "creatinine",
    "blood urea": "blood_urea_nitrogen", "bun": "blood_urea_nitrogen", "urea": "blood_urea_nitrogen",
    "hemoglobin": "hemoglobin", "hb": "hemoglobin", "hgb": "hemoglobin",
    "wbc": "wbc", "white blood count": "wbc", "white blood cell": "wbc",
    "platelet": "platelets", "platelets": "platelets", "plt": "platelets",
    "blood sugar": "blood_sugar", "sugar": "blood_sugar", "glucose": "blood_sugar",
    "hba1c": "hba1c", "a1c": "hba1c",
    "spo2": "spo2", "o2 saturation": "spo2", "oxygen": "spo2", "o2": "spo2",
    "urine output": "urine_output", "urine": "urine_output", "output": "urine_output",
    "ecg": "ecg", "ekg": "ecg",
    "chest xray": "chest_xray", "chest x-ray": "chest_xray", "cxr": "chest_xray",
    "inr": "inr", "pt inr": "inr",
    "lactate": "lactate", "lactic acid": "lactate",
    "troponin": "troponin", "trop": "troponin",
    "crp": "crp", "procalcitonin": "procalcitonin", "pct": "procalcitonin",
}

CRITICAL_THRESHOLDS: dict[str, dict[str, float]] = {
    "potassium": {"low": 3.5, "high": 5.5, "critical_high": 6.0},
    "sodium": {"low": 130, "high": 150},
    "creatinine": {"high": 1.5, "critical_high": 3.0},
    "hemoglobin": {"low": 7.0, "high": 18.0},
    "wbc": {"low": 3000, "high": 12000},
    "platelets": {"low": 50000, "high": 500000},
    "blood_sugar": {"low": 60, "high": 300},
    "spo2": {"low": 90, "critical_low": 85},
    "inr": {"high": 3.0, "critical_high": 4.0},
    "lactate": {"high": 4.0, "critical_high": 6.0},
    "troponin": {"high": 0.04, "critical_high": 0.1},
}

# ── Medication aliases ───────────────────────────────────────────────

MED_ALIASES: dict[str, str] = {
    "paracetamol": "paracetamol", "acetaminophen": "paracetamol",
    "dolo": "paracetamol", "crocin": "paracetamol", "tylenol": "paracetamol",
    "tramadol": "tramadol", "ultram": "tramadol",
    "diclofenac": "diclofenac", "voltaren": "diclofenac",
    "ibuprofen": "ibuprofen", "brufen": "ibuprofen",
    "aspirin": "aspirin", "ecospirin": "aspirin",
    "warfarin": "warfarin", "coumadin": "warfarin",
    "heparin": "heparin",
    "enoxaparin": "enoxaparin", "clexane": "enoxaparin",
    "clopidogrel": "clopidogrel", "plavix": "clopidogrel",
    "metformin": "metformin", "glycomet": "metformin",
    "insulin": "insulin", "humulin": "insulin", "lantus": "insulin",
    "amoxicillin": "amoxicillin", "amox": "amoxicillin",
    "ciprofloxacin": "ciprofloxacin", "cipro": "ciprofloxacin",
    "metronidazole": "metronidazole", "flagyl": "metronidazole",
    "pantoprazole": "pantoprazole", "pantop": "pantoprazole", "pant": "pantoprazole",
    "omeprazole": "omeprazole",
    "losartan": "losartan", "amlodipine": "amlodipine",
    "atenolol": "atenolol", "metoprolol": "metoprolol",
    "saline": "normal_saline", "ns": "normal_saline",
    "ringer": "ringers_lactate", "rl": "ringers_lactate", "dns": "dextrose_normal_saline",
    "morphine": "morphine", "fentanyl": "fentanyl",
    "midazolam": "midazolam", "diazepam": "diazepam", "lorazepam": "lorazepam",
    "ondansetron": "ondansetron", "emset": "ondansetron",
    "calcium": "calcium", "iron": "iron", "folic acid": "folic_acid",
}


@dataclass
class HandoffClaim:
    """A single claim extracted from the handoff transcript."""
    category: str  # "lab", "medication", "condition", "plan"
    claim: str
    key: str
    value: Any = None
    raw: str = ""


@dataclass
class SafetyFlag:
    """A safety issue found during comparison."""
    severity: str  # "correct", "warning", "critical", "wrong", "missed"
    category: str
    message: str
    detail: str = ""
    claim: str = ""


@dataclass
class HandoffReport:
    """Complete safety report for a handoff."""
    patient_name: str = ""
    bed: str = ""
    claims_found: int = 0
    correct: list[SafetyFlag] = field(default_factory=list)
    warnings: list[SafetyFlag] = field(default_factory=list)
    critical: list[SafetyFlag] = field(default_factory=list)
    wrong: list[SafetyFlag] = field(default_factory=list)
    missed: list[SafetyFlag] = field(default_factory=list)

    @property
    def total_issues(self) -> int:
        return len(self.warnings) + len(self.critical) + len(self.wrong)

    def to_text(self) -> str:
        lines: list[str] = []
        header = "📋 Handoff Safety Check"
        if self.patient_name:
            header += f" — {self.bed}, {self.patient_name}" if self.bed else f" — {self.patient_name}"
        elif self.bed:
            header += f" — Bed {self.bed}"
        lines.append(header)
        lines.append("")

        if self.claims_found == 0:
            lines.append("⚠️ Could not extract structured claims from the handoff.")
            lines.append("Please include patient identifiers, lab values, medications, or conditions.")
            return "\n".join(lines)

        lines.append(f"Claims checked: {self.claims_found}")

        if self.total_issues == 0 and not self.correct:
            lines.append("✅ All mentioned items match patient records.")
            return "\n".join(lines)

        if self.correct:
            lines.append("")
            lines.append("✅ Correct:")
            for flag in self.correct:
                lines.append(f"  • {flag.message}")

        if self.critical:
            lines.append("")
            lines.append("🔴 CRITICAL:")
            for flag in self.critical:
                lines.append(f"  🔴 {flag.message}")
                if flag.detail:
                    lines.append(f"     → {flag.detail}")

        if self.wrong:
            lines.append("")
            lines.append("❌ WRONG:")
            for flag in self.wrong:
                lines.append(f"  ❌ {flag.message}")
                if flag.detail:
                    lines.append(f"     → {flag.detail}")

        if self.warnings:
            lines.append("")
            lines.append("⚠️ WARNINGS:")
            for flag in self.warnings:
                lines.append(f"  ⚠️ {flag.message}")
                if flag.detail:
                    lines.append(f"     → {flag.detail}")

        if self.missed:
            lines.append("")
            lines.append("🔴 NOT MENTIONED (critical):")
            for flag in self.missed:
                lines.append(f"  • {flag.message}")
                if flag.detail:
                    lines.append(f"     → {flag.detail}")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patient": self.patient_name,
            "bed": self.bed,
            "claims_checked": self.claims_found,
            "correct": [f.message for f in self.correct],
            "critical": [{"message": f.message, "detail": f.detail} for f in self.critical],
            "wrong": [{"message": f.message, "detail": f.detail} for f in self.wrong],
            "warnings": [{"message": f.message, "detail": f.detail} for f in self.warnings],
            "missed": [f.message for f in self.missed],
            "total_issues": self.total_issues,
            "text": self.to_text(),
        }


class HandoffChecker:
    """Compares a spoken handoff transcript against patient records."""

    def __init__(self, patient_store: PatientStore | None = None) -> None:
        self._store = patient_store or PatientStore()

    def check(self, transcript: str, patient_identifier: str | None = None) -> HandoffReport:
        if patient_identifier:
            patient = self._store.find_patient(patient_identifier)
        else:
            patient = self._extract_and_find_patient(transcript)

        if patient is None:
            report = HandoffReport()
            report.warnings.append(SafetyFlag(
                severity="warning", category="patient",
                message="Could not identify patient. Include bed number or name.",
            ))
            return report

        report = HandoffReport(
            patient_name=str(patient.get("name", "Unknown")),
            bed=str(patient.get("bed", "")),
        )
        claims = self._extract_claims(transcript)
        report.claims_found = len(claims)

        for claim in claims:
            self._compare_claim(claim, patient, report)

        self._check_omissions(claims, patient, report)
        return report

    # ── Patient Extraction ──────────────────────────────────────────

    def _extract_and_find_patient(self, transcript: str) -> dict[str, Any] | None:
        text = transcript.lower()
        bed_match = re.search(r"bed\s*(?:number\s*)?([A-Za-z0-9\-]+)", text)
        if bed_match:
            patient = self._store.search_by_bed(bed_match.group(1))
            if patient:
                return patient

        name_match = re.search(
            r"(?:mr\.?|mrs\.?|ms\.?|shri|smt\.?|ji|sahab|patient)\s+([A-Za-z\s]+?)(?:\s*,|\s*\d|\s+age|\s+years|\s+saal|\s+ka\b|\s+ki\b|\s+ko\b|\s+hai\b|$)",
            text, re.IGNORECASE,
        )
        if name_match:
            patient = self._store.search_by_name(name_match.group(1).strip())
            if patient:
                return patient

        for word in text.split():
            clean = re.sub(r"[^a-z]", "", word)
            if len(clean) >= 3:
                patient = self._store.search_by_name(clean)
                if patient:
                    return patient
        return None

    # ── Claim Extraction ────────────────────────────────────────────

    def _extract_claims(self, transcript: str) -> list[HandoffClaim]:
        claims: list[HandoffClaim] = []
        # Split on sentence boundaries: period/semicolon/Hindi-period + space,
        # or comma + 2+ spaces (list separators). This preserves decimal
        # numbers like "5.8" while still splitting "patient is stable, potassium 5.8".
        for sentence in re.split(r"(?<=[.।;])\s+|,\s{2,}", transcript.lower().strip()):
            sentence = sentence.strip()
            if not sentence:
                continue
            claims.extend(self._extract_lab_claims(sentence))
            claims.extend(self._extract_med_claims(sentence))
            claims.extend(self._extract_condition_claims(sentence))
            claims.extend(self._extract_plan_claims(sentence))
        return claims

    def _extract_lab_claims(self, sentence: str) -> list[HandoffClaim]:
        claims: list[HandoffClaim] = []
        patterns = [
            r"(potassium|sodium|creatinine|hemoglobin|hb|hgb|wbc|platelet|plt|inr|lactate|troponin|crp|procalcitonin|spo2|blood\s*sugar|glucose|sugar|urea|bun|ecg|ekg|chest\s*x[-\s]*ray|cxr|xray|hba1c|a1c)\s+(?:is|was|aaya|hai|came\s*(?:back\s*)?)?\s*(\d+\.?\d*)\s*(?:mg|%|mmol|meq|g|dl|mmhg|ml|ml/hr|mls)?",
            r"(urine\s*output|output)\s+(?:is|was|hai|aaya)?\s*(\d+\.?\d*)\s*(?:ml|mls|milliliters)?",
            r"(spo2|o2\s*saturation|oxygen)\s+(?:is|was|hai)?\s*(\d+\.?\d*)\s*(?:percent|%)?",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, sentence, re.IGNORECASE):
                raw_key = match.group(1).strip()
                value_str = match.group(2).strip()
                canonical = LAB_ALIASES.get(raw_key.lower(), raw_key.lower())
                try:
                    value = float(value_str)
                except ValueError:
                    value = value_str
                claims.append(HandoffClaim(
                    category="lab", claim=sentence.strip(),
                    key=canonical, value=value, raw=match.group(0),
                ))
        return claims

    def _extract_med_claims(self, sentence: str) -> list[HandoffClaim]:
        claims: list[HandoffClaim] = []
        words = sentence.split()
        for i, word in enumerate(words):
            clean = re.sub(r"[^a-z]", "", word.lower())
            canonical = MED_ALIASES.get(clean)
            if canonical:
                context = " ".join(words[max(0, i - 3):min(len(words), i + 4)]).lower()
                status = "active"
                if any(sig in context for sig in [
                    "discontinue", "stopped", "stop", "band", "rok", "hata",
                    "remove", "nikaal", "nahi de", "not given", "skip",
                ]):
                    status = "discontinued"
                claims.append(HandoffClaim(
                    category="medication", claim=sentence.strip(),
                    key=canonical, value=status, raw=word,
                ))
        return claims

    def _extract_condition_claims(self, sentence: str) -> list[HandoffClaim]:
        claims: list[HandoffClaim] = []
        conditions = [
            (r"(?:no|nahi|nhi)\s+(?:urine|peeing|output|passing)", "urine_output", "none"),
            (r"(?:urinating|passing\s+urine|urine\s+(?:aa|coming|Output))", "urine_output", "present"),
            (r"(?:pain|dard|taklif)\s+(?:is|hai|controlled|theek|accha|better)", "pain_controlled", True),
            (r"(?:pain|dard|taklif)\s+(?:bad|worse|severe|zyada|bahut)", "pain_controlled", False),
            (r"(?:fever|bukhar|temperature|temp)\s+(?:hai|is|present|high)", "fever", True),
            (r"(?:post[-\s]*op|surgery|operation)\s+day\s+(\d+)", "post_op_day", None),
            (r"(?:conscious|alert|oriented|hosh)\s*(?:and|&|)\s*(?:oriented|cooperative)", "consciousness", "alert"),
            (r"(?:drowsy|sleepy|unconscious|bechosh|behosh)", "consciousness", "impaired"),
            (r"(?:breathing|saans|respiratory)\s+(?:difficulty|problem|distress|hard|taklif|fast)", "respiratory", "distressed"),
            (r"(?:breathing|saans)\s+(?:okay|fine|normal|accha|theek)", "respiratory", "normal"),
        ]
        for pattern, key, value in conditions:
            match = re.search(pattern, sentence, re.IGNORECASE)
            if match:
                claims.append(HandoffClaim(
                    category="condition", claim=sentence.strip(),
                    key=key, value=value, raw=match.group(0),
                ))
        return claims

    def _extract_plan_claims(self, sentence: str) -> list[HandoffClaim]:
        claims: list[HandoffClaim] = []
        for pattern, category in [
            (r"(?:pending|abhi|baaki|bakaya)\s+(.+?)(?:\.|$)", "pending"),
            (r"(?:need|needs|required|zaroorat|karna\s+hai)\s+(.+?)(?:\.|$)", "plan"),
            (r"(?:planned|plan)\s+(.+?)(?:\.|$)", "plan"),
            (r"(?:waiting\s+for|intezar|wait)\s+(.+?)(?:\.|$)", "pending"),
        ]:
            match = re.search(pattern, sentence, re.IGNORECASE)
            if match:
                claims.append(HandoffClaim(
                    category="plan", claim=sentence.strip(),
                    key=category, value=match.group(1).strip(), raw=match.group(0),
                ))
        return claims

    # ── Comparison ──────────────────────────────────────────────────

    def _compare_claim(self, claim: HandoffClaim, patient: dict[str, Any], report: HandoffReport) -> None:
        if claim.category == "lab":
            self._compare_lab(claim, patient, report)
        elif claim.category == "medication":
            self._compare_medication(claim, patient, report)
        elif claim.category == "condition":
            self._compare_condition(claim, patient, report)
        elif claim.category == "plan":
            self._compare_plan(claim, patient, report)

    def _compare_lab(self, claim: HandoffClaim, patient: dict[str, Any], report: HandoffReport) -> None:
        labs = patient.get("labs", [])
        key = claim.key
        stated_value = claim.value

        matching = [lab for lab in labs if lab.get("test", "").lower() == key or key in lab.get("test", "").lower()]
        if not matching:
            report.warnings.append(SafetyFlag(
                severity="warning", category="lab",
                message=f"Mentioned {key} but no lab result found in records", claim=claim.claim,
            ))
            return

        latest = matching[-1]
        recorded_value = latest.get("value")
        try:
            recorded_num = float(recorded_value)
            stated_num = float(stated_value) if stated_value is not None else None
        except (TypeError, ValueError):
            report.correct.append(SafetyFlag(
                severity="correct", category="lab",
                message=f"{key}: mentioned (value: {stated_value})", claim=claim.claim,
            ))
            return

        if stated_num is not None and abs(stated_num - recorded_num) < 0.01:
            report.correct.append(SafetyFlag(
                severity="correct", category="lab",
                message=f"{key} {stated_num} — matches lab report", claim=claim.claim,
            ))
            thresholds = CRITICAL_THRESHOLDS.get(key, {})
            if stated_num >= thresholds.get("critical_high", float("inf")):
                report.critical.append(SafetyFlag(
                    severity="critical", category="lab",
                    message=f"{key} {stated_num} is in CRITICAL range",
                    detail=self._lab_advice(key, stated_num, "high"), claim=claim.claim,
                ))
            elif stated_num >= thresholds.get("high", float("inf")):
                report.warnings.append(SafetyFlag(
                    severity="warning", category="lab",
                    message=f"{key} {stated_num} is elevated",
                    detail=self._lab_advice(key, stated_num, "high"), claim=claim.claim,
                ))
            elif stated_num <= thresholds.get("low", 0):
                report.warnings.append(SafetyFlag(
                    severity="warning", category="lab",
                    message=f"{key} {stated_num} is low",
                    detail=self._lab_advice(key, stated_num, "low"), claim=claim.claim,
                ))
        elif stated_num is not None:
            report.wrong.append(SafetyFlag(
                severity="wrong", category="lab",
                message=f"{key}: said {stated_num} but records show {recorded_num}",
                detail=f"Latest lab ({latest.get('timestamp', 'unknown')}): {recorded_num}", claim=claim.claim,
            ))

    def _compare_medication(self, claim: HandoffClaim, patient: dict[str, Any], report: HandoffReport) -> None:
        medications = patient.get("medications", [])
        key = claim.key
        stated_status = claim.value

        matching = [med for med in medications if med.get("name", "").lower() == key or key in med.get("name", "").lower()]
        if not matching:
            report.warnings.append(SafetyFlag(
                severity="warning", category="medication",
                message=f"Mentioned {key} but not found in medication list", claim=claim.claim,
            ))
            return

        med = matching[0]
        actual_status = med.get("status", "active").lower()

        if stated_status == actual_status:
            report.correct.append(SafetyFlag(
                severity="correct", category="medication",
                message=f"{key} — {actual_status} (matches records)", claim=claim.claim,
            ))
        elif stated_status == "active" and actual_status == "discontinued":
            report.wrong.append(SafetyFlag(
                severity="wrong", category="medication",
                message=f"Said {key} is active but records show DISCONTINUED",
                detail="Discontinued per medication record.", claim=claim.claim,
            ))
        elif stated_status == "discontinued" and actual_status == "active":
            report.wrong.append(SafetyFlag(
                severity="wrong", category="medication",
                message=f"Said {key} was stopped but records show it is ACTIVE",
                detail=f"Patient is currently receiving {key}.", claim=claim.claim,
            ))

    def _compare_condition(self, claim: HandoffClaim, patient: dict[str, Any], report: HandoffReport) -> None:
        key = claim.key
        value = claim.value

        if key == "urine_output":
            label = "No urine output" if value == "none" else "Urine output present"
            report.correct.append(SafetyFlag(
                severity="correct", category="condition", message=label, claim=claim.claim,
            ))
        elif key == "pain_controlled":
            label = "Pain controlled" if value else "Pain uncontrolled"
            sev = "correct" if value else "warning"
            report.correct.append(SafetyFlag(severity=sev, category="condition", message=label, claim=claim.claim))
        elif key == "consciousness":
            report.correct.append(SafetyFlag(
                severity="correct", category="condition", message=f"Consciousness: {value}", claim=claim.claim,
            ))
        elif key == "respiratory":
            if value == "distressed":
                report.warnings.append(SafetyFlag(
                    severity="warning", category="condition",
                    message="Respiratory distress mentioned", claim=claim.claim,
                ))
            else:
                report.correct.append(SafetyFlag(
                    severity="correct", category="condition",
                    message="Respiratory status normal", claim=claim.claim,
                ))

    def _compare_plan(self, claim: HandoffClaim, patient: dict[str, Any], report: HandoffReport) -> None:
        pending = patient.get("pending", [])
        plan_text = str(claim.value).lower()
        found = any(
            any(word in str(item.get("description", "") or item.get("name", "")).lower() for word in plan_text.split() if len(word) > 3)
            for item in pending
        )
        if found:
            report.correct.append(SafetyFlag(
                severity="correct", category="plan",
                message=f"Plan mentioned: {claim.value}", claim=claim.claim,
            ))
        else:
            report.warnings.append(SafetyFlag(
                severity="warning", category="plan",
                message=f"Plan mentioned: {claim.value} — not found in pending orders", claim=claim.claim,
            ))

    # ── Omissions ───────────────────────────────────────────────────

    def _check_omissions(self, claims: list[HandoffClaim], patient: dict[str, Any], report: HandoffReport) -> None:
        mentioned_keys = {c.key for c in claims}

        for lab in patient.get("labs", []):
            test_name = lab.get("test", "").lower().replace(" ", "_")
            try:
                value = float(lab.get("value", 0))
            except (TypeError, ValueError):
                continue
            thresholds = CRITICAL_THRESHOLDS.get(test_name, {})
            is_critical = value >= thresholds.get("critical_high", float("inf")) or value <= thresholds.get("critical_low", 0)
            if is_critical and test_name not in mentioned_keys:
                report.missed.append(SafetyFlag(
                    severity="missed", category="omission",
                    message=f"CRITICAL lab {test_name} = {value} was NOT mentioned",
                    detail=f"Latest value: {value}. Requires immediate attention.",
                ))

        critical_meds = [
            med for med in patient.get("medications", [])
            if med.get("status") == "active"
            and med.get("name", "").lower() in ("insulin", "heparin", "warfarin", "enoxaparin", "morphine", "fentanyl", "antibiotic")
        ]
        for med in critical_meds:
            med_name = med.get("name", "").lower()
            if med_name not in mentioned_keys:
                report.missed.append(SafetyFlag(
                    severity="missed", category="omission",
                    message=f"Critical medication {med_name} not mentioned",
                    detail=f"Patient is on active {med_name}.",
                ))

        for item in patient.get("pending", []):
            if item.get("priority", "").lower() in ("high", "urgent", "critical"):
                desc = str(item.get("description", "") or item.get("name", "")).lower()
                if not any(word in " ".join(mentioned_keys) for word in desc.split() if len(word) > 3):
                    report.missed.append(SafetyFlag(
                        severity="missed", category="omission",
                        message=f"Urgent pending item not mentioned: {item.get('description', item.get('name', 'unknown'))}",
                    ))

    def _lab_advice(self, lab_name: str, value: float, direction: str) -> str:
        advice = {
            ("potassium", "high"): "Risk of cardiac arrhythmia. Check ECG. Consider insulin+glucose, calcium gluconate.",
            ("potassium", "low"): "Risk of weakness and arrhythmia. Consider potassium supplementation.",
            ("sodium", "high"): "Check fluid balance. Consider D5W.",
            ("sodium", "low"): "Risk of seizures. Consider fluid restriction.",
            ("creatinine", "high"): "Acute kidney injury possible. Check urine output. Review nephrotoxic drugs.",
            ("hemoglobin", "low"): "Anemia. Check for bleeding. Consider transfusion if symptomatic.",
            ("spo2", "low"): "Hypoxia. Check oxygen delivery. Consider escalation.",
            ("inr", "high"): "Bleeding risk. Hold warfarin. Consider vitamin K if actively bleeding.",
            ("lactate", "high"): "Possible sepsis. Check vitals. Consider fluids.",
            ("troponin", "high"): "Possible myocardial injury. Serial ECGs. Cardiology consult.",
        }
        return advice.get((lab_name, direction), f"Value {value} outside normal range. Review clinical context.")
