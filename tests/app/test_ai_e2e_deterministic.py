"""TAEP M1.2/M2.2 — AI 剪辑端到端（确定性 planner）+ 组合根契约测试。

验收（审查 M1.2/M1.3/M2.2）：

- E2E：submit 需求 → plan variants → 选 variant → validate（真实 preview）→
  审批 → 经真实 CommandStack 提交 → 一次 undo 整体恢复 → ``_current_timeline_sequences``
  同步。
- 组合根契约：``_setup_ai_editing`` 装配后 controller/service/bridge/approval 非空；
  ``_teardown_ai_editing`` 后全部清空 + grants 撤销。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.ai_editing_mixin import AiEditingMixin
from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
from src.features.edit.ai_editing.approval_service import ApprovalService
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
from src.features.edit.ai_editing.models import EditPlan
from src.features.edit.ai_editing.plan_compiler import compile_plan
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.ai_editing.transaction_service import (
    PreviewResult,
    _expected_commit_scope,
)
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    TimelineSequenceRef,
)
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# ================================================================
# Fixtures
# ================================================================


def _asset() -> MediaAsset:
    return MediaAsset(
        asset_id="a1", path="/x.mp4", media_type="video", display_name="Clip A",
        metadata={"duration_frames": 5000},
    )


def _state() -> ProjectSequences:
    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0, width=1920, height=1080,
        sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="P", assets={"a1": _asset()}, timelines={"t1": ref},
        root_bin_id="root", active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": seq})


def _index() -> SemanticIndex:
    idx = SemanticIndex()
    for i, (s, d, t) in enumerate([(0, 108, "开场"), (108, 432, "卖点"), (540, 180, "号召")]):
        idx.add(SemanticSegment(
            segment_id=f"t{i}", asset_id="a1", source_range=FrameRange(s, d),
            kind=SegmentKind.TRANSCRIPT, payload={"text": t}, confidence=0.9,
        ))
    return idx


class _Host:
    """EditSessionHost，持有可被 service 读取的 state。"""

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

    def approval_service(self):
        return None


@pytest.fixture
def service():
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
    svc.start_session(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        active_profile_id="prof", profile_connected=True,
    )
    svc.submit_request("30 秒竖版", brief_id="b1")
    yield svc, state
    EditSession.unbind()


# ================================================================
# E2E：submit → plan → select → validate → approve → commit → undo
# ================================================================


def test_e2e_ai_edit_lands_on_command_stack_and_is_undoable(service):
    """M2.2 E2E：AI 方案经真实 CommandStack 提交，一次 undo 整体恢复。

    这条链复刻主窗口里的真实路径（service 产 plan + preview，bridge 把 macro
    推入真实 CommandStack）。区别只在 host bridge 用一个带真实 stack 的 fake
    TimelineView（避免构造完整 MainWindow / GL）。
    """
    svc, state = service
    # 1. 规划 variants（确定性 planner，无云）
    variants = svc.plan_variants(_index(), svc.config)
    assert len(variants) >= 2
    chosen = variants[0]
    plan = svc.plan_repository.get_plan(chosen.edit_plan_ref)
    assert plan is not None

    # 2. 选择 + 真实 preview（M2.3）
    svc.select_variant(chosen.variant_id)
    preview = svc.validate_selected(chosen.variant_id, state=state)
    assert isinstance(preview, PreviewResult)
    assert not preview.has_errors
    assert preview.diff is not None
    assert preview.diff.added_clips  # 至少加了一个 clip

    # 3. 构造一个带真实 CommandStack 的 fake TimelineView（模拟 MainWindow 的桥接）
    seq = state.sequences["t1"]
    stack = CommandStack()

    class _FakeView:
        def __init__(self_, tl):
            self_._timeline = tl
            self_.emits = []

        def execute_command(self_, command):
            new = stack.execute(command, self_._timeline)
            if isinstance(new, tuple):
                new = new[0]
            self_._timeline = new
            self_.emits.append(new)

    view = _FakeView(seq)

    class _FakeMW:
        timeline_panel = type("P", (), {"timeline_view": view})()

    # AUD-2105：最小 fake 不建模 MainWindow/feature_flags，显式声明
    # legacy_no_policy（本测试聚焦 M2.2 提交/undo 闭环）。
    bridge = MainWindowAiEditHostBridge(
        _FakeMW(), svc, ApprovalService(), legacy_no_policy=True,
    )

    # 4. 模拟审批：用 bridge 的 approval_service 签发绑定 plan hash 的 grant
    grant = bridge.approval_service().issue_after_user_confirmation(
        _expected_commit_scope(plan), issued_by="test",
    )
    # consume（原子校验作用域 + 一次性）
    bridge.approval_service().consume(grant.grant_id, _expected_commit_scope(plan))

    # 5. M2.2：经 bridge.commit_ai_command 推入真实 CommandStack
    compiled = compile_plan(plan)
    new_tl = bridge.commit_ai_command(
        plan.target.timeline_id, compiled.macro,
        undo_label=compiled.undo_label,
    )
    assert new_tl is not None
    # timeline 真实变化
    assert len(new_tl.video_tracks[0].clips) >= 1
    # CommandStack 可 undo
    assert stack.can_undo() is True
    # 走了 execute_command（emits 非空，证明是界面刷新链）
    assert len(view.emits) >= 1

    # 6. 一次 undo 整体恢复（M2.2 核心验收）
    undone = stack.undo(view._timeline)
    if isinstance(undone, tuple):
        undone = undone[0]
    assert undone is not None
    assert len(undone.video_tracks[0].clips) == 0  # 回到提交前


def test_e2e_grant_cannot_replay_after_commit(service):
    """M2.2 + M0.1：commit 消费 grant 后，重放被拒。"""
    svc, state = service
    variants = svc.plan_variants(_index(), svc.config)
    plan = svc.plan_repository.get_plan(variants[0].edit_plan_ref)
    bridge = MainWindowAiEditHostBridge(
        type("_MW", (), {"timeline_panel": None})(), svc, ApprovalService(),
    )
    g = bridge.approval_service().issue_after_user_confirmation(
        _expected_commit_scope(plan), issued_by="test",
    )
    bridge.approval_service().consume(g.grant_id, _expected_commit_scope(plan))
    # 重放 → GrantNotFound
    from src.features.edit.ai_editing.approval_service import GrantNotFound
    with pytest.raises(GrantNotFound):
        bridge.approval_service().consume(g.grant_id, _expected_commit_scope(plan))


# ================================================================
# 组合根契约（AiEditingMixin）
# ================================================================


class _FakeMWForMixin(AiEditingMixin):
    """最小 host，验证 mixin 的 setup/teardown 行为（不构造真实 MainWindow）。"""

    def __init__(self):
        self._init_ai_editing_state()
        self._edit_available = True
        self.ai_edit_assistant_panel = None  # 无面板 → setup 应 no-op
        self._current_edit_project = None
        # config_manager 桩
        self._cfg = {}

        class _Cfg:
            def get(self_, k):
                return self._cfg.get(k)

            def set(self_, k, v, auto_save=True):
                self._cfg[k] = v

        self.config_manager = _Cfg()


def test_mixin_init_state_is_none():
    mw = _FakeMWForMixin()
    assert mw._ai_edit_controller is None
    assert mw._ai_assistant_service is None
    assert mw._ai_host_bridge is None
    assert mw._ai_approval_service is None


def test_mixin_setup_noop_without_panel():
    """无 dock 面板时 setup 不抛、保持 None。"""
    mw = _FakeMWForMixin()
    mw._setup_ai_editing()  # panel is None
    assert mw._ai_edit_controller is None


def test_mixin_setup_noop_when_edit_unavailable():
    mw = _FakeMWForMixin()
    mw._edit_available = False
    mw.ai_edit_assistant_panel = object()  # 有面板但 edit 不可用
    mw._setup_ai_editing()
    assert mw._ai_edit_controller is None


def test_mixin_resolves_provisional_timeline_for_new_project_ai_session():
    """空项目已有 UI provisional timeline 时，AI 不应继续显示 NO_PROJECT。"""
    from types import SimpleNamespace

    project = EditProject.create("New Project")
    mw = _FakeMWForMixin()
    mw.timeline_panel = SimpleNamespace(
        get_timeline_view=lambda: SimpleNamespace(
            _timeline=TimelineSequence(timeline_id="provisional-timeline")
        )
    )

    assert mw._resolve_ai_timeline_id(project) == "provisional-timeline"


def test_mixin_setup_starts_session_for_current_project(qapp, monkeypatch):
    """M1.2：组合根完成后面板可安全提交请求，不会读取未初始化 config。"""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
    from src.features.edit.ai_editing.assistant_events import AssistantState

    # The composition root intentionally reads user-level profiles in the real
    # app.  Keep this contract test deterministic and independent of the
    # developer machine's ~/.topos/ai_profiles.json.
    from src.features.edit.ai_editing.provider_profiles import ProfileRegistry
    monkeypatch.setattr(ProfileRegistry, "load_from_user_file", lambda self: False)

    mw = _FakeMWForMixin()
    state = _state()
    mw._current_edit_project = state.project
    mw._current_timeline_sequences = state.sequences
    mw.ai_edit_assistant_panel = AiEditAssistantPanel()

    mw._setup_ai_editing()

    service = mw._ai_assistant_service
    assert service is not None
    assert service.config.project_id == "p1"
    assert service.config.timeline_id == "t1"
    assert service.config.asset_id == "a1"
    assert service.state == AssistantState.NO_PROFILE

    mw._teardown_ai_editing()


def test_mixin_teardown_is_safe_when_unsetup():
    """未装配时 teardown 不抛。"""
    mw = _FakeMWForMixin()
    mw._teardown_ai_editing()  # 不应抛


def test_mixin_teardown_revokes_grants_for_project():
    """teardown 撤销该工程的 grants。"""
    mw = _FakeMWForMixin()
    # 手动注入一个 approval_service + 一条 grant
    mw._ai_approval_service = ApprovalService()
    mw._ai_host_bridge = None
    # 构造一个带 project_id 的假 project
    mw._current_edit_project = type("P", (), {"project_id": "p1"})()
    scope = type(
        "S", (), {
            "action": "commit_plan", "project_id": "p1",
            "timeline_id": "t1", "base_revision": 0, "plan_hash": "h",
        },
    )()
    from src.features.edit.ai_editing.approval_service import ApprovalScope
    real_scope = ApprovalScope(
        action="commit_plan", project_id="p1", timeline_id="t1",
        base_revision=0, plan_hash="h",
    )
    mw._ai_approval_service.issue_after_user_confirmation(real_scope)
    assert mw._ai_approval_service.active_count() == 1
    mw._teardown_ai_editing()
    assert mw._ai_approval_service is None  # 清空


def test_mixin_teardown_uses_previous_session_project_for_grant_revocation():
    """工程已切到新 project 时，仍撤销旧 AI 会话签发的 grants。"""
    mw = _FakeMWForMixin()
    service = type(
        "Service", (), {"config": AssistantConfig(project_id="old-project")}
    )()
    approval = ApprovalService()
    from src.features.edit.ai_editing.approval_service import ApprovalScope

    approval.issue_after_user_confirmation(ApprovalScope(
        action="commit_plan", project_id="old-project", timeline_id="t1",
        base_revision=0, plan_hash="h",
    ))

    class _Bridge:
        def __init__(self):
            self.revoked_project_id = None

        def teardown_for_project(self, project_id):
            self.revoked_project_id = project_id
            approval.revoke_for_project(project_id)

    bridge = _Bridge()
    mw._ai_assistant_service = service
    mw._ai_approval_service = approval
    mw._ai_host_bridge = bridge
    mw._current_edit_project = type("P", (), {"project_id": "new-project"})()

    mw._teardown_ai_editing()

    assert bridge.revoked_project_id == "old-project"
    assert approval.active_count() == 0
