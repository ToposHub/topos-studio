"""W06：Position Lock 统一编辑策略（契约 D3）——footprint 级判定。

三层接入共用 ``core.position_lock`` 的同一判定：
- 命令层 dry-run：move/trim/roll/delete/ripple 拒绝；slip/纯新增允许；
  显式锁定子集只锁指定 clip；no-op 允许。
- ``TimelineView._execute_command``：锁开启时任何足迹变更命令被拒，
  零 revision/零 history；undo/redo 不受限。
- ``EditProjectService.commit_command(position_locked_clip_ids=...)``：
  AI/service 显式策略，拒绝时 applied=False 且两层状态原样。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.features.edit.core.commands import (
    AddClipCommand,
    DeleteClipCommand,
    MoveClipCommand,
    SlipClipCommand,
    TrimClipCommand,
)
from src.features.edit.core.position_lock import (
    FootprintChange,
    PositionLockViolation,
    evaluate_command_footprint,
    evaluate_command_footprint_safe,
    footprint_changes,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, start: int, duration: int = 40,
          track_id: str = "v1", source_start: int = 0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id="a", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=source_start, source_duration=duration,
    )


def _timeline() -> TimelineSequence:
    return TimelineSequence(
        timeline_id="lock", name="Lock", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0,
            clips=(_clip("c1", 0), _clip("c2", 60)),
        ), EditTrack("v2", "V2", "video", 1)),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


# ── footprint diff ────────────────────────────────────────────────────


def test_footprint_diff_categorizes_changes() -> None:
    before = _timeline()
    after = before.with_track_updated(before.video_tracks[0]._rebuild(clips=(
        # c1 moved +10；c2 被移除；新增 c3 不算违规
        _clip("c1", 10),
        _clip("c3", 200),
    )))
    changes = footprint_changes(before, after)
    kinds = {c.clip_id: c.kind for c in changes}
    assert kinds == {"c1": "start", "c2": "removed"}


def test_footprint_diff_detects_track_and_duration() -> None:
    before = _timeline()
    # c1 移到 v2；c2 缩短
    v1 = before.video_tracks[0]._rebuild(clips=(_clip("c2", 60, duration=30),))
    v2 = before.video_tracks[1]._rebuild(clips=(_clip("c1", 0),))
    after = before.with_track_updated(v1).with_track_updated(v2)
    kinds = {c.clip_id: c.kind for c in footprint_changes(before, after)}
    assert kinds == {"c1": "track", "c2": "duration"}


# ── 命令层判定（会话级全局锁） ────────────────────────────────────────


def test_move_rejected_under_session_lock() -> None:
    timeline = _timeline()
    cmd = MoveClipCommand.create("v1", "c1", new_start=20, old_start=0)
    with pytest.raises(PositionLockViolation):
        evaluate_command_footprint(cmd, timeline)


def test_trim_rejected_under_session_lock() -> None:
    timeline = _timeline()
    cmd = TrimClipCommand(
        command_id=str(uuid.uuid4()), label="trim", track_id="v1",
        clip_id="c1", trim_side="right", new_timeline_duration=50,
        new_source_duration=50,
    )
    with pytest.raises(PositionLockViolation):
        evaluate_command_footprint(cmd, timeline)


def test_delete_rejected_under_session_lock() -> None:
    timeline = _timeline()
    cmd = DeleteClipCommand.create("v1", "c2")
    with pytest.raises(PositionLockViolation):
        evaluate_command_footprint(cmd, timeline)


def test_slip_allowed_under_session_lock() -> None:
    timeline = _timeline()
    cmd = SlipClipCommand(
        command_id=str(uuid.uuid4()), label="slip", track_id="v1",
        clip_id="c1", delta_frames=10, max_source_frames=1000,
    )
    # slip 只改 source 窗——足迹不变，允许
    assert evaluate_command_footprint(cmd, timeline) == ()


def test_pure_add_allowed_under_session_lock() -> None:
    timeline = _timeline()
    new_clip = _clip("c9", 200, track_id="v2")
    cmd = AddClipCommand.create("v2", new_clip)
    assert evaluate_command_footprint(cmd, timeline) == ()


def test_explicit_locked_subset_only_locks_those() -> None:
    timeline = _timeline()
    # 只锁 c1：移动 c2 允许，移动 c1 拒绝
    move_c2 = MoveClipCommand.create("v1", "c2", new_start=80, old_start=60)
    assert evaluate_command_footprint(move_c2, timeline, {"c1"}) == ()
    move_c1 = MoveClipCommand.create("v1", "c1", new_start=20, old_start=0)
    with pytest.raises(PositionLockViolation):
        evaluate_command_footprint(move_c1, timeline, {"c1"})


def test_safe_variant_returns_reason() -> None:
    timeline = _timeline()
    cmd = MoveClipCommand.create("v1", "c1", new_start=20, old_start=0)
    allowed, reason = evaluate_command_footprint_safe(cmd, timeline)
    assert allowed is False and reason and "Position lock" in reason
    slip = SlipClipCommand(
        command_id=str(uuid.uuid4()), label="slip", track_id="v1",
        clip_id="c1", delta_frames=5, max_source_frames=1000,
    )
    assert evaluate_command_footprint_safe(slip, timeline) == (True, None)


# ── view 层：_execute_command 预检 ────────────────────────────────────


def _locked_view(qtbot: Any) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline())
    view.set_position_lock_enabled(True)
    return view


def test_view_lock_rejects_move_with_zero_history(qtbot: Any) -> None:
    from src.features.edit.core.position_lock import PositionLockViolation

    view = _locked_view(qtbot)
    before = view.get_timeline()
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    cmd = MoveClipCommand.create("v1", "c1", new_start=20, old_start=0)
    with pytest.raises(PositionLockViolation):
        view._execute_command(cmd)
    assert messages and "Position lock" in messages[0]
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack
    assert view.get_timeline().revision == before.revision


def test_view_lock_allows_slip_and_undo(qtbot: Any) -> None:
    view = _locked_view(qtbot)
    cmd = SlipClipCommand(
        command_id=str(uuid.uuid4()), label="slip", track_id="v1",
        clip_id="c1", delta_frames=10, max_source_frames=1000,
    )
    view._execute_command(cmd)  # 允许
    assert view.get_timeline().get_clip_by_id("c1").source_start == 10
    view.undo()  # undo 不受锁限制
    assert view.get_timeline().get_clip_by_id("c1").source_start == 0


def test_view_unlocked_allows_move(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline())
    cmd = MoveClipCommand.create("v1", "c1", new_start=20, old_start=0)
    view._execute_command(cmd)
    assert view.get_timeline().get_clip_by_id("c1").timeline_start == 20


# ── service 层：AI 显式策略 ──────────────────────────────────────────


def _state() -> Any:
    from src.features.edit.ai_editing.edit_project_service import (
        EditProjectService, ProjectSequences,
    )
    from src.features.edit.core.project import EditProject, TimelineSequenceRef

    timeline = _timeline()
    ref = TimelineSequenceRef(
        timeline_id="lock", name="Lock", fps=24.0,
        width=64, height=48, sample_rate=48000,
    )
    project = EditProject("p", "P", timelines={"lock": ref})
    state = ProjectSequences(project=project, sequences={"lock": timeline})
    EditProjectService.assert_sequences_consistent(state)
    return state


def test_service_policy_rejects_locked_footprint() -> None:
    from src.features.edit.ai_editing.edit_project_service import (
        EditProjectService,
    )

    state = _state()
    cmd = MoveClipCommand.create("v1", "c1", new_start=20, old_start=0)
    result = EditProjectService.commit_command(
        state, "lock", cmd, base_revision=state.sequences["lock"].revision,
        position_locked_clip_ids={"c1", "c2"},
    )
    assert result.applied is False
    assert result.error and "Position lock" in result.error
    # 两层状态原样
    assert result.new_project is state.project
    assert result.new_sequences == dict(state.sequences)


def test_service_policy_allows_unlocked_clip() -> None:
    from src.features.edit.ai_editing.edit_project_service import (
        EditProjectService,
    )

    state = _state()
    cmd = SlipClipCommand(
        command_id=str(uuid.uuid4()), label="slip", track_id="v1",
        clip_id="c1", delta_frames=8, max_source_frames=1000,
    )
    result = EditProjectService.commit_command(
        state, "lock", cmd, base_revision=state.sequences["lock"].revision,
        position_locked_clip_ids={"c1", "c2"},
    )
    assert result.applied is True


def test_service_without_policy_backward_compatible() -> None:
    from src.features.edit.ai_editing.edit_project_service import (
        EditProjectService,
    )

    state = _state()
    cmd = MoveClipCommand.create("v1", "c1", new_start=20, old_start=0)
    result = EditProjectService.commit_command(
        state, "lock", cmd, base_revision=state.sequences["lock"].revision)
    assert result.applied is True
