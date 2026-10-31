"""AQ-07: performance SLO scaffold — benchmark fixtures + budget gate.

Performance numbers are meaningless without a fixed measurement environment.
This module fixes the *shape* of a perf gate: a ``PerfBudget`` per render
profile, a ``BenchmarkSample`` (fixture + environment + measured latency), and a
``PerfGate`` that compares samples to the budget.  The actual measurements are
taken in a benchmark job that pins OS/GPU/driver/renderer-build/font-set/profile
(see the plan §2.3 "测量环境"); this gate is the pass/fail logic over those
measurements, so a regression cannot merge silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class PerfMetric(str, Enum):
    FIRST_FRAME_MS = "first_frame_ms"        # cold first-frame latency
    INTERACTIVE_FRAME_MS = "interactive_frame_ms"  # warm per-frame during scrub
    EXPORT_FPS = "export_fps"                # export throughput (frames/sec)
    ELEMENT_UPDATE_MS = "element_update_ms"  # single-element change -> redraw


@dataclass(frozen=True)
class PerfBudget:
    """Maximum allowed latency per metric for one render profile."""

    profile: str                                 # draft | interactive | preview | final
    limits: Mapping[PerfMetric, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # AUD-30307: ``frozen=True`` only freezes field *binding* — a caller
        # holding the original dict could still mutate this budget's limits
        # after construction (PerfGate shares the inner mapping too). Wrap a
        # defensive copy in a read-only mapping proxy so budgets are truly
        # immutable.
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    def limit(self, metric: PerfMetric) -> float:
        return self.limits.get(metric, float("inf"))


@dataclass(frozen=True)
class BenchmarkEnvironment:
    """The fixed environment a measurement was taken in (part of the evidence)."""

    os: str = ""
    cpu: str = ""
    gpu: str = ""
    driver: str = ""
    renderer_build: str = ""
    font_set_hash: str = ""
    profile: str = "preview"
    fixture_id: str = ""
    dpr: float = 1.0
    asset_hash: str = ""
    fixture_hash: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return the environment as JSON-safe benchmark evidence."""
        return {
            "os": self.os,
            "cpu": self.cpu,
            "gpu": self.gpu,
            "driver": self.driver,
            "renderer_build": self.renderer_build,
            "font_set_hash": self.font_set_hash,
            "profile": self.profile,
            "fixture_id": self.fixture_id,
            "dpr": self.dpr,
            "asset_hash": self.asset_hash,
            "fixture_hash": self.fixture_hash,
        }


@dataclass(frozen=True)
class BenchmarkSample:
    """One measured sample for a fixture/profile/metric."""

    metric: PerfMetric
    value: float                       # ms (or fps for EXPORT_FPS)
    environment: BenchmarkEnvironment = field(default_factory=BenchmarkEnvironment)

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric.value,
            "value": self.value,
            "environment": self.environment.to_dict(),
        }


@dataclass(frozen=True)
class PerfViolation:
    metric: PerfMetric
    budget: float
    measured: float
    profile: str
    fixture_id: str

    @property
    def exceeded_by(self) -> float:
        # For EXPORT_FPS, lower is worse; otherwise higher is worse.
        if self.metric == PerfMetric.EXPORT_FPS:
            return self.budget - self.measured
        return self.measured - self.budget


@dataclass(frozen=True)
class PerfGateResult:
    passed: bool
    violations: tuple[PerfViolation, ...] = ()
    notes: tuple[str, ...] = ()


class PerfGate:
    """Compare benchmark samples to per-profile budgets."""

    def __init__(self, budgets: Mapping[str, PerfBudget]) -> None:
        self._budgets = dict(budgets)

    def evaluate(self, samples: tuple[BenchmarkSample, ...]) -> PerfGateResult:
        violations: list[PerfViolation] = []
        notes: list[str] = []
        for sample in samples:
            budget = self._budgets.get(sample.environment.profile)
            if budget is None:
                notes.append(f"no budget for profile {sample.environment.profile}; sample skipped")
                continue
            limit = budget.limit(sample.metric)
            if limit == float("inf"):
                continue
            worse = sample.value < limit if sample.metric == PerfMetric.EXPORT_FPS else sample.value > limit
            if worse:
                violations.append(PerfViolation(
                    metric=sample.metric, budget=limit, measured=sample.value,
                    profile=sample.environment.profile, fixture_id=sample.environment.fixture_id))
        return PerfGateResult(passed=not violations, violations=tuple(violations), notes=tuple(notes))


def default_budgets() -> dict[str, PerfBudget]:
    """Product-target budgets (the plan stresses these are targets, not measured
    promises — they are frozen into SLO only after a baseline on the reference
    machine)."""
    return {
        "draft": PerfBudget("draft", {
            PerfMetric.FIRST_FRAME_MS: 1000.0,
            PerfMetric.INTERACTIVE_FRAME_MS: 100.0,
            PerfMetric.EXPORT_FPS: 6.0,
        }),
        "interactive": PerfBudget("interactive", {
            PerfMetric.FIRST_FRAME_MS: 500.0,
            PerfMetric.INTERACTIVE_FRAME_MS: 33.0,
            PerfMetric.EXPORT_FPS: 12.0,
        }),
        "preview": PerfBudget("preview", {
            PerfMetric.FIRST_FRAME_MS: 1000.0,
            PerfMetric.INTERACTIVE_FRAME_MS: 66.0,
            PerfMetric.EXPORT_FPS: 12.0,
        }),
        "final": PerfBudget("final", {
            PerfMetric.FIRST_FRAME_MS: 2000.0,
            PerfMetric.EXPORT_FPS: 6.0,
        }),
    }


__all__ = [
    "BenchmarkEnvironment", "BenchmarkSample", "PerfBudget", "PerfGate",
    "PerfGateResult", "PerfMetric", "PerfViolation", "default_budgets",
]
