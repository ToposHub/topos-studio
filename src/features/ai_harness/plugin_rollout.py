"""Plugin-level quality gates and deterministic new-run rollout routing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
import hashlib
import math
import threading
from typing import Any


class PluginRunOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CRASHED = "crashed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PluginRunSample:
    plugin_id: str
    version: str
    run_id: str
    outcome: PluginRunOutcome
    latency_ms: int
    cost: float = 0.0
    rollback_available: bool = True

    def __post_init__(self) -> None:
        if not str(self.plugin_id).strip() or not str(self.version).strip() or not str(self.run_id).strip():
            raise ValueError("plugin run sample identity is required")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if self.cost < 0:
            raise ValueError("cost must be non-negative")
        outcome = self.outcome if isinstance(self.outcome, PluginRunOutcome) else PluginRunOutcome(str(self.outcome))
        object.__setattr__(self, "outcome", outcome)


@dataclass(frozen=True)
class PluginRolloutMetrics:
    plugin_id: str
    version: str
    total_runs: int = 0
    succeeded_runs: int = 0
    failed_runs: int = 0
    crashed_runs: int = 0
    cancelled_runs: int = 0
    p95_latency_ms: int = 0
    average_latency_ms: int = 0
    total_cost: float = 0.0
    average_cost: float = 0.0
    rollback_available: bool = False

    @property
    def failure_rate(self) -> float:
        return (self.failed_runs + self.crashed_runs) / self.total_runs if self.total_runs else 0.0

    @property
    def crash_rate(self) -> float:
        return self.crashed_runs / self.total_runs if self.total_runs else 0.0

    @property
    def success_rate(self) -> float:
        return self.succeeded_runs / self.total_runs if self.total_runs else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "total_runs": self.total_runs,
            "succeeded_runs": self.succeeded_runs,
            "failed_runs": self.failed_runs,
            "crashed_runs": self.crashed_runs,
            "cancelled_runs": self.cancelled_runs,
            "failure_rate": round(self.failure_rate, 6),
            "crash_rate": round(self.crash_rate, 6),
            "success_rate": round(self.success_rate, 6),
            "p95_latency_ms": self.p95_latency_ms,
            "average_latency_ms": self.average_latency_ms,
            "total_cost": round(self.total_cost, 6),
            "average_cost": round(self.average_cost, 6),
            "rollback_available": self.rollback_available,
        }


class PluginRolloutLedger:
    """Bounded, thread-safe samples for a plugin/version quality gate."""

    def __init__(self, *, max_samples: int = 10_000) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self._max_samples = max_samples
        self._samples: list[PluginRunSample] = []
        self._lock = threading.RLock()

    def record(self, sample: PluginRunSample) -> None:
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) > self._max_samples:
                del self._samples[: len(self._samples) - self._max_samples]

    def samples(self, *, plugin_id: str, version: str) -> tuple[PluginRunSample, ...]:
        with self._lock:
            return tuple(
                sample for sample in self._samples
                if sample.plugin_id == str(plugin_id) and sample.version == str(version)
            )

    def metrics(self, *, plugin_id: str, version: str) -> PluginRolloutMetrics:
        return aggregate_plugin_samples(self.samples(plugin_id=plugin_id, version=version), plugin_id=plugin_id, version=version)


def aggregate_plugin_samples(
    samples: Iterable[PluginRunSample],
    *,
    plugin_id: str,
    version: str,
) -> PluginRolloutMetrics:
    rows = tuple(samples)
    latencies = sorted(sample.latency_ms for sample in rows)
    succeeded = sum(sample.outcome == PluginRunOutcome.SUCCEEDED for sample in rows)
    failed = sum(sample.outcome == PluginRunOutcome.FAILED for sample in rows)
    crashed = sum(sample.outcome == PluginRunOutcome.CRASHED for sample in rows)
    cancelled = sum(sample.outcome == PluginRunOutcome.CANCELLED for sample in rows)
    total_cost = sum(sample.cost for sample in rows)
    p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1) if latencies else 0
    return PluginRolloutMetrics(
        plugin_id=str(plugin_id),
        version=str(version),
        total_runs=len(rows),
        succeeded_runs=succeeded,
        failed_runs=failed,
        crashed_runs=crashed,
        cancelled_runs=cancelled,
        p95_latency_ms=latencies[p95_index] if latencies else 0,
        average_latency_ms=int(sum(latencies) / len(latencies)) if latencies else 0,
        total_cost=total_cost,
        average_cost=total_cost / len(rows) if rows else 0.0,
        rollback_available=bool(rows) and all(sample.rollback_available for sample in rows),
    )


@dataclass(frozen=True)
class PluginRolloutPolicy:
    min_samples: int = 20
    max_failure_rate: float = 0.05
    max_crash_rate: float = 0.01
    max_p95_latency_ms: int = 30_000
    max_average_cost: float = 1.0
    require_rollback: bool = True

    def __post_init__(self) -> None:
        if self.min_samples <= 0 or self.max_p95_latency_ms < 0:
            raise ValueError("rollout sample and latency limits must be positive")
        for name in ("max_failure_rate", "max_crash_rate"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.max_average_cost < 0:
            raise ValueError("max_average_cost must be non-negative")


@dataclass(frozen=True)
class PluginRolloutDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    metrics: PluginRolloutMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "metrics": self.metrics.to_dict() if self.metrics else None,
        }


class PluginRolloutGate:
    """Evaluate whether a plugin version is safe to enter a rollout."""

    @staticmethod
    def evaluate(
        metrics: PluginRolloutMetrics,
        *,
        policy: PluginRolloutPolicy = PluginRolloutPolicy(),
    ) -> PluginRolloutDecision:
        reasons: list[str] = []
        if metrics.total_runs < policy.min_samples:
            reasons.append("PLUGIN_GATE_INSUFFICIENT_SAMPLES")
        if metrics.failure_rate > policy.max_failure_rate:
            reasons.append("PLUGIN_GATE_FAILURE_RATE")
        if metrics.crash_rate > policy.max_crash_rate:
            reasons.append("PLUGIN_GATE_CRASH_RATE")
        if metrics.p95_latency_ms > policy.max_p95_latency_ms:
            reasons.append("PLUGIN_GATE_P95_LATENCY")
        if metrics.average_cost > policy.max_average_cost:
            reasons.append("PLUGIN_GATE_AVERAGE_COST")
        if policy.require_rollback and not metrics.rollback_available:
            reasons.append("PLUGIN_GATE_ROLLBACK_UNAVAILABLE")
        return PluginRolloutDecision(not reasons, tuple(reasons), metrics)


@dataclass(frozen=True)
class PluginRolloutFlag:
    plugin_id: str
    target_version: str
    percentage: int
    started_at: float
    enabled: bool = True

    def __post_init__(self) -> None:
        if not str(self.plugin_id).strip() or not str(self.target_version).strip():
            raise ValueError("rollout flag identity is required")
        if not 0 <= self.percentage <= 100:
            raise ValueError("rollout percentage must be between 0 and 100")

    def admits_new_run(self, *, run_id: str, run_created_at: float) -> bool:
        """Stable bucket assignment; runs created before rollout never switch."""
        if not self.enabled or run_created_at < self.started_at or self.percentage <= 0:
            return False
        if self.percentage >= 100:
            return True
        digest = hashlib.sha256(
            f"{self.plugin_id}:{self.target_version}:{run_id}".encode("utf-8"),
        ).hexdigest()
        return int(digest[:8], 16) % 100 < self.percentage


class PluginRolloutRouter:
    """Select target versions for new runs without mutating existing runs.

    线程契约（AUD-22200）：``register`` / ``remove``（配置线程）与
    ``target_for_new_run`` / ``to_dict``（run 创建 / 状态序列化线程）可并发；
    内部由 ``threading.Lock`` 保护——``register`` 的"存在检查再插入"是
    check-then-act，``to_dict`` 对 ``_flags.values()`` 排序迭代，无锁时并发
    写入可抛 ``RuntimeError: dictionary changed size during iteration``。
    """

    def __init__(self, flags: Iterable[PluginRolloutFlag] = ()) -> None:
        self._lock = threading.Lock()
        self._flags: dict[str, PluginRolloutFlag] = {}
        for flag in flags:
            self.register(flag)

    def register(self, flag: PluginRolloutFlag) -> bool:
        with self._lock:
            if flag.plugin_id in self._flags:
                return False
            self._flags[flag.plugin_id] = flag
            return True

    def remove(self, plugin_id: str) -> bool:
        with self._lock:
            return self._flags.pop(str(plugin_id), None) is not None

    def target_for_new_run(
        self,
        plugin_id: str,
        *,
        run_id: str,
        run_created_at: float,
    ) -> str | None:
        # 只在锁内取 flag 快照；admits_new_run 是纯计算（sha256 + 比较），
        # 放在锁外避免延长临界区。
        with self._lock:
            flag = self._flags.get(str(plugin_id))
        if flag is None or not flag.admits_new_run(run_id=run_id, run_created_at=run_created_at):
            return None
        return flag.target_version

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            flags = sorted(self._flags.values(), key=lambda item: item.plugin_id)
        return {
            "flags": [
                {
                    "plugin_id": flag.plugin_id,
                    "target_version": flag.target_version,
                    "percentage": flag.percentage,
                    "started_at": flag.started_at,
                    "enabled": flag.enabled,
                }
                for flag in flags
            ],
        }


__all__ = [
    "PluginRolloutDecision",
    "PluginRolloutFlag",
    "PluginRolloutGate",
    "PluginRolloutLedger",
    "PluginRolloutMetrics",
    "PluginRolloutPolicy",
    "PluginRolloutRouter",
    "PluginRunOutcome",
    "PluginRunSample",
    "aggregate_plugin_samples",
]
