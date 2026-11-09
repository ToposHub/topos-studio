"""CV-B01 宿主测试：真实 chrome 回填、revision 守卫与诚实禁用。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsView, QWidget

from src.app.controllers.color_viewport_capability_host import (
    ColorViewportCapabilityHost,
    _NO_HANDLER_REASON,
)
from src.features.color.ui.canvas import Canvas, VideoControlBar, ViewportMenuBar
from src.features.color.ui.color_viewport_chrome import ColorViewportChrome


class _CanvasHost(QGraphicsView):
    """Native QWidget viewport: no GL context, renderer or decoder in UI QA."""

    update_video_control_bar_position = Canvas.update_video_control_bar_position

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
        current_page="Color",
        canvas=canvas,
        video_controller=None,
        current_file_is_video=False,
        current_file_path=None,
        _get_work_mode=lambda: "all",
        _active_color_timeline_clip_playback_context=lambda: None,
    )
    capability_host = ColorViewportCapabilityHost(window)
    assert capability_host.install() is True
    yield capability_host
    canvas.hide()
    owner.deleteLater()


def _open_image(window) -> None:
    window.current_file_path = "/tmp/grade.png"


def _open_video(window, **controller_overrides) -> None:
    controller_fields = dict(is_playing=False, loop_playback=False,
                             current_frame=0, total_frames=250)
    controller_fields.update(controller_overrides)
    window.video_controller = SimpleNamespace(**controller_fields)
    window.current_file_is_video = True
    window.current_file_path = "/tmp/grade.mov"


def test_install_without_chrome_returns_false(qapp) -> None:
    window = SimpleNamespace(
        current_page="Color", canvas=SimpleNamespace(), video_controller=None)
    capability_host = ColorViewportCapabilityHost(window)
    assert capability_host.install() is False


def test_no_media_disables_monitor_actions_and_transport(host) -> None:
    chrome = host._chrome
    toolbar = host._toolbar
    assert host.last_state.presentation_path == "none"
    assert host.last_state.has_media is False
    assert not toolbar.mask_act.isEnabled()
    assert not toolbar.split_btn.isEnabled()
    assert not chrome.transport.btn_play.isEnabled()
    assert not chrome.transport.slider.isEnabled()
    assert "not wired" in chrome.split_screen_button.toolTip()
    assert host.last_capabilities["view.highlight"].reason


def test_image_media_enables_grading_but_not_transport(host) -> None:
    _open_image(host._window)
    host.refresh()
    chrome = host._chrome
    assert host.last_state.presentation_path == "image"
    assert chrome.toolbar.mask_act.isEnabled()
    assert chrome.toolbar.ab_toggle_act.isEnabled()
    assert chrome.toolbar.capture_still_act.isEnabled()
    assert not chrome.transport.btn_play.isEnabled()
    assert not chrome.transport.btn_loop.isEnabled()


def test_video_media_enables_transport_and_backfills_loop(host) -> None:
    _open_video(host._window, loop_playback=True)
    host.refresh()
    chrome = host._chrome
    assert host.last_state.presentation_path == "source_gl"
    assert host.last_state.time_domain == "clip_local"
    for button in (chrome.transport.btn_first, chrome.transport.btn_prev,
                   chrome.transport.btn_play, chrome.transport.btn_next,
                   chrome.transport.btn_last):
        assert button.isEnabled()
    # loop checked 回填经状态锁：不回发 loop_toggled。
    emitted: list[bool] = []
    chrome.transport.loop_toggled.connect(emitted.append)
    assert chrome.transport.btn_loop.isChecked() is True
    host.refresh()
    assert emitted == []


def test_checked_backfill_does_not_emit_display_toggles(host) -> None:
    _open_image(host._window)
    host._canvas.engine.show_mask_mode = True
    host._canvas.engine.split_view_active = True
    emitted_mask: list[bool] = []
    toolbar = host._toolbar
    toolbar.mask_toggled.connect(emitted_mask.append)
    host.refresh()
    assert toolbar.mask_btn.isChecked() is True
    assert toolbar.mask_act.isChecked() is True
    assert toolbar.split_btn.isChecked() is True
    assert emitted_mask == []


def test_unsupported_actions_carry_reason_tooltips(host) -> None:
    chrome = host._chrome
    host.refresh()
    assert "reference still" in chrome.toolbar.clear_reference_act.toolTip()
    assert "no handler" not in chrome.toolbar.clear_reference_act.toolTip()
    # 领域位为真但没有 handler 的动作（Split Screen 多图）仍禁用。
    host._window_compare_grid_designed = True
    original = host._gather_context

    def _patched(state):
        context = original(state)
        return SimpleNamespace(**{**context.__dict__,
                                  "compare_grid_designed": True})

    host._gather_context = _patched
    host.refresh()
    assert chrome.split_screen_button.isEnabled() is False
    assert chrome.split_screen_button.toolTip().endswith(_NO_HANDLER_REASON)


def test_bump_context_revision_drops_stale_apply(host) -> None:
    _open_image(host._window)
    host.refresh()
    before = host.last_state.context_revision
    original_gather = host._gather_state

    def _gather_with_mid_refresh():
        state = original_gather()
        # 模拟评估期间上下文再次变化（切 clip）。
        host.bump_context_revision()
        return state

    host._gather_state = _gather_with_mid_refresh
    host.refresh()
    assert host.last_state.context_revision == before
    assert host._revision == before + 1
    host._gather_state = original_gather


def test_clip_context_ids_flow_into_state(host) -> None:
    host._window._active_color_timeline_clip_playback_context = lambda: {
        "timeline_id": "t1", "clip": SimpleNamespace(clip_id="c1"),
        "effective_source_start": 0}
    host.refresh()
    assert host.last_state.clip_id == "c1"
    assert host.last_state.timeline_id == "t1"


def test_timeline_preview_and_composite_routing(host) -> None:
    _open_video(host._window)
    host._canvas._timeline_preview_active = True
    host.refresh()
    assert host.last_state.presentation_path == "timeline_preview"
    assert host.last_state.time_domain == "timeline_frame"
    host._canvas._timeline_preview_active = False
    host._window.current_page = "Composite"
    host.refresh()
    assert host.last_state.presentation_path == "composite"
    assert host.last_state.time_domain == "timeline_frame"


def test_menu_open_triggers_refresh(host) -> None:
    _open_image(host._window)
    # 通过 chrome 的刷新请求信号（options 菜单 aboutToShow 路径）。
    host._chrome.capability_refresh_requested.emit()
    assert host.last_state.presentation_path == "image"
    # View 菜单 aboutToShow 也接了刷新。
    host._window.current_file_path = None
    host._toolbar.view_menu.aboutToShow.emit()
    assert host.last_state.presentation_path == "none"


def test_stub_buttons_registered_in_options_menu(host) -> None:
    chrome = host._chrome
    actions = chrome._options_stub_actions
    assert set(actions) == {"compare.split_screen", "monitor.proxy",
                            "monitor.resolution", "monitor.bypass"}
    host.refresh()
    for action in actions.values():
        assert not action.isEnabled()
        assert action.toolTip()
