"""B4: 比例拉伸工具（Rate Stretch）测试。

覆盖：
- EditTool.RATE_STRETCH 枚举与工具切换
- 拖拽换算公式表：speed = 源时长 / 新时长（含 clamp 上下界）
- 提交走 ChangeClipSpeedCommand（linked 音频联动由该命令既有语义保证）
- undo 还原
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import (  # noqa: E402
    MAX_SPEED,
    MIN_SPEED,
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")


def _clip() -> ClipInstance:
    # 1x 基线：源 96 帧 → 时间线 96 帧
    return ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=96,
        source_start=0, source_duration=96,
    )


def _make_view():
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=(_clip(),),
    )
    view.load_timeline(TimelineSequence(video_tracks=(track,), fps=Rational(24, 1)))
    return view


class TestRateStretchTool:
    def test_enum_exists(self) -> None:
        from src.features.edit.ui.timeline_items import EditTool

        assert EditTool.RATE_STRETCH.value == "rate_stretch"

    def test_view_set_tool(self, qapp) -> None:
        from src.features.edit.ui.timeline_items import EditTool

        view = _make_view()
        view.set_tool(EditTool.RATE_STRETCH)
        assert view._active_tool == EditTool.RATE_STRETCH

    def test_speed_command_committed(self, qapp) -> None:
        """拖出 2x（新时长 = 96/2 = 48）：速度命令提交，片段变速。"""
        from src.features.edit.ui.timeline_items import EditTool

        view = _make_view()
        view.set_tool(EditTool.RATE_STRETCH)
        view._on_rate_stretch_finished("c1", "v1", 96, 48)
        clip = view._timeline.get_clip_by_id("c1")
        assert clip.effective_speed() == pytest.approx(2.0)

    def test_formula_and_clamp_bounds(self, qapp) -> None:
        """0.5x / clamp 下界 0.1x / clamp 上界 8.0x。

        B18 契约：original_duration 必须与模型当前时长一致（陈旧信号
        忽略，见 TestRateStretchB18），逐例从 96 帧基线新建视图。
        """
        # 0.5x：新时长 = 96/0.5 = 192
        view = _make_view()
        view._on_rate_stretch_finished("c1", "v1", 96, 192)
        clip = view._timeline.get_clip_by_id("c1")
        assert clip.effective_speed() == pytest.approx(0.5)

        # clamp 下界：请求时长 9600 → speed 0.01 → clamp 到 0.1
        view = _make_view()
        view._on_rate_stretch_finished("c1", "v1", 96, 9600)
        clip = view._timeline.get_clip_by_id("c1")
        assert clip.effective_speed() == pytest.approx(0.1)

        # clamp 上界：请求时长 6 → speed 16 → clamp 到 8.0
        view = _make_view()
        view._on_rate_stretch_finished("c1", "v1", 96, 6)
        clip = view._timeline.get_clip_by_id("c1")
        assert clip.effective_speed() == pytest.approx(8.0)

    def test_undo_restores_speed(self, qapp) -> None:
        view = _make_view()
        before = view._timeline.get_clip_by_id("c1")
        view._on_rate_stretch_finished("c1", "v1", 96, 48)  # 2x
        undone = view._command_stack.undo(view._timeline)
        restored = undone.get_clip_by_id("c1")
        assert restored.effective_speed() == before.effective_speed()


class TestRateStretchB18:
    """B18：混合帧率时基、非目标参数保留与高级路径分派。"""

    def test_b18_mixed_fps_hits_requested_duration(self, qapp) -> None:
        """反例：48fps 源 200 帧/24fps 时间线 100 帧拉到 50 → 2x/50 帧。"""
        from src.features.edit.core.clip_params import SpeedParams

        clip = ClipInstance(
            "v", "asset", "v1", 0, 100, 0, 200,
            speed=SpeedParams(reverse=True, preserve_pitch=True),
            _source_fps_num=48, _source_fps_den=1,
            _timeline_fps_num=24, _timeline_fps_den=1,
        )
        view = _make_view()
        view.load_timeline(TimelineSequence(
            video_tracks=(EditTrack(
                "v1", "V1", "video", 0, clips=(clip,)),),
            fps=Rational(24, 1)))

        view._on_rate_stretch_finished("v", "v1", 100, 50)

        changed = view.get_timeline().get_clip_by_id("v")
        assert changed.timeline_duration == 50
        assert changed.effective_speed() == pytest.approx(2.0)
        # 非目标参数不丢（反例中 reverse/pitch 均被重置为 False）
        assert changed.speed.reverse is True
        assert changed.speed.preserve_pitch is True

    def test_b18_stale_duration_signal_ignored(self, qapp) -> None:
        """信号里的 original_duration 与模型不一致（陈旧信号）→ 忽略。"""
        view = _make_view()
        before = view.get_timeline()
        view._on_rate_stretch_finished("c1", "v1", 48, 24)  # 模型仍是 96
        assert view.get_timeline() is before

    def test_b18_zero_change_no_history(self, qapp) -> None:
        """请求速度与当前相同（clamp 后等于原速）→ 不入历史。"""
        view = _make_view()
        view._on_rate_stretch_finished("c1", "v1", 96, 96)
        assert not view._command_stack._undo_stack

    def test_b18_complex_clip_requests_advanced_backend(self, qapp) -> None:
        """带速度点/动画的片段不发基础命令，走高级请求且模型不变。"""
        from src.features.edit.core.clip_params import SpeedParams, SpeedPoint

        clip = ClipInstance(
            "v", "asset", "v1", 0, 96, 0, 96,
            speed=SpeedParams(speed=1.0, speed_points=(
                SpeedPoint(source_offset=16, speed=2.0),)),
        )
        clip = clip.with_keyframe_added("opacity", 10, 0.2)
        view = _make_view()
        view.load_timeline(TimelineSequence(
            video_tracks=(EditTrack(
                "v1", "V1", "video", 0, clips=(clip,)),),
            fps=Rational(24, 1)))
        requests: list = []
        view.clip_speed_settings_requested.connect(
            lambda *args: requests.append(args))
        before = view.get_timeline()

        view._on_rate_stretch_finished("v", "v1", 96, 48)

        assert len(requests) == 1
        assert requests[0][2]["operation"] == "retime_controls"
        assert requests[0][2]["action"] == "stretch"
        assert view.get_timeline() is before
        assert not view._command_stack._undo_stack

    def test_b18_relative_speed_accumulates(self, qapp) -> None:
        """已有 2x 的片段再拉伸 96→192：速度按当前比换算回 1x。"""
        from src.features.edit.core.clip_params import SpeedParams

        clip = ClipInstance(
            "v", "asset", "v1", 0, 48, 0, 96,
            speed=SpeedParams(speed=2.0),
        )
        view = _make_view()
        view.load_timeline(TimelineSequence(
            video_tracks=(EditTrack(
                "v1", "V1", "video", 0, clips=(clip,)),),
            fps=Rational(24, 1)))

        view._on_rate_stretch_finished("v", "v1", 48, 96)

        changed = view.get_timeline().get_clip_by_id("v")
        assert changed.effective_speed() == pytest.approx(1.0)
        assert changed.timeline_duration == 96
