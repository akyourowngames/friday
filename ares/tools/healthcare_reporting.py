from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List


REQUIRED_SECTIONS = ("## Summary", "## Key Findings", "## Sources")
_section_pattern = re.compile(r"^##\s+.+$", re.MULTILINE)


class ValidationError(ValueError):
    """Raised when a healthcare report bundle fails validation."""


def validate_report_output(report_path: Path, chart_paths: Iterable[Path]) -> bool:
    sections = _section_pattern.findall(report_path.read_text(encoding="utf-8"))
    normalized_sections = [section.lower() for section in sections]

    for required in REQUIRED_SECTIONS:
        if required.lower() not in normalized_sections:
            raise ValidationError(f"Missing required section in report: {required}")

    if "## sources" not in normalized_sections:
        raise ValidationError("Missing required section in report: ## Sources")

    return True


def build_report_bundle(
    query: str,
    summary_markdown: str,
    chart_paths: Iterable[Path],
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:48] or "report"
    report_path = output_dir / f"{Path(__file__).stem}-{slug}.md"
    report_path.write_text(summary_markdown, encoding="utf-8")
    return report_path
