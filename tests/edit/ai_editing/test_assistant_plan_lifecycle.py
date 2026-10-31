"""TAEP M2.1 + M2.3 — AssistantService plan lifecycle 集成测试。

验收（审查 M2.1 / M2.3）：

- plan_variants 后，PlanRepository 含每个 variant 的 immutable plan。
- validate_selected 从仓库取 plan，调用 TransactionService.preview，返回完整
  PreviewResult（含 diff），不再返回"scores + plan ref"占位。
- plan 不在仓库（如过期）→ FAILED + 明确错误。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.assistant_service import (
    AssistantConfig,
    AssistantService,
)
from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.edit_api import EditSession
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.editorial_models import (
    DurationTarget,
    EditorialBrief,
    FrameRange,
    Platform,
    SegmentKind,
    SemanticSegment,
)
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    TimelineSequenceRef,
)
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


class _Host:
    """最小 EditSessionHost，提供 state/manifest。"""

    def __init__(self, state):
        self._state = state

    def get_state(self):
        return self._state

    def get_manifest(self):
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        return build_manifest()

    def on_committed(self, result):
        pass

    def request_user_approval(self, plan, diff):
        return None


def _state():
    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/x.mp4", media_type="video", display_name="A",
        metadata={"duration_frames": 5000},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0, width=1920, height=1080,
        sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="P", assets={"a1": asset}, timelines={"t1": ref},
        root_bin_id="root", active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": seq})


@pytest.fixture
def assistant():
    EditSession.unbind()
    state = _state()
    session = EditSession.bind(_Host(state))
    bs = BriefService()
    bs.save(EditorialBrief(
        brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
        target_duration=DurationTarget(720, 648, 792),
        required_beats=("hook", "product_value", "call_to_action"),
    ))
    svc = AssistantService(session, brief_service=bs)
    # 语义索引
    idx = SemanticIndex()
    for i, (s, d, t) in enumerate([
        (0, 108, "开场"), (108, 432, "卖点"), (540, 180, "行动号召"),
    ]):
        idx.add(SemanticSegment(
            segment_id=f"t{i}", asset_id="a1", source_range=FrameRange(s, d),
            kind=SegmentKind.TRANSCRIPT, payload={"text": t}, confidence=0.9,
        ))
    svc.start_session(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        active_profile_id="prof", profile_connected=True,
    )
    # 模拟 controller 流程：提交需求 → 设置 active_brief_id（planner 据此读 brief）
    svc.submit_request("30 秒竖版短视频", brief_id="b1")
    yield svc, idx, state
    EditSession.unbind()


# ================================================================
# M2.1：plan_variants 把 plan 存入仓库
# ================================================================


def test_plan_variants_stores_plans_in_repository(assistant):
    svc, idx, state = assistant
    variants = svc.plan_variants(idx, svc.config)
    assert len(variants) >= 2
    repo = svc.plan_repository
    # 每个 variant 的 edit_plan_ref 都能在仓库解析到 immutable plan
    for v in variants:
        assert v.edit_plan_ref is not None
        plan = repo.get_plan(v.edit_plan_ref)
        assert plan is not None
        assert plan.plan_id == v.edit_plan_ref
    assert repo.count() >= 2


# ================================================================
# M1.3：显式批准 Brief 后才规划（service 级）
# ================================================================


def test_submit_request_stops_at_brief_approval_not_planning(assistant):
    """M1.3：submit_request 后停在 AWAITING_BRIEF_APPROVAL，未自动规划。

    旧实现会把 submit→propose→apply→plan 同步跑完，AWAITING_BRIEF_APPROVAL 被覆盖。
    现在 service 只负责到 propose；planning 由 controller 在用户批准后单独触发。
    """
    from src.features.edit.ai_editing.assistant_events import AssistantState
    from src.features.edit.ai_editing.brief_service import BriefPatch

    svc, idx, state = assistant
    # submit_request（不调 plan_variants）
    svc.submit_request("30 秒竖版", brief_id="b1")
    patch = svc.propose_brief_patch("b1", "30 秒竖版")
    # 停在 AWAITING_BRIEF_APPROVAL
    assert svc.state == AssistantState.AWAITING_BRIEF_APPROVAL
    # 未规划
    assert len(svc._variants) == 0

    # 用户显式批准 brief 后才规划
    preview = svc.preview_brief_patch("b1", patch)
    approved = preview.with_revision_bumped()
    svc.apply_user_brief("b1", approved)
    svc.plan_variants(idx, svc.config)
    assert len(svc._variants) >= 2


def test_plan_repository_plan_hash_matches_content(assistant):
    svc, idx, state = assistant
    variants = svc.plan_variants(idx, svc.config)
    repo = svc.plan_repository
    for v in variants:
        stored = repo.get(v.edit_plan_ref)
        # find_by_hash 能取回同一份
        assert repo.find_by_hash(stored.content_hash) is not None


# ================================================================
# M2.3：validate_selected 返回真实 PreviewResult
# ================================================================


def test_validate_selected_returns_real_preview_result(assistant):
    svc, idx, state = assistant
    variants = svc.plan_variants(idx, svc.config)
    vid = variants[0].variant_id
    svc.select_variant(vid)
    preview = svc.validate_selected(vid, state=state)
    # 不再是 dict 占位；而是真实 PreviewResult（含 diff）
    from src.features.edit.ai_editing.transaction_service import PreviewResult
    assert isinstance(preview, PreviewResult)
    assert not preview.has_errors
    assert preview.diff is not None
    assert preview.diff.added_clips  # 至少一个 add_clip
    # preview 绑定到仓库
    stored = svc.plan_repository.get(variants[0].edit_plan_ref)
    assert stored.preview is preview or stored.preview is not None


def test_validate_selected_missing_state_fails_clearly(assistant):
    """M2.3：缺 state → 明确 FAILED，不静默。"""
    svc, idx, _ = assistant
    variants = svc.plan_variants(idx, svc.config)
    vid = variants[0].variant_id
    result = svc.validate_selected(vid, state=None)
    assert result is None
    from src.features.edit.ai_editing.assistant_events import AssistantState
    assert svc.state == AssistantState.FAILED


def test_validate_selected_missing_plan_fails_clearly(assistant):
    """M2.3：variant 的 plan 已不在仓库（过期）→ FAILED + 明确错误。"""
    svc, idx, state = assistant
    variants = svc.plan_variants(idx, svc.config)
    vid = variants[0].variant_id
    # 清空仓库模拟"工程重开 plan 丢失"
    svc.plan_repository.clear()
    result = svc.validate_selected(vid, state=state)
    assert result is None
    from src.features.edit.ai_editing.assistant_events import AssistantState
    assert svc.state == AssistantState.FAILED
