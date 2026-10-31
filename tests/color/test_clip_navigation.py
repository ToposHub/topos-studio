"""P1-COLOR-20：Color 页镜头导航（上一镜/下一镜）。

验收核心：
- 不离开 Color 页即可按镜头浏览：navigate_clip 复用点击激活链路
  （同一 clip_activated 信号 → grade context 切换与手动点击一致）
- 边界不循环；无激活时定向到首/尾镜
- 位置标签 (n/total) 与按钮可用态随激活更新
"""

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from PyQt6.QtWidgets import QApplication

from src.features.color.ui.timeline_clip_strip import (
    ColorClipEntry,
    TimelineClipStrip,
)


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _entry(i: int) -> ColorClipEntry:
    return ColorClipEntry(
        timeline_id='t1', clip_id=f'c{i}', track_id='v1', track_name='V1',
        asset_id=f'a{i}', asset_name=f'clip{i}.mov', asset_path='/x.mov',
        media_type='video', timeline_start=i * 100, timeline_duration=100,
        source_start=0, source_duration=100, color_stack_id=None,
        color_shared=False)


@pytest.fixture
def strip(qtbot):
    widget = TimelineClipStrip()
    qtbot.addWidget(widget)
    widget.resize(800, 120)
    widget.show()
    widget._entries = [_entry(i) for i in range(4)]
    widget._rebuild_cards()
    return widget


class TestNavigateClip:
    def test_forward_emits_activation(self, strip, qtbot):
        strip.set_active_clip('c1')
        fired = []
        strip.clip_activated.connect(lambda *a: fired.append(a))
        with qtbot.waitSignal(strip.clip_activated):
            assert strip.navigate_clip(1) is True
        assert strip._active_clip_id == 'c2'
        assert fired[-1] == ('t1', 'c2', 200, 0)

    def test_backward(self, strip):
        strip.set_active_clip('c2')
        assert strip.navigate_clip(-1) is True
        assert strip._active_clip_id == 'c1'

    def test_edges_do_not_wrap(self, strip):
        strip.set_active_clip('c0')
        assert strip.navigate_clip(-1) is False
        assert strip._active_clip_id == 'c0'
        strip.set_active_clip('c3')
        assert strip.navigate_clip(1) is False
        assert strip._active_clip_id == 'c3'

    def test_no_active_directional_start(self, strip):
        strip._active_clip_id = None
        assert strip.navigate_clip(-1) is True
        assert strip._active_clip_id == 'c3'
        strip._active_clip_id = None
        assert strip.navigate_clip(1) is True
        assert strip._active_clip_id == 'c0'

    def test_empty_strip(self, qtbot):
        widget = TimelineClipStrip()
        qtbot.addWidget(widget)
        assert widget.navigate_clip(1) is False
        assert widget.active_clip_position() is None

    def test_position_label(self, strip):
        strip.set_active_clip('c1')
        assert strip.active_clip_position() == (2, 4)
        assert strip._nav_position_label.text() == '2/4'
        strip.navigate_clip(1)
        assert strip._nav_position_label.text() == '3/4'

    def test_nav_buttons_enable_state(self, strip):
        strip.set_active_clip('c0')
        assert not strip._prev_clip_btn.isEnabled()
        assert strip._next_clip_btn.isEnabled()
        strip.set_active_clip('c3')
        assert strip._prev_clip_btn.isEnabled()
        assert not strip._next_clip_btn.isEnabled()
        strip.set_active_clip('')
        assert not strip._prev_clip_btn.isEnabled()
        assert not strip._next_clip_btn.isEnabled()

    def test_clear_resets_navigation(self, strip):
        strip.set_active_clip('c1')
        strip.clear()
        assert strip.active_clip_position() is None
        assert not strip._prev_clip_btn.isEnabled()

    def test_same_signal_as_manual_click(self, strip, qtbot):
        """导航与手动点击走同一 clip_activated 信号（grade context 一致）。"""
        strip.set_active_clip('c0')
        received = []
        strip.clip_activated.connect(
            lambda t, c, ts, ss: received.append((t, c, ts, ss)))
        strip.navigate_clip(1)
        assert received == [('t1', 'c1', 100, 0)]
        # 手动点击产生相同形状的信号
        strip._on_card_clicked('t1', 'c2', 200, 0)
        assert received[-1] == ('t1', 'c2', 200, 0)


class TestGradeFilter:
    """P1-COLOR-20：按调色状态过滤镜头列表。"""

    @staticmethod
    def _entry_with_grade(i: int, graded: bool) -> ColorClipEntry:
        base = _entry(i)
        return ColorClipEntry(
            timeline_id=base.timeline_id, clip_id=base.clip_id,
            track_id=base.track_id, track_name=base.track_name,
            asset_id=base.asset_id, asset_name=base.asset_name,
            asset_path=base.asset_path, media_type=base.media_type,
            timeline_start=base.timeline_start,
            timeline_duration=base.timeline_duration,
            source_start=base.source_start,
            source_duration=base.source_duration,
            color_stack_id=base.color_stack_id,
            color_shared=base.color_shared, color_graded=graded)

    @pytest.fixture
    def mixed_strip(self, qtbot):
        widget = TimelineClipStrip()
        qtbot.addWidget(widget)
        widget.resize(800, 120)
        widget.show()
        widget._all_entries = [
            self._entry_with_grade(0, False),
            self._entry_with_grade(1, True),   # c1 已调色
            self._entry_with_grade(2, False),
            self._entry_with_grade(3, True),   # c3 已调色
        ]
        widget._apply_grade_filter()
        return widget

    def test_default_all(self, mixed_strip):
        assert mixed_strip.grade_filter == "all"
        assert [e.clip_id for e in mixed_strip._entries] == \
            ["c0", "c1", "c2", "c3"]

    def test_filter_ungraded(self, mixed_strip):
        assert mixed_strip.set_grade_filter("ungraded") is True
        assert [e.clip_id for e in mixed_strip._entries] == ["c0", "c2"]

    def test_filter_graded(self, mixed_strip):
        mixed_strip.set_grade_filter("graded")
        assert [e.clip_id for e in mixed_strip._entries] == ["c1", "c3"]

    def test_invalid_and_noop_modes(self, mixed_strip):
        assert mixed_strip.set_grade_filter("nonsense") is False
        assert mixed_strip.set_grade_filter("all") is False  # 已是 all
        assert mixed_strip.grade_filter == "all"

    def test_navigation_respects_filter(self, mixed_strip, qtbot):
        mixed_strip.set_grade_filter("ungraded")   # 显示列表 = [c0, c2]
        mixed_strip.set_active_clip("c0")
        with qtbot.waitSignal(mixed_strip.clip_activated):
            assert mixed_strip.navigate_clip(1) is True
        assert mixed_strip._active_clip_id == "c2"  # 跳过被过滤的 c1

    def test_refresh_reapplies_filter(self, mixed_strip):
        mixed_strip.set_grade_filter("graded")
        mixed_strip.refresh()  # 从 project/timeline 重建（此处为 None → 空）
        assert mixed_strip.grade_filter == "graded"
        assert mixed_strip._entries == []

    def test_clear_empties_both_lists(self, mixed_strip):
        mixed_strip.clear()
        assert mixed_strip._entries == []
        assert mixed_strip._all_entries == []
