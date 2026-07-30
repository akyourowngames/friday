from pathlib import Path
import pytest

from ares.tools.healthcare_reporting import validate_report_output, build_report_bundle


def test_validate_report_output_requires_sections(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text("# Title", encoding="utf-8")

    with pytest.raises(ValueError):
        validate_report_output(report, [])


def test_validate_report_output_requires_sources_section(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Title\n\n## Summary\n\nBody.\n\n## Key Findings\n\n- item",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        validate_report_output(report, [])


def test_validate_report_output_accepts_charts_and_sources(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Title\n\n## Summary\n\nBody.\n\n## Key Findings\n\n- item\n\n## Sources\n\n- [Source](http://example.com)",
        encoding="utf-8",
    )
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"fake-image")

    result = validate_report_output(report, [chart])

    assert result is True


def test_build_report_bundle_creates_output_path(tmp_path: Path):
    charts = []
    path = build_report_bundle(
        query="test query",
        summary_markdown="# Title\n\n## Summary\n\nBody.\n\n## Key Findings\n\n- item\n\n## Sources\n\n- [Source](http://example.com)",
        chart_paths=charts,
        output_dir=tmp_path,
    )

    assert path.exists()
    assert path.suffix == ".md"
