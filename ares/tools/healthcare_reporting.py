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


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "report"


def build_report_bundle(
    query: str,
    summary_markdown: str,
    chart_paths: Iterable[Path],
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(query)
    report_path = output_dir / f"{Path(__file__).stem}-{slug}.md"
    chart_paths = list(chart_paths)
    if chart_paths:
        sections = re.split(r"(?i)(^|\n)(## Sources)\n?", summary_markdown, maxsplit=1)
        if len(sections) >= 3:
            summary_markdown = sections[0].rstrip("\n") + "\n\n## Charts\n\n"
            for index, raw_path in enumerate(list(chart_paths), start=1):
                path = Path(raw_path)
                summary_markdown += (
                    f"### Chart {index}\n![Chart {index}]({path.as_posix()})\n\n"
                )
            summary_markdown += f"{sections[1]}{sections[2]}\n".lstrip("\n")
        else:
            summary_markdown = summary_markdown.rstrip("\n") + "\n\n## Charts\n\n"
            for index, raw_path in enumerate(list(chart_paths), start=1):
                path = Path(raw_path)
                summary_markdown += (
                    f"### Chart {index}\n![Chart {index}]({path.as_posix()})\n\n"
                )
    report_path.write_text(summary_markdown, encoding="utf-8")
    return report_path


def ensure_report_directory(query: str, output_dir: Path | None = None) -> Path:
    base = Path(output_dir or "~/.ares/data/healthcare-reports").expanduser()
    slug = _slugify(query) or "report"
    target = base / f"{Path(__file__).stem}-{slug}"
    target.mkdir(parents=True, exist_ok=True)
    return target
