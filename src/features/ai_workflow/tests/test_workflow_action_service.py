"""B4 Workflow EditPlan action-boundary tests."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.features.edit.ai_editing.approval_service import ApprovalService
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.models import EditOperation, EditPlan, OperationKind, PlanTarget
from src.features.edit.ai_editing.transaction_service import TransactionService, _expected_commit_scope
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.shared.media.metadata import Rational
from src.features.ai_workflow.runtime.workflow_action_service import WorkflowActionService


def _state(revision: int = 0) -> ProjectSequences:
    sequence = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1), width=1920,
        height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
        revision=revision,
    )
    project = EditProject(
        project_id="p1", name="AI Project",
        assets={"a1": MediaAsset(
            asset_id="a1", path="/redacted/clip.mp4", media_type="video",
            display_name="Clip A", metadata={"duration_frames": 5000},
        )},
        timelines={"t1": TimelineSequenceRef(
            timeline_id="t1", name="Main", fps=24.0, width=1920,
            height=1080, sample_rate=48000,
        )},
        root_bin_id="root", active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": sequence})


def _plan() -> EditPlan:
    return EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(EditOperation(
            "add-intro", OperationKind.ADD_CLIP,
            params={
                "track_id": "v1", "asset_id": "a1",
                "source": {"start": 0, "duration": 100},
                "timeline": {"start": 0, "duration": 100},
            },
        ),),
        title="Add intro",
    )


class _Service:
    def preview_plan(self, plan, state):
        return TransactionService().preview(plan, state)


class _Bridge:
    def __init__(self) -> None:
        self.state = _state()
        self.commit_count = 0

    def get_project_sequences(self):
        return self.state

    def commit_ai_command(self, timeline_id, macro, *, undo_label):
        self.commit_count += 1
        proposed = macro.apply(self.state.sequences[timeline_id])
        if isinstance(proposed, tuple):
            proposed = proposed[0]
        self.state = ProjectSequences(self.state.project, {timeline_id: proposed})
        return proposed

    def refresh_after_ai_commit(self, result):
        return None

    def request_plan_approval(self, plan, preview):
        return SimpleNamespace(grant_id="host-grant-1")


class _RollbackBridge(_Bridge):
    def rollback_ai_command(self, timeline_id, *, expected_undo_label, expected_revision):
        assert timeline_id == "t1"
        assert expected_undo_label == "Add intro"
        assert expected_revision == self.state.sequences[timeline_id].revision
        restored = replace(self.state.sequences[timeline_id], revision=expected_revision + 1)
        self.state = ProjectSequences(self.state.project, {timeline_id: restored})
        return restored


def _action_service(bridge, approval):
    return WorkflowActionService(
        service_provider=lambda: _Service(),
        bridge_provider=lambda: bridge,
        approval_provider=lambda: approval,
    )


def test_preview_then_approval_applies_once_and_is_idempotent() -> None:
    bridge = _Bridge()
    approval = ApprovalService()
    actions = _action_service(bridge, approval)
    plan = _plan()
    actions.register_plan("run-1", plan)

    preview = actions.preview("run-1")
    assert preview.ok
    grant = approval.issue_after_user_confirmation(
        _expected_commit_scope(plan, diff_hash=preview.diff_hash),
    )
    applied = actions.apply("run-1", grant.grant_id)
    replay = actions.apply("run-1", grant.grant_id)
    assert applied.ok and applied.status == "applied"
    assert replay.ok and replay.status == "already_applied"
    assert bridge.commit_count == 1


def test_stale_revision_blocks_apply_before_consuming_grant() -> None:
    bridge = _Bridge()
    approval = ApprovalService()
    actions = _action_service(bridge, approval)
    plan = _plan()
    actions.register_plan("run-1", plan)
    preview = actions.preview("run-1")
    grant = approval.issue_after_user_confirmation(
        _expected_commit_scope(plan, diff_hash=preview.diff_hash),
    )
    bridge.state = ProjectSequences(
        bridge.state.project,
        {"t1": replace(bridge.state.sequences["t1"], revision=1)},
    )
    result = actions.apply("run-1", grant.grant_id)
    assert not result.ok
    assert result.diagnostic_code == "STALE_REVISION"
    assert approval.active_count() == 1
    assert bridge.commit_count == 0


def test_missing_approval_is_fail_closed() -> None:
    bridge = _Bridge()
    actions = _action_service(bridge, ApprovalService())
    actions.register_plan("run-1", _plan())
    result = actions.apply("run-1", "")
    assert not result.ok
    assert result.diagnostic_code == "APPROVAL_REQUIRED"
    assert bridge.commit_count == 0


def test_request_approval_uses_fresh_preview_and_returns_opaque_grant() -> None:
    bridge = _Bridge()
    actions = _action_service(bridge, ApprovalService())
    actions.register_plan("run-1", _plan())
    result = actions.request_approval("run-1")
    assert result.ok
    assert result.status == "approved"
    assert result.grant_id == "host-grant-1"
    assert result.diff_hash


def test_rollback_requires_applied_plan_and_uses_host_guard() -> None:
    bridge = _RollbackBridge()
    approval = ApprovalService()
    actions = _action_service(bridge, approval)
    plan = _plan()
    actions.register_plan("run-rollback", plan)
    preview = actions.preview("run-rollback")
    grant = approval.issue_after_user_confirmation(
        _expected_commit_scope(plan, diff_hash=preview.diff_hash),
    )
    applied = actions.apply("run-rollback", grant.grant_id)
    assert applied.ok
    assert actions.can_rollback("run-rollback") is True
    result = actions.rollback("run-rollback")
    assert result.ok and result.status == "rolled_back"
    assert actions.can_rollback("run-rollback") is False


def test_rollback_without_applied_plan_returns_no_applied_plan() -> None:
    """只 register 未 apply → rollback 以 NO_APPLIED_PLAN fail closed。"""
    bridge = _Bridge()
    actions = _action_service(bridge, ApprovalService())
    actions.register_plan("run-no-rollback", _plan())
    assert actions.rollback("run-no-rollback").diagnostic_code == "NO_APPLIED_PLAN"


def test_rollback_fails_closed_without_host_capability() -> None:
    """AUD-23705：宿主 bridge 无 rollback_ai_command 时 rollback fail closed。

    必须先 apply 成功（越过 NO_APPLIED_PLAN 分支），才真正覆盖
    ROLLBACK_UNAVAILABLE（宿主能力缺失）分支。
    """
    bridge = _Bridge()  # 无 rollback_ai_command 能力
    approval = ApprovalService()
    actions = _action_service(bridge, approval)
    plan = _plan()
    actions.register_plan("run-no-rollback", plan)
    preview = actions.preview("run-no-rollback")
    assert preview.ok
    grant = approval.issue_after_user_confirmation(
        _expected_commit_scope(plan, diff_hash=preview.diff_hash),
    )
    applied = actions.apply("run-no-rollback", grant.grant_id)
    assert applied.ok and applied.status == "applied"
    # 宿主无回滚入口：fail closed，不得触碰时间线
    result = actions.rollback("run-no-rollback")
    assert not result.ok
    assert result.diagnostic_code == "ROLLBACK_UNAVAILABLE"
    assert bridge.commit_count == 1  # apply 提交过一次；rollback 不再产生提交
