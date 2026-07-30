from pathlib import Path
import pytest

from ares.tools.executor import ToolExecutor
from ares.tools.healthcare_reporting import build_report_bundle, ensure_report_directory, validate_report_output
from ares.memory import MemoryStore


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


def test_generate_chart_creates_pngs_and_embeds_them(tmp_path: Path):
    memory_store = MemoryStore(Path(":memory:"))
    executor = ToolExecutor(memory_store=memory_store)
    report_dir = ensure_report_directory("test query", tmp_path)

    chart_paths = [
        str(report_dir / "india-diabetes-trend.png"),
        str(report_dir / "urban-rural-diabetes.png"),
        str(report_dir / "statewise-diabetes.png"),
    ]
    for path in chart_paths:
        result = executor._generate_chart({
            "chart_type": "line",
            "title": Path(path).stem,
            "labels": ["2015", "2017", "2019", "2021", "2024"],
            "values": [69.1, 72.3, 74.9, 77.0, 89.8],
            "output": path,
            "response_format": "structured",
        })
        assert "saved to" in result
        assert Path(path).exists()

    summary = "# Title\n\n## Summary\n\nBody.\n\n## Key Findings\n\n- item\n\n## Sources\n\n- [Source](http://example.com)"
    report_path = build_report_bundle(
        query="test query",
        summary_markdown=summary,
        chart_paths=[Path(path) for path in chart_paths],
        output_dir=tmp_path,
    )
    text = report_path.read_text(encoding="utf-8")
    assert "## Charts" in text
    for path in chart_paths:
        assert Path(path).name in text
        assert Path(path).exists()
