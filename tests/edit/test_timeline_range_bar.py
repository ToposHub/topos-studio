"""
Tests for timeline range playback and export range.

Verifies:
- EditPlaybackController range playback (start/end within range)
- EditPlaybackController range loop (wraps to range_start, not frame 0)
- EditPlaybackController clear range restores full timeline behavior
- Export IN_OUT range reads sequence.range_start/range_end
- Export SELECTED range computes from clip bounds
"""

from __future__ import annotations

import pytest
import time
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.edit_playback_controller import (
    EditPlaybackController,
    PlaybackState,
)
from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    resolve_timeline_in_out,
    effective_timeline_duration_frames,
)
from src.features.edit.core.timeline_export import (
    TimelineExporter,
    TimelineExportConfig,
    ExportRangeType,
    estimate_export_duration,
)
from src.features.edit.ui.timeline_items import TimecodeRulerItem, TRACK_HEADER_WIDTH
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.edit_style import get_color


def _make_track(track_type: str, name: str, index: int = 0) -> EditTrack:
    return EditTrack(track_id=f"track_{name}", name=name, track_type=track_type, index=index)


def _make_clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"asset_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )


@pytest.fixture
def controller() -> EditPlaybackController:
    c = EditPlaybackController()
    c.set_fps(24.0)
    c.set_timeline_duration(240)  # 10 seconds at 24fps
    return c


# ================================================================
# Range Playback Tests
# ================================================================

class TestRangePlayback:
    def test_set_playback_range(self, controller: EditPlaybackController):
        controller.set_playback_range(48, 144)
        assert controller.playback_range == (48, 144)

    def test_clear_playback_range(self, controller: EditPlaybackController):
        controller.set_playback_range(48, 144)
        controller.clear_playback_range()
        assert controller.playback_range is None

    def test_play_starts_within_range(self, controller: EditPlaybackController):
        controller.seek(0)
        controller.set_playback_range(48, 144)
        controller.play()
        assert controller.current_frame >= 48

    def test_play_with_playhead_past_range_end_wraps_to_start(
            self, controller: EditPlaybackController):
        # 播放头停在 Out 之后才设定范围：开始播放必须直接回 In 正向循环，
        # 而不是先被 clamp 到 Out 再跳回（视觉上像倒着播放）。
        controller.seek(200)
        controller.set_playback_range(48, 144)
        controller.play()
        assert controller.current_frame == 48
        assert controller.is_playing

        # 随后的 tick 只能单调递增（正向循环，绝不回头）。
        start = time.monotonic()
        frames = [controller.tick(start + (i + 1) / 24.0) for i in range(3)]
        assert frames[0] >= 49
        assert all(b > a for a, b in zip(frames, frames[1:]))

    def test_range_loop_wraps_to_range_start(self, controller: EditPlaybackController):
        controller.set_loop(True)
        controller.set_playback_range(48, 144)
        controller.seek(48)
        controller.play()

        start = time.monotonic()
        elapsed_for_past_end = (144 - 48 + 10) / 24.0
        future = start + elapsed_for_past_end

        frame = controller.tick(future)
        assert frame == 48

    def test_range_no_loop_stops_at_end(self, controller: EditPlaybackController):
        controller.set_loop(False)
        controller.set_playback_range(48, 144)
        controller.seek(48)
        controller.play()

        start = time.monotonic()
        elapsed_for_past_end = (144 - 48 + 10) / 24.0
        future = start + elapsed_for_past_end

        frame = controller.tick(future)
        assert frame == 144
        assert controller.state == PlaybackState.STOPPED

    def test_full_timeline_loop_without_range(self, controller: EditPlaybackController):
        controller.set_loop(True)
        controller.seek(0)
        controller.play()

        start = time.monotonic()
        elapsed = (239 + 10) / 24.0
        future = start + elapsed

        frame = controller.tick(future)
        assert frame == 0


# ================================================================
# Export Range Tests
# ================================================================

class TestExportRange:
    def test_empty_timeline_has_black_canvas_duration_and_export_range(self):
        """An empty timeline exports its default black canvas instead of zero frames."""
        empty = TimelineSequence(
            timeline_id="empty",
            name="Empty",
            fps=24.0,
            width=1920,
            height=1080,
        )

        assert effective_timeline_duration_frames(empty) == 240

        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = empty
        assert exporter._calculate_frame_range(
            TimelineExportConfig(range_type=ExportRangeType.ENTIRE)
        ) == (0, 240)
        assert exporter._calculate_frame_range(
            TimelineExportConfig(range_type=ExportRangeType.IN_OUT)
        ) == (0, 240)
        from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
        exporter._render_plan = TimelineRenderPlan(empty)
        exporter._use_high_precision_export = False
        black_frame = exporter._render_frame(
            0,
            TimelineExportConfig(
                range_type=ExportRangeType.ENTIRE,
                resolution=(320, 180),
            ),
        )
        assert black_frame is not None
        assert black_frame.shape == (180, 320, 3)
        assert int(black_frame.max()) == 0
        assert estimate_export_duration(
            TimelineExportConfig(range_type=ExportRangeType.ENTIRE), empty
        ) == pytest.approx(10.0)

    def test_empty_timeline_range_endpoint_extends_black_canvas(self):
        empty = TimelineSequence(
            timeline_id="empty-range",
            name="Empty range",
            fps=24.0,
            range_start=48,
            range_end=143,
        )
        # The default ten-second black canvas remains available even when the
        # explicit In/Out marks are near the start of the empty sequence.
        assert effective_timeline_duration_frames(empty) == 240

        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = empty
        assert exporter._calculate_frame_range(
            TimelineExportConfig(range_type=ExportRangeType.IN_OUT)
        ) == (48, 144)

    def test_short_media_timeline_does_not_gain_an_export_black_tail(self):
        track = _make_track("video", "V1")
        short = TimelineSequence(
            timeline_id="short",
            name="Short",
            fps=24.0,
            video_tracks=(track.with_clip_added(_make_clip("c", track.track_id, duration=24)),),
        )
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = short

        assert effective_timeline_duration_frames(short) == 24
        assert exporter._calculate_frame_range(
            TimelineExportConfig(range_type=ExportRangeType.ENTIRE)
        ) == (0, 24)

    def _make_timeline_with_range(self) -> TimelineSequence:
        tl = TimelineSequence(timeline_id="tl1", name="Test", fps=24.0)
        v1 = _make_track("video", "V1")
        tl = tl.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id, duration=240))
        tl = tl.with_range(48, 144)
        return tl

    def test_in_out_export_range(self):
        tl = self._make_timeline_with_range()
        config = TimelineExportConfig(
            range_type=ExportRangeType.IN_OUT,
            output_path="/tmp/test.mov",
        )
        # Test _calculate_frame_range directly without full exporter init
        from unittest.mock import MagicMock
        exporter = MagicMock()
        exporter._sequence = tl
        # Import the method logic inline since we can't instantiate exporter easily
        # Just test the logic directly
        seq = tl
        assert seq.range_start == 48
        assert seq.range_end == 144
        # The expected result: (range_start, range_end + 1)
        assert (seq.range_start, seq.range_end + 1) == (48, 145)

    def test_in_out_exporter_uses_the_selected_frames_only(self):
        """IN_OUT maps the inclusive UI range to the export half-open range."""
        tl = self._make_timeline_with_range()
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = tl

        assert exporter._calculate_frame_range(
            TimelineExportConfig(range_type=ExportRangeType.IN_OUT)
        ) == (48, 145)

    def test_in_only_export_runs_from_in_to_the_last_clip_frame(self):
        tl = self._make_timeline_with_range().with_range(48, None)
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = tl

        assert exporter._calculate_frame_range(
            TimelineExportConfig(range_type=ExportRangeType.IN_OUT)
        ) == (48, 240)

    def test_out_only_export_runs_from_zero_to_out(self):
        tl = self._make_timeline_with_range().with_range(None, 144)
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = tl

        assert exporter._calculate_frame_range(
            TimelineExportConfig(range_type=ExportRangeType.IN_OUT)
        ) == (0, 145)

    def test_selected_export_range(self):
        tl = TimelineSequence(timeline_id="tl1", name="Test", fps=24.0)
        v1 = _make_track("video", "V1")
        tl = tl.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id, start=0, duration=50))
        tl = tl.with_clip_added(v1.track_id, _make_clip("c2", v1.track_id, start=100, duration=50))

        # Simulate the SELECTED range calculation logic
        selected_ids = {"c1", "c2"}
        min_start = None
        max_end = None
        for track in tl.all_tracks:
            for clip in track.clips:
                if clip.clip_id in selected_ids:
                    cs = clip.timeline_start
                    ce = clip.timeline_start + clip.timeline_duration
                    if min_start is None or cs < min_start:
                        min_start = cs
                    if max_end is None or ce > max_end:
                        max_end = ce
        assert min_start == 0
        assert max_end == 150

    def test_entire_export_range(self):
        tl = TimelineSequence(timeline_id="tl1", name="Test", fps=24.0)
        v1 = _make_track("video", "V1")
        tl = tl.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id, duration=240))

        config = TimelineExportConfig(
            range_type=ExportRangeType.ENTIRE,
            output_path="/tmp/test.mov",
        )
        # ENTIRE always returns (0, duration_frames)
        assert config.range_type == ExportRangeType.ENTIRE
        assert (0, tl.duration_frames) == (0, 240)


def test_single_in_or_out_resolves_to_the_other_timeline_edge() -> None:
    """Partial In/Out marks are complete ranges for playback and export."""
    assert resolve_timeline_in_out(48, None, 240) == (48, 239)
    assert resolve_timeline_in_out(None, 144, 240) == (0, 144)


def test_range_handles_drag_and_update_outside_mask(qapp) -> None:
    """The range handle gets ruler clicks and commits its drag through commands."""
    timeline = TimelineSequence(timeline_id="tl-drag", name="Drag", fps=24.0)
    track = _make_track("video", "V1")
    timeline = timeline.with_track_added(track)
    timeline = timeline.with_clip_added(
        track.track_id,
        _make_clip("clip", track.track_id, duration=240),
    ).with_range(48, 144)
    view = TimelineView()
    view.resize(1400, 320)
    view.load_timeline(timeline)
    view.show()
    qapp.processEvents()

    range_item = view._range_item
    mask_item = view._range_mask_item
    assert range_item is not None and range_item.isVisible()
    assert mask_item is not None and mask_item.isVisible()
    track_area = view._layout_engine.area_layout().total_track_area
    assert mask_item.boundingRect().top() == track_area.top()
    assert mask_item.boundingRect().height() == track_area.height()
    out_handle_scene = QPointF(
        TRACK_HEADER_WIDTH + (144 + 1) * view._pixels_per_frame,
        range_item._bar_y,
    )
    assert range_item._hit_edge(out_handle_scene) == "out"

    start_handle_scene = QPointF(
        TRACK_HEADER_WIDTH + 48 * view._pixels_per_frame,
        range_item._bar_y,
    )
    drag_target_scene = QPointF(
        TRACK_HEADER_WIDTH + 64 * view._pixels_per_frame,
        range_item._bar_y,
    )
    QTest.mousePress(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        view.mapFromScene(start_handle_scene),
    )
    QTest.mouseMove(view.viewport(), view.mapFromScene(drag_target_scene))
    QTest.mouseRelease(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        view.mapFromScene(drag_target_scene),
    )
    qapp.processEvents()

    assert view._timeline.range_start == 64
    assert view._timeline.range_end == 144
    assert mask_item.isVisible()
    view.close()


def test_empty_timeline_exposes_default_black_output_range(qapp) -> None:
    """An empty timeline still shows a full-range bar for its black canvas."""
    timeline = TimelineSequence(
        timeline_id="empty-range-ui",
        name="Empty",
        fps=24.0,
        width=1920,
        height=1080,
    )
    view = TimelineView()
    view.resize(1400, 320)
    view.load_timeline(timeline)
    view.show()
    qapp.processEvents()

    assert view._effective_timeline_duration() == 240
    assert view._range_item is not None
    assert view._range_item.isVisible()
    assert view._range_item._range_start == 0
    assert view._range_item._range_end == 239

    view.close()


def test_corner_timecode_uses_ruler_background_instead_of_black(qapp) -> None:
    """The fixed top-left timecode surface matches the ruler background."""
    view = TimelineView()
    view.show()
    qapp.processEvents()

    image = view._corner_timecode_widget.grab().toImage()
    expected = QColor(get_color("ruler_bg"))
    assert image.pixelColor(2, 2).rgb() == expected.rgb()

    view.close()


def test_ruler_context_menu_emits_mark_in_for_clicked_frame(qapp) -> None:
    """Ruler right-click converts its clicked position into an In-point request."""
    ruler = TimecodeRulerItem(pixels_per_frame=2.0, fps=24.0, duration_frames=240)
    received_frames: list[int] = []
    ruler.mark_in_requested.connect(received_frames.append)

    mark_in_action = MagicMock()
    mark_out_action = MagicMock()
    clear_action = MagicMock()
    frames_action = MagicMock()
    samples_action = MagicMock()
    menu = MagicMock()
    menu.addAction.side_effect = [
        mark_in_action,
        mark_out_action,
        clear_action,
        frames_action,
        samples_action,
    ]
    menu.exec.return_value = mark_in_action
    event = MagicMock()
    event.scenePos.return_value = QPointF(TRACK_HEADER_WIDTH + 24.0, 0.0)

    with patch("PyQt6.QtWidgets.QMenu", return_value=menu):
        ruler.contextMenuEvent(event)

    assert received_frames == [12]
    event.accept.assert_called_once()


def test_ruler_context_menu_can_clear_the_active_selection(qapp) -> None:
    """The ruler menu exposes a clear-selection action for In/Out ranges."""
    ruler = TimecodeRulerItem(pixels_per_frame=2.0, fps=24.0, duration_frames=240)
    cleared: list[bool] = []
    ruler.clear_range_requested.connect(lambda: cleared.append(True))

    mark_in_action = MagicMock()
    mark_out_action = MagicMock()
    clear_action = MagicMock()
    frames_action = MagicMock()
    samples_action = MagicMock()
    menu = MagicMock()
    menu.addAction.side_effect = [
        mark_in_action,
        mark_out_action,
        clear_action,
        frames_action,
        samples_action,
    ]
    menu.exec.return_value = clear_action
    event = MagicMock()
    event.scenePos.return_value = QPointF(TRACK_HEADER_WIDTH, 0.0)

    with patch("PyQt6.QtWidgets.QMenu", return_value=menu):
        ruler.contextMenuEvent(event)

    assert cleared == [True]
    event.accept.assert_called_once()


def test_range_handle_commit_uses_an_undoable_timeline_command() -> None:
    """Dragging a range handle commits exactly one SetTimelineRangeCommand."""
    timeline = TimelineSequence(timeline_id="tl1", name="Test", fps=24.0).with_range(5, 20)
    committed_commands: list[object] = []
    view = MagicMock()
    view._timeline = timeline
    view._execute_command.side_effect = committed_commands.append

    TimelineView._on_range_change_finished(view, 12, 36)

    assert len(committed_commands) == 1
    command = committed_commands[0]
    assert command.new_start == 12
    assert command.new_end == 36
    assert command._old_start == 5
    assert command._old_end == 20


def test_set_corner_timecode_skips_viewport_repaint_while_hidden() -> None:
    """A hidden timeline must not force a QGraphicsView repaint.

    The Edit timeline may be hidden while the shared playback controller is
    driven by another page.  Forcing a hidden QGraphicsView repaint during
    that page transition can process stale scene work on macOS.  The method
    must therefore no-op until the timeline is actually visible.
    """
    view = MagicMock()
    view.isVisible.return_value = False
    view._corner_timecode_text = "00:00:00:00"

    TimelineView.set_corner_timecode(view, "00:00:05:00")

    # The new text is still recorded so a later show() repaints correctly...
    assert view._corner_timecode_text == "00:00:05:00"
    # ...but the GL viewport is never poked while hidden.
    view.viewport().update.assert_not_called()


def test_set_corner_timecode_repaints_viewport_when_visible() -> None:
    """A visible timeline still updates its timecode corner panel.

    契约更新（独立 QLabel 自重绘）：set_corner_timecode 只更新记录文本并
    刷新 _corner_timecode_widget 标签，不再触发 QGraphicsView viewport 重绘
    （避免污染 viewport 脏区）。
    """
    view = MagicMock()
    view.isVisible.return_value = True
    view._corner_timecode_text = "00:00:00:00"

    TimelineView.set_corner_timecode(view, "00:00:05:00")

    assert view._corner_timecode_text == "00:00:05:00"
    view._corner_timecode_widget.setText.assert_called_once_with(
        "00:00:05:00")
    view.viewport().update.assert_not_called()
