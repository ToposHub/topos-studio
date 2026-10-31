"""P1-04 专业 Trim 测试：手柄余量 / 最近标记 / 数值 trim / 键盘步进。

验收（计划表 P1-04）：
- 数值 trim（Trim/Ripple/Roll）经统一 builder，钳制后执行
- 键盘 ,/. 单帧步进（Shift=入点、Alt=多帧），Trim/Ripple 工具分流
- 手柄余量诊断（head/tail）供 UI 与钳制共用
- "对齐到最近标记" snap 语义
- Trim Monitor 显示 cut 两侧 TC + Δ + 手柄
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.commands import (
    CommandStack,
    RippleTrimCommand,
    RollTrimCommand,
    TrimClipCommand,
)
from src.features.edit.core.timeline_ops import (
    build_numeric_trim_command,
    clip_handle_headroom,
    nearest_marker_frame,
    numeric_trim_bounds,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineMarker,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id, track_id="v1", start=0, duration=50, source_start=10,
          source_duration=50):
    return ClipInstance(
        clip_id=clip_id, asset_id=f"asset-{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=source_start, source_duration=source_duration, speed=1.0,
    )


def _timeline(clips, track_id="v1", track_type="video"):
    from fractions import Fraction
    from src.features.edit.core.timeline_v2 import Rational
    track = EditTrack(track_id=track_id, track_type=track_type, name="V1",
                      clips=tuple(clips))
    return TimelineSequence(
        timeline_id="tl", name="TL", fps=Rational(25, 1),
        video_tracks=(track,) if track_type == "video" else (),
        audio_tracks=(track,) if track_type == "audio" else (),
        subtitle_tracks=(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 手柄余量 / 最近标记
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleHeadroom:

    def test_basic_head_tail(self):
        clip = _clip("c1", source_start=10, source_duration=50)
        head, tail = clip_handle_headroom(clip, max_source_frames=200)
        assert head == 10
        assert tail == 200 - 60  # 140

    def test_unknown_max_means_no_tail(self):
        clip = _clip("c1", source_start=5, source_duration=40)
        head, tail = clip_handle_headroom(clip, max_source_frames=None)
        assert head == 5
        assert tail == 0  # 保守：不允许延长

    def test_never_negative(self):
        clip = _clip("c1", source_start=0, source_duration=100)
        head, tail = clip_handle_headroom(clip, max_source_frames=50)
        assert head == 0
        assert tail == 0


class TestNearestMarkerFrame:

    def _markers(self, *frames):
        return [TimelineMarker(marker_id=f"m{f}", frame=f) for f in frames]

    def test_forward_and_backward(self):
        markers = self._markers(10, 50, 120)
        assert nearest_marker_frame(markers, 30, 1) == 50
        assert nearest_marker_frame(markers, 30, -1) == 10

    def test_wrap_around(self):
        markers = self._markers(10, 100)
        assert nearest_marker_frame(markers, 150, 1) == 10   # wrap
        assert nearest_marker_frame(markers, 5, -1) == 100   # wrap

    def test_empty_returns_none(self):
        assert nearest_marker_frame([], 30, 1) is None

    def test_excludes_current_frame(self):
        markers = self._markers(30, 60)
        assert nearest_marker_frame(markers, 30, 1) == 60
        assert nearest_marker_frame(markers, 30, -1) == 60  # wrap


# ─────────────────────────────────────────────────────────────────────────────
# 数值 trim 边界与命令构建
# ─────────────────────────────────────────────────────────────────────────────


class TestNumericTrimBounds:

    def test_right_trim_bounded_by_neighbor_and_handle(self):
        # c1 [0,50) c2 [60,110)：右侧间隙 10 帧；素材 tail 40
        tl = _timeline([_clip("c1", start=0, duration=50, source_start=10,
                              source_duration=50),
                        _clip("c2", start=60, duration=50)])
        track = tl.get_track_by_id("v1")
        c1 = track.get_clip_by_id("c1")
        # max_source = 10+50+40=100 → tail=40；邻居间隙 10 → max_delta=10
        assert numeric_trim_bounds(track, c1, "right",
                                   max_source_frames=100) == (-49, 10)
        # 无素材上限信息 → tail=0
        assert numeric_trim_bounds(track, c1, "right") == (-49, 0)

    def test_left_trim_bounded_by_head_and_neighbor(self):
        tl = _timeline([_clip("c0", start=0, duration=30),
                        _clip("c1", start=40, duration=50, source_start=10,
                              source_duration=50)])
        track = tl.get_track_by_id("v1")
        c1 = track.get_clip_by_id("c1")
        # head=10；邻居间隙 40-30=10 → min_delta=-10；至少留 1 帧
        assert numeric_trim_bounds(track, c1, "left",
                                   max_source_frames=100) == (-10, 49)

    def test_ripple_ignores_neighbor_bound(self):
        tl = _timeline([_clip("c1", start=0, duration=50, source_start=10,
                              source_duration=50),
                        _clip("c2", start=60, duration=50)])
        track = tl.get_track_by_id("v1")
        c1 = track.get_clip_by_id("c1")
        # ripple：出点延长只受手柄限制（tail=140），不受 c2 位置限制
        low, high = numeric_trim_bounds(track, c1, "right",
                                        max_source_frames=200, ripple=True)
        assert low == -49
        assert high == 140


class TestBuildNumericTrimCommand:

    def _apply(self, timeline, command):
        stack = CommandStack()
        return stack.execute(command, timeline)

    def test_trim_right_extends(self):
        tl = _timeline([_clip("c1", start=0, duration=50, source_start=10,
                              source_duration=50)])
        cmd, clamped = build_numeric_trim_command(
            tl, "v1", "c1", "right", 30, max_source_frames=200)
        assert isinstance(cmd, TrimClipCommand)
        assert clamped == 30
        result = self._apply(tl, cmd)
        c1 = result.get_track_by_id("v1").get_clip_by_id("c1")
        assert c1.timeline_duration == 80
        assert c1.source_duration == 80

    def test_trim_right_clamps_to_bounds(self):
        tl = _timeline([_clip("c1", start=0, duration=50, source_start=10,
                              source_duration=50),
                        _clip("c2", start=60, duration=50)])
        # 间隙 10 且手柄充足（tail=140）→ delta 999 钳到间隙 10
        cmd, clamped = build_numeric_trim_command(
            tl, "v1", "c1", "right", 999, max_source_frames=200)
        assert clamped == 10
        assert cmd.new_timeline_duration == 60

    def test_delta_clamped_to_zero_returns_none(self):
        tl = _timeline([_clip("c1", start=0, duration=50),
                        _clip("c2", start=50, duration=50)])
        # 无间隙且无手柄 → 右延长钳为 0
        assert build_numeric_trim_command(tl, "v1", "c1", "right", 30) == (None, 0)

    def test_ripple_right_uses_new_end(self):
        tl = _timeline([_clip("c1", start=0, duration=50),
                        _clip("c2", start=50, duration=50)])
        cmd, clamped = build_numeric_trim_command(
            tl, "v1", "c1", "right", 20, mode="ripple", max_source_frames=200)
        assert isinstance(cmd, RippleTrimCommand)
        assert clamped == 20
        assert cmd.trim_side == "right"
        result = self._apply(tl, cmd)
        track = result.get_track_by_id("v1")
        c1 = track.get_clip_by_id("c1")
        c2 = track.get_clip_by_id("c2")
        assert c1.timeline_duration == 70            # 0..70
        assert c2.timeline_start == 70               # 波纹后移

    def test_ripple_left_shorten_pulls_followers_left(self):
        """入点右缩短（波纹）→ 后续片段左移收拢，不留洞（审计修复回归）。"""
        tl = _timeline([_clip("c1", start=50, duration=50, source_start=10,
                              source_duration=50),
                        _clip("c2", start=100, duration=30)])
        cmd, clamped = build_numeric_trim_command(
            tl, "v1", "c1", "left", 10, mode="ripple", max_source_frames=200)
        assert clamped == 10
        stack = CommandStack()
        result = stack.execute(cmd, tl)
        track = result.get_track_by_id("v1")
        c1 = track.get_clip_by_id("c1")
        c2 = track.get_clip_by_id("c2")
        # 起点锚定、右缘收短（等价于波纹删除头部 10 帧）
        assert c1.timeline_start == 50
        assert c1.timeline_end == 90
        assert c2.timeline_start == 90            # 跟随左移收拢（旧代码：110 留洞）
        # undo 恢复
        undone = stack.undo(result)
        track = undone.get_track_by_id("v1")
        assert track.get_clip_by_id("c1").timeline_start == 50
        assert track.get_clip_by_id("c2").timeline_start == 100

    def test_ripple_left_extend_into_adjacent_follower(self):
        """入点左延长（波纹）→ 相邻后续片段右移让位（旧代码抛重叠）。"""
        tl = _timeline([_clip("c1", start=50, duration=50, source_start=10,
                              source_duration=50),
                        _clip("c2", start=100, duration=30)])
        cmd, clamped = build_numeric_trim_command(
            tl, "v1", "c1", "left", -10, mode="ripple", max_source_frames=200)
        assert clamped == -10
        stack = CommandStack()
        result = stack.execute(cmd, tl)   # 旧代码此处抛 "Updated clip overlaps"
        track = result.get_track_by_id("v1")
        c1 = track.get_clip_by_id("c1")
        c2 = track.get_clip_by_id("c2")
        assert c1.timeline_start == 40
        assert c1.timeline_duration == 60
        assert c1.timeline_end == 100     # 右缘不动
        assert c2.timeline_start == 100   # 后续不动（旧代码：90 撞重叠）

    def test_ripple_left_extend_respects_prev_neighbor(self):
        """审计修复回归：ripple 左延长边界含前邻约束（apply 不再抛）。"""
        tl = _timeline([_clip("c0", start=0, duration=50),
                        _clip("c1", start=50, duration=50, source_start=10,
                              source_duration=50)])
        track = tl.get_track_by_id("v1")
        c1 = track.get_clip_by_id("c1")
        low, high = numeric_trim_bounds(track, c1, "left",
                                        max_source_frames=500, ripple=True)
        assert low == 0  # 前邻相接：不可向左延长（旧代码 -head 放行后 apply 抛）

    def test_roll_uses_right_neighbor_and_clamps(self):
        tl = _timeline([_clip("c1", start=0, duration=50, source_start=0,
                              source_duration=50),
                        _clip("c2", start=50, duration=50, source_start=20,
                              source_duration=50)])
        cmd, clamped = build_numeric_trim_command(
            tl, "v1", "c1", "right", 999, mode="roll", max_source_frames=200)
        assert isinstance(cmd, RollTrimCommand)
        # 右片段至少留 1 帧 → 49；左片段手柄充足
        assert clamped == 49
        # 向左：右片段 source_start=20 限制 → -20
        cmd_l, clamped_l = build_numeric_trim_command(
            tl, "v1", "c1", "right", -999, mode="roll", max_source_frames=200)
        assert clamped_l == -20

    def test_roll_without_neighbor_returns_none(self):
        tl = _timeline([_clip("c1", start=0, duration=50)])
        assert build_numeric_trim_command(
            tl, "v1", "c1", "right", 10, mode="roll") == (None, 0)

    def test_left_trim_in_point_moves(self):
        tl = _timeline([_clip("c1", start=40, duration=50, source_start=10,
                              source_duration=50)])
        cmd, clamped = build_numeric_trim_command(
            tl, "v1", "c1", "left", -10, max_source_frames=200)
        assert clamped == -10
        result = self._apply(tl, cmd)
        c1 = result.get_track_by_id("v1").get_clip_by_id("c1")
        assert c1.timeline_start == 30
        assert c1.source_start == 0
        assert c1.timeline_duration == 60


# ─────────────────────────────────────────────────────────────────────────────
# 键盘步进（视图层，fake self 复用 _apply_step_trim）
# ─────────────────────────────────────────────────────────────────────────────


class _FakeSelection:
    def __init__(self, ids):
        self.selected_clip_ids = list(ids)


class _FakeTrimView:
    """TimelineView._apply_step_trim 的最小宿主。"""

    def __init__(self, timeline, selected, ripple=False, linked=False):
        from src.features.edit.ui.timeline_items import EditTool
        self._timeline = timeline
        self._selection = _FakeSelection(selected)
        self._active_tool = EditTool.RIPPLE if ripple else EditTool.TRIM
        self._linked_selection_enabled = linked
        self._is_track_locked = lambda _track_id: False
        self.executed = []

    def _get_asset_duration(self, asset_id):
        return 1000

    def _execute_command(self, cmd):
        self.executed.append(cmd)


class TestKeyboardStepTrim:

    def _view(self, timeline, clip_id="c1", **kwargs):
        view = _FakeTrimView(timeline, [clip_id], **kwargs)
        return view

    def test_step_out_extends_via_trim_command(self):
        from src.features.edit.ui.timeline_view import TimelineView
        tl = _timeline([_clip("c1", start=0, duration=50),
                        _clip("c2", start=100, duration=50)])
        view = self._view(tl)
        assert TimelineView._apply_step_trim(view, "out", 5) is True
        assert len(view.executed) == 1
        stack = CommandStack()
        result = stack.execute(view.executed[0], tl)
        c1 = result.get_track_by_id("v1").get_clip_by_id("c1")
        assert c1.timeline_duration == 55

    def test_step_ripple_tool_uses_ripple(self):
        from src.features.edit.ui.timeline_view import TimelineView
        tl = _timeline([_clip("c1", start=0, duration=50),
                        _clip("c2", start=50, duration=50)])
        view = self._view(tl, ripple=True)
        assert TimelineView._apply_step_trim(view, "out", 10) is True
        assert isinstance(view.executed[0], RippleTrimCommand)

    def test_step_in_edge_shortens(self):
        from src.features.edit.ui.timeline_view import TimelineView
        tl = _timeline([_clip("c1", start=10, duration=50, source_start=10,
                              source_duration=50)])
        view = self._view(tl)
        assert TimelineView._apply_step_trim(view, "in", -5) is True
        stack = CommandStack()
        result = stack.execute(view.executed[0], tl)
        c1 = result.get_track_by_id("v1").get_clip_by_id("c1")
        assert c1.timeline_start == 5
        assert c1.source_start == 5

    def test_requires_single_media_clip(self):
        from src.features.edit.ui.timeline_view import TimelineView
        tl = _timeline([_clip("c1"), _clip("c2", start=100)])
        view = _FakeTrimView(tl, ["c1", "c2"])
        assert TimelineView._apply_step_trim(view, "out", 5) is False
        assert view.executed == []

    def test_undo_after_numeric_trim_restores(self):
        """审计修复回归：数值/键盘 trim 的 old_* 齐全，undo 不抛不丢历史。"""
        from src.features.edit.ui.timeline_view import TimelineView
        tl = _timeline([_clip("c1", start=0, duration=50, source_start=10,
                              source_duration=50)])
        view = self._view(tl)
        assert TimelineView._apply_step_trim(view, "out", 5) is True
        stack = CommandStack()
        result = stack.execute(view.executed[0], tl)
        c1 = result.get_track_by_id("v1").get_clip_by_id("c1")
        assert c1.timeline_duration == 55
        undone = stack.undo(result)  # 旧代码在此抛 "old values not stored"
        c1 = undone.get_track_by_id("v1").get_clip_by_id("c1")
        assert c1.timeline_duration == 50
        assert c1.source_duration == 50

    def test_locked_track_rejects_step_trim(self):
        """审计修复回归：锁轨上键盘/数值 trim 不再绕过。"""
        from src.features.edit.ui.timeline_view import TimelineView
        tl = _timeline([_clip("c1", start=0, duration=50)])
        view = self._view(tl)
        view._is_track_locked = lambda track_id: True
        assert TimelineView._apply_step_trim(view, "out", 5) is False
        assert view.executed == []

    def test_clamped_to_zero_rejects(self):
        from src.features.edit.ui.timeline_view import TimelineView
        # 无手柄（素材上限=当前结束）且无间隙 → 钳为 0
        tl = _timeline([_clip("c1", start=0, duration=50, source_start=0,
                              source_duration=50),
                        _clip("c2", start=50, duration=50)])
        view = _FakeTrimView(tl, ["c1"])

        class _NoAsset:
            def _get_asset_duration(self, asset_id):
                return 50  # 正好用完

            _execute_command = lambda self, cmd: None  # noqa: E731
        view._get_asset_duration = _NoAsset()._get_asset_duration
        assert TimelineView._apply_step_trim(view, "out", 30) is False
        assert view.executed == []


# ─────────────────────────────────────────────────────────────────────────────
# 数值 trim 对话框（UI）
# ─────────────────────────────────────────────────────────────────────────────


class TestTrimNumericDialog:

    def test_bounds_refresh_and_values(self, qapp):
        from src.features.edit.ui.trim_numeric_dialog import TrimNumericDialog
        calls = []

        def bounds_for(edge, mode):
            calls.append((edge, mode))
            return (-5, 20) if edge == "out" else (-30, 10)

        dialog = TrimNumericDialog(
            None, bounds_for=bounds_for, headroom=(12, 40),
            duration_frames=100, can_roll=True, markers_available=True)
        assert dialog._delta_spin.minimum() == -5
        assert dialog._delta_spin.maximum() == 20
        assert "12" in dialog._headroom_label.text()
        assert "40" in dialog._headroom_label.text()

        dialog._edge_combo.setCurrentIndex(1)  # In
        assert dialog._delta_spin.minimum() == -30
        edge, mode, delta, align = dialog.values()
        assert edge == "in"
        assert mode == "trim"
        assert align is False

        # roll 模式：edge 锁定
        dialog._mode_combo.setCurrentIndex(2)  # Roll
        assert dialog._edge_combo.isEnabled() is False
        assert dialog.current_mode() == "roll"

    def test_no_roll_option_without_neighbor(self, qapp):
        from src.features.edit.ui.trim_numeric_dialog import TrimNumericDialog
        dialog = TrimNumericDialog(None, bounds_for=lambda e, m: (-9, 9),
                                   can_roll=False)
        assert dialog._mode_combo.count() == 2


class TestTrimMonitorDialog:

    def test_roll_and_trim_update(self, qapp):
        from src.features.edit.ui.trim_numeric_dialog import TrimMonitorDialog
        monitor = TrimMonitorDialog()
        monitor.update_roll("00:00:04:00", "00:00:04:02", 2, headroom=(30, 10))
        assert monitor._label_a.text() == "00:00:04:00"
        assert monitor._label_b.text() == "00:00:04:02"
        assert "+2" in monitor._delta_label.text()
        assert "30" in monitor._info_label.text()
        monitor.update_trim("00:00:01:00", -5, headroom=(3, 7))
        assert monitor._label_a.text() == "00:00:01:00"
        assert monitor._label_b.text() == "—"
        assert "-5" in monitor._delta_label.text()
