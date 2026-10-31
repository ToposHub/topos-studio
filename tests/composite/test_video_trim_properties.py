"""Video Frame Properties trim control and layer-range synchronization."""

from types import SimpleNamespace

from src.features.composite.core.layer_model import Layer
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.node_properties_panel import (
    NodePropertiesPanel,
    NodeSectionWidget,
)
from src.shared.ui.widgets.drag_value import DragValue


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
