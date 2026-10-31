"""Composition-range and media-layer contracts for the Keyframes timeline."""

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_model import FrameMapper, KeyframesModel
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.keyframes_timeline_view import (
    KeyframesTimelineView,
)


@pytest.mark.usefixtures("qapp")
def test_keyframes_panel_starts_with_full_composition_work_area(qapp) -> None:
    panel = KeyframesPanel()
    panel.set_frame_range(0, 239, fps=24.0)

    assert panel.effective_play_range() == (0, 239)
    assert panel.play_range_points() == (0, 239)
    assert panel._timeline_view.play_range() == (0, 239)

    panel.set_play_range(24, 119)
    panel.set_frame_range(0, 179, fps=24.0)

    # A user-edited work area is preserved, but remains inside the new comp.
    assert panel.effective_play_range() == (24, 119)
    assert panel._timeline_view.play_range() == (24, 119)


@pytest.mark.usefixtures("qapp")
def test_keyframes_panel_detach_stops_owned_playback_timer(qapp) -> None:
    panel = KeyframesPanel()
    panel.set_frame_range(0, 24, fps=24.0)
    panel._internal_set_playing(True)

    assert panel._playing is True
    assert panel._play_timer.isActive() is True

    panel.set_composition_active(False)

    assert panel._playing is False
    assert panel._play_timer.isActive() is False

    panel.dispose()
    panel.dispose()
    assert panel._disposed is True
    assert panel._play_timer.isActive() is False


@pytest.mark.usefixtures("qapp")
def test_keyframes_panel_default_fit_uses_composition_extent(qapp) -> None:
    panel = KeyframesPanel()
    panel.resize(1000, 300)
    panel.set_frame_range(0, 999, fps=24.0)
    panel.set_comp_extent(0, 99)
    panel._timeline_view.resize(700, 200)

    panel.fit_time_range()

    # The first 100 composition frames fill the visible timeline. The
    # media-extended ruler remains addressable beyond the right edge.
    assert panel._frame_mapper.frame_to_x(100) == pytest.approx(700.0)
    assert panel._frame_mapper.frame_to_x(1000) > 700.0


@pytest.mark.usefixtures("qapp")
def test_default_node_bars_use_composition_extent_after_rebuild(qapp) -> None:
    graph = NodeGraph()
    graph.add_node(ConstantNode(node_id="constant"))
    panel = KeyframesPanel()
    panel.set_frame_range(0, 999, fps=24.0)
    panel.set_comp_extent(0, 99)
    panel.set_node_graph(graph)

    track = panel._model.track_for("node:constant")
    assert track is not None and track.range_bar is not None
    assert track.range_bar.end_frame == 99


@pytest.mark.usefixtures("qapp")
def test_long_media_bar_remains_visible_beyond_composition(qapp) -> None:
    graph = NodeGraph()
    video = VideoFrameNode(node_id="video")
    video.set_parameter("total_frames", 500)
    graph.add_node(video)

    model = KeyframesModel()
    model.set_comp_range(0, 99)
    model.rebuild(graph)
    view = KeyframesTimelineView(model, FrameMapper(pixels_per_frame=4.0))
    view.set_composition_range(0, 99)
    view.resize(2200, 200)
    assert view.project_range() == (0, 99)

    track = model.track_for("node:video")
    assert track is not None and track.range_bar is not None
    assert track.range_bar.end_frame == 499  # source length remains real

    x0, x1 = view._range_bar_x_bounds(track.range_bar)
    assert x0 == pytest.approx(0.0)
    assert x1 == pytest.approx(1996.0)  # full source end, frame 499
    assert view._hit_range_bar(x1, 42) == ("node:video", "right")

    # Trim handles are allowed to reach beyond the comp extent, but never
    # beyond the source. This is what makes a 2911-frame source editable
    # instead of silently shortening it to the 74-frame composition.
    view._drag_row_id = "node:video"
    view._drag_edge = "right"
    view._do_range_edge_drag(view._fm.frame_to_x(300, 0.0))
    assert track.range_bar.end_frame == 300

    # After trimming, the body can slide while preserving its duration and
    # remains clamped to the source's [0, total-1] interval.
    view._drag_edge = None
    view._range_press_frame = 100
    view._range_press_start = 0
    view._range_press_end = 300
    view._do_range_body_drag(view._fm.frame_to_x(100, 0.0))
    assert (track.range_bar.start_frame, track.range_bar.end_frame) == (0, 300)
    view._do_range_body_drag(view._fm.frame_to_x(200, 0.0))
    assert (track.range_bar.start_frame, track.range_bar.end_frame) == (100, 400)


def test_composite_page_sync_exposes_media_range_but_keeps_comp_extent() -> None:
    page = CompositePage.__new__(CompositePage)
    calls: list[tuple[str, tuple]] = []

    class _Panel:
        def set_frame_range(self, start, end, fps=None):
            calls.append(("frame", (start, end, fps)))

        def set_comp_extent(self, start, end):
            calls.append(("extent", (start, end)))

    page._keyframes_panel = _Panel()
    page._spline_panel = None
    page._total_frames_for_node = lambda: 240
    page._max_video_media_frames = lambda: 2911
    page.comp_clip_fps = lambda: 24.0

    page._sync_keyframes_panel_frame_range()

    assert calls == [
        ("frame", (0, 2910, 24.0)),
        ("extent", (0, 239)),
    ]
