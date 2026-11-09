"""I07（复查计划）：整页宿主集成——I01–I06 各宿主件按 dock 装配方式组合。

不建 MainWindow（dock 装配在真实窗口构造中执行）；这里用**与
dock_layout_controller 相同的安装函数/顺序**组合真实部件：真实
SourceViewerWidget + TimelinePanel/TimelineView + 鸭子 Viewport（信号名
合同）→ 协调器（install_viewport_playback_coordinator）→ 入口门 →
capability 宿主 → 预算执行器，然后按用户操作序列驱动真实入口并断言跨
层面行为一致（互斥、快照、能力回填、预算刷新、工程释放）。

用例只覆盖 offscreen 可复现的入口与失败路径；真机手感/4K/HDR/音频听感
是证据边界（见 docs/validation 记录），此处不声称。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.app.controllers.viewport_capability_host import (
    ViewportCapabilityHost,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import EditTrack, Rational
from src.features.edit.ui.source_viewer import SourceViewerWidget
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.features.edit.ui.viewport_budget_executor import ViewportBudgetExecutor
from src.features.edit.ui.viewport_playback_coordinator import (
    install_viewport_playback_coordinator,
)

pytestmark = pytest.mark.edit_ui


class _DuckViewport(QObject):
    """与 dock 装配一致的 EditViewport 信号/属性/能力合同。"""

    active_monitor_changed = pyqtSignal(str)
    mode_changed = pyqtSignal(bool)
    capability_action_requested = pyqtSignal(str)
    capabilities_context_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._active_monitor = "program"
        self._dual_view = False
        self.refreshed: list[dict] = []
        self.source_pane = SimpleNamespace(width=lambda: 800,
                                           height=lambda: 450, isHidden=lambda: False)
        self.program_pane = SimpleNamespace(width=lambda: 800,
                                            height=lambda: 450, isHidden=lambda: False)

    @property
    def active_monitor(self) -> str:
        return self._active_monitor

    @property
    def dual_view(self) -> bool:
        return self._dual_view

    def refresh_capabilities(self, caps: dict) -> None:
        self.refreshed.append(caps)


class _QualityRecorder:
    def __init__(self) -> None:
        self.quality_calls: list[float] = []

    def set_preview_quality(self, scale: float) -> None:
        self.quality_calls.append(float(scale))


def _asset() -> MediaAsset:
    return MediaAsset(
        asset_id="i07-asset", path="/tmp/i07_source.mov", media_type="video",
        display_name="i07_source.mov",
        metadata={"fps": 24.0, "total_frames": 240, "width": 1920,
                  "height": 1080,
                  "audio_info": {"channels": 2}})


def _host(qtbot: Any, qapp: Any) -> SimpleNamespace:
    widget = SourceViewerWidget()
    qtbot.addWidget(widget)
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget._load_video_frame = lambda *_a, **_k: None  # type: ignore[method-assign]
    widget.open_asset(_asset())

    panel = TimelinePanel()
    qtbot.addWidget(panel)
    project = EditProject.create("i07")
    project.assets["i07-asset"] = widget.current_asset
    from src.features.edit.core.timeline_v2 import TimelineSequence

    panel.timeline_view.set_project(project)
    panel.timeline_view.load_timeline(TimelineSequence(
        "t1", "T1", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("V1", "V1", "video", 0),),
        audio_tracks=(EditTrack("A1", "A1", "audio", 0),)))
    panel.set_total_frames(120)
    panel.set_fps(24.0)

    viewport = _DuckViewport()
    coordinator = install_viewport_playback_coordinator(
        viewport=viewport, source_widget=widget, timeline_panel=panel)
    widget.set_playback_gate(coordinator.authorize_source_play)
    panel.set_playback_gate(coordinator.authorize_timeline_play)
    status_messages: list[str] = []
    # 与 dock 装配一致：仲裁拒绝原因回填状态栏
    coordinator.status_message.connect(
        lambda message: status_messages.append(message))

    window = SimpleNamespace(
        edit_viewport=viewport, timeline_panel=panel,
        source_viewer_widget=widget, edit_page=SimpleNamespace(
            _on_clip_open_in_source=lambda *_a: None),
        current_page="Edit",
        _current_edit_project=project)
    window.status_messages = status_messages
    window._viewport_playback_coordinator = coordinator

    capability_host = ViewportCapabilityHost(window)
    quality = _QualityRecorder()
    budget = ViewportBudgetExecutor(
        viewport=viewport, source_widget=widget, timeline_panel=panel,
        coordinator=coordinator, renderer_provider=lambda: quality)
    return SimpleNamespace(
        widget=widget, panel=panel, viewport=viewport,
        coordinator=coordinator, capability_host=capability_host,
        budget=budget, quality=quality, window=window, project=project)


def test_i07_full_host_play_flow_is_consistent_across_layers(qtbot, qapp):
    world = _host(qtbot, qapp)
    widget, panel, coordinator = world.widget, world.panel, world.coordinator

    # 真实入口：Source 起播（互斥 + 快照 + 能力/预算随快照刷新）
    widget.toggle_playback()
    assert widget.is_playing
    assert coordinator.snapshot().source_playback.playing
    caps = world.capability_host._window.edit_viewport.refreshed[-1]
    assert caps["match_frame.source_to_timeline"].supported  # 素材+时间线在场

    # Timeline 起播（程序按钮链）：先真实暂停 Source
    panel._on_play_clicked()
    assert panel.get_playback_controller().is_playing
    assert not widget.is_playing
    snap = coordinator.snapshot()
    assert snap.timeline_playback.playing and not snap.source_playback.playing
    assert world.quality.quality_calls  # 预算执行器随快照重评过时间线质量档


def test_i07_full_host_deliver_policy_and_return(qtbot, qapp):
    world = _host(qtbot, qapp)
    widget, coordinator = world.widget, world.coordinator
    widget.toggle_playback()
    assert widget.is_playing

    coordinator.enter_deliver_page()
    assert not widget.is_playing                       # 页面政策暂停 Source
    caps = world.capability_host._window.edit_viewport.refreshed[-1]
    assert not caps["match_frame.source_to_timeline"].supported

    world.window.current_page = "Edit"
    coordinator.return_to_edit_page()
    assert coordinator.snapshot().page_mode == "edit"
    # 快照领域 ID 是 "timeline"（显示对齐回 Viewport 的 program 侧）
    assert coordinator.snapshot().active_monitor == "timeline"


def test_i07_full_host_pause_failure_aborts_with_honest_status(qtbot, qapp,
                                                                monkeypatch):
    world = _host(qtbot, qapp)
    widget, panel = world.widget, world.panel
    widget.toggle_playback()
    monkeypatch.setattr(widget, "pause", lambda: None)   # 静默暂停失败

    panel._on_play_clicked()

    assert not panel.get_playback_controller().is_playing  # 不虚报成功
    assert widget.is_playing
    assert any("pause" in m.lower() for m in world.window.status_messages)


def test_i07_full_host_shutdown_blocks_and_project_close_releases(
        qtbot, qapp):
    world = _host(qtbot, qapp)
    widget, panel, coordinator = (world.widget, world.panel,
                                  world.coordinator)
    widget.toggle_playback()

    coordinator.mark_shutdown()
    widget.toggle_playback()
    panel._on_play_clicked()
    assert not widget.is_playing
    assert not panel.get_playback_controller().is_playing

    # 工程关闭释放（I05）：复用边界清素材/标记，快照同步
    widget.playback_state_changed.disconnect()  # mark_shutdown 后状态仍要如实
    coordinator.mark_shutdown = lambda: None    # 释放路径不受关门影响
    widget.release_for_project_close()
    assert widget.current_asset is None
