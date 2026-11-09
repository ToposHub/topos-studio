"""CV-B03 验证：Loop 政策、内容范围与停止/倒放能力声明。

门槛（计划 CV-B03）：Loop OFF 用真实内容范围（排除 TimelinePanel 空尾）
或显式 in/out；Loop ON 用选中 clip 范围；切 clip 复位 Loop（不从
Composite 共享默认值）；停止原位/倒放按真实后端声明。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QWidget

from src.app.controllers.file_controller import FileOpsMixin
from src.features.color.core.color_viewer_capabilities import (
    PRESENTATION_SOURCE_GL,
    ColorViewerCapabilityContext,
    ColorViewerState,
    color_capability_by_action,
    evaluate_color_viewer_capabilities,
)
from src.features.color.ui.canvas import VideoControlBar
from src.features.edit.core.timeline_v2 import ClipInstance


class _FakeTimelineController:
    """记录 range/loop/播放命令的共享时钟替身。"""

    def __init__(self) -> None:
        self.is_playing = False
        self.range_calls: list[tuple[int, int]] = []
        self.loop_calls: list[bool] = []

    def set_playback_range(self, start: int, end: int) -> None:
        self.range_calls.append((int(start), int(end)))

    def set_loop(self, enabled: bool) -> None:
        self.loop_calls.append(bool(enabled))

    def play(self) -> None:
        self.is_playing = True

    def pause(self) -> None:
        self.is_playing = False

    def seek(self, frame: int) -> None:
        pass


class _Controller(FileOpsMixin):
    def __init__(self, bar: VideoControlBar,
                 timeline_controller: _FakeTimelineController,
                 clip: ClipInstance, timeline) -> None:
        self.current_page = "Color"
        self.canvas = SimpleNamespace(video_control_bar=bar)
        self.video_controller = None
        self.timeline_panel = SimpleNamespace(
            get_playback_controller=lambda: timeline_controller)
        # 预览渲染器哨兵：_color_timeline_playback_controller 只要求
        # 非 None + 活上下文（range 政策不触碰 renderer 本体）。
        self._color_timeline_preview_renderer = object()
        self._color_timeline_clip_playback_context = None
        self._current_timeline_sequences = {"tl-1": timeline}
        self._set_color_timeline_clip_playback_context("tl-1", clip)


def _clip(timeline_start: int = 100, duration: int = 40) -> ClipInstance:
    return ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=timeline_start, timeline_duration=duration,
        source_start=50, source_duration=duration)


def _timeline(clips, *, range_start=None, range_end=None) -> SimpleNamespace:
    return SimpleNamespace(
        all_tracks=(SimpleNamespace(clips=tuple(clips)),),
        range_start=range_start,
        range_end=range_end,
    )


@pytest.fixture
def harness(qapp):
    owner = QWidget()
    owner.update_video_control_bar_position = lambda: None
    bar = VideoControlBar(owner)
    bar.show_for_video()
    clip = _clip()
    # 内容到 140 结束（TimelinePanel 的布局空尾在其后，不得可播）。
    later = _clip(timeline_start=141, duration=20)
    timeline = _timeline((clip, later))
    controller = _Controller(bar, _FakeTimelineController(), clip, timeline)
    yield controller, bar
    owner.deleteLater()


def test_loop_on_uses_selected_clip_range(harness) -> None:
    controller, bar = harness
    bar.btn_loop.setChecked(True)  # 用户显式请求循环当前 clip
    controller._configure_color_timeline_playback_range()
    assert controller.timeline_panel.get_playback_controller().range_calls[-1] == (100, 139)
    assert controller.timeline_panel.get_playback_controller().loop_calls[-1] is True


def test_loop_off_uses_content_range_excluding_panel_padding(harness) -> None:
    controller, bar = harness
    assert not bar.btn_loop.isChecked()  # 切 clip 已复位
    controller._configure_color_timeline_playback_range()
    tl = controller.timeline_panel.get_playback_controller()
    # 内容末端 = 141+20-1 = 160；空尾不可播。
    assert tl.range_calls[-1] == (0, 160)
    assert tl.loop_calls[-1] is False


def test_explicit_timeline_in_out_wins_over_content_extent(harness) -> None:
    controller, bar = harness
    controller._current_timeline_sequences["tl-1"].range_start = 20
    controller._current_timeline_sequences["tl-1"].range_end = 99
    controller._configure_color_timeline_playback_range()
    assert (controller.timeline_panel.get_playback_controller()
            .range_calls[-1] == (20, 99))


def test_clip_switch_resets_loop_off_not_composite_default(harness, qapp) -> None:
    controller, bar = harness
    bar.btn_loop.setChecked(True)
    assert controller.video_controller is None  # 纯时间线路径
    controller._set_color_timeline_clip_playback_context(
        "tl-1", controller._color_timeline_clip_playback_context["clip"])
    # 切 clip（同 clip 重绑也是切上下文）：Loop 复位 OFF，不继承。
    assert bar.btn_loop.isChecked() is False
    # 复位引发的 loop_toggled 在无 video_controller 时是空操作（不崩）。
    controller._configure_color_timeline_playback_range()
    assert (controller.timeline_panel.get_playback_controller()
            .loop_calls[-1] is False)


def test_first_last_stay_clip_local_on_video_controller_path(qapp) -> None:
    """首末按钮经 on_video_first/last_frame 保持 clip-local 端点。"""
    owner = QWidget()
    owner.update_video_control_bar_position = lambda: None
    bar = VideoControlBar(owner)
    bar.show_for_video()

    class _VideoController:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.current_frame = 55

        def first_frame(self) -> None:
            self.calls.append("first")

        def last_frame(self) -> None:
            self.calls.append("last")

    video_controller = _VideoController()
    controller = _Controller(bar, _FakeTimelineController(), _clip(),
                             _timeline((_clip(),)))
    controller.video_controller = video_controller
    # 有 clip 上下文：首末转 on_video_seek(0/total-1)（clip-local 域），
    # 不直接调 video_controller.first_frame()（那是源全域语义）。
    seeks: list[int] = []
    bar.seek_requested.connect(seeks.append)
    controller.on_video_first_frame()
    assert video_controller.calls == []
    # 无 clip 上下文（独立视频）：才直达源端点。
    controller._color_timeline_clip_playback_context = None
    controller.timeline_panel = None
    controller._color_timeline_preview_renderer = None
    controller.on_video_first_frame()
    controller.on_video_last_frame()
    assert video_controller.calls == ["first", "last"]
    owner.deleteLater()


# ── 能力声明（停止原位/倒放） ─────────────────────────────────────────


def _caps(**context_overrides):
    return color_capability_by_action(
        evaluate_color_viewer_capabilities(
            ColorViewerCapabilityContext(**context_overrides)))


def test_stop_in_place_capability_follows_transport_availability() -> None:
    state = ColorViewerState(
        presentation_path=PRESENTATION_SOURCE_GL, has_media=True)
    caps = _caps(state=state, engine_available=True,
                 video_controller_available=True)
    assert caps["transport.stop_in_place"].supported is True
    caps = _caps(state=state, engine_available=True,
                 video_controller_available=False)
    assert caps["transport.stop_in_place"].supported is False


def test_reverse_play_declared_unsupported_on_both_paths() -> None:
    state = ColorViewerState(
        presentation_path=PRESENTATION_SOURCE_GL, has_media=True)
    caps = _caps(state=state, engine_available=True,
                 video_controller_available=True)
    assert caps["transport.reverse_play"].supported is False
    assert "not implemented" in caps["transport.reverse_play"].reason
    # 无 handler 的动作经宿主二道门仍禁用（B01 协议）。
    from src.app.controllers.color_viewport_capability_host import (
        _HANDLED_ACTIONS,
    )
    assert "transport.reverse_play" not in _HANDLED_ACTIONS
    assert "transport.stop_in_place" not in _HANDLED_ACTIONS
