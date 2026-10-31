from __future__ import annotations

from types import SimpleNamespace

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.ui.project_settings_widget import ProjectSettingsWidget
from src.features.edit.core.composite_graph_data import PROJECT_BACKGROUND_PARAM
from src.shared.color.color_conversion import srgb_to_linear


def _graph() -> NodeGraph:
    graph = NodeGraph()
    graph.fps = 30.0
    graph.duration_frames = 150
    graph.nodes["output"] = SimpleNamespace(
        node_type="output",
        parameters={"width": 2048, "height": 858},
    )
    return graph


def test_project_settings_loads_persisted_timing(qapp) -> None:
    widget = ProjectSettingsWidget()

    widget.load_from_graph(_graph())

    assert widget._fps_combo.currentData() == 30.0
    assert widget._duration_drag.value() == 5.0
    assert widget._width_drag.value() == 2048.0
    assert widget._height_drag.value() == 858.0


def test_timeline_bound_project_settings_follow_runtime_metadata(qapp) -> None:
    widget = ProjectSettingsWidget()
    graph = _graph()

    widget.load_from_graph(
        graph,
        width=3840,
        height=2160,
        fps=25.0,
        duration_frames=75,
        timeline_bound=True,
    )

    assert widget._width_drag.value() == 3840.0
    assert widget._height_drag.value() == 2160.0
    assert widget._fps_combo.currentData() == 25.0
    assert widget._duration_drag.value() == 3.0
    assert not widget._width_drag.isEnabled()
    assert not widget._fps_combo.isEnabled()
    assert not widget._duration_drag.isEnabled()


def test_project_settings_reads_marked_background_constant_only(qapp) -> None:
    widget = ProjectSettingsWidget()
    graph = _graph()
    graph.nodes["background"] = SimpleNamespace(
        node_type="constant",
        parameters={
            PROJECT_BACKGROUND_PARAM: True,
            "color_r": 0.1,
            "color_g": 0.2,
            "color_b": 0.3,
            "color_a": 1.0,
        },
    )
    graph.nodes["generator"] = SimpleNamespace(
        node_type="constant",
        parameters={
            "color_r": 0.9,
            "color_g": 0.8,
            "color_b": 0.7,
            "color_a": 1.0,
        },
    )

    widget.load_from_graph(graph)

    assert widget._bg_color_value[:3] == tuple(
        srgb_to_linear(value) for value in (0.1, 0.2, 0.3)
    )
