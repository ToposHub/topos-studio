"""P6 production host action-boundary policy tests."""

from __future__ import annotations

from types import SimpleNamespace

from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
from src.features.edit.ai_editing.approval_service import ApprovalService
from src.features.edit.ai_editing.models import EditOperation, EditPlan, OperationKind, PlanTarget
from src.shared.feature_flags import FeatureFlags


def _bridge(*, agent_auto_mode: bool = False, auto_export: bool = False):
    project = SimpleNamespace(project_id="p1", active_timeline_id="t1")
    sequence = SimpleNamespace(timeline_id="t1", revision=4)
    host = SimpleNamespace(
        _current_edit_project=project,
        _current_timeline_sequences={"t1": sequence},
        feature_flags=FeatureFlags(
            generation=True,
            agent_auto_mode=agent_auto_mode,
            cloud_upload=True,
            auto_export=auto_export,
        ),
    )
    return MainWindowAiEditHostBridge(host, SimpleNamespace(), ApprovalService())


def test_final_action_boundary_requires_explicit_approval_in_default_mode() -> None:
    bridge = _bridge()
    denied = bridge.check_ai_action(
        "final_export", timeline_id="t1", base_revision=4,
    )
    assert not denied.allowed and denied.needs_approval
    allowed = bridge.check_ai_action(
        "final_export", timeline_id="t1", base_revision=4,
        approval_granted=True,
    )
    assert allowed.allowed


def test_restricted_mode_allows_scoped_write_after_current_revision_check() -> None:
    bridge = _bridge(agent_auto_mode=True, auto_export=True)
    allowed = bridge.check_ai_action(
        "timeline_write", timeline_id="t1", base_revision=4,
        approval_granted=True,
    )
    assert allowed.allowed
    stale = bridge.check_ai_action(
        "timeline_write", timeline_id="t1", base_revision=3,
        approval_granted=True,
    )
    assert not stale.allowed


def test_automatic_plan_approval_requires_explicit_opt_in() -> None:
    plan = EditPlan.create(
        plan_id="auto-boundary-plan",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        operations=(EditOperation(
            op_id="add-1", kind=OperationKind.ADD_CLIP,
            params={"asset_id": "a1"},
        ),),
    )
    preview = SimpleNamespace(has_errors=False, diff_hash="diff-1")

    assert _bridge().issue_automatic_plan_approval(plan, preview, asset_ids=("a1",)) is None
    grant = _bridge(agent_auto_mode=True).issue_automatic_plan_approval(
        plan, preview, asset_ids=("a1",),
    )
    assert grant is not None
    assert grant.issued_by == "restricted-automatic"


def test_automatic_plan_approval_respects_emergency_kill_switch() -> None:
    from src.features.edit.ai_editing.kill_switch import Capability, KillSwitch

    bridge = _bridge(agent_auto_mode=True)
    bridge._mw._ai_kill_switch = KillSwitch()
    assert bridge.is_autonomous_mode_enabled() is True
    bridge._mw._ai_kill_switch.disable(
        Capability.SKILLS, reason="incident response",
    )

    assert bridge.is_autonomous_mode_enabled() is False
    plan = EditPlan.create(
        plan_id="kill-switch-plan",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        operations=(EditOperation(
            op_id="add-1", kind=OperationKind.ADD_CLIP,
            params={"asset_id": "a1"},
        ),),
    )
    assert bridge.issue_automatic_plan_approval(
        plan, SimpleNamespace(has_errors=False, diff_hash="diff-1"),
        asset_ids=("a1",),
    ) is None


def test_host_post_commit_verifier_checks_added_asset_and_revision() -> None:
    bridge = _bridge(agent_auto_mode=True)
    plan = EditPlan.create(
        plan_id="post-verify-plan",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        operations=(EditOperation(
            op_id="add-1", kind=OperationKind.ADD_CLIP,
            params={"asset_id": "a1"},
        ),),
    )
    before = SimpleNamespace(sequences={"t1": SimpleNamespace(revision=4)})
    after = SimpleNamespace(
        timeline_id="t1", revision=5,
        all_tracks=(SimpleNamespace(clips=(SimpleNamespace(asset_id="a1"),)),),
    )

    result = bridge.verify_autonomous_commit(plan, before, after, SimpleNamespace())

    assert result["passed"] is True


def test_host_detects_exact_add_plan_replay_after_revision_advanced() -> None:
    bridge = _bridge(agent_auto_mode=True)
    plan = EditPlan.create(
        plan_id="replay-plan",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        operations=(EditOperation(
            op_id="add-1", kind=OperationKind.ADD_CLIP,
            params={
                "asset_id": "a1", "track_id": "v1",
                "timeline": {"start": 0, "duration": 24},
            },
        ),),
    )
    from src.features.edit.ai_editing.plan_compiler import _derive_clip_id

    current = SimpleNamespace(
        timeline_id="t1", revision=5,
        all_tracks=(SimpleNamespace(clips=(SimpleNamespace(
            clip_id=_derive_clip_id("replay-plan", "add-1"), asset_id="a1",
            track_id="v1", timeline_start=0, timeline_duration=24,
        ),), subtitle_clips=()),),
    )
    state = SimpleNamespace(sequences={"t1": current})

    result = bridge.detect_autonomous_replay(plan, state)

    assert result["known"] is True
    assert result["already_applied"] is True


def test_host_blocks_partial_add_plan_replay_for_manual_inspection() -> None:
    bridge = _bridge(agent_auto_mode=True)
    plan = EditPlan.create(
        plan_id="partial-replay-plan",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        operations=tuple(
            EditOperation(
                op_id=op_id, kind=OperationKind.ADD_CLIP,
                params={
                    "asset_id": "a1", "track_id": "v1",
                    "timeline": {"start": 0, "duration": 24},
                },
            )
            for op_id in ("add-1", "add-2")
        ),
    )
    from src.features.edit.ai_editing.plan_compiler import _derive_clip_id

    current = SimpleNamespace(
        timeline_id="t1", revision=5,
        all_tracks=(SimpleNamespace(clips=(SimpleNamespace(
            clip_id=_derive_clip_id("partial-replay-plan", "add-1"), asset_id="a1",
            track_id="v1", timeline_start=0, timeline_duration=24,
        ),), subtitle_clips=()),),
    )

    result = bridge.detect_autonomous_replay(
        plan, SimpleNamespace(sequences={"t1": current}),
    )

    assert result["known"] is True
    assert result["ambiguous"] is True


def test_host_does_not_treat_same_clip_id_with_different_content_as_replay() -> None:
    bridge = _bridge(agent_auto_mode=True)
    plan = EditPlan.create(
        plan_id="collision-plan",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        operations=(EditOperation(
            op_id="add-1", kind=OperationKind.ADD_CLIP,
            params={
                "asset_id": "a1", "track_id": "v1",
                "timeline": {"start": 0, "duration": 24},
            },
        ),),
    )
    from src.features.edit.ai_editing.plan_compiler import _derive_clip_id

    current = SimpleNamespace(
        timeline_id="t1", revision=5,
        all_tracks=(SimpleNamespace(clips=(SimpleNamespace(
            clip_id=_derive_clip_id("collision-plan", "add-1"), asset_id="other",
            track_id="v1", timeline_start=0, timeline_duration=24,
        ),), subtitle_clips=()),),
    )

    result = bridge.detect_autonomous_replay(
        plan, SimpleNamespace(sequences={"t1": current}),
    )

    assert result["known"] is True
    assert result.get("already_applied") is not True
    assert result["ambiguous"] is True
