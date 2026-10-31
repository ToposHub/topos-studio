"""Tests for the domain-neutral long-running goal supervisor."""

from __future__ import annotations

from dataclasses import dataclass

from src.features.edit.ai_editing.goal_supervisor import (
    GoalControl,
    GoalCheckpoint,
    GoalCycleResult,
    GoalLimits,
    GoalSpec,
    GoalStatus,
    GoalSupervisor,
    InMemoryGoalCheckpointStore,
)


@dataclass
class _Runner:
    results: list[GoalCycleResult]

    def run_cycle(self, _goal, _checkpoint):
        return self.results.pop(0)


def _goal(**overrides):
    values = {
        "goal_id": "goal-1",
        "project_id": "project-1",
        "timeline_id": "timeline-1",
        "objective_ref": "skill:product-ad",
    }
    values.update(overrides)
    return GoalSpec(**values)


def test_supervisor_retries_finite_cycles_and_persists_success() -> None:
    store = InMemoryGoalCheckpointStore()
    runner = _Runner([
        GoalCycleResult(retryable=True, completed_step_ids=("observe",)),
        GoalCycleResult(completed=True, completed_step_ids=("commit",)),
    ])

    result = GoalSupervisor(
        runner, limits=GoalLimits(max_cycles=3), checkpoints=store,
    ).run(_goal())

    assert result.status == GoalStatus.SUCCEEDED
    assert result.checkpoint.cycle == 2
    assert result.checkpoint.completed_step_ids == ("commit", "observe")
    assert store.load("goal-1") == result.checkpoint


def test_pause_then_explicit_resume_uses_checkpoint() -> None:
    store = InMemoryGoalCheckpointStore()
    first = GoalSupervisor(
        _Runner([GoalCycleResult(completed=True)]), checkpoints=store,
    ).run(_goal(), control=lambda: GoalControl.PAUSE)
    assert first.status == GoalStatus.PAUSED

    second = GoalSupervisor(
        _Runner([GoalCycleResult(completed=True, completed_step_ids=("resume",))]),
        checkpoints=store,
    ).run(_goal(), resume=True)
    assert second.status == GoalStatus.SUCCEEDED
    assert second.checkpoint.cycle == 1


def test_scope_change_fails_closed_on_resume() -> None:
    store = InMemoryGoalCheckpointStore()
    GoalSupervisor(_Runner([GoalCycleResult(completed=True)]), checkpoints=store).run(_goal())

    result = GoalSupervisor(
        _Runner([GoalCycleResult(completed=True)]), checkpoints=store,
    ).run(_goal(timeline_id="other-timeline"), resume=True)

    assert result.status == GoalStatus.WAITING_FOR_USER
    assert "scope changed" in result.checkpoint.reason


def test_tool_budget_stops_retry_loop() -> None:
    runner = _Runner([
        GoalCycleResult(retryable=True, tool_calls=2),
        GoalCycleResult(completed=True),
    ])
    result = GoalSupervisor(
        runner, limits=GoalLimits(max_cycles=4, max_tool_calls=2),
    ).run(_goal())

    assert result.status == GoalStatus.BUDGET_EXCEEDED
    assert result.checkpoint.tool_calls == 2


def test_checkpoint_redacts_prompt_like_diagnostics() -> None:
    checkpoint = GoalSupervisor(
        _Runner([GoalCycleResult(completed=False, retryable=False, reason="prompt=secret")]),
    ).run(_goal())

    payload = checkpoint.checkpoint.to_dict()
    assert "prompt" not in str(payload).lower()
    assert "secret" not in str(payload).lower()


def test_goal_spec_roundtrip_rejects_scope_tampering() -> None:
    spec = _goal()
    restored = type(spec).from_dict(spec.to_dict())
    assert restored == spec

    tampered = dict(spec.to_dict())
    tampered["timeline_id"] = "other"
    try:
        type(spec).from_dict(tampered)
    except ValueError as exc:
        assert "scope hash" in str(exc)
    else:
        raise AssertionError("tampered goal scope must be rejected")


def test_external_async_cycle_is_governed_by_same_goal_budget() -> None:
    store = InMemoryGoalCheckpointStore()
    supervisor = GoalSupervisor(
        checkpoints=store, limits=GoalLimits(max_cycles=2, max_tool_calls=4),
    )

    first = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True, tool_calls=2, reason="stale"),
    )
    assert first.status == GoalStatus.RUNNING
    second = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True, tool_calls=2, reason="still stale"),
    )
    assert second.status == GoalStatus.BUDGET_EXCEEDED


def test_external_cycle_cannot_overwrite_terminal_success() -> None:
    store = InMemoryGoalCheckpointStore()
    supervisor = GoalSupervisor(checkpoints=store)
    completed = supervisor.record_cycle(_goal(), GoalCycleResult(completed=True))
    later = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True, reason="should not retry"),
    )

    assert completed.status == GoalStatus.SUCCEEDED
    assert later.status == GoalStatus.SUCCEEDED


def test_external_cycle_cannot_bypass_waiting_for_user_boundary() -> None:
    store = InMemoryGoalCheckpointStore()
    supervisor = GoalSupervisor(checkpoints=store)

    waiting = supervisor.record_cycle(
        _goal(), GoalCycleResult(needs_user_input=True, reason="approval required"),
    )
    late = supervisor.record_cycle(
        _goal(), GoalCycleResult(completed=True, reason="late callback"),
    )
    resumed = supervisor.record_cycle(
        _goal(),
        GoalCycleResult(completed=True, reason="approved"),
        resume_interactive=True,
    )

    assert waiting.status == GoalStatus.WAITING_FOR_USER
    assert late.status == GoalStatus.WAITING_FOR_USER
    assert late.checkpoint.cycle == 1
    assert resumed.status == GoalStatus.SUCCEEDED
    assert resumed.checkpoint.cycle == 2


def test_external_cycle_cannot_bypass_paused_boundary() -> None:
    store = InMemoryGoalCheckpointStore()
    supervisor = GoalSupervisor(checkpoints=store)

    paused = GoalSupervisor(
        _Runner([GoalCycleResult(completed=True)]), checkpoints=store,
    ).run(_goal(), control=lambda: GoalControl.PAUSE)
    late = supervisor.record_cycle(
        _goal(), GoalCycleResult(completed=True, reason="late callback"),
    )

    assert paused.status == GoalStatus.PAUSED
    assert late.status == GoalStatus.PAUSED
    assert late.checkpoint.cycle == 0


def test_external_cycle_cannot_reset_persisted_budget() -> None:
    store = InMemoryGoalCheckpointStore()
    supervisor = GoalSupervisor(
        checkpoints=store, limits=GoalLimits(max_cycles=2),
    )

    first = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True, reason="first"),
    )
    bypass = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True, reason="reset attempt"),
        resume=False,
    )

    assert first.checkpoint.cycle == 1
    assert bypass.status == GoalStatus.RUNNING
    assert bypass.checkpoint.cycle == 1
    assert "explicit resume" in bypass.diagnostics[0]


def test_external_cycle_enforces_persisted_wall_time_budget() -> None:
    store = InMemoryGoalCheckpointStore()
    now = [100.0]
    supervisor = GoalSupervisor(
        checkpoints=store,
        limits=GoalLimits(max_cycles=4, max_wall_time_seconds=10.0),
        wall_clock=lambda: now[0],
    )

    first = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True, reason="still working"),
    )
    now[0] = 111.0
    expired = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True, reason="late result"),
    )

    assert first.status == GoalStatus.RUNNING
    assert first.checkpoint.started_at == 100.0
    assert expired.status == GoalStatus.BUDGET_EXCEEDED
    assert expired.checkpoint.cycle == 1
    assert "wall-time" in expired.checkpoint.reason


def test_external_cycle_can_use_provider_start_time_for_first_result() -> None:
    store = InMemoryGoalCheckpointStore()
    now = [110.0]
    supervisor = GoalSupervisor(
        checkpoints=store,
        limits=GoalLimits(max_cycles=4, max_wall_time_seconds=10.0),
        wall_clock=lambda: now[0],
    )

    expired = supervisor.record_cycle(
        _goal(), GoalCycleResult(retryable=True), started_at=99.0,
    )

    assert expired.status == GoalStatus.BUDGET_EXCEEDED
    assert expired.checkpoint.cycle == 0


def test_goal_checkpoint_roundtrip_preserves_start_time_and_legacy_fallback() -> None:
    checkpoint = GoalCheckpoint(
        goal_id="goal-1",
        status=GoalStatus.RUNNING,
        started_at=10.0,
        updated_at=12.0,
    )
    restored = GoalCheckpoint.from_dict(checkpoint.to_dict())
    assert restored.started_at == 10.0

    legacy = checkpoint.to_dict()
    legacy.pop("started_at")
    restored_legacy = GoalCheckpoint.from_dict(legacy)
    assert restored_legacy.started_at == 12.0


def test_goal_checkpoint_compare_and_save_rejects_stale_revision() -> None:
    store = InMemoryGoalCheckpointStore()
    first = GoalCheckpoint(
        goal_id="goal-1",
        status=GoalStatus.RUNNING,
        revision=1,
    )
    assert store.compare_and_save("goal-1", 0, first)
    stale = GoalCheckpoint(
        goal_id="goal-1",
        status=GoalStatus.SUCCEEDED,
        revision=2,
    )
    assert not store.compare_and_save("goal-1", 0, stale)
    assert store.load("goal-1").status == GoalStatus.RUNNING
    assert store.load("goal-1").revision == 1
