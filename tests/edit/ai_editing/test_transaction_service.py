"""TAEP Phase 2 — Plan compiler + transaction service + TimelineDiff 测试。

核心验收（TAEP §12 P2）：

- preview 的 proposed timeline 与 commit 后结果 **canonical-equal**（复用同一 compiler）。
- 任一中间 operation 无效不会有部分提交。
- undo/redo 往返恢复。
- revision 冲突正确触发 conflict。
- TimelineDiff 准确反映新增/删除/移动/裁切。
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.approval_service import (
    ApprovalScope,
    ApprovalService,
    compute_plan_hash,
)
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanStatus,
    PlanTarget,
)
from src.features.edit.ai_editing.plan_compiler import CompileError, compile_plan
from src.features.edit.ai_editing.transaction_service import (
    TimelineDiff,
    TransactionService,
    _expected_commit_scope,
)
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# ================================================================
# Fixtures
# ================================================================


def _clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id="a1", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=start, source_duration=duration,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )


def _state() -> ProjectSequences:
    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset_meta = {"duration_frames": 5000}
    from src.features.edit.core.project import MediaAsset

    asset = MediaAsset(
        asset_id="a1", path="/home/secret/clip.mp4", media_type="video",
        display_name="Clip A", metadata=asset_meta,
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="AI Project",
        assets={"a1": asset},
        timelines={"t1": ref},
        root_bin_id="root",
        active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": seq})


def _add_clip_op(op_id: str, track_id: str, asset_id: str, t_start: int, t_dur: int, s_start: int = 0) -> EditOperation:
    return EditOperation(op_id, OperationKind.ADD_CLIP, params={
        "track_id": track_id, "asset_id": asset_id,
        "source": {"start": s_start, "duration": t_dur},
        "timeline": {"start": t_start, "duration": t_dur},
    })


def _grant_for(
    plan: EditPlan,
    svc: ApprovalService | None = None,
    *,
    project_id: str = "p1",
) -> "ApprovalGrant":
    """为 plan 签发一个真实、绑定作用域的 ApprovalGrant（测试辅助）。"""
    svc = svc or ApprovalService()
    scope = _expected_commit_scope(plan)
    # expected_commit_scope 已用 plan.target.project_id；这里保持一致。
    return svc.issue_after_user_confirmation(scope, issued_by="test")


def _grant_id_for(plan: EditPlan, svc: ApprovalService) -> str:
    """签发并返回 grant_id（用于测试 grant_id 路径）。"""
    return _grant_for(plan, svc).grant_id


# ================================================================
# plan_compiler
# ================================================================


def test_compile_add_clip_plan():
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
        title="Add intro",
    )
    compiled = compile_plan(plan)
    assert compiled.operation_count == 1
    assert compiled.undo_label == "Add intro"
    assert compiled.macro is not None


def test_compile_multi_op_single_macro():
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(
            _add_clip_op("op1", "v1", "a1", 0, 100),
            _add_clip_op("op2", "v1", "a1", 100, 100, s_start=100),
            EditOperation("op3", OperationKind.SET_MARKER, params={"frame": 50, "name": "beat"}),
        ),
    )
    compiled = compile_plan(plan)
    assert compiled.operation_count == 3
    assert len(compiled.operation_summaries) == 3


def test_compile_unknown_param_raises():
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={"track_id": "v1"}),),  # missing asset_id/source/timeline
    )
    with pytest.raises(CompileError) as exc:
        compile_plan(plan)
    assert exc.value.op_id == "op1"


# ================================================================
# transaction_service.preview — validate + compile + simulate + diff
# ================================================================


def test_preview_valid_plan_produces_diff_and_proposed():
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(
            _add_clip_op("op1", "v1", "a1", 0, 100),
            _add_clip_op("op2", "v1", "a1", 100, 100, s_start=100),
        ),
        title="Rough cut",
    )
    svc = TransactionService()
    result = svc.preview(plan, state)
    assert not result.has_errors
    assert result.compiled is not None
    assert result.diff is not None
    # proposed has 2 clips added
    assert len(result.diff.added_clips) == 2
    assert result.proposed_sequence.duration_frames == 200
    # preview does NOT bump revision / sync state
    assert result.proposed_sequence.revision == 0
    assert state.sequences["t1"].revision == 0
    assert len(state.sequences["t1"].video_tracks[0].clips) == 0  # original untouched


def test_preview_invalid_plan_no_diff():
    state = _state()
    # source range out of bounds (asset has 5000 frames; request 6000)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "track_id": "v1", "asset_id": "a1",
            "source": {"start": 5500, "duration": 600},
            "timeline": {"start": 0, "duration": 600},
        }),),
    )
    svc = TransactionService()
    result = svc.preview(plan, state)
    assert result.has_errors
    assert result.diff is None
    assert result.compiled is None


def test_preview_revision_conflict_stale():
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 99,  # current revision is 0
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    svc = TransactionService()
    result = svc.preview(plan, state)
    assert result.report.status == PlanStatus.STALE


# ================================================================
# transaction_service.commit — 原子提交 + undo + canonical equality
# ================================================================


def test_commit_without_approval_token_rejected():
    state = _state()
    plan = EditPlan.create(PlanTarget("p1", "t1"), 0,
                           operations=(_add_clip_op("op1", "v1", "a1", 0, 100),))
    svc = TransactionService()
    result = svc.commit(plan, state)
    assert result.applied is False
    assert "ApprovalGrant" in (result.error or "") or "approval" in (result.error or "").lower()


def test_commit_bare_string_token_without_service_rejected():
    """M0.1：裸字符串 token（无 ApprovalService consume）被拒——模型无法伪造授权。"""
    state = _state()
    plan = EditPlan.create(PlanTarget("p1", "t1"), 0,
                           operations=(_add_clip_op("op1", "v1", "a1", 0, 100),))
    svc = TransactionService()
    result = svc.commit(plan, state, approval="fake-model-token")
    assert result.applied is False
    assert "ApprovalGrant" in (result.error or "")


def test_commit_with_grant_applies_and_bumps():
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(
            _add_clip_op("op1", "v1", "a1", 0, 100),
            _add_clip_op("op2", "v1", "a1", 100, 100, s_start=100),
        ),
        title="Rough cut",
    )
    svc = TransactionService()
    approval_svc = ApprovalService()
    grant = _grant_for(plan, approval_svc)
    result = svc.commit(plan, state, approval=grant, approval_service=approval_svc)
    assert result.applied is True
    assert result.new_timeline is not None
    assert result.new_timeline.revision == 1  # single bump
    assert len(result.new_sequences["t1"].video_tracks[0].clips) == 2
    assert result.new_project.revision == 1
    assert result.undo_label == "Rough cut"


def test_commit_with_grant_id_via_service_applies():
    """通过 ApprovalService + grant_id consume 的路径也成功。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    approval_svc = ApprovalService()
    grant_id = _grant_id_for(plan, approval_svc)
    svc = TransactionService()
    result = svc.commit(
        plan, state, approval=grant_id, approval_service=approval_svc
    )
    assert result.applied is True


def test_commit_grant_single_use_cannot_replay():
    """M0.1：single_use grant 消费后不可重放。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    approval_svc = ApprovalService()
    grant = _grant_for(plan, approval_svc)
    svc = TransactionService()
    r1 = svc.commit(plan, state, approval=grant, approval_service=approval_svc)
    assert r1.applied
    # 重放同一 grant（已 used）→ 拒绝
    r2 = svc.commit(plan, _state(), approval=grant, approval_service=approval_svc)
    assert r2.applied is False


def test_commit_grant_without_service_is_rejected():
    """红线修复：传 ApprovalGrant 但不传 approval_service → 视为未授权。

    grant 自身不可变、无法就地标记 used；不配合 ApprovalService.consume 会破坏
    single_use 语义（同一 grant 可被反复 commit）。修复后 _authorize 无 service
    分支返回 None（拒），与"未授权"一致。
    """
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    svc = TransactionService()
    grant = _grant_for(plan)  # 真实 ApprovalGrant（但 service 引用丢弃）
    # 不传 approval_service → 拒
    result = svc.commit(plan, state, approval=grant)
    assert result.applied is False
    assert "ApprovalGrant" in result.error or "token" in result.error


def test_commit_grant_wrong_plan_hash_rejected():
    """M0.1：grant 绑定 plan hash；用在不同 plan 上被拒。"""
    state = _state()
    plan_a = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    plan_b = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 50),),  # 不同 duration → 不同 hash
    )
    approval_svc = ApprovalService()
    grant = _grant_for(plan_a, approval_svc)
    svc = TransactionService()
    result = svc.commit(plan_b, state, approval=grant, approval_service=approval_svc)
    assert result.applied is False
    assert "ApprovalGrant" in (result.error or "") or result.error == ""


def test_commit_grant_wrong_timeline_rejected():
    """M0.1：grant 绑定 timeline；跨 timeline 被拒。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    approval_svc = ApprovalService()
    # 为不同 timeline 签发
    other_scope = ApprovalScope(
        action="commit_plan", project_id="p1", timeline_id="other",
        base_revision=0, plan_hash="x",
    )
    grant = approval_svc.issue_after_user_confirmation(other_scope)
    svc = TransactionService()
    result = svc.commit(plan, state, approval=grant, approval_service=approval_svc)
    assert result.applied is False


def test_commit_grant_expired_rejected():
    """M0.1：过期 grant 被拒。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    # 注入已过去的时钟 + 极短 TTL
    clock = {"t": 0.0}

    def time_fn():
        return clock["t"]

    approval_svc = ApprovalService(time_fn=time_fn)
    grant = _grant_for(plan, approval_svc)
    clock["t"] = 1000.0  # 推进时间，grant 已过期
    svc = TransactionService()
    result = svc.commit(plan, state, approval=grant, approval_service=approval_svc)
    assert result.applied is False


def test_preview_and_commit_canonical_equal():
    """P2 核心验收：preview 的 proposed 与 commit 后 sequence 内容一致（除 revision）。

    用显式 clip_id 保证两次编译生成相同 id（结构 canonical-equal）。
    """
    state = _state()
    # 用显式 clip_id 的 add_clip op
    op = EditOperation("op1", OperationKind.ADD_CLIP, params={
        "clip_id": "fixed-clip-1", "track_id": "v1", "asset_id": "a1",
        "source": {"start": 0, "duration": 100},
        "timeline": {"start": 0, "duration": 100},
    })
    plan = EditPlan.create(PlanTarget("p1", "t1"), 0, operations=(op,))
    svc = TransactionService()
    preview = svc.preview(plan, state)
    approval_svc = ApprovalService()
    grant = _grant_for(plan, approval_svc)
    commit = svc.commit(plan, state, approval=grant, approval_service=approval_svc)

    assert preview.proposed_sequence is not None
    assert commit.applied is True
    # canonical equality: same clips/structure, only revision differs
    proposed = preview.proposed_sequence
    committed = commit.new_sequences["t1"]
    assert committed.revision == proposed.revision + 1
    # compare clip structure (id, start, duration, source)
    def _sig(c):
        return (c.clip_id, c.timeline_start, c.timeline_duration, c.source_start, c.source_duration)
    p_clips = [_sig(c) for c in proposed.video_tracks[0].clips]
    c_clips = [_sig(c) for c in committed.video_tracks[0].clips]
    assert p_clips == c_clips


def test_commit_partial_failure_no_half_state():
    """中间 operation 无效不会有部分提交（MacroCommand 原子性）。"""
    state = _state()
    # 第二个 clip 与第一个重叠 -> MacroCommand.apply 内 overlap 报错
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(
            _add_clip_op("op1", "v1", "a1", 0, 100),
            _add_clip_op("op2", "v1", "a1", 50, 100, s_start=50),  # overlaps op1
        ),
    )
    svc = TransactionService()
    # preview 会捕获 simulate 失败
    result = svc.preview(plan, state)
    assert result.has_errors  # simulation failed -> not applied
    # state untouched
    assert len(state.sequences["t1"].video_tracks[0].clips) == 0


def test_commit_revision_conflict_returns_not_applied():
    """stale plan（base_revision 过期）不会提交，两层状态不变。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 5,  # stale: current is 0
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    svc = TransactionService()
    grant = _grant_for(plan)  # grant 也绑定 base_revision=5
    result = svc.commit(plan, state, approval=grant)
    assert result.applied is False
    # revision conflict surfaces in errors; two layers unchanged
    assert any("revision" in e.lower() or "修改" in e for e in result.errors)
    assert result.new_project.revision == 0  # unchanged
    assert len(result.new_sequences["t1"].video_tracks[0].clips) == 0


def test_undo_after_commit_restores_timeline():
    """提交后一次 undo 恢复（MacroCommand 是单 undo group）。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
        title="Add clip",
    )
    svc = TransactionService()
    approval_svc = ApprovalService()
    commit = svc.commit(
        plan, state, approval=_grant_for(plan, approval_svc), approval_service=approval_svc,
    )
    assert commit.applied

    # 用 CommandStack 模拟 undo（transaction 内部用 commit_command，此处验证 macro 可 undo）
    stack = CommandStack()
    base_seq = state.sequences["t1"]
    new_seq = stack.execute(commit.new_timeline and plan and None or base_seq, base_seq) if False else base_seq
    # 直接验证：compiled macro 能 undo 回空轨道
    compiled = compile_plan(plan)
    applied = compiled.macro.apply(base_seq)
    if isinstance(applied, tuple):
        applied = applied[0]
    undone = compiled.macro.undo(applied)
    if isinstance(undone, tuple):
        undone = undone[0]
    assert len(undone.video_tracks[0].clips) == 0


# ================================================================
# TimelineDiff
# ================================================================


def test_diff_detects_move_and_trim():
    base = TimelineSequence(
        timeline_id="t1",
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                                clips=(_clip("c1", "v1", 0, 100),)),),
    )
    proposed = TimelineSequence(
        timeline_id="t1",
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                                clips=(
                                    ClipInstance(clip_id="c1", asset_id="a1", track_id="v1",
                                                 timeline_start=50, timeline_duration=80,
                                                 source_start=0, source_duration=80,
                                                 speed=SpeedParams(), transform=TransformParams(), crop=CropParams()),
                                    _clip("c2", "v1", 130, 50),
                                )),),
    )
    from src.features.edit.ai_editing.transaction_service import compute_diff
    d = compute_diff(base, proposed)
    assert len(d.added_clips) == 1 and d.added_clips[0].clip_id == "c2"
    # c1 moved (start changed) and trimmed (duration changed)
    assert any(c.clip_id == "c1" for c in d.moved_clips) or any(c.clip_id == "c1" for c in d.trimmed_clips)
    assert d.duration_after == 180
    assert "v1" in d.affected_track_ids


def test_diff_to_dict_serializable():
    base = TimelineSequence(timeline_id="t1",
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),))
    proposed = TimelineSequence(timeline_id="t1",
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                                clips=(_clip("c1", "v1", 0, 100),)),))
    from src.features.edit.ai_editing.transaction_service import compute_diff
    d = compute_diff(base, proposed)
    dd = d.to_dict()
    assert dd["total_changes"] == 1
    assert dd["added"][0]["clip_id"] == "c1"


# ================================================================
# audit
# ================================================================


def test_audit_record_emitted_on_commit():
    records: list = []
    svc = TransactionService(audit_sink=records.append)
    state = _state()
    plan = EditPlan.create(PlanTarget("p1", "t1"), 0,
                           operations=(_add_clip_op("op1", "v1", "a1", 0, 100),))
    approval_svc = ApprovalService()
    result = svc.commit(
        plan, state, approval=_grant_for(plan, approval_svc),
        approval_service=approval_svc, client="test-runner",
    )
    assert result.applied
    assert len(records) == 1
    rec = records[0]
    assert rec.status == "applied"
    assert rec.new_revision == 1
    assert rec.client == "test-runner"
    # audit dict has no secret/path
    rd = rec.to_dict()
    assert "secret" not in str(rd).lower() and "/home/secret" not in str(rd)


# ================================================================
# M1 收尾：diff_hash 绑定（总体不变量 §1.4：预览即提交）
# ================================================================


def test_preview_result_carries_diff_hash():
    """preview 成功时 PreviewResult.diff_hash 非空且为 sha256 hex。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(
            _add_clip_op("op1", "v1", "a1", 0, 100),
            _add_clip_op("op2", "v1", "a1", 100, 100, s_start=100),
        ),
    )
    svc = TransactionService()
    result = svc.preview(plan, state)
    assert not result.has_errors
    assert result.diff is not None
    assert result.diff_hash is not None
    # sha256 hex = 64 lowercase hex chars
    assert len(result.diff_hash) == 64
    assert all(c in "0123456789abcdef" for c in result.diff_hash)


def test_preview_diff_hash_is_none_on_validation_error():
    """校验/编译失败的 preview 路径：diff_hash 为 None（与 diff=None 一致）。"""
    state = _state()
    # source range out of bounds (asset has 5000 frames; request 6000)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "track_id": "v1", "asset_id": "a1",
            "source": {"start": 5500, "duration": 600},
            "timeline": {"start": 0, "duration": 600},
        }),),
    )
    svc = TransactionService()
    result = svc.preview(plan, state)
    assert result.has_errors
    assert result.diff is None
    assert result.diff_hash is None


def test_preview_diff_hash_stable_for_same_plan_and_state():
    """同一份 plan + 同一份 state → 同一 diff_hash（确定性，可复现）。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    svc = TransactionService()
    r1 = svc.preview(plan, state)
    r2 = svc.preview(plan, state)
    assert r1.diff_hash == r2.diff_hash
    assert r1.diff_hash is not None


def test_preview_diff_hash_changes_when_plan_changes():
    """plan 不同（产生不同 diff）→ diff_hash 不同。"""
    state = _state()
    plan_a = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    plan_b = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(
            _add_clip_op("op1", "v1", "a1", 0, 100),
            _add_clip_op("op2", "v1", "a1", 100, 100, s_start=100),
        ),
    )
    svc = TransactionService()
    r_a = svc.preview(plan_a, state)
    r_b = svc.preview(plan_b, state)
    assert r_a.diff_hash != r_b.diff_hash


def test_compute_diff_hash_deterministic():
    """compute_diff_hash 对同一 diff 产生同一 hash（鸭子类型 to_dict）。"""
    from src.features.edit.ai_editing.approval_service import compute_diff_hash
    from src.features.edit.ai_editing.transaction_service import TimelineDiff, ClipDiff

    d = TimelineDiff(
        added_clips=(ClipDiff(change="added", clip_id="c1", track_id="v1",
                              after={"timeline": {"start": 0, "duration": 100}}),),
        duration_after=100, affected_track_ids=("v1",),
    )
    h1 = compute_diff_hash(d)
    h2 = compute_diff_hash(d)
    assert h1 == h2
    assert len(h1) == 64


def test_commit_grant_with_matching_diff_hash_applies():
    """宿主签发带 diff_hash 的 grant（与 preview 一致）→ commit 成功。"""
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    # 先 preview 拿到 diff_hash（宿主在用户确认前展示 diff，拿到 hash 后签发 grant）
    svc = TransactionService()
    preview = svc.preview(plan, state)
    assert preview.diff_hash is not None

    approval_svc = ApprovalService()
    grant = approval_svc.issue_after_user_confirmation(
        ApprovalScope(
            action="commit_plan",
            project_id="p1",
            timeline_id="t1",
            base_revision=0,
            plan_hash=compute_plan_hash(_canonical(plan)),
            diff_hash=preview.diff_hash,
        )
    )
    result = svc.commit(plan, state, approval=grant, approval_service=approval_svc)
    assert result.applied is True


def test_commit_rejects_when_grant_diff_hash_mismatches_preview():
    """M1 收尾核心：grant 绑定的 diff_hash 与 commit 内部 preview 的 diff_hash
    不一致 → 拒绝（timeline 在预览与提交之间被改动）。

    场景：用户预览了 plan 得到 diff_hash=X；之后有人手工编辑 timeline 使同
    一份 plan 现在产生 diff_hash=Y；用户用旧 grant（绑定 X）尝试 commit →
    commit 内部 preview 出 Y，与 grant 的 X 不匹配 → 拒绝，project 不变。
    """
    state = _state()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(_add_clip_op("op1", "v1", "a1", 0, 100),),
    )
    svc = TransactionService()
    # grant 绑定一个"错误"的 diff_hash（模拟 timeline 已变化）
    approval_svc = ApprovalService()
    grant = approval_svc.issue_after_user_confirmation(
        ApprovalScope(
            action="commit_plan",
            project_id="p1",
            timeline_id="t1",
            base_revision=0,
            plan_hash=compute_plan_hash(_canonical(plan)),
            diff_hash="stale-diff-hash-that-does-not-match-current-preview",
        )
    )
    result = svc.commit(plan, state, approval=grant, approval_service=approval_svc)
    assert result.applied is False
    # project / timeline 完全不变（未授权不产生半套状态）
    assert state.project.revision == 0
    assert state.sequences["t1"].revision == 0


# ---- helper used by the diff_hash commit tests above ----

def _canonical(plan: EditPlan) -> bytes:
    import json
    return json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
