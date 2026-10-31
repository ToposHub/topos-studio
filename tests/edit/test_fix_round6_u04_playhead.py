"""第六轮审计 U04 回归：暂停导航必须同步可见播放头。

真实窗口审计发现：点 Program Next 后 controller=26 但视图仍停在 25；
点 Home 后 controller=0 而视图/页面仍显示 25。根因是
``_on_controller_frame_changed`` 只更新时间码，可见播放头仅由播放
timer 的 frame_ready 驱动，暂停态的 step/stop/seek 无法覆盖。

修复后：未播放时 frame_changed 同步推送视图播放头（set_playhead →
playhead_changed → EditPage 更新页面帧并请求预览，带去重）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication

from src.features.edit.core.edit_playback_controller import (
    EditPlaybackController,
)
from src.features.edit.ui.timeline_panel import TimelinePanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def panel(qapp):
    panel = TimelinePanel()
    panel.resize(1200, 400)
    panel.show()
    panel.set_total_frames(500)
    controller = panel.get_playback_controller()
    controller.seek(25)
    qapp.processEvents()
    # 基线：视图播放头与控制器同帧。
    assert panel.timeline_view.get_playhead_frame() == 25
    yield panel
    panel.close()


def _visible_frame(panel: TimelinePanel) -> int:
    return int(panel.timeline_view.get_playhead_frame())


def test_next_frame_moves_visible_playhead(panel, qapp):
    """U04：Program Next 之后视图播放头必须跟控制器同帧。"""
    panel._on_next_frame_clicked()
    qapp.processEvents()

    controller = panel.get_playback_controller()
    assert controller.current_frame == 26
    assert _visible_frame(panel) == 26, "可见播放头仍停在旧帧"


def test_prev_frame_moves_visible_playhead(panel, qapp):
    panel._on_prev_frame_clicked()
    qapp.processEvents()

    controller = panel.get_playback_controller()
    assert controller.current_frame == 24
    assert _visible_frame(panel) == 24


def test_home_moves_visible_playhead_to_zero(panel, qapp):
    """U04：Home（stop→0）后控制器 0、视图也必须是 0，不再停在 25。"""
    panel._on_jump_start_clicked()
    qapp.processEvents()

    controller = panel.get_playback_controller()
    assert controller.current_frame == 0
    assert _visible_frame(panel) == 0


def test_end_jump_moves_visible_playhead(panel, qapp):
    panel._on_jump_end_clicked()
    qapp.processEvents()

    controller = panel.get_playback_controller()
    assert controller.current_frame == 499
    assert _visible_frame(panel) == 499


def test_timecode_seek_moves_visible_playhead(panel, qapp):
    panel._seek_to_frame(120)
    qapp.processEvents()

    controller = panel.get_playback_controller()
    assert controller.current_frame == 120
    assert _visible_frame(panel) == 120


def test_external_seek_signal_moves_visible_playhead(panel, qapp):
    """外部（如 AI/播放列表）经 frame_changed 驱动也要同步可见播放头。"""
    controller: EditPlaybackController = panel.get_playback_controller()
    controller.pause()
    controller.seek(200)
    qapp.processEvents()

    assert controller.current_frame == 200
    assert _visible_frame(panel) == 200


def test_paused_navigation_emits_view_playhead_signal(panel, qapp):
    """视图 playhead_changed 是 EditPage 页面帧/预览请求的入口——
    暂停导航必须让它发出（这是“四端同帧”里页面与预览两端的通路）。"""
    received: list[int] = []
    panel.timeline_view.playhead_changed.connect(received.append)

    panel._on_next_frame_clicked()
    qapp.processEvents()

    assert 26 in received, "暂停 step 未驱动视图 playhead_changed"
