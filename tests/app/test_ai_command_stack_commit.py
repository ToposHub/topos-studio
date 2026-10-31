"""TAEP M2.2 — AI 提交进入真实 CommandStack 契约测试。

验收（审查 M2.2 / 总体不变量 §1.5：一个 AI 提交 = 一个 Undo 单元）：

- AI 的 MacroCommand 经 ``bridge.commit_ai_command`` 推入 ``TimelineView`` 的真实
  ``CommandStack``；``can_undo()`` 为 true。
- 一次 ``undo`` 恢复提交前的完整 timeline（整体撤销，不是逐 operation）。
- ``timeline_changed`` 被 emit（证明走的是界面那条刷新链，不是绕过 stack 的
  ``commit_command``）。
- 非 AI 的手动编辑与 AI 编辑共享同一 stack，Ctrl+Z 顺序一致。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.app

from src.features.edit.ai_editing.plan_compiler import compile_plan
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanTarget,
)
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


def _seq(clip_count: int = 0) -> TimelineSequence:
    clips = tuple(
        ClipInstance(
            clip_id=f"c{i}", asset_id="a1", track_id="v1",
            timeline_start=i * 100, timeline_duration=100,
            source_start=i * 100, source_duration=100,
            speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
        )
        for i in range(clip_count)
    )
    return TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=clips),),
    )


def _add_clip_plan(timeline_id: str = "t1", clip_id: str = "ai-clip-1") -> EditPlan:
    """一个最小 AI plan：add_clip。"""
    return EditPlan.create(
        PlanTarget("p1", timeline_id), base_revision=0,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "clip_id": clip_id, "track_id": "v1", "asset_id": "a1",
            "source": {"start": 0, "duration": 100},
            "timeline": {"start": 0, "duration": 100},
        }),),
        title="AI rough cut",
    )


# ================================================================
# CommandStack 直接支持 MacroCommand（M2.2 前提）
# ================================================================


def test_command_stack_supports_macro_single_undo():
    """CommandStack.execute 接收 MacroCommand，单步 undo 恢复整体。"""
    seq = _seq(clip_count=0)
    plan = _add_clip_plan()
    compiled = compile_plan(plan)
    stack = CommandStack()

    new_seq = stack.execute(compiled.macro, seq)
    if isinstance(new_seq, tuple):
        new_seq = new_seq[0]
    assert len(new_seq.video_tracks[0].clips) == 1
    assert stack.can_undo() is True

    undone = stack.undo(new_seq)
    if isinstance(undone, tuple):
        undone = undone[0]
    # 一次 undo → 整个 AI 编辑恢复（0 clips）
    assert undone is not None
    assert len(undone.video_tracks[0].clips) == 0


def test_multi_op_ai_plan_is_single_undo_unit():
    """多 operation 的 AI plan 仍是单个 undo 条目。"""
    seq = _seq(clip_count=0)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(
            EditOperation("op1", OperationKind.ADD_CLIP, params={
                "clip_id": "a1", "track_id": "v1", "asset_id": "a1",
                "source": {"start": 0, "duration": 100},
                "timeline": {"start": 0, "duration": 100},
            }),
            EditOperation("op2", OperationKind.ADD_CLIP, params={
                "clip_id": "a2", "track_id": "v1", "asset_id": "a1",
                "source": {"start": 100, "duration": 100},
                "timeline": {"start": 100, "duration": 100},
            }),
        ),
        title="two-clip plan",
    )
    compiled = compile_plan(plan)
    stack = CommandStack()
    new_seq = stack.execute(compiled.macro, seq)
    if isinstance(new_seq, tuple):
        new_seq = new_seq[0]
    assert len(new_seq.video_tracks[0].clips) == 2
    # 一次 undo 恢复两个 clip
    undone = stack.undo(new_seq)
    if isinstance(undone, tuple):
        undone = undone[0]
    assert len(undone.video_tracks[0].clips) == 0


# ================================================================
# bridge.commit_ai_command 走真实 TimelineView.execute_command
# ================================================================


class _FakeTimelineView:
    """最小 TimelineView 桩：持有真实 CommandStack + _timeline，emit timeline_changed。"""

    def __init__(self, timeline):
        self._timeline = timeline
        self._command_stack = CommandStack()
        self.timeline_changed_emits = []
        # 模拟 pyqtSignal：connect/emit
        self._tc_callbacks = []

    @property
    def timeline_changed(self):
        outer = self

        class _Sig:
            def connect(self, cb):
                outer._tc_callbacks.append(cb)

            def emit(self, tl):
                outer.timeline_changed_emits.append(tl)
                for cb in outer._tc_callbacks:
                    cb(tl)

        return _Sig()

    def execute_command(self, command):
        """复刻真实 TimelineView._execute_command 的核心：stack.execute + emit。"""
        if self._timeline is None:
            return
        old = self._timeline
        new = self._command_stack.execute(command, self._timeline)
        if new is not None:
            if isinstance(new, tuple):
                new = new[0]
            self._timeline = new
            self.timeline_changed.emit(new)

    def undo(self):
        new = self._command_stack.undo(self._timeline)
        if new is not None:
            if isinstance(new, tuple):
                new = new[0]
            self._timeline = new
            self.timeline_changed.emit(new)


class _FakeTimelinePanel:
    def __init__(self, view):
        self.timeline_view = view


class _FakeMainWindow:
    """最小 MainWindow 桩，仅含 bridge 需要的属性。"""

    def __init__(self, view):
        self.timeline_panel = _FakeTimelinePanel(view)


def test_bridge_commit_ai_command_uses_real_command_stack():
    """M2.2：bridge.commit_ai_command 把 macro 推入真实 CommandStack。

    - 提交后 view._timeline 真实变化；
    - can_undo 为 true；
    - timeline_changed 被 emit 一次（证明走界面刷新链，非 commit_command）。
    """
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
    from src.features.edit.ai_editing.approval_service import ApprovalService

    seq = _seq(clip_count=0)
    view = _FakeTimelineView(seq)
    mw = _FakeMainWindow(view)

    # AssistantService 只为 get_plan_by_id 转发；这里用桩。
    class _SvcStub:
        plan_repository = None

    # AUD-2105：最小 fake 宿主不建模 MainWindow/feature_flags，显式声明
    # legacy_no_policy（隐式 fail-open 已移除；本测试聚焦 CommandStack 提交链）。
    bridge = MainWindowAiEditHostBridge(
        mw, _SvcStub(), ApprovalService(), legacy_no_policy=True,
    )
    plan = _add_clip_plan()
    compiled = compile_plan(plan)

    new_tl = bridge.commit_ai_command(
        "t1", compiled.macro, undo_label=compiled.undo_label,
    )
    assert new_tl is not None
    assert len(new_tl.video_tracks[0].clips) == 1
    # 真实 CommandStack 记录了 undo
    assert view._command_stack.can_undo() is True
    # timeline_changed 被 emit（走界面刷新链）
    assert len(view.timeline_changed_emits) >= 1


def test_bridge_commit_then_undo_restores_timeline():
    """M2.2：bridge 提交后，view 的 undo（模拟 Ctrl+Z）恢复完整 timeline。"""
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
    from src.features.edit.ai_editing.approval_service import ApprovalService

    seq = _seq(clip_count=0)
    view = _FakeTimelineView(seq)
    mw = _FakeMainWindow(view)

    class _SvcStub:
        plan_repository = None

    # AUD-2105：显式声明 legacy_no_policy（最小 fake 不建模 feature_flags）。
    bridge = MainWindowAiEditHostBridge(
        mw, _SvcStub(), ApprovalService(), legacy_no_policy=True,
    )
    plan = _add_clip_plan()
    compiled = compile_plan(plan)
    bridge.commit_ai_command("t1", compiled.macro, undo_label=compiled.undo_label)

    assert len(view._timeline.video_tracks[0].clips) == 1
    # 模拟用户在 timeline 按 Ctrl+Z
    view.undo()
    # 完整恢复：AI 编辑整体撤销
    assert len(view._timeline.video_tracks[0].clips) == 0


def test_bridge_commit_wrong_timeline_returns_none():
    """目标 timeline 与活动不一致 → 返回 None（不误提交）。"""
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
    from src.features.edit.ai_editing.approval_service import ApprovalService

    seq = _seq(clip_count=0)  # timeline_id = "t1"
    view = _FakeTimelineView(seq)
    mw = _FakeMainWindow(view)

    class _SvcStub:
        plan_repository = None

    bridge = MainWindowAiEditHostBridge(mw, _SvcStub(), ApprovalService())
    plan = _add_clip_plan(timeline_id="other")  # 不同 timeline
    compiled = compile_plan(plan)
    result = bridge.commit_ai_command("other", compiled.macro, undo_label="x")
    assert result is None
    # 未提交
    assert view._command_stack.can_undo() is False


def test_bridge_commit_missing_view_returns_none():
    """无 timeline view（工程未加载）→ None。"""
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge
    from src.features.edit.ai_editing.approval_service import ApprovalService

    class _MWNoPanel:
        timeline_panel = None

    class _SvcStub:
        plan_repository = None

    bridge = MainWindowAiEditHostBridge(_MWNoPanel(), _SvcStub(), ApprovalService())
    plan = _add_clip_plan()
    compiled = compile_plan(plan)
    assert bridge.commit_ai_command("t1", compiled.macro, undo_label="x") is None
