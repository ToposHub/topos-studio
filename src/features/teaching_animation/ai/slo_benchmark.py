# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Performance SLO + benchmark contracts for Motion Graphic (MG-26).

MG-26 gives the pipeline an explicit set of performance SLOs: time-to-first-
proposal, preview first frame, preview complete, export, cancel-convergence,
peak memory / disk / CPU / GPU.  Without SLOs, "it works on my machine" is
the only signal and degradation slips in unnoticed.

This module defines the SLO table + a :class:`BenchmarkResult` recorder.  The
actual benchmark runner lives in ``scripts/`` (or CI); this module is the
typed contract the runner writes to and the dashboard reads from.
"""

from __future__ import annotations

from dataclasses import dataclass
import statistics
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# SLO definitions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PerformanceSLO:
    """One performance target."""

    slo_id: str
    label: str
    unit: str                      # seconds / bytes / ratio
    p50_target: float
    p95_target: float
    direction: str = "lower"       # lower-is-better / upper-is-better
    degradation_threshold: float = 0.25

    def to_dict(self) -> dict[str, Any]:
        return {
            "slo_id": self.slo_id,
            "label": self.label,
            "unit": self.unit,
            "p50_target": self.p50_target,
            "p95_target": self.p95_target,
            "direction": self.direction,
            "degradation_threshold": self.degradation_threshold,
        }


# Canonical SLO table for the 15-second-clip target experience.
DEFAULT_SLOS: tuple[PerformanceSLO, ...] = (
    PerformanceSLO("time_to_first_proposal", "首次方案", "seconds", 4.0, 10.0),
    PerformanceSLO("preview_first_frame", "预览首帧", "seconds", 0.5, 2.0),
    PerformanceSLO("preview_complete", "预览完成", "seconds", 6.0, 15.0),
    PerformanceSLO("export_1080p", "1080p 导出", "seconds", 8.0, 25.0),
    PerformanceSLO("export_4k", "4K 导出", "seconds", 40.0, 120.0),
    PerformanceSLO("cancel_convergence", "取消收敛", "seconds", 0.2, 1.0),
    PerformanceSLO("peak_memory", "峰值内存", "bytes", 600 * 1024 * 1024, 1.5 * 1024 * 1024 * 1024),
    PerformanceSLO("peak_disk", "峰值磁盘", "bytes", 200 * 1024 * 1024, 1 * 1024 * 1024 * 1024),
    PerformanceSLO("cpu_utilization", "CPU 占用", "ratio", 0.4, 0.85),
    PerformanceSLO("gpu_utilization", "GPU 占用", "ratio", 0.3, 0.85),
)


# --------------------------------------------------------------------------- #
# Sample + result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BenchmarkSample:
    """One measurement of one SLO at one tier."""

    slo_id: str
    value: float
    tier: str = "default"           # e.g. "1080p" / "4k" / "ai_agent_intro"
    fixture_id: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slo_id": self.slo_id,
            "value": self.value,
            "tier": self.tier,
            "fixture_id": self.fixture_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class SLOSummary:
    slo: PerformanceSLO
    p50: float | None
    p95: float | None
    sample_count: int
    meets_p50: bool
    meets_p95: bool
    degraded_vs_baseline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "slo": self.slo.to_dict(),
            "p50": self.p50,
            "p95": self.p95,
            "sample_count": self.sample_count,
            "meets_p50": self.meets_p50,
            "meets_p95": self.meets_p95,
            "degraded_vs_baseline": self.degraded_vs_baseline,
        }


@dataclass(frozen=True)
class BenchmarkReport:
    """Aggregated result of one benchmark run."""

    summaries: tuple[SLOSummary, ...] = ()
    baseline_hash: str | None = None
    notes: str = ""

    @property
    def all_meet_targets(self) -> bool:
        # AUD-27110: an empty report (sampling failure / all sample slo_ids
        # unknown / no samples) used to be vacuously True — exactly the
        # "degradation slips in unnoticed" false-green this module exists to
        # prevent.  No summaries ⇒ targets are NOT met; the gate fails closed.
        if not self.summaries:
            return False
        return all(s.meets_p50 and s.meets_p95 for s in self.summaries)

    @property
    def any_degraded(self) -> bool:
        return any(s.degraded_vs_baseline for s in self.summaries)

    def summary_for(self, slo_id: str) -> SLOSummary | None:
        return next((s for s in self.summaries if s.slo.slo_id == slo_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summaries": [s.to_dict() for s in self.summaries],
            "baseline_hash": self.baseline_hash,
            "notes": self.notes,
            "all_meet_targets": self.all_meet_targets,
            "any_degraded": self.any_degraded,
        }


# --------------------------------------------------------------------------- #
# Aggregator
# --------------------------------------------------------------------------- #


def summarize_samples(
    samples: Iterable[BenchmarkSample],
    *,
    slos: Iterable[PerformanceSLO] = DEFAULT_SLOS,
    baseline: Mapping[str, float] | None = None,
) -> BenchmarkReport:
    """Aggregate raw samples into P50/P95 summaries and SLO compliance flags."""
    slo_table = {slo.slo_id: slo for slo in slos}
    bucket: dict[str, list[float]] = {slo_id: [] for slo_id in slo_table}
    for sample in samples:
        if sample.slo_id in bucket:
            bucket[sample.slo_id].append(sample.value)
    summaries: list[SLOSummary] = []
    for slo_id, slo in slo_table.items():
        values = sorted(bucket[slo_id])
        if not values:
            continue
        p50 = statistics.median(values)
        p95 = _percentile(values, 0.95)
        meets_p50 = _meets(slo.direction, p50, slo.p50_target)
        meets_p95 = _meets(slo.direction, p95, slo.p95_target)
        degraded = False
        if baseline and slo_id in baseline:
            baseline_value = baseline[slo_id]
            if slo.direction == "lower":
                degraded = p50 > baseline_value * (1.0 + slo.degradation_threshold)
            else:
                degraded = p50 < baseline_value * (1.0 - slo.degradation_threshold)
        summaries.append(SLOSummary(
            slo=slo,
            p50=p50,
            p95=p95,
            sample_count=len(values),
            meets_p50=meets_p50,
            meets_p95=meets_p95,
            degraded_vs_baseline=degraded,
        ))
    return BenchmarkReport(summaries=tuple(summaries))


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = quantile * (len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = index - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _meets(direction: str, value: float, target: float) -> bool:
    if direction == "upper":
        return value >= target
    return value <= target


__all__ = [
    "BenchmarkReport",
    "BenchmarkSample",
    "PerformanceSLO",
    "SLOSummary",
    "DEFAULT_SLOS",
    "summarize_samples",
]
