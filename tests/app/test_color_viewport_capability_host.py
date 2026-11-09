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
    assert chrome.split_screen_button.isEnabled()  # setup remains available
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
    # R05：独立源（无 clip 上下文）显式声明 source 帧域。
    assert host.last_state.time_domain == "source_frame"
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


def test_source_controller_zero_is_authoritative_over_stale_transport(host) -> None:
    _open_video(host._window, current_frame=0, total_frames=0)
    host._chrome.transport.set_total_frames(250)
    host._chrome.transport.set_current_frame(48)
    host.refresh()
    assert host.last_state.current_frame == 0
    assert host.last_state.total_frames == 0


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
    assert chrome.split_screen_button.isEnabled() is True
    assert chrome._options_stub_actions["compare.split_screen"].toolTip().endswith(_NO_HANDLER_REASON)


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


# ── R05：按 presentation_path 取唯一权威控制器与域 ──────────────────


class TestR05PathAuthoritativeFacts:
    def _clip_context(self, window, clip):
        window._active_color_timeline_clip_playback_context = lambda: {
            "timeline_id": "t1", "clip": clip, "effective_source_start": None}

    def test_clip_context_maps_position_and_total_to_clip_local(self, host):
        """计划复现数字：源 120/1000、clip 局部 20/50——快照不得再报
        clip_local 的 120/1000。"""
        _open_video(host._window, current_frame=120, total_frames=1000)
        clip = SimpleNamespace(clip_id="c1", source_start=100,
                               source_duration=50, timeline_duration=50)
        self._clip_context(host._window, clip)
        host._window._color_source_frame_to_local_frame = (
            lambda f: max(0, int(f) - 100))
        host._window._color_timeline_clip_total_frames = lambda: 50
        host.refresh()
        state = host.last_state
        assert state.presentation_path == "source_gl"
        assert state.time_domain == "clip_local"
        assert state.current_frame == 20
        assert state.total_frames == 50

    def test_independent_source_declares_source_domain(self, host):
        _open_video(host._window, current_frame=33, total_frames=250)
        host.refresh()
        state = host.last_state
        assert state.time_domain == "source_frame"
        assert state.current_frame == 33
        assert state.total_frames == 250

    def test_clip_mapping_falls_back_to_clip_geometry(self, host):
        """宿主缺映射 helper：按 clip 几何（source_start/时长）近似，
        仍不混入源全域帧数。"""
        _open_video(host._window, current_frame=120, total_frames=1000)
        clip = SimpleNamespace(clip_id="c2", source_start=100,
                               source_duration=60)
        self._clip_context(host._window, clip)
        host.refresh()
        state = host.last_state
        assert state.time_domain == "clip_local"
        assert state.current_frame == 20
        assert state.total_frames == 60

    def _timeline_window(self, host, *, playing, current):
        host._canvas._timeline_preview_active = True
        controller = SimpleNamespace(is_playing=playing, loop_enabled=True,
                                     current_frame=current)
        host._window.timeline_panel = SimpleNamespace(
            get_playback_controller=lambda: controller,
            get_total_frames=lambda: 300)
        return controller

    def test_timeline_preview_uses_timeline_controller_not_source(self, host):
        """TimelinePreview 权威是共享时间线控制器：源控制器静止/越域
        帧数不得进入快照，也不得参与可播判定。"""
        _open_video(host._window, is_playing=False, current_frame=888,
                    total_frames=1000)  # 源控制器静止、帧数在别的域
        self._timeline_window(host, playing=True, current=42)
        host.refresh()
        state = host.last_state
        assert state.presentation_path == "timeline_preview"
        assert state.time_domain == "timeline_frame"
        assert state.playing is True            # 来自时间线控制器
        assert state.loop_enabled is True
        assert state.current_frame == 42
        assert state.total_frames == 300
        caps = host.last_capabilities
        assert caps["transport.play"].supported is True
        assert caps["transport.play"].reason is None

    def test_timeline_preview_without_timeline_controller_disables_transport(
            self, host):
        """只有源控制器在场：TimelinePreview 的 transport 仍禁用（源
        控制器与时间线域无关，不冒充可用）。"""
        _open_video(host._window)  # 源控制器在场
        host._canvas._timeline_preview_active = True
        host.refresh()
        assert host.last_state.presentation_path == "timeline_preview"
        caps = host.last_capabilities
        assert caps["transport.play"].supported is False

    def test_composite_uses_timeline_controller_as_authority(self, host):
        host._window.current_page = "Composite"
        self._timeline_window(host, playing=False, current=77)
        host._canvas._timeline_preview_active = False
        host.refresh()
        state = host.last_state
        assert state.presentation_path == "composite"
        assert state.time_domain == "timeline_frame"
        assert state.current_frame == 77
        assert state.total_frames == 300

    def test_refresh_emits_no_seek_scrub_or_toggle(self, host):
        """刷新只读事实：不发 seek/scrub/loop toggle（程序化回填零回声）。"""
        _open_video(host._window, current_frame=120, total_frames=1000,
                    loop_playback=True)
        clip = SimpleNamespace(clip_id="c3", source_start=100,
                               source_duration=50, timeline_duration=50)
        self._clip_context(host._window, clip)
        host._window._color_source_frame_to_local_frame = (
            lambda f: max(0, int(f) - 100))
        host._window._color_timeline_clip_total_frames = lambda: 50
        transport = host._chrome.transport
        seeks: list[int] = []
        scrubs: list[int] = []
        loops: list[bool] = []
        transport.seek_requested.connect(seeks.append)
        transport.scrub_started.connect(scrubs.append)
        transport.loop_toggled.connect(loops.append)
        host.refresh()
        host.refresh()  # 连续刷新同样零回声
        assert seeks == [] and scrubs == []
        assert loops == []
        assert host.last_state.current_frame == 20
