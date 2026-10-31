"""Focused tests for the restricted autonomous timeline write boundary."""

from __future__ import annotations

from types import SimpleNamespace

from src.features.edit.ai_editing.approval_service import ApprovalService
from src.features.edit.ai_editing.autonomous_agent import (
    AutonomousAgent,
    AutonomousAgentConfig,
    AutonomousStageEvent,
)
from src.features.edit.ai_editing.models import EditOperation, EditPlan, OperationKind, PlanTarget
from src.features.edit.ai_editing.transaction_service import _expected_commit_scope


def _plan(*, kind: OperationKind = OperationKind.ADD_CLIP, base_revision: int = 4) -> EditPlan:
    return EditPlan.create(
        plan_id="auto-plan-1",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=base_revision,
        title="自动剪辑",
        operations=(EditOperation(
            op_id="op-1",
            kind=kind,
            params={"asset_id": "a1"},
        ),),
    )


class _Host:
    def __init__(self, *, revision: int = 4, commit_revision: int = 5) -> None:
        self.revision = revision
        self.commit_revision = commit_revision
        self.approvals = ApprovalService()
        self.committed = False
        self.rolled_back = False
        self.selected_asset_ids = ("a1",)

    def get_project_id(self) -> str:
        return "p1"

    def get_project_sequences(self) -> object:
        return SimpleNamespace(
            project=SimpleNamespace(project_id="p1"),
            sequences={"t1": SimpleNamespace(revision=self.revision)},
        )

    def check_ai_action(self, _action: str, **_kwargs: object) -> object:
        return SimpleNamespace(allowed=True, reason="")

    def issue_automatic_plan_approval(
        self, plan: EditPlan, preview: object, *, asset_ids: tuple[str, ...] = (),
    ):
        assert set(asset_ids).issubset(set(self.selected_asset_ids))
        return self.approvals.issue_after_user_confirmation(
            _expected_commit_scope(plan, diff_hash=getattr(preview, "diff_hash", None)),
            issued_by="restricted-automatic",
        )

    def approval_service(self) -> ApprovalService:
        return self.approvals

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        return self.selected_asset_ids

    def commit_ai_command(self, _timeline_id: str, _macro: object, *, undo_label: str) -> object:
        self.committed = True
        return SimpleNamespace(revision=self.commit_revision, undo_label=undo_label)

    def rollback_ai_command(self, _timeline_id: str, *, expected_undo_label: str, expected_revision: int | None):
        self.rolled_back = True
        return SimpleNamespace(revision=self.revision)

    def refresh_after_ai_commit(self, _result: object) -> None:
        return None


def _agent(
    host: _Host, *, enabled: bool = True,
    on_stage=None,
    preview_compiled=None,
    commit_compiled=None,
) -> AutonomousAgent:
    preview_value = preview_compiled or SimpleNamespace(
        macro=object(), undo_label="AI 自动剪辑",
    )
    commit_value = commit_compiled or SimpleNamespace(
        macro=object(), undo_label="AI 自动剪辑",
    )
    return AutonomousAgent(
        host,
        preview_plan=lambda _plan, _state: SimpleNamespace(
            has_errors=False,
            compiled=preview_value,
            diff_hash="diff-1",
        ),
        compile_plan=lambda _plan: commit_value,
        config=AutonomousAgentConfig(enabled=enabled),
        on_stage=on_stage,
    )


def test_autonomous_agent_commits_scoped_plan_once() -> None:
    host = _Host()
    result = _agent(host).apply(_plan())

    assert result.applied is True
    assert result.code == "AUTO_APPLIED"
    assert result.new_revision == 5
    assert host.committed is True


def test_autonomous_agent_emits_guarded_stage_events_in_order() -> None:
    host = _Host()
    events: list[AutonomousStageEvent] = []
    result = _agent(host, on_stage=events.append).apply(_plan())

    assert result.applied is True
    assert [event.stage for event in events] == ["preview", "commit", "verify"]
    assert all(event.passed for event in events)


def test_autonomous_agent_blocks_preview_commit_compile_mismatch_before_approval() -> None:
    host = _Host()
    preview = SimpleNamespace(
        macro=object(), undo_label="AI 自动剪辑", plan_id="plan-a", operation_count=1,
    )
    commit = SimpleNamespace(
        macro=object(), undo_label="AI 自动剪辑", plan_id="plan-b", operation_count=1,
    )

    result = _agent(host, preview_compiled=preview, commit_compiled=commit).apply(_plan())

    assert result.applied is False
    assert result.code == "AUTO_PREVIEW_COMPILE_MISMATCH"
    assert host.committed is False
    assert host.approvals.active_count() == 0


def test_autonomous_agent_rejects_stale_plan_before_preview() -> None:
    host = _Host(revision=5)
    result = _agent(host).apply(_plan(base_revision=4))

    assert result.applied is False
    assert result.code == "STALE_PLAN"
    assert host.committed is False


def test_autonomous_agent_skips_exact_replay_after_restart() -> None:
    host = _Host(revision=5)
    host.detect_autonomous_replay = lambda _plan, _state: {
        "known": True, "already_applied": True,
    }

    result = _agent(host).apply(_plan(base_revision=4))

    assert result.applied is True
    assert result.code == "AUTO_ALREADY_APPLIED"
    assert result.new_revision == 5
    assert host.committed is False
    assert host.approvals.active_count() == 0


def test_autonomous_agent_blocks_partial_replay_instead_of_duplicate_write() -> None:
    host = _Host(revision=5)
    host.detect_autonomous_replay = lambda _plan, _state: {
        "known": True,
        "ambiguous": True,
        "reason": "partial plan",
    }

    result = _agent(host).apply(_plan(base_revision=4))

    assert result.applied is False
    assert result.code == "AUTO_REPLAY_AMBIGUOUS"
    assert host.committed is False
    assert host.approvals.active_count() == 0


def test_autonomous_agent_rejects_high_risk_operation() -> None:
    host = _Host()
    result = _agent(host).apply(_plan(kind=OperationKind.DELETE_CLIP))

    assert result.applied is False
    assert result.code == "AUTO_RISK_OPERATION"
    assert host.committed is False


def test_autonomous_agent_rejects_plan_outside_current_selection() -> None:
    host = _Host()
    plan = EditPlan.create(
        plan_id="auto-plan-out-of-scope",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        operations=(EditOperation(
            op_id="op-1", kind=OperationKind.ADD_CLIP,
            params={"asset_id": "a2"},
        ),),
    )
    result = _agent(host).apply(plan)

    assert result.applied is False
    assert result.code == "AUTO_ASSET_SCOPE_MISMATCH"
    assert host.committed is False


def test_autonomous_agent_rolls_back_when_revision_does_not_advance() -> None:
    host = _Host(commit_revision=4)
    result = _agent(host).apply(_plan())

    assert result.applied is False
    assert result.code == "AUTO_VERIFY_FAILED"
    assert result.rolled_back is True
    assert host.rolled_back is True


def test_autonomous_agent_revokes_grant_when_live_safety_check_changes() -> None:
    host = _Host()
    host.is_autonomous_mode_enabled = lambda: False
    result = _agent(host).apply(_plan())

    assert result.applied is False
    assert result.code == "AUTO_MODE_DISABLED"
    assert host.committed is False
    assert host.approvals.active_count() == 0


def test_autonomous_agent_rolls_back_when_post_commit_verification_fails() -> None:
    host = _Host()
    host.verify_autonomous_commit = lambda *_args: {
        "passed": False, "reason": "新增片段未落地",
    }
    result = _agent(host).apply(_plan())

    assert result.applied is False
    assert result.code == "AUTO_POST_VERIFY_FAILED"
    assert result.rolled_back is True
    assert host.committed is True
    assert host.rolled_back is True
