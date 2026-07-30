"""Patient data store for the handoff safety checker.

Each patient is a JSON file in ~/.ares/data/healthcare/patients/.
The store reads, writes, and queries patient records.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default patient data directory
DEFAULT_PATIENT_DIR = Path("~/.ares/data/healthcare/patients").expanduser()


class PatientStore:
    """Manages patient JSON files on disk."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._dir = Path(data_dir).expanduser() if data_dir else DEFAULT_PATIENT_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── Read ────────────────────────────────────────────────────────

    def get_patient(self, patient_id: str) -> dict[str, Any] | None:
        """Load a patient file by ID (bed number or filename)."""
        path = self._resolve(patient_id)
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load patient %s: %s", patient_id, exc)
            return None

    def list_patients(self) -> list[dict[str, Any]]:
        """Return all patient records."""
        patients = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["_file"] = path.name
                patients.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return patients

    def search_by_name(self, name: str) -> dict[str, Any] | None:
        """Find a patient by partial name match (case-insensitive)."""
        name_lower = name.lower().strip()
        for patient in self.list_patients():
            full_name = str(patient.get("name", "")).lower()
            if name_lower in full_name or name_full_match(name_lower, full_name):
                return patient
        return None

    def search_by_bed(self, bed: str) -> dict[str, Any] | None:
        """Find a patient by bed number."""
        bed_str = str(bed).strip().lower()
        for patient in self.list_patients():
            if str(patient.get("bed", "")).strip().lower() == bed_str:
                return patient
        return None

    def find_patient(self, identifier: str) -> dict[str, Any] | None:
        """Smart search: try bed first, then name."""
        # Try bed number (e.g., "bed 4", "4", "B-12")
        bed_match = re.search(r"(?:bed\s*)?([A-Za-z0-9\-]+)", identifier, re.IGNORECASE)
        if bed_match:
            bed_candidate = bed_match.group(1)
            patient = self.search_by_bed(bed_candidate)
            if patient:
                return patient
            # Also try the full identifier as bed
            patient = self.search_by_bed(identifier)
            if patient:
                return patient
        # Try name
        return self.search_by_name(identifier)

    # ── Write ───────────────────────────────────────────────────────

    def save_patient(self, patient_id: str, data: dict[str, Any]) -> Path:
        """Save or update a patient record."""
        safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", patient_id)
        path = self._dir / f"{safe_id}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def add_lab_result(
        self,
        patient_id: str,
        test_name: str,
        value: str,
        unit: str = "",
        timestamp: str | None = None,
        status: str = "normal",
    ) -> bool:
        """Append a lab result to a patient record."""
        patient = self.get_patient(patient_id)
        if patient is None:
            return False
        labs = patient.setdefault("labs", [])
        labs.append({
            "test": test_name,
            "value": value,
            "unit": unit,
            "timestamp": timestamp or datetime.now().isoformat(),
            "status": status,
        })
        self.save_patient(patient_id, patient)
        return True

    def add_medication(
        self,
        patient_id: str,
        name: str,
        dose: str = "",
        route: str = "",
        frequency: str = "",
        status: str = "active",
        notes: str = "",
    ) -> bool:
        """Add or update a medication for a patient."""
        patient = self.get_patient(patient_id)
        if patient is None:
            return False
        meds = patient.setdefault("medications", [])
        # Check if medication already exists, update it
        for med in meds:
            if med.get("name", "").lower() == name.lower():
                med.update({
                    "dose": dose or med.get("dose", ""),
                    "route": route or med.get("route", ""),
                    "frequency": frequency or med.get("frequency", ""),
                    "status": status,
                    "notes": notes or med.get("notes", ""),
                })
                self.save_patient(patient_id, patient)
                return True
        # New medication
        meds.append({
            "name": name,
            "dose": dose,
            "route": route,
            "frequency": frequency,
            "status": status,
            "notes": notes,
        })
        self.save_patient(patient_id, patient)
        return True

    def add_note(
        self,
        patient_id: str,
        note_type: str,
        content: str,
        author: str = "",
    ) -> bool:
        """Add a clinical note (nursing, doctor, progress)."""
        patient = self.get_patient(patient_id)
        if patient is None:
            return False
        notes = patient.setdefault("notes", [])
        notes.append({
            "type": note_type,
            "content": content,
            "author": author,
            "timestamp": datetime.now().isoformat(),
        })
        self.save_patient(patient_id, patient)
        return True

    # ── Internal ────────────────────────────────────────────────────

    def _resolve(self, patient_id: str) -> Path | None:
        """Find the JSON file for a patient ID."""
        safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", patient_id)
        path = self._dir / f"{safe_id}.json"
        if path.is_file():
            return path
        # Try exact name
        path = self._dir / f"{patient_id}.json"
        if path.is_file():
            return path
        return None


def name_full_match(query: str, full_name: str) -> bool:
    """Check if query matches full name (supports 'sharma' matching 'rajesh sharma')."""
    parts = full_name.split()
    return query in parts
