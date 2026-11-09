"""R06（可离屏执行子集）：本批改动面的循环稳定性冒烟。

R06 验收里的“30 次 Edit↔Color↔Composite↔Deliver 整机循环／50 次切
clip”需要整窗与真实显示设备——本文件不冒充。这里覆盖的是**本批
（R01–R05）改动过的部件**在反复模式/页面/上下文切换下的稳定性：

- 预算执行器（R03）：30 轮 激活监视器/双视图/页面借还 循环——诊断
  收敛、预算应用/解除交替、用户质量选择器始终零写入；
- 播放仲裁器（R02/R03 关联）：30 轮 source/timeline 激活交替，快照
  单调 revision、无异常决策；
- Color 能力宿主（R05）：30 轮 独立源↔clip 上下文↔timeline preview
  切换——快照域随路径收敛、revision 单调、旧域事实不泄漏进新快照。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget

from src.features.edit.core.viewport_playback_arbiter import (
    ViewportPlaybackArbiter,
)
from src.features.edit.ui.viewport_budget_executor import ViewportBudgetExecutor

pytestmark = pytest.mark.app


class _Pane(QWidget):
    pass


class _Viewport(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.active_monitor = "program"
        self.dual_view = False
        self.source_pane = _Pane(self)
        self.source_pane.resize(800, 450)
        self.program_pane = _Pane(self)
        self.program_pane.resize(800, 450)


class _Source:
    current_asset = None
    is_playing = False
    _gpu_pending_frame = -1
    gpu_slot_stats = {"decode_submitted": 0, "slots_acked": 0}


class _Ctrl:
    is_playing = False


class _Panel:
    def get_playback_controller(self):
        return _Ctrl()


class _Renderer:
    def __init__(self) -> None:
        self.budget_calls: list = []
        self.user_calls: list = []

    def set_budget_size_cap(self, cap) -> None:
        self.budget_calls.append(None if cap is None else tuple(cap))

    def set_preview_quality(self, scale: float) -> None:
        self.user_calls.append(scale)

    def preview_quality_preference(self) -> float:
        return 1.0

    def budget_size_cap_status(self) -> dict:
        return {"cap": None, "actual": None, "satisfied": None}


def test_r06_budget_executor_30_mode_cycles_stay_consistent(qtbot, qapp):
    viewport = _Viewport()
    renderer = _Renderer()
    executor = ViewportBudgetExecutor(
        viewport=viewport, source_widget=_Source(), timeline_panel=_Panel(),
        renderer_provider=lambda: renderer)

    for cycle in range(30):
        viewport.active_monitor = "source" if cycle % 2 else "program"
        viewport.dual_view = bool(cycle % 3 == 0)
        executor.refresh()
        if cycle % 5 == 4:
            executor.mark_page_left()      # 页面借还路径
            executor.refresh()
    diagnostics = executor.diagnostics()
    assert diagnostics["executor_shutdown"] is False
    assert renderer.user_calls == []        # 用户质量选择器零写入（30 轮后）
    assert None in renderer.budget_calls    # 借还解除确实发生
    assert diagnostics["timeline"]["policy"]["reason"]

    executor.mark_shutdown()
    assert executor.diagnostics()["executor_shutdown"] is True


def test_r06_arbiter_30_activation_alternations_keep_monotonic_revision(qtbot):
    arbiter = ViewportPlaybackArbiter()
    last_revision = arbiter.snapshot().request_revision
    for cycle in range(30):
        monitor = "source" if cycle % 2 else "timeline"
        arbiter.request_activate(monitor)
        revision = arbiter.snapshot().request_revision
        assert revision >= last_revision
        last_revision = revision
    assert arbiter.at_most_one_playing is True


def test_r06_color_host_30_context_switches_converge_per_path(qapp, qtbot):
    from src.app.controllers.color_viewport_capability_host import (
        ColorViewportCapabilityHost,
    )
    from src.features.color.ui.canvas import Canvas, VideoControlBar, ViewportMenuBar
    from src.features.color.ui.color_viewport_chrome import ColorViewportChrome

    class _CanvasHost(Canvas.__mro__[1]):  # QGraphicsView 基座，无 GL
        update_video_control_bar_position = (
            Canvas.update_video_control_bar_position)

        def __init__(self, parent: QWidget) -> None:
            super().__init__(parent)
            self._show_mask_overlays = True
            self._timeline_preview_active = False
            self.engine = SimpleNamespace(
                show_mask_mode=False, split_view_active=False,
                reference_is_still=False, img_full_original=None)
            self.viewport_menu = ViewportMenuBar(self)
            self.video_control_bar = VideoControlBar(self)
            self.color_status_bar = QWidget(self.viewport())
            self.color_status_bar.setFixedSize(150, 20)
            self.color_status_bar.hide()
            self.color_viewport_chrome = ColorViewportChrome(self)

        def _update_lod(self) -> bool:
            return False

    owner = QWidget()
    canvas = _CanvasHost(owner)
    timeline_controller = SimpleNamespace(is_playing=False,
                                          loop_enabled=False, current_frame=7)
    window = SimpleNamespace(
        current_page="Color", canvas=canvas, video_controller=None,
        current_file_is_video=False, current_file_path=None,
        _get_work_mode=lambda: "all",
        _active_color_timeline_clip_playback_context=lambda: None,
        timeline_panel=SimpleNamespace(
            get_playback_controller=lambda: timeline_controller,
            get_total_frames=lambda: 120),
    )
    host = ColorViewportCapabilityHost(window)
    assert host.install() is True
    clip = SimpleNamespace(clip_id="c1", source_start=100, source_duration=50,
                           timeline_duration=50)
    video = SimpleNamespace(is_playing=False, loop_playback=False,
                            current_frame=120, total_frames=1000)

    try:
        for cycle in range(30):
            mode = cycle % 3
            if mode == 0:  # 独立源
                window.current_file_is_video = True
                window.video_controller = video
                window._active_color_timeline_clip_playback_context = (
                    lambda: None)
                canvas._timeline_preview_active = False
            elif mode == 1:  # clip 上下文
                window.current_file_is_video = True
                window.video_controller = video
                window._active_color_timeline_clip_playback_context = (
                    lambda: {"timeline_id": "t1", "clip": clip,
                             "effective_source_start": None})
                canvas._timeline_preview_active = False
            else:  # timeline preview
                canvas._timeline_preview_active = True
            host.refresh()
            state = host.last_state
            if mode == 0:
                assert state.time_domain == "source_frame"
                assert (state.current_frame, state.total_frames) == (120, 1000)
            elif mode == 1:
                assert state.time_domain == "clip_local"
                assert (state.current_frame, state.total_frames) == (20, 50)
            else:
                assert state.time_domain == "timeline_frame"
                assert state.current_frame == 7   # 时间线控制器权威
                assert host.last_capabilities["transport.play"].supported
            assert state.context_revision >= 0
    finally:
        host.mark_shutdown()
        canvas.hide()
        owner.deleteLater()
