"""Regression tests for the bounded autonomous editing mission ledger."""

from __future__ import annotations

from src.features.edit.ai_editing.autonomous_mission import (
    AutonomousMissionStore,
    MissionStage,
    MissionStatus,
)


def _complete_plan(store: AutonomousMissionStore, run_id: str = "run-1") -> None:
    store.start(run_id, "plan-1")
    store.record(run_id, MissionStage.PLAN, passed=True, reason="planned")


def test_mission_requires_ordered_quality_gates_and_completes() -> None:
    store = AutonomousMissionStore()
    _complete_plan(store)

    assert store.record("run-1", MissionStage.COMMIT, passed=True) is not None
    mission = store.get("run-1")
    assert mission is not None
    assert mission.status == MissionStatus.NEEDS_REPLAN
    assert mission.completed_stages == (MissionStage.PLAN,)


def test_failed_gate_stops_mission_and_cannot_be_overwritten() -> None:
    store = AutonomousMissionStore()
    _complete_plan(store)
    store.record("run-1", MissionStage.PREVIEW, passed=False, reason="stale revision")

    mission = store.get("run-1")
    assert mission is not None
    assert mission.status == MissionStatus.NEEDS_REPLAN
    assert store.record("run-1", MissionStage.COMMIT, passed=True) == mission


def test_mission_roundtrip_is_bounded_and_does_not_store_prompt_or_path() -> None:
    store = AutonomousMissionStore()
    _complete_plan(store)
    store.record(
        "run-1", MissionStage.PREVIEW, passed=True,
        reason="prompt=secret /Users/testuser/private.mov",
    )
    payload = store.export_all()
    assert payload is not None
    row = payload["missions"][0]
    assert "prompt" not in str(row).lower()
    assert "/Users/" not in str(row)

    restored = AutonomousMissionStore()
    assert restored.import_all(payload) == 1
    mission = restored.get("run-1")
    assert mission is not None
    assert mission.plan_id == "plan-1"
    assert mission.current_stage == MissionStage.COMMIT


def test_success_requires_all_four_stages() -> None:
    store = AutonomousMissionStore()
    _complete_plan(store)
    for stage in (MissionStage.PREVIEW, MissionStage.COMMIT, MissionStage.VERIFY):
        mission = store.record("run-1", stage, passed=True)
        assert mission is not None
    mission = store.get("run-1")
    assert mission is not None
    assert mission.status == MissionStatus.SUCCEEDED
    assert mission.quality_passed is True
