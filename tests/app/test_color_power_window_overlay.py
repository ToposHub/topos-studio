"""CV-B05 验证：Power Window overlay 接线、Highlight/overlay 独立、Alpha 归因。

门槛（计划 CV-B05）：关闭窗口线框不改变最终画面 mask；Highlight 切换与
qualifier 面板一致且不触发第二次 render；不存在 alpha 不显示伪灰度；
overlay 显示开关与 matte 预览是两个独立状态。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsView, QWidget

from src.app.controllers.color_viewport_capability_host import (
    ColorViewportCapabilityHost,
)
from src.features.color.core.color_viewer_capabilities import (
    PRESENTATION_IMAGE,
    ColorViewerCapabilityContext,
    ColorViewerState,
    color_capability_by_action,
    evaluate_color_viewer_capabilities,
)
from src.features.color.ui.canvas import Canvas, VideoControlBar, ViewportMenuBar
from src.features.color.ui.color_viewport_chrome import ColorViewportChrome


class _CanvasHost(QGraphicsView):
    """真实 chrome + 可记录的引擎/matte 双状态替身。"""

    update_video_control_bar_position = Canvas.update_video_control_bar_position

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._show_mask_overlays = True
        self.overlay_calls: list[bool] = []
        self.matte_calls: list[tuple[bool, str]] = []
        self.engine = SimpleNamespace(
            show_mask_mode=False, split_view_active=False,
            reference_is_still=False, img_full_original=None,
            set_mask_preview_mode=self._record_matte)
        self.viewport_menu = ViewportMenuBar(self)
        # 生产 Canvas.__init__ 的既有接线：mask_toggled → 引擎 matte 预览。
        self.viewport_menu.mask_toggled.connect(
            lambda checked: self.engine.set_mask_preview_mode(checked, None))
        self.video_control_bar = VideoControlBar(self)
        self.color_status_bar = QWidget(self.viewport())
        self.color_status_bar.hide()
        self.color_viewport_chrome = ColorViewportChrome(self)

    def _record_matte(self, enabled: bool, style=None) -> None:
        self.matte_calls.append((bool(enabled), str(style)))

    def _update_lod(self) -> bool:
        return False

    def set_overlay_visible(self, visible: bool) -> None:
        self.overlay_calls.append(bool(visible))
        self._show_mask_overlays = bool(visible)


@pytest.fixture
def host(qapp):
    owner = QWidget()
    canvas = _CanvasHost(owner)
    canvas.setWindowFlag(Qt.WindowType.Window, True)
    canvas.resize(1000, 600)
    canvas.viewport_menu.resize(1000, 30)
    canvas.viewport_menu.show()
    canvas.show()
    qapp.processEvents()
    window = SimpleNamespace(
        current_page="Color", canvas=canvas, video_controller=None,
        current_file_is_video=False, current_file_path=None,
        _get_work_mode=lambda: "all",
        _active_color_timeline_clip_playback_context=lambda: None)
    capability_host = ColorViewportCapabilityHost(window)
    assert capability_host.install() is True
    yield capability_host, canvas
    canvas.hide()
    owner.deleteLater()


def test_power_window_button_toggles_overlay_display_only(host) -> None:
    capability_host, canvas = host
    chrome = canvas.color_viewport_chrome
    window = capability_host._window
    window.current_file_path = "/tmp/x.png"  # 媒体在场 → 按钮可用
    capability_host.refresh()
    assert chrome.power_window_button.isEnabled()
    assert chrome.power_window_button.isChecked() is True  # 默认显示线框

    chrome.power_window_button.click()
    assert canvas.overlay_calls == [False]
    assert canvas._show_mask_overlays is False
    # 只改显示：matte 预览（引擎业务状态）零调用。
    assert canvas.matte_calls == []

    chrome.power_window_button.click()
    assert canvas.overlay_calls == [False, True]
    assert canvas._show_mask_overlays is True


def test_capability_checked_reflects_real_overlay_state(host) -> None:
    capability_host, canvas = host
    chrome = canvas.color_viewport_chrome
    capability_host._window.current_file_path = "/tmp/x.png"
    capability_host.refresh()
    canvas._show_mask_overlays = False  # 外部路径改了真实状态
    capability_host.refresh()
    # 状态锁回填：checked 跟随真实状态，不回发 toggled。
    assert chrome.power_window_button.isChecked() is False
    assert canvas.overlay_calls == []


def test_highlight_toggle_does_not_touch_overlay_state(host, qapp) -> None:
    capability_host, canvas = host
    chrome = canvas.color_viewport_chrome
    capability_host._window.current_file_path = "/tmp/x.png"
    capability_host.refresh()
    # Highlight（引擎 matte 预览）与线框 overlay 独立。
    chrome.toolbar.mask_btn.click()
    assert len(canvas.matte_calls) == 1     # 一次 toggle 一次引擎调用
    assert canvas.overlay_calls == []       # overlay 未动
    assert canvas._show_mask_overlays is True


def test_overlay_without_media_stays_disabled(host) -> None:
    capability_host, canvas = host
    chrome = canvas.color_viewport_chrome
    capability_host.refresh()  # 无媒体
    assert not chrome.power_window_button.isEnabled()
    chrome.power_window_button.click()  # disabled 按钮 click() 仍发 toggled?
    # 无论点击是否穿透，后端不被无媒体状态触发。
    assert canvas.overlay_calls == []


# ── Alpha 归因（域件） ────────────────────────────────────────────────


def _caps(**overrides):
    state = ColorViewerState(presentation_path=PRESENTATION_IMAGE,
                             has_media=True)
    context = ColorViewerCapabilityContext(
        state=state, engine_available=True, **overrides)
    return color_capability_by_action(
        evaluate_color_viewer_capabilities(context))


def test_alpha_capability_reason_separates_media_from_display_path() -> None:
    no_alpha = _caps()["monitor.alpha"]
    assert no_alpha.supported is False
    assert "no alpha channel" in no_alpha.reason

    with_alpha = _caps(media_has_alpha=True)["monitor.alpha"]
    assert with_alpha.supported is False  # 显示路径未实现，仍不虚亮
    assert "alpha preview display path is not implemented" in with_alpha.reason


def test_host_feeds_media_alpha_flag_into_context(host) -> None:
    capability_host, canvas = host
    window = capability_host._window
    window.current_file_path = "/tmp/4444.mov"
    window.current_file_is_video = True
    window.video_controller = SimpleNamespace(
        is_playing=False, loop_playback=False, current_frame=0,
        total_frames=10)
    window._color_media_has_alpha = True
    capability_host.refresh()
    cap = capability_host.last_capabilities["monitor.alpha"]
    assert cap.supported is False
    assert "not implemented" in cap.reason
