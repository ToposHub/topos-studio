"""P9 plugin quality gate and new-run rollout tests."""

from __future__ import annotations

from src.features.ai_harness.plugin_rollout import (
    PluginRolloutFlag,
    PluginRolloutGate,
    PluginRolloutLedger,
    PluginRolloutPolicy,
    PluginRolloutRouter,
    PluginRunOutcome,
    PluginRunSample,
    aggregate_plugin_samples,
)


def _sample(outcome: PluginRunOutcome, latency: int, *, cost: float = 0.1, rollback: bool = True) -> PluginRunSample:
    return PluginRunSample(
        plugin_id="topos.rollout",
        version="2.0.0",
        run_id=f"run-{outcome.value}-{latency}-{cost}",
        outcome=outcome,
        latency_ms=latency,
        cost=cost,
        rollback_available=rollback,
    )


def test_rollout_metrics_aggregate_failure_latency_cost_and_rollback() -> None:
    samples = (
        _sample(PluginRunOutcome.SUCCEEDED, 100),
        _sample(PluginRunOutcome.FAILED, 400, cost=0.2),
        _sample(PluginRunOutcome.CRASHED, 900, cost=0.3),
        _sample(PluginRunOutcome.CANCELLED, 50, rollback=False),
    )
    metrics = aggregate_plugin_samples(
        samples,
        plugin_id="topos.rollout",
        version="2.0.0",
    )

    assert metrics.total_runs == 4
    assert metrics.failure_rate == 0.5
    assert metrics.crash_rate == 0.25
    assert metrics.p95_latency_ms == 900
    assert metrics.average_latency_ms == 362
    assert metrics.total_cost == 0.7
    assert metrics.rollback_available is False


def test_rollout_gate_blocks_bad_release_and_allows_healthy_release() -> None:
    ledger = PluginRolloutLedger(max_samples=10)
    for sample in (
        _sample(PluginRunOutcome.SUCCEEDED, 100),
        _sample(PluginRunOutcome.FAILED, 500, cost=3.0, rollback=False),
    ):
        ledger.record(sample)
    bad = ledger.metrics(plugin_id="topos.rollout", version="2.0.0")
    decision = PluginRolloutGate.evaluate(
        bad,
        policy=PluginRolloutPolicy(
            min_samples=2,
            max_failure_rate=0.1,
            max_crash_rate=0.01,
            max_p95_latency_ms=200,
            max_average_cost=1.0,
        ),
    )
    assert decision.allowed is False
    assert {
        "PLUGIN_GATE_FAILURE_RATE",
        "PLUGIN_GATE_P95_LATENCY",
        "PLUGIN_GATE_AVERAGE_COST",
        "PLUGIN_GATE_ROLLBACK_UNAVAILABLE",
    } <= set(decision.reasons)

    healthy = aggregate_plugin_samples(
        (_sample(PluginRunOutcome.SUCCEEDED, 100) for _ in range(3)),
        plugin_id="topos.rollout",
        version="2.0.0",
    )
    good = PluginRolloutGate.evaluate(
        healthy,
        policy=PluginRolloutPolicy(min_samples=3, max_p95_latency_ms=200),
    )
    assert good.allowed is True
    assert good.reasons == ()


def test_rollout_router_is_deterministic_and_never_switches_existing_runs() -> None:
    flag = PluginRolloutFlag(
        plugin_id="topos.rollout",
        target_version="2.0.0",
        percentage=100,
        started_at=100.0,
    )
    router = PluginRolloutRouter((flag,))
    assert router.target_for_new_run(
        "topos.rollout",
        run_id="old-run",
        run_created_at=99.0,
    ) is None
    assert router.target_for_new_run(
        "topos.rollout",
        run_id="new-run",
        run_created_at=100.0,
    ) == "2.0.0"

    half = PluginRolloutFlag(
        plugin_id="topos.half",
        target_version="3.0.0",
        percentage=50,
        started_at=0.0,
    )
    half_router = PluginRolloutRouter((half,))
    first = half_router.target_for_new_run("topos.half", run_id="stable", run_created_at=1.0)
    second = half_router.target_for_new_run("topos.half", run_id="stable", run_created_at=1.0)
    assert first == second
    assert half_router.register(half) is False
