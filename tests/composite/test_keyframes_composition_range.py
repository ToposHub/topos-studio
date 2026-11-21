"""Composition-range and media-layer contracts for the Keyframes timeline."""

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_model import KEYFRAMES_ROW_H, FrameMapper, KeyframesModel
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.keyframes_timeline_view import (
    KeyframesTimelineView,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


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
    view._start_drag("range_body", view._fm.frame_to_x(100, 0.0),
                     row_id="node:video")
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


def test_layer_ruler_and_hits_stop_at_comp_end_without_trimming_source(qapp) -> None:
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    video = VideoFrameNode(node_id="source")
    video.set_parameter("total_frames", 500)
    graph.add_node(video)
    controller = LayerStackController(graph, layer_stack_version=2)
    layer_id = controller.add_media_layer("Video", video.id)
    root = graph.get_node(graph.get_layer(layer_id).root_node_id)
    rotation = KeyframeTrack(param_name="rotation", min_val=-360, max_val=360)
    rotation.add_keyframe(Keyframe(20, 0))
    rotation.add_keyframe(Keyframe(100, 90))
    root.set_keyframe_track("rotation", rotation)
    panel = KeyframesPanel()
    panel.resize(1100, 350)
    panel.set_layer_stack_controller(controller)
    panel.set_layer_view_mode("layers")
    panel.set_frame_range(0, 499, fps=24)
    panel.set_comp_extent(0, 99)
    panel.set_node_graph(graph, selected_layer_id=layer_id)
    panel._show_layer_properties("rotation")
    panel.show()
    try:
        qapp.processEvents()
        panel.fit_time_range()
        view = panel._timeline_view
        track = panel._model.track_for(f"layer:{layer_id}")
        layer_y = view.ruler_height() + KEYFRAMES_ROW_H // 2
        assert track.range_bar.end_frame == 499 and track.source_total == 500
        assert view._hit_range_bar(view.width() - 1, layer_y) == (f"layer:{layer_id}", None)
        assert view._hit_range_bar(view.width() + 1, layer_y) is None
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(view.width() - 1, 21))
        assert panel._state.current_frame == 99
        panel.seek_to_frame(400)
        assert panel._state.current_frame == 99
        view.select_all_keyframes()
        assert view.selected_keyframes() == [(f"param:{root.id}:rotation", 20)]
        assert len(root.keyframe_tracks["rotation"]["keyframes"]) == 2

        fit_zoom = panel._frame_mapper.pixels_per_frame
        view.zoom_time_at(view.width() / 2, 0.25)
        assert panel._frame_mapper.pixels_per_frame == pytest.approx(fit_zoom)
        view.zoom_time_at(view.width() / 2, 2)
        view.set_horizontal_offset(-1_000_000)
        assert panel._frame_mapper.x_to_frame(view.width()) == pytest.approx(100)
        view.set_horizontal_offset(1_000_000)
        assert panel._frame_mapper.x_to_frame(0) == pytest.approx(0)

        panel.set_comp_extent(0, 199)
        view.select_all_keyframes()
        assert [frame for _, frame in view.selected_keyframes()] == [20, 100]
        assert view.copy_selected_keyframes() == 2
        assert not view.paste_keyframes(150)
        assert not view.scale_selected_keyframes(3)
        assert track.range_bar.end_frame == 499
        view._select_keyframe(f"param:{root.id}:rotation", 100)
        panel.set_comp_extent(0, 99)
        deletes = []
        panel.layer_delete_requested.connect(deletes.append)
        panel._delete_layer_timeline_selection()
        assert not deletes
    finally:
        panel.close()


@pytest.mark.parametrize("frames", (1, 100_000))
def test_layer_fit_includes_the_entire_composition_at_extreme_durations(qapp, frames) -> None:
    model = KeyframesModel()
    model.set_view_mode("layers")
    mapper = FrameMapper()
    view = KeyframesTimelineView(model, mapper)
    view.resize(800, 200)
    view.set_composition_range(0, frames - 1)
    view.fit_range(0, frames - 1)
    assert mapper.frame_to_x(frames) == pytest.approx(view.width())
    view.zoom_time_at(400, 0.25)
    assert mapper.frame_to_x(frames) == pytest.approx(view.width())


def test_layer_navigator_and_work_area_have_independent_drag_targets(qapp) -> None:
    panel = KeyframesPanel()
    panel.resize(1150, 360)
    panel.set_layer_view_mode("layers")
    panel.set_frame_range(0, 239, fps=24)
    panel.show()
    try:
        qapp.processEvents()
        panel.fit_time_range()
        view = panel._timeline_view
        mapper = panel._frame_mapper
        rail = view._navigator_rail()
        y = round(rail.center().y())
        button = Qt.MouseButton.LeftButton
        original = panel.effective_play_range()
        assert view.ruler_height() == panel._tree_view._header_height

        def drag(x0: int, x1: int, row_y: int) -> None:
            QTest.mousePress(view, button, pos=QPoint(x0, row_y))
            QTest.mouseMove(view, QPoint(x1, row_y))
            QTest.mouseRelease(view, button, pos=QPoint(x1, row_y))

        drag(round(rail.right() - 4), round(rail.center().x() - 4), y)
        left, right = view._navigator_window()
        assert left == pytest.approx(0) and right == pytest.approx(120, abs=1)
        assert panel.effective_play_range() == original

        middle = round((rail.left() + rail.center().x()) / 2)
        drag(middle, middle + round(rail.width() / 4), y)
        left, right = view._navigator_window()
        assert left == pytest.approx(60, abs=1) and right == pytest.approx(180, abs=1)
        assert panel.effective_play_range() == original

        QTest.mouseDClick(view, button, pos=QPoint(round(rail.center().x()), y))
        assert view._navigator_window() == pytest.approx((0, 240))
        panel.set_play_range(30, 120)
        grab_y = view.ruler_height() - 8
        handle = round(mapper.frame_to_x(121)) - 7
        assert view._hit_workarea(handle, grab_y) == "out"
        drag(handle, handle + round(mapper.pixels_per_frame * 20), grab_y)
        assert panel.effective_play_range() == (30, 140)
        body = round(mapper.frame_to_x(80))
        drag(body, body + round(mapper.pixels_per_frame * 15), grab_y)
        assert panel.effective_play_range() == (45, 155)
        assert view._navigator_window() == pytest.approx((0, 240))
        assert panel._state.current_frame == 0
    finally:
        panel.close()


def test_layer_one_frame_work_area_still_has_a_body_to_drag(qapp) -> None:
    panel = KeyframesPanel()
    panel.resize(1150, 360)
    panel.set_layer_view_mode("layers")
    panel.set_frame_range(0, 2399)
    panel.show()
    try:
        qapp.processEvents()
        panel.fit_time_range()
        panel.set_play_range(100, 100)
        view = panel._timeline_view
        x = round(panel._frame_mapper.frame_to_x(100.5))
        assert view._hit_workarea(x, view.ruler_height() - 8) == "body"
    finally:
        panel.close()
