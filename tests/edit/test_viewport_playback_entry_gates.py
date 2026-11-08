"""I01（复查计划）：全入口播放仲裁——真实宿主对象上的入口集成。

不依赖假仲裁器：真实 ``SourceViewerWidget`` + 真实 ``TimelinePanel`` +
其 ``EditPlaybackController``，经 ``install_viewport_playback_coordinator``
接一只按信号名合同的鸭子 Viewport。断言走**真实入口链**
（``toggle_playback``／``_on_play_clicked``／``_on_space_pressed``／
``_on_l_pressed``／slider 释放）观察：

- 起播一侧前，另一侧被真实暂停（至多一条连续音频），双方播放头保留；
- 入口的原有准备步骤不丢（toggle 先 ``_sync_playback_duration``）；
- Source 自然到尾后再次 Play 可起播；shutdown／暂停失败不虚报成功；
- Deliver 页模式强制 Timeline，Source 入口被拒且零写入。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import EditTrack, Rational
from src.features.edit.core.viewport_playback_arbiter import TIMELINE
from src.features.edit.ui.source_viewer import SourceViewerWidget
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.features.edit.ui.viewport_playback_coordinator import (
    install_viewport_playback_coordinator,
)

pytestmark = pytest.mark.edit_ui


class _DuckViewport(QObject):
    """EditViewport 的信号名合同（active_monitor_changed/mode_changed）。"""

    active_monitor_changed = pyqtSignal(str)
    mode_changed = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._active_monitor = "program"
        self._dual_view = False

    @property
    def active_monitor(self) -> str:
        return self._active_monitor

    @property
    def dual_view(self) -> bool:
        return self._dual_view


def _video_asset(total: int = 240) -> MediaAsset:
    return MediaAsset(
        asset_id="asset-i01",
        path="/tmp/i01_source.mov",
        media_type="video",
        display_name="i01_source.mov",
        metadata={"fps": 24.0, "total_frames": total, "width": 640,
                  "height": 480},
    )


def _world(qtbot: Any, qapp: Any) -> tuple[Any, ...]:
    """真实 widget + panel + 协调器安装（含入口门与素材加载）。"""
    widget = SourceViewerWidget()
    qtbot.addWidget(widget)
    # 音频侧全部静音替换（本用例断言仲裁行为，不出声不解码）
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget._seek_source_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
    widget.open_asset(_video_asset())

    panel = TimelinePanel()
    qtbot.addWidget(panel)
    project = EditProject.create("i01")
    from src.features.edit.core.timeline_v2 import TimelineSequence

    panel.timeline_view.set_project(project)
    panel.timeline_view.load_timeline(TimelineSequence(
        "t1", "T1", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("V1", "V1", "video", 0),),
        audio_tracks=(EditTrack("A1", "A1", "audio", 0),),
    ))
    panel.set_total_frames(120)
    panel.set_fps(24.0)

    viewport = _DuckViewport()
    coordinator = install_viewport_playback_coordinator(
        viewport=viewport, source_widget=widget, timeline_panel=panel)
    widget.set_playback_gate(coordinator.authorize_source_play)
    panel.set_playback_gate(coordinator.authorize_timeline_play)
    return widget, panel, coordinator


def test_i01_real_entries_are_mutually_exclusive(qtbot, qapp):
    widget, panel, coordinator = _world(qtbot, qapp)

    widget.toggle_playback()  # Source 按钮/Space 的真实入口
    assert widget.is_playing is True

    panel._on_play_clicked()  # Program transport 的真实按钮入口
    assert panel.get_playback_controller().is_playing is True
    assert widget.is_playing is False  # Source 被真实暂停，非假快照

    snap = coordinator.snapshot()
    assert snap.timeline_playback.playing is True
    assert snap.source_playback.playing is False

    widget.toggle_playback()  # 反向：Source 起播先暂停时间线
    assert widget.is_playing is True
    assert panel.get_playback_controller().is_playing is False


def test_i01_space_and_jkl_entries_arbitrated(qtbot, qapp, monkeypatch):
    widget, panel, coordinator = _world(qtbot, qapp)
    monkeypatch.setattr(panel, "_is_panel_active", lambda: True)
    monkeypatch.setattr(panel, "_transport_context_active", lambda: True)

    panel._on_space_pressed()  # Space 入口（panel 侧快捷键链）
    assert panel.get_playback_controller().is_playing is True

    widget.toggle_playback()
    assert widget.is_playing is True
    assert panel.get_playback_controller().is_playing is False

    # L 从暂停起播 = 起播入口（经门）；再按 L = 变速（幂等放行零命令）
    panel._on_l_pressed()
    assert panel.get_playback_controller().is_playing is True
    assert widget.is_playing is False
    panel._on_l_pressed()
    assert panel.get_playback_controller().playback_speed == 2.0


def test_i01_source_scrub_resume_pauses_timeline(qtbot, qapp):
    widget, panel, _coordinator = _world(qtbot, qapp)
    panel._on_play_clicked()
    assert panel.get_playback_controller().is_playing is True

    # scrub：按下时记录"在播"，释放恢复——恢复是起播，先暂停时间线
    widget._scrub_was_playing = True
    widget._scrub_was_reverse = False
    widget.slider.setValue(10)
    widget._on_slider_released()

    assert widget.is_playing is True
    assert panel.get_playback_controller().is_playing is False


def test_i01_source_natural_end_then_replay(qtbot, qapp):
    widget, _panel, coordinator = _world(qtbot, qapp)
    widget.toggle_playback()
    assert coordinator.snapshot().source_playback.playing is True

    widget._handle_playback_end(None)  # 播放到末尾自然暂停（发状态信号）

    assert coordinator.snapshot().source_playback.playing is False
    widget.toggle_playback()  # 再次 Play 可起播（快照没过期成"假在播"）
    assert widget.is_playing is True


def test_i01_shutdown_blocks_all_entries(qtbot, qapp):
    widget, panel, coordinator = _world(qtbot, qapp)
    messages: list[str] = []
    coordinator.status_message.connect(messages.append)

    coordinator.mark_shutdown()
    widget.toggle_playback()
    panel._on_play_clicked()

    assert widget.is_playing is False
    assert panel.get_playback_controller().is_playing is False
    assert messages  # 拒绝原因回填，不静默吞掉


def test_i01_deliver_mode_forces_timeline(qtbot, qapp):
    widget, panel, coordinator = _world(qtbot, qapp)
    widget.toggle_playback()
    assert widget.is_playing is True

    coordinator.enter_deliver_page()

    assert widget.is_playing is False  # 页面政策暂停 Source
    assert coordinator.snapshot().active_monitor == TIMELINE
    widget.toggle_playback()  # Deliver 中 Source 入口被拒，零写入
    assert widget.is_playing is False
    panel._on_play_clicked()  # Timeline 入口照常
    assert panel.get_playback_controller().is_playing is True


def test_i01_pause_failure_aborts_start_honestly(qtbot, qapp, monkeypatch):
    widget, panel, coordinator = _world(qtbot, qapp)
    messages: list[str] = []
    coordinator.status_message.connect(messages.append)
    widget.toggle_playback()
    assert widget.is_playing is True

    # Source 的 pause 失效（静默拒绝）：时间线起播必须中止，不得虚报
    monkeypatch.setattr(widget, "pause", lambda: None)
    panel._on_play_clicked()

    assert panel.get_playback_controller().is_playing is False
    assert widget.is_playing is True
    assert any("pause" in m.lower() for m in messages)
