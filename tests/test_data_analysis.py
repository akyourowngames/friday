"""Tests for the data analysis tool — CSV/text analysis and chart generation."""

from pathlib import Path

import pytest

from ares.tools.data_analysis import analyze_csv, analyze_text


SAMPLE_CSV = """\
patient_id,name,age,blood_pressure_systolic,temperature,status
P001,John Smith,45,120,98.6,normal
P002,Jane Doe,67,180,101.2,critical
P003,Bob Wilson,34,115,98.4,normal
P004,Alice Brown,82,195,103.0,urgent
P005,Charlie Davis,55,130,99.1,normal
P006,Diana Evans,71,175,100.8,critical
P007,Eve Foster,29,110,98.2,normal
P008,Frank Green,63,145,99.5,normal
P009,Grace Hill,78,185,102.1,urgent
P010,Henry Irving,41,125,98.7,normal
"""


def test_analyze_csv_basic(tmp_path: Path):
    """Basic CSV analysis returns summary keys and insights."""
    csv_file = tmp_path / "patients.csv"
    csv_file.write_text(SAMPLE_CSV, encoding="utf-8")

    result = analyze_csv(csv_file, generate_charts=False)

    assert result["ok"] is True
    assert result["summary"]["rows"] == 10
    assert result["summary"]["columns"] == 6
    assert "insights" in result
    assert len(result["insights"]) > 0
    assert "numeric_summary" in result["summary"]
    assert "patient_id" in result["data_info"]["categorical_columns"]


def test_analyze_csv_urgent_focus(tmp_path: Path):
    """Urgent focus detects patients with critical values."""
    csv_file = tmp_path / "patients.csv"
    csv_file.write_text(SAMPLE_CSV, encoding="utf-8")

    result = analyze_csv(csv_file, focus="urgent", generate_charts=False)

    assert result["ok"] is True
    assert len(result["urgent_cases"]) > 0
    # Should find rows with high blood pressure or temperature
    urgent_ids = {case.get("patient_id") for case in result["urgent_cases"]}
    # P002 (BP 180, temp 101.2), P004 (BP 195, temp 103), P009 (BP 185, temp 102.1)
    assert len(urgent_ids) >= 2


def test_analyze_csv_patients_focus(tmp_path: Path):
    """Patients focus returns urgent cases with full row info."""
    csv_file = tmp_path / "patients.csv"
    csv_file.write_text(SAMPLE_CSV, encoding="utf-8")

    result = analyze_csv(csv_file, focus="patients", generate_charts=False)

    assert result["ok"] is True
    for case in result["urgent_cases"]:
        assert "patient_id" in case
        assert "blood_pressure_systolic" in case
        assert "temperature" in case


def test_analyze_csv_generates_charts(tmp_path: Path):
    """Analysis generates chart PNG files."""
    csv_file = tmp_path / "patients.csv"
    csv_file.write_text(SAMPLE_CSV, encoding="utf-8")
    chart_dir = tmp_path / "charts"

    result = analyze_csv(csv_file, focus="urgent", chart_output_dir=chart_dir)

    assert result["ok"] is True
    assert len(result["chart_paths"]) > 0
    for path_str in result["chart_paths"]:
        assert Path(path_str).exists()
        assert path_str.endswith(".png")


def test_analyze_csv_missing_values(tmp_path: Path):
    """CSV with missing values reports them correctly."""
    csv_with_nan = "id,value\n1,10\n2,\n3,30\n4,\n5,50\n"
    csv_file = tmp_path / "nan.csv"
    csv_file.write_text(csv_with_nan, encoding="utf-8")

    result = analyze_csv(csv_file, generate_charts=False)

    assert result["ok"] is True
    assert result["summary"]["missing_total"] == 2
    assert "value" in result["summary"]["missing_columns"]


def test_analyze_csv_outliers(tmp_path: Path):
    """IQR outlier detection finds extreme values."""
    # Create CSV with a clear outlier
    csv_data = "x\n1\n2\n3\n4\n5\n6\n7\n8\n9\n100\n"
    csv_file = tmp_path / "outliers.csv"
    csv_file.write_text(csv_data, encoding="utf-8")

    result = analyze_csv(csv_file, generate_charts=False)

    assert result["ok"] is True
    assert len(result["outliers"]) > 0
    assert result["outliers"][0]["column"] == "x"
    assert result["outliers"][0]["count"] >= 1


def test_analyze_text_basic(tmp_path: Path):
    """Basic text analysis returns word frequency and line stats."""
    text_file = tmp_path / "sample.txt"
    text_file.write_text("hello world\nhello python\nhello world\n", encoding="utf-8")

    result = analyze_text(text_file)

    assert result["ok"] is True
    assert result["summary"]["lines"] == 3
    assert result["summary"]["words"] == 6
    assert len(result["top_words"]) > 0
    assert result["top_words"][0]["word"] == "hello"
    assert result["top_words"][0]["count"] == 3


def test_analyze_csv_file_not_found():
    """Returns error for missing file."""
    result = analyze_csv("/nonexistent/file.csv")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_analyze_text_file_not_found():
    """Returns error for missing text file."""
    result = analyze_text("/nonexistent/file.txt")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_analyze_csv_correlations(tmp_path: Path):
    """Correlation focus finds relationships between numeric columns."""
    csv_data = "a,b\n1,2\n2,4\n3,6\n4,8\n5,10\n6,12\n7,14\n8,16\n"
    csv_file = tmp_path / "corr.csv"
    csv_file.write_text(csv_data, encoding="utf-8")

    result = analyze_csv(csv_file, focus="correlations", generate_charts=False)

    assert result["ok"] is True
    # Should find strong positive correlation between a and b
    corr_insights = [i for i in result["insights"] if "correlation" in i.lower()]
    assert len(corr_insights) > 0
