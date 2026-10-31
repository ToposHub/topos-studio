"""复查批次 A 回归（交互层）：R13 Roll seam 取消 / R14 linked trim 与
Slide 取消 / R17 disabled clip 加载投影。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.2
（RI-01/RI-02/RI-06），复现见 docs/audit/2026-10-14-edit-recheck/。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QPointF

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.ui.timeline_interaction import InteractionState
from src.features.edit.ui.timeline_items import ClipItem

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int, **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 500),
        **kw,
    )


def _three_clip_timeline() -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = timeline.with_track_added(video)
    timeline = timeline.with_clip_added("v1", _clip("a", "v1", 0, 100))
    timeline = timeline.with_clip_added("v1", _clip("b", "v1", 100, 100))
    return timeline


def _linked_timeline() -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    audio = EditTrack(track_id="a1", name="A1", track_type="audio", index=0)
    timeline = timeline.with_track_added(video).with_track_added(audio)
    timeline = timeline.with_clip_added(
        "v1", _clip("vc", "v1", 0, 100, linked_clip_id="ac")
    )
    timeline = timeline.with_clip_added(
        "a1", _clip("ac", "a1", 0, 100, linked_clip_id="vc")
    )
    return timeline



def _press_like(item: ClipItem, clip) -> None:
    """模拟真实 mousePress 保存 original 的动作（测试直接调 preview 预览
    处理器时使用，真实链路由 ClipItem 自己保存）。"""
    item._original_start = clip.timeline_start
    item._original_duration = clip.timeline_duration
    item._original_source_start = clip.source_start
    item._original_source_duration = clip.source_duration
    item._original_fade_in = clip.fade_in_duration
    item._original_fade_out = clip.fade_out_duration
    item._is_dragging = True
    item._drag_mode = "trim_left"

def _item_geometry(item: ClipItem) -> tuple[int, int, int, int]:
    return (
        item._start_frame,
        item._duration_frames,
        item._source_start,
        item._source_duration,
    )


# ------------------------------------------------------------------ R13

def test_roll_cancel_discards_preview_and_rejects_late_release(qtbot: Any) -> None:
    """RI-01：Roll 预览后取消——双片回模型、会话 IDLE、迟到 release 不提交。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _three_clip_timeline()
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    view._on_roll_trim_started("a", "b", 100)
    # RI-01 核心契约：started 必须进入会话，Esc/切工具/失焦才能取消
    assert view._interaction.state == InteractionState.ROLLING

    view._on_roll_trim_dragged(10)
    left_item = view._roll_preview_state["left_item"]
    right_item = view._roll_preview_state["right_item"]
    assert left_item._duration_frames == 110  # 预览已生效

    view._cancel_current_interaction()
    assert view._roll_preview_state is None
    assert view._interaction.is_idle
    assert _item_geometry(left_item) == (0, 100, 0, 500)
    assert _item_geometry(right_item) == (100, 100, 0, 500)

    # 迟到的 release（旧 bug：Esc→release 仍提交 delta）
    view._on_roll_trim_finished("a", "b", 10)
    assert view._timeline.get_clip_by_id("a").timeline_duration == 100
    assert len(view._command_stack._undo_stack) == 0


def test_roll_handle_cancel_resets_delta_without_emitting(qtbot: Any) -> None:
    """RI-01：handle.cancel_drag 复位 _current_delta——release 不再发 finished。"""
    timeline = _three_clip_timeline()
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    view._on_roll_trim_started("a", "b", 100)
    handle = view._roll_preview_state.get("handle")
    if handle is None:
        pytest.skip("roll handle not materialized without tool activation")

    handle._current_delta = 10
    handle._is_dragging = True
    finished: list[tuple] = []
    handle.roll_trim_finished.connect(lambda *a: finished.append(a))
    handle.cancel_drag()

    assert handle._current_delta == 0
    assert handle._is_dragging is False
    # 模拟取消后的 mouseReleaseEvent：_current_delta==0 不发 finished
    from PyQt6.QtCore import Qt

    class _Evt:
        def button(self):
            return Qt.MouseButton.LeftButton

        def accept(self):
            pass

    handle.mouseReleaseEvent(_Evt())
    assert finished == []


def test_roll_normal_release_commits(qtbot: Any) -> None:
    """RI-01 回归保护：正常 release 仍按原路径提交。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _three_clip_timeline()
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    view._on_roll_trim_started("a", "b", 100)
    view._on_roll_trim_dragged(10)
    view._on_roll_trim_finished("a", "b", 10)

    assert view._interaction.is_idle
    assert view._timeline.get_clip_by_id("a").timeline_duration == 110
    assert view._timeline.get_clip_by_id("b").timeline_start == 110
    assert len(view._command_stack._undo_stack) == 1


# ------------------------------------------------------------------ R14

def test_linked_trim_cancel_restores_partner(qtbot: Any) -> None:
    """RI-02：linked 左修取消——音频伙伴的源区间/几何一并回模型。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _linked_timeline()
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    video_item = view._items_map["vc"]
    audio_item = view._items_map["ac"]
    _press_like(video_item, view._timeline.get_clip_by_id("vc"))
    view._interaction.begin(
        InteractionState.TRIMMING_LEFT,
        QPointF(0.0, 0.0),
        primary_clip_id="vc",
        primary_track_id="v1",
    )
    # 预览左修 10 帧：V/A 双片同时被改写
    view._on_clip_trim_left("vc", "v1", 10)
    assert video_item._start_frame == 10
    assert audio_item._start_frame == 10
    assert audio_item._source_start == 10

    view._cancel_current_interaction()
    vc = view._timeline.get_clip_by_id("vc")
    ac = view._timeline.get_clip_by_id("ac")
    assert _item_geometry(video_item) == (0, 100, vc.source_start, vc.source_duration)
    assert _item_geometry(audio_item) == (0, 100, ac.source_start, ac.source_duration)
    assert not view._preview_affected_item_ids
    assert len(view._command_stack._undo_stack) == 0


def test_linked_trim_right_cancel_restores_partner(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _linked_timeline()
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    video_item = view._items_map["vc"]
    audio_item = view._items_map["ac"]
    _press_like(video_item, view._timeline.get_clip_by_id("vc"))
    video_item._drag_mode = "trim_right"
    view._interaction.begin(
        InteractionState.TRIMMING_RIGHT,
        QPointF(0.0, 0.0),
        primary_clip_id="vc",
        primary_track_id="v1",
    )
    view._on_clip_trim_right("vc", "v1", 90)
    assert video_item._duration_frames == 90
    assert audio_item._duration_frames == 90

    view._cancel_current_interaction()
    vc = view._timeline.get_clip_by_id("vc")
    ac = view._timeline.get_clip_by_id("ac")
    assert _item_geometry(video_item) == (0, 100, vc.source_start, vc.source_duration)
    assert _item_geometry(audio_item) == (0, 100, ac.source_start, ac.source_duration)


def test_slide_cancel_restores_neighbors(qtbot: Any) -> None:
    """RI-02：Slide 取消——左右邻片几何一并回模型（审计实测左邻 duration
    残留 110、右邻 start 残留 210）。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = timeline.with_track_added(video)
    timeline = timeline.with_clip_added("v1", _clip("left", "v1", 0, 100))
    timeline = timeline.with_clip_added("v1", _clip("mid", "v1", 100, 100))
    timeline = timeline.with_clip_added("v1", _clip("right", "v1", 200, 100))
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    prev_item = view._items_map["left"]
    next_item = view._items_map["right"]
    # Mirror the ClipItem press entry so the immutable gesture snapshot is set.
    view._on_item_interaction_started("mid", "v1", "slide")
    view._on_clip_slide("mid", "v1", 10)
    assert prev_item._duration_frames == 110
    assert next_item._start_frame == 210

    view._cancel_current_interaction()
    prev_model = view._timeline.get_clip_by_id("left")
    next_model = view._timeline.get_clip_by_id("right")
    assert _item_geometry(prev_item) == (
        prev_model.timeline_start, prev_model.timeline_duration,
        prev_model.source_start, prev_model.source_duration,
    )
    assert _item_geometry(next_item) == (
        next_model.timeline_start, next_model.timeline_duration,
        next_model.source_start, next_model.source_duration,
    )
    assert not view._preview_affected_item_ids


# ------------------------------------------------------------------ R17

def test_disabled_clip_loads_with_correct_projection(qtbot: Any) -> None:
    """RI-06：disabled 模型首次加载即投影为 disabled Item。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _three_clip_timeline()
    timeline = timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_clip_updated(
            timeline.get_track_by_id("v1").get_clip_by_id("a").with_enabled(False)
        )
    )
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    item = view._items_map["a"]
    assert isinstance(item, ClipItem)
    assert item._is_enabled is False


def test_track_state_update_does_not_pollute_clip_enabled(qtbot: Any) -> None:
    """RI-06：轨道状态刷新不得用 track.is_enabled/is_muted 覆盖 clip 自身
    enabled（旧代码把 disabled clip 污染成 enabled）。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _three_clip_timeline()
    track = timeline.get_track_by_id("v1")
    timeline = timeline.with_track_updated(
        track.with_clip_updated(
            track.get_clip_by_id("a").with_enabled(False)
        )
    )
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    view._update_track_clip_states("v1")
    assert view._items_map["a"]._is_enabled is False


def test_disabled_projection_survives_incremental_create(qtbot: Any) -> None:
    """RI-06：增量创建路径（_create_clip_item）同样携带 is_enabled。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _three_clip_timeline()
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)

    track = view._timeline.get_track_by_id("v1")
    disabled_clip = _clip("new-dis", "v1", 300, 50).with_enabled(False)
    view._create_clip_item(disabled_clip)
    item = view._items_map["new-dis"]
    assert isinstance(item, ClipItem)
    assert item._is_enabled is False


# ------------------------------------------------------- 反向剪辑探针工具

def test_reverse_speed_params_fixture_is_valid() -> None:
    """保证本文件使用的反向 SpeedParams 通过领域校验。"""
    assert SpeedParams(speed=1.0, reverse=True).reverse is True
