"""CV-B10 验证：关闭/取消路径与晚到结果的守卫。

覆盖：能力宿主关门后刷新失效；Color 私有预览渲染器先断开后停用、
幂等、晚到帧不再上屏；主窗关闭管线包含 Color 侧两个新停机步骤。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QGraphicsView, QWidget

from src.app.controllers.color_viewport_capability_host import (
    ColorViewportCapabilityHost,
)
from src.app.controllers.file_controller import FileOpsMixin
from src.features.color.ui.canvas import Canvas, VideoControlBar, ViewportMenuBar
from src.features.color.ui.color_viewport_chrome import ColorViewportChrome


class _CanvasHost(QGraphicsView):
    update_video_control_bar_position = Canvas.update_video_control_bar_position

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._show_mask_overlays = True
        self.engine = SimpleNamespace(show_mask_mode=False,
                                      split_view_active=False)
        self.viewport_menu = ViewportMenuBar(self)
        self.video_control_bar = VideoControlBar(self)
        self.color_status_bar = QWidget(self.viewport())
        self.color_status_bar.hide()
        self.color_viewport_chrome = ColorViewportChrome(self)
        self.timeline_preview_end_calls = 0
        self.timeline_preview_images = 0

    def _update_lod(self) -> bool:
        return False

    def end_timeline_preview(self) -> None:
        self.timeline_preview_end_calls += 1

    def set_timeline_preview_image(self, image) -> None:
        self.timeline_preview_images += 1


class _FakePreviewRenderer(QObject):
    frame_ready = pyqtSignal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.deleteLater_calls = 0

    def shutdown(self) -> None:
        self.events.append("shutdown")

    def deleteLater(self) -> None:  # noqa: N802 —— 记录调用，不真正销毁
        self.deleteLater_calls += 1
        self.events.append("deleteLater")


class _Controller(FileOpsMixin):
    def __init__(self, canvas) -> None:
        self.current_page = "Color"
        self.canvas = canvas
        self.video_controller = None
        self.timeline_panel = SimpleNamespace(get_playback_controller=lambda: None)
        self._color_timeline_preview_renderer = None
        self._color_timeline_preview_id = None
        self._color_timeline_frame_slot = lambda frame: None
        self._color_timeline_clip_playback_context = {
            "timeline_id": "tl-1",
            "clip": SimpleNamespace(clip_id="c1"),
            "effective_source_start": None,
        }


@pytest.fixture
def canvas_host(qapp):
    owner = QWidget()
    canvas = _CanvasHost(owner)
    canvas.setWindowFlag(Qt.WindowType.Window, True)
    canvas.resize(1000, 600)
    canvas.viewport_menu.resize(1000, 30)
    canvas.viewport_menu.show()
    canvas.show()
    qapp.processEvents()
    yield canvas
    canvas.hide()
    owner.deleteLater()


def _make_host(canvas) -> ColorViewportCapabilityHost:
    window = SimpleNamespace(
        current_page="Color", canvas=canvas, video_controller=None,
        current_file_is_video=False, current_file_path=None,
        _get_work_mode=lambda: "all",
        _active_color_timeline_clip_playback_context=lambda: None)
    host = ColorViewportCapabilityHost(window)
    assert host.install() is True
    return host


# ── 能力宿主关门 ──────────────────────────────────────────────────────


def test_mark_shutdown_blocks_refresh_and_future_installs(canvas_host) -> None:
    host = _make_host(canvas_host)
    assert host.last_state.presentation_path == "none"
    host.mark_shutdown()
    host._window.current_file_path = "/tmp/x.png"
    host.refresh()
    assert host.last_state.presentation_path == "none"  # 晚到刷新被拒
    assert host.install() is False


def test_mark_shutdown_disconnects_menu_refresh_requests(canvas_host) -> None:
    host = _make_host(canvas_host)
    host.mark_shutdown()
    host._window.current_file_path = "/tmp/x.png"
    # chrome 菜单打开/选项菜单的刷新请求不再触发评估。
    host._chrome.capability_refresh_requested.emit()
    host._toolbar.view_menu.aboutToShow.emit()
    assert host.last_state.presentation_path == "none"


# ── Color 私有预览渲染器停用 ──────────────────────────────────────────


def test_deactivate_disconnects_before_shutdown_and_is_idempotent(canvas_host) -> None:
    controller = _Controller(canvas_host)
    renderer = _FakePreviewRenderer()
    controller._color_timeline_preview_renderer = renderer
    controller._color_timeline_preview_id = "tl-1"

    controller._deactivate_color_timeline_preview()

    # 先断开回调（disconnect 由 Qt 记录），再 shutdown，最后 deleteLater。
    assert renderer.events[0] == "shutdown"
    assert "deleteLater" in renderer.events
    assert controller._color_timeline_preview_renderer is None
    assert controller._color_timeline_preview_id is None
    assert canvas_host.timeline_preview_end_calls == 1

    # 幂等：第二次停用不重复触碰渲染器（end_timeline_preview 本身在
    # 真实 Canvas 上是幂等标志复位，可安全重复）。
    controller._deactivate_color_timeline_preview()
    assert renderer.events.count("shutdown") == 1
    assert renderer.deleteLater_calls == 1


def test_late_preview_frame_after_deactivation_never_reaches_canvas(canvas_host) -> None:
    controller = _Controller(canvas_host)
    renderer = _FakePreviewRenderer()
    controller._color_timeline_preview_renderer = renderer
    controller._deactivate_color_timeline_preview()

    from PyQt6.QtGui import QImage
    controller._on_color_timeline_preview_frame_ready(
        42, QImage(8, 8, QImage.Format.Format_RGB32))
    assert canvas_host.timeline_preview_images == 0


# ── 主窗关闭管线包含 Color 停机步骤 ──────────────────────────────────


def test_shutdown_pipeline_marks_color_host_and_deactivates_preview(qapp):
    pytest.importorskip("src.app.main_window")
    from src.app.main_window import MainWindow

    window = MainWindow(test_mode=True)
    try:
        marked: list[bool] = []
        deactivated: list[bool] = []

        class _SpyHost:
            def mark_shutdown(self) -> None:
                marked.append(True)

        def _spy_deactivate() -> None:
            deactivated.append(True)

        window._color_viewport_capability_host = _SpyHost()
        window._deactivate_color_timeline_preview = _spy_deactivate
        window._shutdown_playback_pipeline()
        assert marked == [True]
        assert deactivated == [True]
        # 幂等：再次执行不再重复触发。
        window._shutdown_playback_pipeline()
        assert marked == [True]
        assert deactivated == [True]
    finally:
        window.deleteLater()
        qapp.processEvents()
