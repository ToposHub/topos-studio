"""F2.2 · 三/四点编辑、backtime 与 fit-to-fill。

两层覆盖：
- 纯函数层 ``resolve_source_edit_plan``（timeline_ops）：判定表全矩阵
  （无 range/仅 In/仅 Out backtime/四点等长/冲突描述符/fit_speed 精确
  命中含混合帧率/trim_source 裁剪/不可行抛错）。
- 接线层 ``TimelineView.insert_clip/overwrite_clip``：时间线 In/Out
  range 参与落点（In=落点、Out=backtime 恰好结束、四点等长、冲突
  fit-to-fill 变速落轨、取消零写入、无 range 行为不变）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_ops import (
    SourceEditConflict,
    resolve_source_edit_plan,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    Rational,
    TimelineSequence,
)

FPS24 = (24, 1)


# ── 纯函数层 ────────────────────────────────────────────────────────────


class TestResolveSourceEditPlan:
    def test_no_range_uses_default_playhead(self) -> None:
        plan = resolve_source_edit_plan(10, 57, None, None, default_playhead=100)
        assert plan.mode == "three_point"
        assert plan.playhead == 100
        assert plan.timeline_duration == 48

    def test_in_only_lands_at_range_start(self) -> None:
        plan = resolve_source_edit_plan(10, 57, 200, None)
        assert plan.mode == "three_point"
        assert plan.playhead == 200

    def test_out_only_backtime_ends_at_range_end(self) -> None:
        """backtime：片段恰好结束在时间线 Out。"""
        plan = resolve_source_edit_plan(10, 57, None, 300)
        assert plan.mode == "backtime"
        assert plan.playhead == 300 - 48 + 1
        assert plan.playhead + plan.timeline_duration - 1 == 300

    def test_equal_durations_match_directly(self) -> None:
        plan = resolve_source_edit_plan(0, 47, 50, 97)
        assert plan.mode == "four_point_match"
        assert plan.playhead == 50 and plan.speed == 1.0

    def test_mismatch_returns_conflict_descriptor(self) -> None:
        conflict = resolve_source_edit_plan(0, 47, 50, 73)
        assert isinstance(conflict, SourceEditConflict)
        assert conflict.source_duration == 48 and conflict.target_duration == 24
        assert conflict.suggested_speed == pytest.approx(2.0)
        assert conflict.speed_in_range and conflict.trim_available

    def test_fit_speed_hits_target_exactly(self) -> None:
        plan = resolve_source_edit_plan(0, 47, 50, 73, fit_policy="fit_speed")
        assert plan.mode == "four_point_fit"
        assert plan.speed == pytest.approx(2.0)
        assert plan.timeline_duration == 24
        assert plan.playhead + plan.timeline_duration - 1 == 73

    def test_fit_speed_mixed_framerate(self) -> None:
        """48fps 源 48 帧 → 24fps 时间线 12 帧 range：speed 2.0 精确命中。"""
        plan = resolve_source_edit_plan(
            0, 47, 0, 11,
            source_fps_num=48, source_fps_den=1,
            timeline_fps_num=24, timeline_fps_den=1,
            fit_policy="fit_speed")
        assert plan.mode == "four_point_fit"
        assert plan.speed == pytest.approx(2.0)
        assert plan.timeline_duration == 12

    def test_trim_source_cuts_source_window(self) -> None:
        plan = resolve_source_edit_plan(0, 47, 50, 73, fit_policy="trim_source")
        assert plan.mode == "four_point_trim"
        assert plan.source_in == 0 and plan.source_out == 23
        assert plan.speed == 1.0 and plan.timeline_duration == 24

    def test_trim_mixed_framerate_converts_frames(self) -> None:
        """24fps 时间线取 12 帧 = 48fps 源 24 帧。"""
        plan = resolve_source_edit_plan(
            0, 47, 0, 11,
            source_fps_num=48, source_fps_den=1,
            timeline_fps_num=24, timeline_fps_den=1,
            fit_policy="trim_source")
        assert plan.source_out == 23 and plan.timeline_duration == 12

    def test_trim_with_longer_target_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_source_edit_plan(0, 23, 0, 47, fit_policy="trim_source")

    def test_fit_with_out_of_range_speed_rejected(self) -> None:
        """100 帧源塞 5 帧目标需 speed 20（>8.0 上限）——fit 拒绝。"""
        with pytest.raises(ValueError):
            resolve_source_edit_plan(0, 99, 0, 4, fit_policy="fit_speed")
        conflict = resolve_source_edit_plan(0, 99, 0, 4)
        assert not conflict.speed_in_range

    def test_invalid_ranges_raise(self) -> None:
        with pytest.raises(ValueError):
            resolve_source_edit_plan(57, 10, None, None)
        with pytest.raises(ValueError):
            resolve_source_edit_plan(0, 10, 90, 50)

    def test_conflict_target_longer_trim_unavailable(self) -> None:
        """目标更长：裁剪不可用（无法反向延长源窗），变速仍合法。"""
        conflict = resolve_source_edit_plan(0, 99, 0, 399)  # 400 帧目标
        assert isinstance(conflict, SourceEditConflict)
        assert not conflict.trim_available  # 400 > 100
        assert conflict.speed_in_range  # 100/400 = 0.25 ∈ [0.1, 8.0]
        assert conflict.suggested_speed == pytest.approx(0.25)


# ── 接线层 ──────────────────────────────────────────────────────────────


def _silent_asset(asset_id: str = "sv1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path="/tmp/sv.mp4", media_type="video",
        display_name="sv", metadata={"fps": 24.0})


def _sequence(range_start=None, range_end=None) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-34pt", name="PT", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        range_start=range_start, range_end=range_end,
    )


class TestTimelineViewWiring:
    def _view(self, qapp, qtbot, range_start=None, range_end=None):
        from src.features.edit.ui.timeline_view import TimelineView

        project = EditProject("p", "P", assets={"sv1": _silent_asset()})
        view = TimelineView()
        view.load_timeline(_sequence(range_start, range_end))
        view.set_project(project)
        return view

    def _clips(self, view) -> list:
        return list(view._timeline.video_tracks[0].clips)

    def test_no_range_keeps_playhead_behavior(self, qapp, qtbot) -> None:
        view = self._view(qapp, qtbot)
        assert view.insert_clip("sv1", 0, 47, playhead=30) is True
        (clip,) = self._clips(view)
        assert clip.timeline_start == 30 and clip.timeline_duration == 48

    def test_timeline_in_sets_landing(self, qapp, qtbot) -> None:
        view = self._view(qapp, qtbot, range_start=100)
        assert view.insert_clip("sv1", 0, 47, playhead=30) is True
        (clip,) = self._clips(view)
        assert clip.timeline_start == 100

    def test_backtime_ends_at_timeline_out(self, qapp, qtbot) -> None:
        view = self._view(qapp, qtbot, range_end=200)
        assert view.insert_clip("sv1", 0, 47, playhead=30) is True
        (clip,) = self._clips(view)
        assert clip.timeline_start + clip.timeline_duration - 1 == 200

    def test_four_point_match_lands_exact(self, qapp, qtbot) -> None:
        view = self._view(qapp, qtbot, range_start=50, range_end=97)
        assert view.overwrite_clip("sv1", 0, 47, playhead=30) is True
        (clip,) = self._clips(view)
        assert clip.timeline_start == 50 and clip.timeline_duration == 48

    def test_conflict_fit_speed_applies_speed(
            self, qapp, qtbot, monkeypatch) -> None:
        """四点冲突选变速填充：48 帧源速填 24 帧目标（speed 2.0）。"""
        from src.features.edit.ui.timeline_view import TimelineView

        monkeypatch.setattr(
            TimelineView, "_ask_four_point_conflict",
            lambda self, conflict: "fit_speed")
        view = self._view(qapp, qtbot, range_start=50, range_end=73)
        assert view.overwrite_clip("sv1", 0, 47, playhead=30) is True
        (clip,) = self._clips(view)
        assert clip.timeline_start == 50 and clip.timeline_duration == 24
        assert float(clip.effective_speed()) == pytest.approx(2.0)

    def test_conflict_cancel_writes_nothing(
            self, qapp, qtbot, monkeypatch) -> None:
        from src.features.edit.ui.timeline_view import TimelineView

        monkeypatch.setattr(
            TimelineView, "_ask_four_point_conflict",
            lambda self, conflict: None)
        view = self._view(qapp, qtbot, range_start=50, range_end=73)
        assert view.insert_clip("sv1", 0, 47, playhead=30) is False
        assert self._clips(view) == []

    def test_conflict_trim_source_shortens_window(
            self, qapp, qtbot, monkeypatch) -> None:
        from src.features.edit.ui.timeline_view import TimelineView

        monkeypatch.setattr(
            TimelineView, "_ask_four_point_conflict",
            lambda self, conflict: "trim_source")
        view = self._view(qapp, qtbot, range_start=50, range_end=73)
        assert view.insert_clip("sv1", 0, 47, playhead=30) is True
        (clip,) = self._clips(view)
        assert clip.timeline_duration == 24
        assert clip.source_duration == 24  # 源窗被裁到 24 帧
        assert float(clip.effective_speed()) == pytest.approx(1.0)

    def test_explicit_speed_call_skips_planning(self, qapp, qtbot) -> None:
        """显式变速调用（API/自动化）不参与三/四点判定。"""
        view = self._view(qapp, qtbot, range_start=100)
        assert view.insert_clip("sv1", 0, 47, playhead=30, speed=2.0) is True
        (clip,) = self._clips(view)
        assert clip.timeline_start == 30  # 播放头，非 range In


class TestLockedTrackFeedback(TestTimelineViewWiring):
    def test_insert_on_locked_track_feedback_not_exception(
            self, qapp, qtbot) -> None:
        """F2.2：锁轨插入明确反馈（状态栏语义）而非向 Qt 槽外抛异常。"""
        view = self._view(qapp, qtbot)
        view._timeline = view._timeline._rebuild(
            video_tracks=(EditTrack(
                "v1", "V1", "video", 0, is_locked=True),))
        messages = []
        view.status_message_requested.connect(messages.append)
        assert view.insert_clip("sv1", 0, 47, playhead=0) is False
        assert messages and "locked" in messages[0]
        assert self._clips(view) == []
