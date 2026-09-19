from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any


def infer_machine_type(
    machine_type: str | None,
    gpu_name: str | None,
    vram_total_bytes: int | None,
) -> str:
    if machine_type:
        return str(machine_type)
    name = str(gpu_name or "").lower()
    gib = float(vram_total_bytes or 0) / 2**30
    if "5090" in name:
        return "5090_32g"
    if "4090" in name and gib >= 40:
        return "4090_48g"
    if "4090" in name:
        return "4090_24g"
    return "unmanaged"


def percentile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(
        float(value)
        for value in values
        if value is not None and math.isfinite(float(value))
    )
    if not ordered:
        return None
    # Operational latency dashboards use the nearest-rank definition: the
    # reported percentile is always an observed value and never an interpolated
    # synthetic latency.
    position = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return ordered[position]


def metric_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    data = [
        float(value)
        for value in values
        if value is not None and math.isfinite(float(value))
    ]
    return {
        "n": len(data),
        "min": round(min(data), 3) if data else None,
        "mean": round(statistics.fmean(data), 3) if data else None,
        "p50": round(percentile(data, 0.50), 3) if data else None,
        "p95": round(percentile(data, 0.95), 3) if data else None,
        "max": round(max(data), 3) if data else None,
    }


def mark_iqr_outliers(
    rows: list[dict[str, Any]],
    *,
    value_key: str,
    bucket_keys: Sequence[str],
    minimum_samples: int = 4,
) -> list[dict[str, Any]]:
    """Annotate infrastructure long tails without deleting the raw observation."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is None or not math.isfinite(float(value)):
            row["effective_outlier"] = False
            row["effective_outlier_threshold"] = None
            continue
        groups[tuple(row.get(key) for key in bucket_keys)].append(row)

    for items in groups.values():
        values = [float(item[value_key]) for item in items]
        threshold: float | None = None
        if len(values) >= minimum_samples:
            q1 = percentile(values, 0.25)
            q3 = percentile(values, 0.75)
            if q1 is not None and q3 is not None:
                threshold = q3 + 1.5 * (q3 - q1)
        for item in items:
            item["effective_outlier_threshold"] = (
                round(threshold, 3) if threshold is not None else None
            )
            item["effective_outlier"] = bool(
                threshold is not None and float(item[value_key]) > threshold
            )
    return rows
