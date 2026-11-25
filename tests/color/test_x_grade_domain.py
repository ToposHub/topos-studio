"""Keep unsupported working signals on the original corrector path."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.features.color.core.image_engine import _layer_render_data
from src.features.color.core.layer import Layer
from src.features.color.core.warper_color import WARPER_X_GRADE_SPACE
from src.features.color.core.warper_state import (
    WarperStroke, XGradeState, legacy_from_ndarray, renderable_projection,
)
from src.features.color.core.x_grade_domain import (
    active_x_grade_node, x_grade_preview_graph, x_grade_working_supported,
)
from src.features.edit.core.timeline_export import ExportGradeDegradedError, TimelineExporter
from src.shared.color.color_space_descriptor import LINEAR_SCENE, descriptor_for_space_preset
from src.shared.project.color_graph import ColorGraphData, ColorNodeData, NodeType
from src.shared.project.color_stack import ColorStackData


def _corrector(*, x_grade: bool) -> tuple[Layer, ColorGraphData, np.ndarray]:
    mesh = np.full((6, 6, 2), 0.04, dtype=np.float32)
    layer = Layer("Corrector")
    base = legacy_from_ndarray(mesh)
    if x_grade:
        red, uv = (0.65, 0.25, 0.15), (0.32, 0.42)
        stroke = WarperStroke("red", "normal", red, red, {"exposure": 0.5}, (uv, uv))
        base = replace(base, x_grade=XGradeState(strokes=(stroke,)))
    layer.warper_state = base
    layer.warper_params, layer.warper_space = renderable_projection(base)
    node = ColorNodeData(node_id="x-grade", node_type=NodeType.CORRECTOR.value, legacy_payload=layer.to_dict())
    return layer, ColorGraphData(nodes=(node,)), mesh


@pytest.mark.parametrize("preset", ["rec2100-pq", "rec2100-hlg", "davinci-wide-gamut-intermediate",
                                    "acescct", "aces2065-1", "acescg", "srgb"])
def test_explicit_other_domains_are_unsupported(preset: str) -> None:
    assert not x_grade_working_supported(descriptor_for_space_preset(preset))


def test_rec709_and_untagged_paths_are_supported() -> None:
    assert x_grade_working_supported(None)
    assert x_grade_working_supported(LINEAR_SCENE)
    assert x_grade_working_supported(descriptor_for_space_preset("rec709-g24"))


def test_unsupported_previews_preserve_authoring_and_original_warper_projection() -> None:
    layer, graph, mesh = _corrector(x_grade=True)
    stored = layer.to_dict()
    assert active_x_grade_node(graph) == "x-grade"
    assert graph_node_layer_provider(graph)("x-grade")["warper_space"] == WARPER_X_GRADE_SPACE
    preview_graph = x_grade_preview_graph(graph, supported=False)
    assert preview_graph is not graph
    assert graph.working_color_context == {}
    for preview in (graph_node_layer_provider(preview_graph)("x-grade"),
                    _layer_render_data(layer, x_grade_supported=False)):
        assert preview["warper_space"] == 0
        np.testing.assert_array_equal(preview["warper_params"], mesh)
    assert layer.to_dict() == stored
    _, untouched, _ = _corrector(x_grade=False)
    assert x_grade_preview_graph(untouched, supported=False) is untouched


@pytest.mark.parametrize("with_x_grade", [False, True])
def test_timeline_delivery_checks_working_space_before_backend_selection(with_x_grade: bool) -> None:
    _, graph, _ = _corrector(x_grade=with_x_grade)
    stack = ColorStackData(stack_id="s", color_graph=graph)
    pq = descriptor_for_space_preset("rec2100-pq")
    rec709 = descriptor_for_space_preset("rec709-g24")
    exporter = SimpleNamespace(_project=SimpleNamespace(color_stacks={"s": stack}),
                               _export_video_signal_spaces=(pq, rec709), allow_degraded_grade=True)
    clip_layer = SimpleNamespace(clip=SimpleNamespace(color_stack_id="s"), clip_id="c01")
    if with_x_grade:
        with pytest.raises(ExportGradeDegradedError, match="c01.*x-grade.*linear Rec.709"):
            TimelineExporter._clip_color_graph(exporter, clip_layer)
    else:
        assert TimelineExporter._clip_color_graph(exporter, clip_layer) == graph
    exporter._export_video_signal_spaces = (rec709, pq)
    assert TimelineExporter._clip_color_graph(exporter, clip_layer) == graph
