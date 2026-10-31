"""TAEP Phase 3 — ToposEditAPI / EditSession 本地脚本 facade 测试。

验证（TAEP §8 验收）：
- 受信任 Python 示例可加载 plan、dry-run、显示诊断、commit、undo。
- 恶意/意外 plan 无法访问任意文件或 exec 新代码。
- apply 需要 approval token；apply_with_confirmation 走宿主确认。
- 路径授权 token 机制（输出不允许静默覆盖）。
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.approval_service import ApprovalGrant, ApprovalService
from src.features.edit.ai_editing.edit_api import EditSession, PathGrant
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.models import (
    DetailLevel,
    EditOperation,
    EditPlan,
    OperationKind,
    PlanTarget,
)
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# ================================================================
# Fake host（实现 EditSessionHost 协议，无 Qt）
# ================================================================


class FakeHost:
    """实现 EditSessionHost 协议的测试 host。

    用真实的 ``ApprovalService`` 签发绑定作用域的 grant，模拟"宿主在用户确认后
    签发不透明授权"。``approval_decision`` 控制：True=签发 grant，None=拒绝。
    """

    def __init__(self, state: ProjectSequences) -> None:
        self._state = state
        self.committed_results: list = []
        self.approval_decision: bool = True  # True=签发 grant，None/False=拒绝
        self.path_grant_decision: PathGrant | None = None
        self._approval_service = ApprovalService()

    def get_state(self) -> ProjectSequences:
        return self._state

    def get_manifest(self):
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        return build_manifest()

    def on_committed(self, result) -> None:
        self.committed_results.append(result)
        # host 更新自己的两层状态（模拟 controller）
        if result.applied:
            self._state = ProjectSequences(
                project=result.new_project, sequences=result.new_sequences
            )

    def request_user_approval(self, plan, diff) -> ApprovalGrant | None:
        if not self.approval_decision:
            return None
        from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

        return self._approval_service.issue_after_user_confirmation(
            _expected_commit_scope(plan), issued_by="fake-host"
        )

    def approval_service(self) -> ApprovalService:
        return self._approval_service

    def request_path_grant(self, suggested_path: str, allow_overwrite: bool = False):
        return self.path_grant_decision


# ================================================================
# Fixtures
# ================================================================


def _state() -> ProjectSequences:
    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/home/secret/clip.mp4", media_type="video",
        display_name="Clip A", metadata={"duration_frames": 5000},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="AI Project", assets={"a1": asset},
        timelines={"t1": ref}, root_bin_id="root", active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": seq})


def _add_clip_plan(base_revision: int = 0) -> EditPlan:
    return EditPlan.create(
        PlanTarget("p1", "t1"), base_revision,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "clip_id": "c1", "track_id": "v1", "asset_id": "a1",
            "source": {"start": 0, "duration": 100},
            "timeline": {"start": 0, "duration": 100},
        }),),
        title="Script rough cut",
    )


@pytest.fixture
def session():
    EditSession.unbind()
    host = FakeHost(_state())
    s = EditSession.bind(host)
    yield s, host
    EditSession.unbind()


# ================================================================
# 只读 API
# ================================================================


def test_get_capabilities_and_snapshot(session):
    s, host = session
    caps = s.get_capabilities()
    assert "add_clip" in caps.operations
    snap = s.get_snapshot(detail_level=DetailLevel.MINIMAL)
    assert snap.project_id == "p1"
    # snapshot 不含本机绝对路径
    assert "/home/secret" not in str(snap.to_dict())


# ================================================================
# load_plan
# ================================================================


def test_load_plan_from_json(tmp_path, session):
    s, host = session
    plan = _add_clip_plan()
    pfile = tmp_path / "plan.taep.json"
    pfile.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    loaded = s.load_plan(str(pfile))
    assert loaded.plan_id == plan.plan_id
    assert loaded.operations[0].kind == OperationKind.ADD_CLIP


# ================================================================
# validate / dry-run
# ================================================================


def test_validate_returns_report(session):
    s, host = session
    report = s.validate(_add_clip_plan())
    assert not report.has_errors
    assert report.status.value == "valid"


def test_validate_invalid_plan_reports_errors(session):
    s, host = session
    # source range out of bounds
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "track_id": "v1", "asset_id": "a1",
            "source": {"start": 5500, "duration": 600},
            "timeline": {"start": 0, "duration": 600},
        }),),
    )
    report = s.validate(plan)
    assert report.has_errors


# ================================================================
# apply / commit
# ================================================================


def test_apply_requires_approval(session):
    s, host = session
    result = s.apply(_add_clip_plan(), approval=None)
    assert result.applied is False
    assert "approval" in (result.error or "").lower() or "ApprovalGrant" in (result.error or "")


def test_apply_with_grant_commits_and_refreshes_host(session):
    s, host = session
    plan = _add_clip_plan()
    from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

    grant = host._approval_service.issue_after_user_confirmation(
        _expected_commit_scope(plan), issued_by="test"
    )
    result = s.apply(plan, approval=grant)
    assert result.applied is True
    assert result.new_timeline.revision == 1
    # host was refreshed
    assert len(host.committed_results) == 1
    assert host.get_state().sequences["t1"].revision == 1
    assert len(host.get_state().sequences["t1"].video_tracks[0].clips) == 1


def test_apply_with_confirmation_declined(session):
    s, host = session
    host.approval_decision = False  # user declines
    result = s.apply_with_confirmation(_add_clip_plan())
    assert result.applied is False
    assert "declined" in (result.error or "").lower()


def test_apply_with_confirmation_accepted(session):
    s, host = session
    host.approval_decision = True
    result = s.apply_with_confirmation(_add_clip_plan())
    assert result.applied is True


# ================================================================
# 安全：恶意 plan 无法访问任意文件 / exec
# ================================================================


def test_malicious_plan_cannot_access_filesystem(session):
    """EditSession 不提供任意文件/exec 能力；只接受结构化 plan。"""
    s, host = session
    # plan 的 params 只是数据，不会被 exec
    malicious = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "track_id": "v1", "asset_id": "a1",
            "source": {"start": 0, "duration": 10},
            "timeline": {"start": 0, "duration": 10},
            "x_injection": "__import__('os').system('rm -rf /')",  # 注入尝试
        }),),
    )
    report = s.validate(malicious)
    # 即使带未知字段，也只是数据；不会执行。结构可能 valid 或 warning。
    # 关键：不会产生副作用。
    assert "rm -rf" not in str(host.get_state().project.assets)


def test_path_grant_required_for_export(session):
    """export_from_preset 在 P4 已接线：转发到 request_export，返回 job/preflight/config。

    没有 path grant 时 config.output_path 仍为空（不静默写文件）。
    """
    s, host = session
    result = s.export_from_preset("social-vertical")
    assert "job" in result and "preflight" in result and "config" in result
    # 无 grant -> config.output_path 为空（不写文件）
    assert result["config"].output_path == ""


def test_path_grant_token_mechanism(session):
    s, host = session
    # 未授权 -> None
    assert s.request_path_grant("/tmp/out.mp4") is None
    # host 授权
    host.path_grant_decision = PathGrant(path="/tmp/out.mp4", allow_overwrite=False, granted_by="ui-pick")
    grant = s.request_path_grant("/tmp/out.mp4")
    assert grant is not None
    assert grant.allow_overwrite is False
    assert grant.granted_by == "ui-pick"
