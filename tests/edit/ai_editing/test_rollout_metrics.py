"""P11 rollout metric and promotion gate tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.rollout_metrics import (
    RolloutChannel,
    RolloutMetrics,
    evaluate_rollout_gate,
)


def test_beta_gate_requires_real_success_and_satisfaction_metrics() -> None:
    metrics = RolloutMetrics(
        tasks=20, successful_tasks=18, satisfied_users=14,
        user_edits=6, total_cost_units=10.0,
    )
    gate = evaluate_rollout_gate(RolloutChannel.BETA, metrics)
    assert gate.allowed
    assert metrics.cost_per_task == 0.5


def test_stable_gate_blocks_crashes_repeated_charges_and_provider_failures() -> None:
    gate = evaluate_rollout_gate(
        RolloutChannel.STABLE,
        RolloutMetrics(
            tasks=10, successful_tasks=10, satisfied_users=10,
            crashes=1, repeated_charges=1, provider_failures=1,
        ),
    )
    assert not gate.allowed
    assert len(gate.reasons) == 3
