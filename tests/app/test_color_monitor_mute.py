"""CV-B06 验证：监看静音（Master 总线）真实接入与作用域恢复。

门槛（计划 CV-B06）：静音后设备实际无声、视频继续；恢复不把用户本来
静音的总线打开；跨页按约定恢复；服务缺席诚实禁用/拒绝。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsView, QWidget

from src.app.controllers.color_viewport_capability_host import (
    ColorViewportCapabilityHost,
)
from src.app.controllers.file_controller import FileOpsMixin
from src.features.color.ui.canvas import Canvas, VideoControlBar, ViewportMenuBar
from src.features.color.ui.color_viewport_chrome import ColorViewportChrome
from src.features.edit.core.audio_playback import AudioMixer
from src.features.edit.ui.edit_page import EditPage


# ── 底层：AudioMixer / EditPage API ───────────────────────────────────


def test_audio_mixer_master_mute_roundtrip_is_thread_safe_state() -> None:
    mixer = AudioMixer()
    assert mixer.is_master_muted() is False
    mixer.set_master_muted(True)
    assert mixer.is_master_muted() is True
    mixer.set_master_muted(False)
    assert mixer.is_master_muted() is False


def test_edit_page_monitor_mute_api_applies_and_reads_back() -> None:
    page = EditPage.__new__(EditPage)
    page._audio_manager = None
    assert page.set_monitor_master_muted(True) is False  # 服务缺席诚实拒绝
    assert page.monitor_master_muted() is None

    mixer = AudioMixer()
    page._audio_manager = SimpleNamespace(
        set_master_muted=mixer.set_master_muted,
        is_master_muted=mixer.is_master_muted)
    assert page.set_monitor_master_muted(True) is True
    assert page.monitor_master_muted() is True


# ── 服务层：FileOpsMixin 保留先前状态 + 桥不发声 ──────────────────────


class _FakeEditPage:
    def __init__(self, master_muted: bool = False) -> None:
        self.master_muted = master_muted
        self.mute_calls: list[bool] = []
        self.audio_starts: list[tuple[object, int]] = []

    def set_monitor_master_muted(self, muted: bool) -> bool:
        self.mute_calls.append(bool(muted))
        self.master_muted = bool(muted)
        return True

    def monitor_master_muted(self) -> bool:
        return self.master_muted

    def start_timeline_audio_playback(self, timeline, start_frame: int) -> bool:
        self.audio_starts.append((timeline, int(start_frame)))
        return True

    def pause_timeline_audio_playback(self) -> None:
        pass


class _Controller(FileOpsMixin):
    def __init__(self, edit_page) -> None:
        self.current_page = "Color"
        self.canvas = SimpleNamespace(
            video_control_bar=SimpleNamespace())
        self.video_controller = SimpleNamespace(current_frame=10)
        self.edit_page = edit_page
        self._color_timeline_clip_playback_context = {
            "timeline_id": "tl-1",
            "clip": SimpleNamespace(clip_id="c1", timeline_start=0,
                                    source_start=0, source_duration=100,
                                    timeline_duration=100, speed=1.0),
            "effective_source_start": None,
        }
        self._current_timeline_sequences = {}


def test_monitor_mute_preserves_users_previous_master_state() -> None:
    edit_page = _FakeEditPage(master_muted=True)  # 用户自己静了 master
    controller = _Controller(edit_page)
    assert controller._set_color_monitor_mute(True) is True
    assert edit_page.mute_calls == [True]
    assert controller._color_monitor_mute_is_active() is True

    controller._set_color_monitor_mute(False)
    # 解除后恢复用户先前的静音（不擅自打开用户静着的总线）。
    assert edit_page.master_muted is True
    assert controller._color_monitor_mute_is_active() is False
    assert edit_page.mute_calls == [True, True]  # 原master静音绝不短暂打开


def test_monitor_mute_unmute_restores_unmuted_default() -> None:
    edit_page = _FakeEditPage(master_muted=False)
    controller = _Controller(edit_page)
    controller._set_color_monitor_mute(True)
    controller._set_color_monitor_mute(False)
    assert edit_page.master_muted is False
    assert edit_page.mute_calls == [True, False]


def test_monitor_mute_fails_honestly_without_service() -> None:
    controller = _Controller(_FakeEditPage())
    controller.edit_page = SimpleNamespace()  # 无 API
    assert controller._set_color_monitor_mute(True) is False
    assert controller._color_monitor_mute_is_active() is False


def test_restore_is_noop_when_not_muted_and_applies_when_muted() -> None:
    edit_page = _FakeEditPage()
    controller = _Controller(edit_page)
    controller._restore_color_monitor_mute()
    assert edit_page.mute_calls == []  # 未静音时零调用

    controller._set_color_monitor_mute(True)
    controller._restore_color_monitor_mute()
    assert edit_page.master_muted is False


def test_audio_bridge_does_not_start_audio_while_monitor_muted() -> None:
    edit_page = _FakeEditPage()
    controller = _Controller(edit_page)
    controller._color_monitor_muted = True
    controller.video_controller = SimpleNamespace(
        current_frame=10, is_playing=True, loop_playback=False)
    controller._on_color_video_play_state_for_audio(True)
    assert edit_page.audio_starts == []  # 静音：视频照常，不起声

    controller._color_monitor_muted = False
    controller._on_color_video_play_state_for_audio(True)
    assert len(edit_page.audio_starts) == 1  # 解除后正常起声


def test_unmute_resumes_bridge_that_was_skipped_when_video_started_muted() -> None:
    edit_page = _FakeEditPage()
    controller = _Controller(edit_page)
    controller.video_controller.is_playing = True
    controller._set_color_monitor_mute(True)
    controller._on_color_video_play_state_for_audio(True)
    assert edit_page.audio_starts == []
    controller._set_color_monitor_mute(False)
    assert len(edit_page.audio_starts) == 1
    assert edit_page.audio_starts[0][1] == 10


@pytest.mark.parametrize("state", ["paused", "no_context", "timeline_preview"])
def test_unmute_never_starts_an_unowned_audio_clock(state: str) -> None:
    edit_page = _FakeEditPage()
    controller = _Controller(edit_page)
    controller.video_controller.is_playing = True
    controller._set_color_monitor_mute(True)
    controller._on_color_video_play_state_for_audio(True)
    if state == "paused":
        controller.video_controller.is_playing = False
    elif state == "no_context":
        controller._color_timeline_clip_playback_context = None
    else:
        controller._color_timeline_preview_renderer = object()
    controller._set_color_monitor_mute(False)
    assert edit_page.audio_starts == []


def test_failed_unmute_keeps_color_mute_owned_for_truthful_checked_and_retry() -> None:
    edit_page = _FakeEditPage()
    controller = _Controller(edit_page)
    assert controller._set_color_monitor_mute(True)
    edit_page.set_monitor_master_muted = lambda value: False
    assert not controller._set_color_monitor_mute(False)
    assert controller._color_monitor_mute_is_active()


# ── UI 层：chrome 按钮 → 宿主 → 服务 → checked 回填 ──────────────────


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

    def _update_lod(self) -> bool:
        return False


@pytest.fixture
def window_host(qapp):
    owner = QWidget()
    canvas = _CanvasHost(owner)
    canvas.setWindowFlag(Qt.WindowType.Window, True)
    canvas.resize(1000, 600)
    canvas.viewport_menu.resize(1000, 30)
    canvas.viewport_menu.show()
    canvas.show()
    qapp.processEvents()
    edit_page = _FakeEditPage()
    from src.app.controllers.file_controller import FileOpsMixin as _FO

    service_controller = _Controller(edit_page)
    window = SimpleNamespace(
        current_page="Color", canvas=canvas,
        video_controller=SimpleNamespace(
            is_playing=True, loop_playback=False, current_frame=5,
            total_frames=100),
        current_file_is_video=True, current_file_path="/tmp/x.mov",
        _get_work_mode=lambda: "all",
        _active_color_timeline_clip_playback_context=lambda: None,
        _set_color_monitor_mute=service_controller._set_color_monitor_mute,
        _color_monitor_mute_is_active=(
            service_controller._color_monitor_mute_is_active),
    )
    capability_host = ColorViewportCapabilityHost(window)
    assert capability_host.install() is True
    yield capability_host, canvas, edit_page
    canvas.hide()
    owner.deleteLater()


def test_mute_button_routes_to_master_bus_and_backfills_checked(window_host) -> None:
    capability_host, canvas, edit_page = window_host
    chrome = canvas.color_viewport_chrome
    capability_host.refresh()
    assert chrome.mute_button.isEnabled()      # 服务在：可用
    assert chrome.mute_button.isChecked() is False

    chrome.mute_button.click()
    assert edit_page.master_muted is True      # 真实 Master 总线生效
    assert chrome.mute_button.isChecked() is True
    assert capability_host.last_state.monitor_muted is True

    chrome.mute_button.click()
    assert edit_page.master_muted is False
    assert chrome.mute_button.isChecked() is False


def test_mute_checked_reflects_external_state_changes(window_host) -> None:
    capability_host, canvas, edit_page = window_host
    chrome = canvas.color_viewport_chrome
    capability_host.refresh()
    edit_page.master_muted = True
    # 外部改真实状态 → 刷新回填（不虚报）。
    capability_host._window._color_monitor_mute_is_active = lambda: True
    capability_host.refresh()
    assert chrome.mute_button.isChecked() is True


def test_mute_service_missing_keeps_button_disabled(qapp) -> None:
    owner = QWidget()
    canvas = _CanvasHost(owner)
    window = SimpleNamespace(
        current_page="Color", canvas=canvas, video_controller=None,
        current_file_is_video=False, current_file_path=None,
        _get_work_mode=lambda: "all",
        _active_color_timeline_clip_playback_context=lambda: None)
    host = ColorViewportCapabilityHost(window)
    host.install()
    host.refresh()
    assert not canvas.color_viewport_chrome.mute_button.isEnabled()
    assert "not wired" in canvas.color_viewport_chrome.mute_button.toolTip()
    canvas.hide()
    owner.deleteLater()
