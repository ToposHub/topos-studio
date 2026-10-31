"""Regression tests for autonomous editing recovery boundaries."""

from __future__ import annotations

import time

from src.features.edit.ai_editing.audit_store import AuditEntry, AuditStore
from src.features.edit.ai_editing.autonomous_recovery import (
    AutonomousCheckpoint,
    AutonomousCheckpointStatus,
    AutonomousCheckpointStore,
)


def _checkpoint(**overrides):
    values = {
        "run_id": "run-1",
        "plan_id": "plan-1",
        "project_id": "project-1",
        "timeline_id": "timeline-1",
        "base_revision": 4,
        "asset_ids": ("asset-1",),
    }
    values.update(overrides)
    return AutonomousCheckpoint(**values)


def test_checkpoint_roundtrip_is_bounded_and_does_not_store_prompt() -> None:
    store = AutonomousCheckpointStore()
    store.upsert(_checkpoint(goal_id="goal-1", reason="temporary preview failure"))

    payload = store.export_all()
    assert payload is not None
    assert payload["schema_version"] == 1
    row = payload["checkpoints"][0]
    assert row["plan_id"] == "plan-1"
    assert row["goal_id"] == "goal-1"
    assert "prompt" not in row
    assert "path" not in row

    restored = AutonomousCheckpointStore()
    assert restored.import_all(payload) == 1
    assert restored.get("run-1").asset_ids == ("asset-1",)
    assert restored.get("run-1").goal_id == "goal-1"


def test_legacy_checkpoint_without_goal_id_falls_back_to_run_id() -> None:
    restored = AutonomousCheckpoint.from_dict({
        "schema_version": 1,
        "run_id": "run-legacy",
        "plan_id": "plan-legacy",
        "project_id": "project-1",
        "timeline_id": "timeline-1",
        "base_revision": 1,
        "asset_ids": [],
    })

    assert restored.goal_id == "run-legacy"


def test_expired_checkpoint_is_not_eligible_for_restart() -> None:
    store = AutonomousCheckpointStore(ttl_seconds=10)
    store.upsert(_checkpoint(updated_at=time.time() - 11))

    assert store.list_pending() == ()
    expired = store.get("run-1")
    assert expired is not None
    assert expired.status == AutonomousCheckpointStatus.EXPIRED


def test_cancelled_checkpoint_is_terminal_and_never_pending() -> None:
    store = AutonomousCheckpointStore()
    store.upsert(_checkpoint())
    cancelled = store.mark(
        "run-1", AutonomousCheckpointStatus.CANCELLED, "user cancelled",
    )

    assert cancelled is not None
    assert cancelled.status == AutonomousCheckpointStatus.CANCELLED
    assert store.list_pending() == ()


def test_invalid_checkpoint_rows_are_skipped() -> None:
    store = AutonomousCheckpointStore()
    assert store.import_all({
        "schema_version": 1,
        "checkpoints": [{"run_id": "missing-fields"}, "not-an-object"],
    }) == 0


def test_audit_history_roundtrip_is_bounded_and_redacted() -> None:
    store = AuditStore(max_entries=3)
    store.record(AuditEntry(
        timestamp=1.0,
        tool="topos.autonomous_agent",
        plan_id="plan-1",
        applied=False,
        new_revision=None,
        client="run:1",
        diagnostics_summary={"AUTO_PREVIEW_FAILED": 1},
        plan_snapshot={"path": "/Users/testuser/private.mov", "token": "sk-1234567890123456"},
    ))
    store.record(AuditEntry(
        timestamp=2.0,
        tool="topos.autonomous_agent",
        plan_id="plan-2",
        applied=True,
        new_revision=5,
    ))
    initial_payload = store.export_all()
    assert initial_payload is not None
    assert "/Users" not in str(initial_payload)
    assert "sk-1234567890123456" not in str(initial_payload)
    store.record(AuditEntry(
        timestamp=3.0,
        tool="topos.autonomous_agent",
        plan_id="plan-3",
        applied=False,
        new_revision=None,
    ))
    store.record(AuditEntry(
        timestamp=4.0,
        tool="topos.autonomous_agent",
        plan_id="plan-4",
        applied=False,
        new_revision=None,
    ))

    payload = store.export_all()
    assert payload is not None
    assert len(payload["entries"]) == 3

    restored = AuditStore(max_entries=3)
    assert restored.import_all(payload) == 3
    assert [entry.plan_id for entry in restored.list_entries()] == ["plan-2", "plan-3", "plan-4"]
