"""CV-B09 回归：共享 VideoControlBar 的页面隔离与状态栏安全区。

覆盖计划 CV-B09 的门槛项：Color／Composite 连续切换 ruler 模式／高度／
I/O／keyframe／默认 Loop 不串；程序化回填不发播放头回声；图片模式
禁用／reset／清 range；状态栏按可见 footer 定位、窄窗折叠到 tooltip
且保留 provider 诊断。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsView, QWidget

from src.features.color.ui.canvas import Canvas, VideoControlBar
from src.features.color.ui.color_status_bar import ColorMonitorStatusBar


# ── 共享 transport 的页面隔离（纯 widget，无 chrome） ──────────────────


def test_fresh_bar_keeps_composite_default_loop_on(qapp) -> None:
    bar = VideoControlBar()
    try:
        assert bar.btn_loop.isChecked() is True
        assert bar.label_time is bar.timecode_label
        assert bar._fusion_mode is False
    finally:
        bar.close()


def test_keyframe_markers_and_io_survive_color_composite_roundtrips(qapp) -> None:
    bar = VideoControlBar()
    try:
        bar.show_for_video_with_io()
        bar.set_total_frames(200)
        bar.set_keyframe_frames([10, 50, 120])
        bar.set_in_out_points(20, 80)
        for _ in range(2):
            bar.show_for_video()          # Color 分支（隐藏 I/O 与 ruler）
            assert bar.height() == 36
            assert bar.ruler_slider.isHidden()
            bar.show_for_video_with_io()  # 回 Composite
            assert bar.height() == 74
            assert not bar.ruler_slider.isHidden()
            assert not bar.btn_mark_in.isHidden()
            assert bar.ruler_slider._keyframe_frames == [10, 50, 120]
            assert (bar._in_point, bar._out_point) == (20, 80)
            assert bar.btn_loop.isChecked() is True
    finally:
        bar.close()


def test_programmatic_frame_updates_do_not_emit_scrub_or_seek(qapp) -> None:
    """set_current_frame/set_total_frames 走 blockSignals：零回声。"""
    bar = VideoControlBar()
    try:
        events: list[tuple[str, int]] = []
        bar.scrub_started.connect(lambda v: events.append(("start", v)))
        bar.scrub_moved.connect(lambda v: events.append(("move", v)))
        bar.scrub_finished.connect(lambda v: events.append(("finish", v)))
        bar.seek_requested.connect(lambda v: events.append(("seek", v)))
        bar.set_total_frames(100)
        bar.set_current_frame(42)
        assert bar.current_frame == 42
        assert bar.slider.value() == 42
        assert events == []
    finally:
        bar.close()


def test_hide_for_image_resets_state_without_signal_echo(qapp) -> None:
    bar = VideoControlBar()
    try:
        loop_events: list[bool] = []
        bar.loop_toggled.connect(loop_events.append)
        bar.show_for_video()
        bar.set_total_frames(100)
        bar.set_current_frame(50)
        bar.set_in_out_points(10, 60)
        bar.set_playing(True)
        bar.btn_loop.setChecked(True)
        loop_events.clear()

        bar.hide_for_image()

        assert bar.isHidden()
        assert not bar.btn_play.isEnabled()
        assert not bar.slider.isEnabled()
        assert bar.current_frame == 0
        assert bar.total_frames == 0
        assert bar.is_playing is False
        assert bar._in_point is None and bar._out_point is None
        # 状态锁语义：hide/reset 不回发 loop_toggled，也不改 Composite
        # 默认 Loop ON 的 checked 状态（Color 页由 FileOps 另行复位）。
        assert loop_events == []
        assert bar.btn_loop.isChecked() is True
    finally:
        bar.close()


def test_timecode_mirror_emits_display_string_without_seek(qapp) -> None:
    bar = VideoControlBar()
    try:
        seeks: list[int] = []
        mirrored: list[str] = []
        bar.seek_requested.connect(seeks.append)
        bar.timecode_changed.connect(mirrored.append)
        bar.set_frame_rate(25.0)
        bar.set_total_frames(2500)
        bar.set_current_frame(24)
        assert bar.timecode_label.text() == "00:00:00:24"
        assert mirrored[-1] == "00:00:00:24"
        assert seeks == []
    finally:
        bar.close()


# ── 状态栏安全区（真实 ColorMonitorStatusBar + Canvas 定位裁决） ───────


class _CanvasHost(QGraphicsView):
    """Native QWidget viewport hosting the real status bar and transport."""

    update_video_control_bar_position = Canvas.update_video_control_bar_position

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.video_control_bar = VideoControlBar(self)
        self.color_status_bar = ColorMonitorStatusBar(self.viewport())
        self.color_status_bar.hide()

    def _update_lod(self) -> bool:
        return False


@pytest.fixture
def status_host(qapp):
    owner = QWidget()
    canvas = _CanvasHost(owner)
    canvas.setWindowFlag(Qt.WindowType.Window, True)
    canvas.resize(1000, 600)
    canvas.show()
    qapp.processEvents()
    status = canvas.color_status_bar
    status.set_source_state(
        transfer="smpte2084", primaries="bt2020", range_tag="limited")
    status.set_working_space("ACEScc")
    status.set_display_space("PQ (ST 2084)")
    status.set_provider_diagnostics({
        "active_provider": "ocio", "requested_engine": "ocio",
        "ocio_config_loaded": True,
    })
    yield canvas, status
    canvas.hide()
    owner.deleteLater()


def test_wide_viewport_keeps_full_chain(status_host, qapp) -> None:
    canvas, status = status_host
    status.show()
    canvas.resize(1000, 600)
    qapp.processEvents()
    canvas.update_video_control_bar_position()
    assert not status._src_label.isHidden()
    assert status._src_label.text().startswith("SRC BT.2020 PQ")
    assert status.toolTip() == ""
    assert not status._provider_label.isHidden()
    assert "OCIO" in status._provider_label.text()


def test_narrow_viewport_collapses_chain_but_keeps_provider(status_host, qapp) -> None:
    canvas, status = status_host
    status.show()
    canvas.resize(300, 420)
    qapp.processEvents()
    canvas.update_video_control_bar_position()
    assert status._src_label.isHidden()
    assert status._work_label.isHidden()
    assert status._out_label.isHidden()
    # 完整链路进 tooltip，不丢诊断信息。
    assert "SRC BT.2020 PQ lim" in status.toolTip()
    assert "WORK ACEScc" in status.toolTip()
    # provider 徽标保留（监看诊断核心）。
    assert not status._provider_label.isHidden()
    assert "OCIO" in status._provider_label.text()


def test_status_stays_above_transport_in_safe_area(status_host, qapp) -> None:
    canvas, status = status_host
    canvas.video_control_bar.show_for_video()
    status.show()
    for width in (1000, 300):
        canvas.resize(width, 480)
        qapp.processEvents()
        canvas.update_video_control_bar_position()
        bar_top = canvas.video_control_bar.geometry().top()
        status_bottom = status.mapTo(canvas, status.rect().bottomLeft()).y()
        assert status_bottom < bar_top
        assert status.geometry().bottom() < canvas.viewport().height()


def test_denoise_badge_collapses_only_when_tiny(status_host, qapp) -> None:
    canvas, status = status_host
    status.set_denoise_quality_provider(lambda: "preview")
    status.show()
    canvas.resize(300, 420)
    qapp.processEvents()
    canvas.update_video_control_bar_position()
    assert not status._denoise_label.isHidden()  # 300px：链路折叠、降噪保留
    canvas.resize(200, 420)
    qapp.processEvents()
    canvas.update_video_control_bar_position()
    assert status._denoise_label.isHidden()      # 200px：极窄连降噪折叠
    assert status._denoise_label.text() == "DN·preview"  # 内容不丢
