"""Privacy-safe operational metrics derived from immutable ``RunRecord`` snapshots.

The aggregation layer intentionally accepts snapshots instead of subscribing to
providers.  This keeps dashboard numbers consistent with the runtime source of
truth and makes the same calculation usable by tests, UI and future rollout
reports.  Only stable diagnostic codes are retained; messages, prompts, paths
and provider payloads never enter the metrics object.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from src.features.ai_harness.invocation_models import RunRecord, RunStatus


_SUCCESS_STATES = frozenset({RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS})
_RECOVERY_EVENTS = frozenset({"run.retry_requested", "run.resumed"})


@dataclass(frozen=True)
class RunMetricsSnapshot:
    """Aggregated counters and latency/cost facts safe for dashboard display."""

    total_runs: int = 0
    active_runs: int = 0
    terminal_runs: int = 0
    succeeded_runs: int = 0
    failed_runs: int = 0
    cancelled_runs: int = 0
    retry_attempts: int = 0
    recovery_attempts: int = 0
    recovery_successes: int = 0
    approval_waits: int = 0
    handoff_requests: int = 0
    diagnostic_count: int = 0
    quality_gate_failures: int = 0
    total_cost: float = 0.0
    average_latency_ms: int = 0
    p95_latency_ms: int = 0
    diagnostic_codes: tuple[tuple[str, int], ...] = ()

    @property
    def success_rate(self) -> float:
        return self.succeeded_runs / self.terminal_runs if self.terminal_runs else 0.0

    @property
    def retry_rate(self) -> float:
        return self.retry_attempts / self.total_runs if self.total_runs else 0.0

    @property
    def recovery_rate(self) -> float:
        return self.recovery_successes / self.recovery_attempts if self.recovery_attempts else 0.0

    @property
    def handoff_rate(self) -> float:
        return self.handoff_requests / self.total_runs if self.total_runs else 0.0

    def to_public_dict(self) -> dict[str, object]:
        """Return dashboard-safe primitives with no diagnostic messages."""
        return {
            "total_runs": self.total_runs,
            "active_runs": self.active_runs,
            "terminal_runs": self.terminal_runs,
            "succeeded_runs": self.succeeded_runs,
            "failed_runs": self.failed_runs,
            "cancelled_runs": self.cancelled_runs,
            "retry_attempts": self.retry_attempts,
            "recovery_attempts": self.recovery_attempts,
            "recovery_successes": self.recovery_successes,
            "approval_waits": self.approval_waits,
            "handoff_requests": self.handoff_requests,
            "diagnostic_count": self.diagnostic_count,
            "quality_gate_failures": self.quality_gate_failures,
            "total_cost": round(self.total_cost, 6),
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "success_rate": round(self.success_rate, 6),
            "retry_rate": round(self.retry_rate, 6),
            "recovery_rate": round(self.recovery_rate, 6),
            "handoff_rate": round(self.handoff_rate, 6),
            "diagnostic_codes": [
                {"code": code, "count": count}
                for code, count in self.diagnostic_codes
            ],
        }


def aggregate_run_metrics(records: Iterable[RunRecord]) -> RunMetricsSnapshot:
    """Aggregate runtime snapshots without reading private provider state."""
    runs = tuple(records)
    latencies: list[int] = []
    diagnostic_codes: Counter[str] = Counter()
    retry_attempts = recovery_attempts = recovery_successes = 0
    approval_waits = handoff_requests = diagnostic_count = quality_gate_failures = 0
    total_cost = 0.0
    succeeded = failed = cancelled = active = terminal = 0

    for record in runs:
        status = str(record.status)
        if RunStatus.is_active(status):
            active += 1
        if RunStatus.is_terminal(status):
            terminal += 1
        if status in _SUCCESS_STATES:
            succeeded += 1
        elif status == RunStatus.FAILED:
            failed += 1
        elif status == RunStatus.CANCELLED:
            cancelled += 1
        total_cost += float(record.cost or 0.0)

        event_kinds = tuple(str(event.kind) for event in record.events)
        retry_attempts += event_kinds.count("run.retry_requested")
        recovery_events = sum(kind in _RECOVERY_EVENTS for kind in event_kinds)
        recovery_attempts += recovery_events
        if recovery_events and status in _SUCCESS_STATES:
            recovery_successes += recovery_events
        approval_waits += event_kinds.count("approval.required")
        handoff_requests += event_kinds.count("run.handoff_requested")

        diagnostic_count += len(record.diagnostics)
        for diagnostic in record.diagnostics:
            code = str(diagnostic.get("code", "UNKNOWN") or "UNKNOWN")[:80]
            diagnostic_codes[code] += 1
            if "gate" in code.lower() or code.upper().startswith("QUALITY"):
                quality_gate_failures += 1

        if record.finished_at is not None:
            started_at = record.started_at or record.created_at
            latencies.append(max(0, int((record.finished_at - started_at) * 1000)))

    latencies.sort()
    average = int(sum(latencies) / len(latencies)) if latencies else 0
    p95_index = min(len(latencies) - 1, max(0, int(len(latencies) * 0.95) - 1))
    p95 = latencies[p95_index] if latencies else 0
    return RunMetricsSnapshot(
        total_runs=len(runs), active_runs=active, terminal_runs=terminal,
        succeeded_runs=succeeded, failed_runs=failed, cancelled_runs=cancelled,
        retry_attempts=retry_attempts, recovery_attempts=recovery_attempts,
        recovery_successes=recovery_successes, approval_waits=approval_waits,
        handoff_requests=handoff_requests, diagnostic_count=diagnostic_count,
        quality_gate_failures=quality_gate_failures, total_cost=total_cost,
        average_latency_ms=average, p95_latency_ms=p95,
        diagnostic_codes=tuple(diagnostic_codes.most_common(5)),
    )


__all__ = ["RunMetricsSnapshot", "aggregate_run_metrics"]
