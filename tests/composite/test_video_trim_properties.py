"""Video Frame Properties trim control and layer-range synchronization."""

from types import SimpleNamespace

from src.features.composite.core.layer_model import Layer
from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_model import FrameMapper, KeyframesModel
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.keyframes_timeline_view import KeyframesTimelineView
from src.features.composite.ui.node_properties_panel import (
    NodePropertiesPanel,
    NodeSectionWidget,
)
from src.shared.ui.widgets.drag_value import DragValue
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest


def _video_params(node: VideoFrameNode) -> dict:
    return dict(node.parameters)


def test_video_properties_build_combined_trim_editor(qapp) -> None:
    panel = NodePropertiesPanel()
    node = VideoFrameNode(node_id="video")
    node.set_parameter("total_frames", 1751)
    pdefs = node.get_param_defs()
    section = NodeSectionWidget(
        node_id=node.id,
        node_type=node.node_type,
        node_title=node.node_title,
        parameters=_video_params(node),
        param_options=node.get_param_options(),
        param_defs=pdefs,
        parent_panel=panel,
    )
    panel._sections[node.id] = section

    trim = section._param_widgets["trim_range"]["widget"]
    assert trim.values() == (0, 1750)
    assert "source_in" not in section._param_widgets
    assert "source_out" not in section._param_widgets

    assert isinstance(trim._in_spin, DragValue)
    assert isinstance(trim._out_spin, DragValue)
    values_layout = trim.layout().itemAt(1).layout()
    assert values_layout is not None
    assert values_layout.indexOf(trim._in_label) >= 0
    assert values_layout.indexOf(trim._in_spin) >= 0
    assert values_layout.indexOf(trim._out_label) >= 0
    assert values_layout.indexOf(trim._out_spin) >= 0


def test_trim_range_updates_source_and_layer_length() -> None:
    node = VideoFrameNode(node_id="video")
    node.set_parameter("total_frames", 500)

    CompositePage._apply_video_trim_range(node, 30, 130)

    assert node.parameters["source_in"] == 30
    assert node.parameters["source_out"] == 130
    assert node.time_range == {
        "enabled": True,
        "comp_start": 0,
        "comp_end": 100,
        "source_in": 30,
        "source_out": 130,
    }


def test_trim_snapshot_restores_source_and_layer_range() -> None:
    node = VideoFrameNode(node_id="video")
    node.set_parameter("total_frames", 500)
    node.time_range = {"enabled": True, "comp_start": 12, "comp_end": 40}
    before = CompositePage._video_trim_snapshot(node)

    CompositePage._apply_video_trim_range(node, 20, 80)
    CompositePage._restore_video_trim_snapshot(node, before)

    assert node.parameters["source_in"] == 0
    assert node.parameters["source_out"] == -1
    assert node.time_range == {"enabled": True, "comp_start": 12, "comp_end": 40}


def test_default_video_layer_head_trim_advances_source_in() -> None:
    page = CompositePage()
    graph = page._node_graph
    node = VideoFrameNode(node_id="video")
    node.set_parameter("total_frames", 500)
    graph.add_node(node)
    page._on_keyframes_panel_time_range_changed(
        node.id,
        {"comp_start": 30, "comp_end": 499, "enabled": True,
         "edit": "trim_in"},
    )

    assert node.parameters["source_in"] == 30
    assert node.time_range["comp_start"] == 30


def test_layer_bar_trim_gates_the_video_source_before_comp_start(qapp) -> None:
    page = CompositePage()
    graph = NodeGraph()
    video = VideoFrameNode(node_id="video")
    video.set_parameter("total_frames", 500)
    root = TransformNode(node_id="root")
    graph.add_node(video)
    graph.add_node(root)
    layer = Layer(layer_id="layer", name="Video", root_node_id="root",
                  source_node_id="video")
    page._node_graph = graph
    page._layer_stack_controller = SimpleNamespace(
        layers_map=lambda: {"layer": layer})

    page._on_keyframes_panel_time_range_changed(
        "root",
        {"comp_start": 16, "comp_end": 120, "enabled": True,
         "edit": "trim_in"},
    )

    assert video.parameters["source_in"] == 16
    assert video.time_range == root.time_range


def test_layer_video_range_drag_uses_composition_frames_and_one_undo(qapp) -> None:
    page = CompositePage()
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="output"))
    page._node_graph = graph
    video = VideoFrameNode(node_id="video")
    video.set_parameter("total_frames", 100)
    graph.add_node(video)
    controller = LayerStackController(graph, page._undo_stack)
    page._layer_stack_controller = controller
    layer_id = controller.add_media_layer("Video", video.id)
    layer = graph.get_layer(layer_id)
    model = KeyframesModel()
    model.set_comp_range(0, 300)
    model.rebuild_layers(controller)
    view = KeyframesTimelineView(model, FrameMapper(pixels_per_frame=5.0))
    view.resize(900, 180)
    view.show()
    view.range_changed.connect(page._on_keyframes_panel_time_range_changed)
    row_id = f"layer:{layer_id}"

    def drag(start_frame: int, end_frame: int) -> None:
        button = Qt.MouseButton.LeftButton
        none = Qt.MouseButton.NoButton
        modifier = Qt.KeyboardModifier.NoModifier
        start = QPointF(start_frame * 5, 42)
        end = QPointF(end_frame * 5, 42)
        assert view._hit_range_bar(start.x(), start.y()) is not None
        view.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress,
                                         start, start, button, button, modifier))
        view.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove,
                                        end, end, none, button, modifier))
        view.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease,
                                           end, end, button, none, modifier))

    drag(20, 35)
    assert (layer.in_frame, layer.out_frame) == (15, 114)
    assert video.parameters["source_in"] == 0
    assert video.parameters["source_out"] == -1
    assert video._resolve_frame_number(15) == 0
    assert video._resolve_frame_number(114) == 99
    drag(35, 40)
    assert (layer.in_frame, layer.out_frame) == (20, 119)

    page._undo_stack.undo()
    assert (layer.in_frame, layer.out_frame) == (15, 114)
    page._undo_stack.redo()
    assert (layer.in_frame, layer.out_frame) == (20, 119)
    drag(20, 35)
    assert (layer.in_frame, layer.out_frame) == (35, 119)
    assert video.parameters["source_in"] == 15
    assert video._resolve_frame_number(119) + video.parameters["source_in"] == 99
    drag(119, 35)
    assert (layer.in_frame, layer.out_frame) == (35, 35)
    assert video.parameters["source_out"] == 15
    assert view._hit_range_bar(176.5, 42) == (row_id, None)

    saved = NodeGraph.from_dict(graph.to_dict())
    assert saved.snapshot(frame_number=34).layer_frame_gates[layer.root_node_id] == (35, 35)
    assert saved.get_node(video.id).time_range["comp_start"] == 35


def test_static_layer_range_gates_its_pixels_and_restores_on_undo(qapp) -> None:
    from src.features.composite.nodes.constant_node import ConstantNode

    page = CompositePage()
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="output"))
    page._node_graph = graph
    page._layer_stack_controller = LayerStackController(graph, page._undo_stack)
    node = ConstantNode(node_id="solid")
    graph.add_node(node)
    layer_id = page._layer_stack_controller.add_media_layer("Solid", node.id)
    layer = graph.get_layer(layer_id)
    page._on_keyframes_panel_time_range_changed(
        layer.root_node_id, {"comp_start": 6, "comp_end": 12, "enabled": True,
                             "edit": "slide", "layer_id": layer_id},
    )

    assert graph.snapshot(frame_number=3).layer_frame_gates[layer.root_node_id] == (6, 12)
    assert graph.get_node(layer.root_node_id).time_range is None
    page._undo_stack.undo()
    assert layer.root_node_id not in graph.snapshot(frame_number=3).layer_frame_gates


def test_layer_timing_shortcuts_commit_via_host_and_keep_work_area_separate(qapp, qtbot) -> None:
    page = CompositePage()
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="output"))
    page._node_graph = graph
    page._composite_duration_frames = 240
    page._layer_stack_controller = LayerStackController(graph, page._undo_stack)
    video = VideoFrameNode(node_id="video")
    video.set_parameter("total_frames", 100)
    graph.add_node(video)
    layer_id = page._layer_stack_controller.add_media_layer("Clip", video.id)
    layer = graph.get_layer(layer_id)
    panel = KeyframesPanel()
    qtbot.addWidget(panel)
    page.set_keyframes_panel(panel)
    panel.set_composition_active(True)
    panel.set_node_graph(graph, selected_layer_id=layer_id)
    panel.resize(900, 200)
    panel.show()
    timeline = panel._timeline_view
    timeline.setFocus()
    qapp.processEvents()

    panel.set_current_frame(15)
    QTest.keyClick(timeline, Qt.Key.Key_BracketLeft)
    assert (layer.in_frame, layer.out_frame) == (15, 114)
    assert video.parameters["source_in"] == 0
    assert video.parameters["source_out"] == -1

    panel.set_current_frame(20)
    QTest.keyClick(timeline, Qt.Key.Key_BracketLeft, Qt.KeyboardModifier.AltModifier)
    assert (layer.in_frame, layer.out_frame) == (20, 114)
    assert video.parameters["source_in"] == 5
    assert video._resolve_frame_number(114) + video.parameters["source_in"] == 99

    work_area = panel.effective_play_range()
    QTest.keyClick(timeline, Qt.Key.Key_O)
    assert panel._state.current_frame == 114
    QTest.keyClick(timeline, Qt.Key.Key_I)
    assert panel._state.current_frame == 20
    assert panel.effective_play_range() == work_area

    panel.set_current_frame(91)
    QTest.keyClick(timeline, Qt.Key.Key_BracketRight, Qt.KeyboardModifier.AltModifier)
    assert (layer.in_frame, layer.out_frame) == (20, 91)
    assert video.parameters["source_out"] == 76
    panel.set_current_frame(111)
    QTest.keyClick(timeline, Qt.Key.Key_BracketRight)
    assert (layer.in_frame, layer.out_frame) == (40, 111)
    assert video.parameters["source_out"] == 76
    page._undo_stack.undo()
    assert (layer.in_frame, layer.out_frame) == (20, 91)

    panel.set_current_frame(26)
    QTest.keyClick(timeline, Qt.Key.Key_B)
    panel.set_current_frame(45)
    QTest.keyClick(timeline, Qt.Key.Key_N)
    assert panel.effective_play_range() == (26, 45)
    QTest.keyClick(timeline, Qt.Key.Key_Home, Qt.KeyboardModifier.ShiftModifier)
    assert panel._state.current_frame == 26
    QTest.keyClick(timeline, Qt.Key.Key_End, Qt.KeyboardModifier.ShiftModifier)
    assert panel._state.current_frame == 45
    panel.fit_time_range()
    full_zoom = panel._frame_mapper.pixels_per_frame
    work_area_modifier = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    QTest.keyClick(timeline, Qt.Key.Key_B, work_area_modifier)
    assert panel.effective_play_range() == (20, 91)
    assert panel._frame_mapper.pixels_per_frame == full_zoom
    panel.set_current_frame(100)
    QTest.keyClick(timeline, Qt.Key.Key_B)
    assert panel.effective_play_range() == (100, 100)
    panel.set_current_frame(45)
    QTest.keyClick(timeline, Qt.Key.Key_N)
    assert panel.effective_play_range() == (45, 45)
    panel._selected_layer_id = None
    QTest.keyClick(timeline, Qt.Key.Key_B, work_area_modifier)
    assert panel.effective_play_range() == (0, 239)
    panel.set_play_range(26, 45)

    panel._search_edit.setFocus()
    QTest.keyClick(panel._search_edit, Qt.Key.Key_I)
    assert panel._search_edit.text().casefold() == "i"
    assert panel._state.current_frame == 45
    assert panel.effective_play_range() == (26, 45)
    panel._search_edit.clearFocus()
    panel.hide()
    panel.set_layer_view_mode(KeyframesModel.VIEW_MODE_NODES)
    panel._search_edit.clear()
    panel.show()
    timeline.setFocus()
    qapp.processEvents()
    panel.set_current_frame(34)
    assert panel._shortcut_set_in.isEnabled()
    assert panel._shortcut_dispatcher.registry.command_for("I") == "edit.set_work_in"
    panel._shortcut_dispatcher.focus_query = lambda: None
    assert panel._shortcut_dispatcher.handle_key("I") == "edit.set_work_in"
    assert panel.effective_play_range() == (34, 45)


def test_layer_slide_moves_owned_tracks_and_keeps_shared_tracks(qapp) -> None:
    from src.features.composite.core.layer_model import BOTTOM_UP_LAYER_STACK_VERSION
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.text_node import TextNode
    from src.shared.keyframe import Keyframe, KeyframeTrack

    page = CompositePage()
    graph = NodeGraph()
    page._node_graph = graph
    controller = LayerStackController(
        graph, page._undo_stack, layer_stack_version=BOTTOM_UP_LAYER_STACK_VERSION)
    page._layer_stack_controller = controller
    graph.add_node(OutputNode(node_id="output"))
    title = TextNode(node_id="title")
    effect = BlurNode(node_id="blur")
    shared = ConstantNode(node_id="shared")
    for node in (title, effect, shared):
        graph.add_node(node)
    controller.add_media_layer("Shared", shared.id)
    layer_id = controller.add_media_layer("Title", title.id)
    assert controller.add_node_to_layer(layer_id, effect.id)
    assert graph.connect(shared.id, "result", effect.id, "velocity")
    merge = controller.layer_merge_node(layer_id)
    assert merge is not None

    def animate(node, name: str, value: float) -> None:
        track = KeyframeTrack(param_name=name, min_val=0.0, max_val=100.0)
        track.add_keyframe(Keyframe(frame=5, value=value, handle_right=(2.0, 0.1)))
        node.set_keyframe_track(name, track)

    nodes_and_tracks = (
        (title, "translate_x"), (effect, "radius"), (merge, "opacity"),
    )
    for node, param in (*nodes_and_tracks, (shared, "color_r")):
        animate(node, param, 0.75)

    def edit(start: int, end: int, kind: str) -> None:
        page._on_keyframes_panel_time_range_changed(
            effect.id, {"comp_start": start, "comp_end": end, "enabled": True,
                        "edit": kind, "layer_id": layer_id},
        )

    edit(10, 30, "slide")
    for node, param in nodes_and_tracks:
        key = node.keyframe_tracks[param]["keyframes"][0]
        assert (key["frame"], key["handle_right"]) == (15, [2.0, 0.1])
    assert shared.keyframe_tracks["color_r"]["keyframes"][0]["frame"] == 5

    edit(12, 30, "trim_in")
    assert title.keyframe_tracks["translate_x"]["keyframes"][0]["frame"] == 15
    page._undo_stack.undo()
    page._undo_stack.undo()
    for node, param in nodes_and_tracks:
        assert node.keyframe_tracks[param]["keyframes"][0]["frame"] == 5
    page._undo_stack.redo()
    assert merge.keyframe_tracks["opacity"]["keyframes"][0]["frame"] == 15
