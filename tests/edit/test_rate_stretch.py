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
        """0.5x / clamp 下界 0.1x / clamp 上界 8.0x。"""
        view = _make_view()
        # 0.5x：新时长 = 96/0.5 = 192
        view._on_rate_stretch_finished("c1", "v1", 96, 192)
        clip = view._timeline.get_clip_by_id("c1")
        assert clip.effective_speed() == pytest.approx(0.5)

        # clamp 下界：请求时长 9600 → speed 0.01 → clamp 到 0.1
        view._on_rate_stretch_finished("c1", "v1", 96, 9600)
        clip = view._timeline.get_clip_by_id("c1")
        assert clip.effective_speed() == pytest.approx(0.1)

        # clamp 上界：请求时长 6 → speed 16 → clamp 到 8.0
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
