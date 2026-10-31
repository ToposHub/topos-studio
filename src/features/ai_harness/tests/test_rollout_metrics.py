"""A-022 rollout metrics tests."""
from __future__ import annotations

from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    RunPolicy,
)
from src.features.ai_harness.rollout_metrics import aggregate_run_metrics
from src.features.ai_harness.runtime import HarnessRuntime


def _invocation() -> Invocation:
    return Invocation(
        source=InvocationSource.WORKFLOW,
        skill_id="metrics.test",
        scope=InvocationScope(project_id="p1"),
        policy=RunPolicy(),
        user_text="不应进入 metrics",
    )


def test_metrics_aggregate_recovery_handoff_cost_and_latency() -> None:
    runtime = HarnessRuntime()

    resumed = runtime.start(_invocation())
    runtime.mark_running(resumed)
    runtime.mark_waiting_for_user(resumed, message="approval")
    runtime.resume(resumed)
    runtime.record_cost(resumed, 1.25)
    runtime.record_diagnostic(
        resumed,
        {"code": "QUALITY_GATE_FAILED", "message": "/private/secret must not be exported"},
    )
    runtime.finish(resumed)

    handed_off = runtime.start(_invocation())
    runtime.mark_running(handed_off)
    runtime.handoff(handed_off)
    runtime.finish(handed_off, success=False)

    retried = runtime.retry(handed_off)
    assert retried
    runtime.mark_running(retried)
    runtime.finish(retried)

    metrics = runtime.get_metrics()
    assert metrics.total_runs == 3
    assert metrics.terminal_runs == 3
    assert metrics.succeeded_runs == 2
    assert metrics.cancelled_runs == 1
    assert metrics.approval_waits == 1
    assert metrics.handoff_requests == 1
    assert metrics.retry_attempts == 1
    assert metrics.recovery_attempts == 2
    assert metrics.recovery_successes == 2
    assert metrics.success_rate == 2 / 3
    assert metrics.total_cost == 1.25
    assert metrics.quality_gate_failures == 1

    public = metrics.to_public_dict()
    assert "/private/secret" not in str(public)
    assert public["diagnostic_codes"] == [{"code": "QUALITY_GATE_FAILED", "count": 1}]


def test_empty_metrics_are_zero_and_serializable() -> None:
    metrics = aggregate_run_metrics(())
    assert metrics.total_runs == 0
    assert metrics.success_rate == 0.0
    assert metrics.to_public_dict()["p95_latency_ms"] == 0
