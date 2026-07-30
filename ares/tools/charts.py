from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


def generate_chart_images(
    chart_plans: Sequence[dict],
    output_dir: Path,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, plan in enumerate(list(chart_plans), start=1):
        chart_type = str(plan.get("chart_type") or plan.get("type") or "line").strip().lower()
        title = str(plan.get("title") or plan.get("name") or f"Chart {index}").strip()
        slug = title.lower()
        slug = "".join(ch if ch.isalnum() or ch in "-_ " else "" for ch in slug)
        slug = "-".join(slug.split())[:48] or f"chart-{index}"
        chart_path = output_dir / f"{slug}.png"
        try:
            if chart_type == "pie":
                _save_pie_chart(chart_path, title, plan)
            elif chart_type == "bar":
                _save_bar_chart(chart_path, title, plan)
            else:
                _save_trend_chart(chart_path, title, plan)
            paths.append(chart_path)
        except Exception as exc:
            logger.exception("Failed to generate chart %s: %s", title, exc)
    return paths


def _save_trend_chart(path: Path, title: str, plan: dict) -> None:
    labels = _coerce_str_list(plan.get("labels") or plan.get("periods") or plan.get("x"))
    values = _coerce_num_list(plan.get("values") or plan.get("y") or plan.get("series"))
    if not labels or not values:
        raise ValueError("trend chart requires labels and values")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    ax.plot(labels[: len(values)], values[: len(labels)], marker="o")
    ax.set_title(title)
    ax.set_xlabel("Period")
    ax.set_ylabel("Value")
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _save_bar_chart(path: Path, title: str, plan: dict) -> None:
    labels = _coerce_str_list(plan.get("labels") or plan.get("categories") or plan.get("x"))
    values = _coerce_num_list(plan.get("values") or plan.get("y") or plan.get("series"))
    if not labels or not values:
        raise ValueError("bar chart requires labels and values")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    bars = ax.bar(labels[: len(values)], values[: len(labels)])
    ax.set_title(title)
    ax.set_xlabel("Category")
    ax.set_ylabel("Value")
    ax.grid(True, axis="y", linestyle="--", alpha=0.4)
    if len(bars) <= 12:
        ax.bar_label(bars, fmt="%.2g")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _save_pie_chart(path: Path, title: str, plan: dict) -> None:
    labels = _coerce_str_list(plan.get("labels") or plan.get("categories") or plan.get("slices"))
    values = _coerce_num_list(plan.get("values") or plan.get("sizes") or plan.get("series"))
    if not labels or not values:
        raise ValueError("pie chart requires labels and values")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 7), dpi=150)
    ax.pie(values[: len(labels)], labels=labels[: len(values)], autopct="%1.1f%%", startangle=140)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _coerce_str_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(key) for key in value.keys() if str(key).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _coerce_num_list(value: object) -> list[float]:
    if isinstance(value, (list, tuple)):
        result: list[float] = []
        for item in value:
            try:
                result.append(float(item))
            except (TypeError, ValueError):
                continue
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            try:
                result.append(float(item))
            except (TypeError, ValueError):
                continue
        return result
    try:
        return [float(value)]
    except (TypeError, ValueError):
        return []
