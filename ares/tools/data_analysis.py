"""Data analysis engine — CSV, TSV, and text file analysis with chart generation."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import pandas as pd
    _PANDAS_AVAILABLE = True
except Exception:
    pd = None  # type: ignore[assignment]
    _PANDAS_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _MATPLOTLIB_AVAILABLE = True
except Exception:
    plt = None  # type: ignore[assignment]
    _MATPLOTLIB_AVAILABLE = False


_URGENT_KEYWORDS = frozenset({
    "critical", "urgent", "emergency", "severe", "acute", "danger",
    "life-threatening", "immediate", "stabilize", "deteriorating",
    "code red", "code blue", "icu", "triage",
})


def analyze_csv(
    file_path: str | Path,
    *,
    focus: str = "summary",
    chart_output_dir: str | Path | None = None,
    generate_charts: bool = True,
) -> dict[str, Any]:
    """Read a CSV file and perform analysis with optional chart generation.

    Parameters
    ----------
    file_path : path to a CSV or TSV file.
    focus : one of "summary", "urgent", "patients", "trends", "correlations".
    chart_output_dir : directory to write chart PNGs; defaults to ~/.ares/data/analysis/.
    generate_charts : whether to produce matplotlib charts.

    Returns
    -------
    dict with keys: summary, insights, chart_paths, urgent_cases, outliers, data_info.
    """
    if not _PANDAS_AVAILABLE:
        return {"ok": False, "error": "pandas is not installed"}

    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}

    # --- Read CSV -----------------------------------------------------------
    try:
        df = _read_csv_auto(path)
    except Exception as exc:
        return {"ok": False, "error": f"failed to read CSV: {exc}"}

    if df.empty:
        return {"ok": False, "error": "CSV file is empty"}

    # --- Basic info ---------------------------------------------------------
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    categorical_cols = df.select_dtypes(include=["object", "category", "string"]).columns.tolist()
    missing = {col: int(df[col].isna().sum()) for col in df.columns if df[col].isna().any()}

    data_info = {
        "rows": len(df),
        "columns": len(df.columns),
        "column_names": df.columns.tolist(),
        "numeric_columns": numeric_cols,
        "categorical_columns": categorical_cols,
        "missing_values": missing,
    }

    # --- Numeric summary ----------------------------------------------------
    numeric_summary: dict[str, Any] = {}
    for col in numeric_cols:
        series = df[col].dropna()
        if series.empty:
            continue
        numeric_summary[col] = {
            "mean": round(float(series.mean()), 4),
            "median": round(float(series.median()), 4),
            "std": round(float(series.std()), 4),
            "min": round(float(series.min()), 4),
            "max": round(float(series.max()), 4),
            "q25": round(float(series.quantile(0.25)), 4),
            "q75": round(float(series.quantile(0.75)), 4),
            "count": int(series.count()),
        }

    # --- Outlier detection (IQR) -------------------------------------------
    outliers = _detect_outliers_iqr(df, numeric_cols)

    # --- Urgent cases -------------------------------------------------------
    urgent_cases = []
    if focus in ("urgent", "patients"):
        urgent_cases = _find_urgent_cases(df, numeric_cols, categorical_cols)

    # --- Insights -----------------------------------------------------------
    insights = _build_insights(df, numeric_cols, categorical_cols, numeric_summary, focus)

    # --- Charts -------------------------------------------------------------
    chart_paths: list[str] = []
    if generate_charts and _MATPLOTLIB_AVAILABLE:
        output_dir = Path(chart_output_dir or "~/.ares/data/analysis").expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        chart_paths = _generate_charts(df, numeric_cols, categorical_cols, output_dir, focus)

    return {
        "ok": True,
        "summary": {
            "rows": len(df),
            "columns": len(df.columns),
            "numeric_summary": numeric_summary,
            "missing_columns": list(missing.keys()),
            "missing_total": sum(missing.values()),
        },
        "insights": insights,
        "chart_paths": chart_paths,
        "urgent_cases": urgent_cases,
        "outliers": outliers,
        "data_info": data_info,
    }


def analyze_text(file_path: str | Path) -> dict[str, Any]:
    """Basic text analysis — word frequency, line stats, character counts."""
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "error": f"failed to read file: {exc}"}

    lines = text.splitlines()
    words = re.findall(r"\b\w+\b", text.lower())

    word_freq = Counter(words).most_common(25)
    avg_line_length = sum(len(line) for line in lines) / max(len(lines), 1)
    avg_word_length = sum(len(w) for w in words) / max(len(words), 1)

    insights = [
        f"File has {len(lines)} lines, {len(words)} words, {len(text)} characters.",
        f"Average line length: {avg_line_length:.1f} characters.",
        f"Average word length: {avg_word_length:.1f} characters.",
    ]
    if word_freq:
        top = word_freq[0]
        insights.append(f"Most frequent word: '{top[0]}' ({top[1]} occurrences).")

    return {
        "ok": True,
        "summary": {
            "lines": len(lines),
            "words": len(words),
            "characters": len(text),
            "avg_line_length": round(avg_line_length, 1),
            "avg_word_length": round(avg_word_length, 1),
        },
        "top_words": [{"word": w, "count": c} for w, c in word_freq],
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_csv_auto(path: Path) -> pd.DataFrame:
    """Try reading a CSV with auto-detection of separator and encoding."""
    for encoding in ("utf-8", "latin-1", "cp1252"):
        for sep in (",", "\t", ";"):
            try:
                return pd.read_csv(path, sep=sep, encoding=encoding)
            except Exception:
                continue
    return pd.read_csv(path)


def _detect_outliers_iqr(df: pd.DataFrame, numeric_cols: list[str]) -> list[dict[str, Any]]:
    """Detect outliers using the IQR method (1.5x rule)."""
    outliers: list[dict[str, Any]] = []
    for col in numeric_cols:
        series = df[col].dropna()
        if series.count() < 4:
            continue
        q25 = float(series.quantile(0.25))
        q75 = float(series.quantile(0.75))
        iqr = q75 - q25
        if iqr == 0:
            continue
        lower = q25 - 1.5 * iqr
        upper = q75 + 1.5 * iqr
        mask = (series < lower) | (series > upper)
        outlier_indices = series[mask].index.tolist()
        if outlier_indices:
            outliers.append({
                "column": col,
                "count": len(outlier_indices),
                "lower_bound": round(lower, 4),
                "upper_bound": round(upper, 4),
                "sample_indices": outlier_indices[:10],
            })
    return outliers


def _find_urgent_cases(
    df: pd.DataFrame,
    numeric_cols: list[str],
    categorical_cols: list[str],
) -> list[dict[str, Any]]:
    """Identify rows that look urgent based on value heuristics."""
    urgent_indices: set[int] = set()

    # Numeric: values > 2 standard deviations above the mean
    for col in numeric_cols:
        series = df[col].dropna()
        if series.count() < 2:
            continue
        mean = series.mean()
        std = series.std()
        if std == 0:
            continue
        threshold = mean + 2 * std
        mask = series > threshold
        urgent_indices.update(series[mask].index.tolist())

    # Categorical: cells containing urgent keywords
    for col in categorical_cols:
        series = df[col].dropna().astype(str).str.lower()
        mask = series.apply(lambda val: any(kw in val for kw in _URGENT_KEYWORDS))
        urgent_indices.update(series[mask].index.tolist())

    if not urgent_indices:
        return []

    results = []
    for idx in sorted(urgent_indices):
        row = df.loc[idx]
        row_dict: dict[str, Any] = {"row_index": int(idx)}
        for col in df.columns:
            val = row[col]
            if pd.isna(val):
                row_dict[col] = None
            elif hasattr(val, "item"):
                row_dict[col] = val.item()
            else:
                row_dict[col] = str(val)
        results.append(row_dict)

    return results


def _build_insights(
    df: pd.DataFrame,
    numeric_cols: list[str],
    categorical_cols: list[str],
    numeric_summary: dict[str, Any],
    focus: str,
) -> list[str]:
    """Generate human-readable insights from the data."""
    insights: list[str] = []

    insights.append(f"Dataset has {len(df)} rows and {len(df.columns)} columns.")
    if numeric_cols:
        insights.append(f"Numeric columns ({len(numeric_cols)}): {', '.join(numeric_cols)}.")
    if categorical_cols:
        insights.append(f"Categorical columns ({len(categorical_cols)}): {', '.join(categorical_cols)}.")

    missing_total = int(df.isna().sum().sum())
    if missing_total:
        insights.append(f"Total missing values: {missing_total} across {len(df.columns[df.isna().any()])} columns.")

    # Focus-specific insights
    if focus in ("urgent", "patients"):
        urgent_count = 0
        for col in numeric_cols:
            s = df[col].dropna()
            if s.count() < 2 or s.std() == 0:
                continue
            threshold = s.mean() + 2 * s.std()
            urgent_count += int((s > threshold).sum())
        if urgent_count:
            insights.append(f"Found {urgent_count} data point(s) above critical threshold (mean + 2*std).")

    if focus == "correlations" and len(numeric_cols) >= 2:
        corr = df[numeric_cols].corr()
        strong = []
        for i, c1 in enumerate(numeric_cols):
            for c2 in numeric_cols[i + 1:]:
                r = float(corr.loc[c1, c2])
                if abs(r) >= 0.7:
                    strong.append((c1, c2, round(r, 3)))
        for c1, c2, r in strong:
            direction = "positive" if r > 0 else "negative"
            insights.append(f"Strong {direction} correlation ({r}) between '{c1}' and '{c2}'.")

    if focus == "trends" and numeric_cols:
        for col in numeric_cols[:3]:
            s = df[col].dropna()
            if len(s) < 3:
                continue
            first_half = s.iloc[: len(s) // 2].mean()
            second_half = s.iloc[len(s) // 2 :].mean()
            if second_half > first_half * 1.1:
                insights.append(f"'{col}' shows an upward trend ({first_half:.2f} -> {second_half:.2f}).")
            elif second_half < first_half * 0.9:
                insights.append(f"'{col}' shows a downward trend ({first_half:.2f} -> {second_half:.2f}).")

    return insights


def _generate_charts(
    df: pd.DataFrame,
    numeric_cols: list[str],
    categorical_cols: list[str],
    output_dir: Path,
    focus: str,
) -> list[str]:
    """Generate matplotlib charts and return their file paths."""
    chart_paths: list[str] = []
    uid = uuid.uuid4().hex[:8]

    # 1. Numeric distributions
    for col in numeric_cols[:6]:
        series = df[col].dropna()
        if series.empty:
            continue
        fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
        ax.hist(series, bins=min(30, max(5, len(series) // 3)), edgecolor="white", alpha=0.8)
        ax.set_title(f"Distribution: {col}")
        ax.set_xlabel(col)
        ax.set_ylabel("Frequency")
        ax.grid(True, axis="y", linestyle="--", alpha=0.4)
        fig.tight_layout()
        path = output_dir / f"dist-{col}-{uid}.png"
        fig.savefig(path)
        plt.close(fig)
        chart_paths.append(str(path))

    # 2. Correlation heatmap (2+ numeric columns)
    if len(numeric_cols) >= 2 and focus in ("correlations", "summary", "trends"):
        corr = df[numeric_cols].corr()
        fig, ax = plt.subplots(figsize=(max(6, len(numeric_cols)), max(5, len(numeric_cols) * 0.8)), dpi=150)
        cax = ax.matshow(corr, cmap="RdBu_r", vmin=-1, vmax=1)
        fig.colorbar(cax)
        ax.set_xticks(range(len(numeric_cols)))
        ax.set_yticks(range(len(numeric_cols)))
        ax.set_xticklabels(numeric_cols, rotation=45, ha="left")
        ax.set_yticklabels(numeric_cols)
        ax.set_title("Correlation Matrix")
        for i in range(len(numeric_cols)):
            for j in range(len(numeric_cols)):
                ax.text(j, i, f"{corr.iloc[i, j]:.2f}", ha="center", va="center", fontsize=9)
        fig.tight_layout()
        path = output_dir / f"correlation-{uid}.png"
        fig.savefig(path)
        plt.close(fig)
        chart_paths.append(str(path))

    # 3. Top categorical distributions
    for col in categorical_cols[:3]:
        vc = df[col].value_counts().head(10)
        if vc.empty:
            continue
        fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
        vc.plot(kind="bar", ax=ax, edgecolor="white")
        ax.set_title(f"Top values: {col}")
        ax.set_ylabel("Count")
        ax.grid(True, axis="y", linestyle="--", alpha=0.4)
        fig.tight_layout()
        path = output_dir / f"cat-{col}-{uid}.png"
        fig.savefig(path)
        plt.close(fig)
        chart_paths.append(str(path))

    # 4. Urgent cases summary
    if focus in ("urgent", "patients"):
        urgent_count = 0
        for c in numeric_cols:
            s = df[c].dropna()
            if s.count() < 2 or s.std() == 0:
                continue
            urgent_count += int((s > s.mean() + 2 * s.std()).sum())
        if urgent_count:
            fig, ax = plt.subplots(figsize=(6, 4), dpi=150)
            ax.bar(["Normal", "Urgent"], [len(df) - urgent_count, urgent_count],
                   color=["#4CAF50", "#F44336"], edgecolor="white")
            ax.set_title("Urgent vs Normal Cases")
            ax.set_ylabel("Count")
            fig.tight_layout()
            path = output_dir / f"urgent-summary-{uid}.png"
            fig.savefig(path)
            plt.close(fig)
            chart_paths.append(str(path))

    return chart_paths
