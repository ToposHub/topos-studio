"""Regression tests for typed Composite animation values (P1-05)."""

import pytest

from src.shared.keyframe import (
    DiscreteKeyframeTrack,
    InterpolationType,
    Keyframe,
    VectorKeyframeTrack,
    keyframe_track_from_dict,
    make_keyframe_track,
)
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.base_node import Node, ParamDef
from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
from src.features.composite.edit_service import CompositeEditService


pytestmark = pytest.mark.composite_core


class TypedNode(Node):
    node_type = "typed_test"
    PARAM_DEFS = [
        ParamDef("position", "vec2", (0.0, 0.0), min_val=-100.0, max_val=100.0),
        ParamDef("enabled", "bool", False),
        ParamDef("mode", "option", "linear", labels=["linear", "hold"]),
    ]


def test_vector_track_interpolates_component_wise_and_round_trips():
    track = VectorKeyframeTrack(
        "position", dimension=2, min_val=-100.0, max_val=100.0)
    track.add_keyframe(Keyframe(0, (0.0, 2.0), InterpolationType.LINEAR))
    track.add_keyframe(Keyframe(10, (10.0, 6.0), InterpolationType.LINEAR))

    assert track.get_value(5) == pytest.approx((5.0, 4.0))
    restored = keyframe_track_from_dict(track.to_dict(), value_type="vec2")
    assert isinstance(restored, VectorKeyframeTrack)
    assert restored.get_value(5) == pytest.approx((5.0, 4.0))
    assert restored.to_dict()["keyframes"][0]["value"] == [0.0, 2.0]


def test_discrete_track_holds_bool_and_option_without_numeric_coercion():
    for value_type, first, second in (
            ("bool", False, True), ("option", "linear", "hold")):
        track = DiscreteKeyframeTrack("value", value_type=value_type)
        track.add_keyframe(Keyframe(0, first))
        track.add_keyframe(Keyframe(10, second))

        assert track.get_value(5, default=second) == first
        assert track.get_value(10, default=first) == second
        assert keyframe_track_from_dict(
            track.to_dict(), value_type=value_type).get_value(5) == first


def test_node_graph_snapshot_preserves_typed_animation_domains():
    graph = NodeGraph()
    node = TypedNode(node_id="typed-1")
    graph.add_node(node)

    position = make_keyframe_track("position", "vec2", -100.0, 100.0)
    position.add_keyframe(Keyframe(0, (0.0, 0.0)))
    position.add_keyframe(Keyframe(10, (10.0, 20.0)))
    enabled = make_keyframe_track("enabled", "bool")
    enabled.add_keyframe(Keyframe(0, False))
    enabled.add_keyframe(Keyframe(10, True))
    mode = make_keyframe_track("mode", "option")
    mode.add_keyframe(Keyframe(0, "linear"))
    mode.add_keyframe(Keyframe(10, "hold"))
    node.set_keyframe_track("position", position)
    node.set_keyframe_track("enabled", enabled)
    node.set_keyframe_track("mode", mode)

    params, _, _ = graph._evaluate_snapshot_parameters(5, 1920, 1080, 24.0)
    assert params[node.id]["position"] == pytest.approx((5.0, 10.0))
    assert params[node.id]["enabled"] is False
    assert params[node.id]["mode"] == "linear"
    assert node.keyframe_tracks["position"]["value_type"] == "vec2"
    assert node.keyframe_tracks["enabled"]["value_type"] == "bool"
    assert node.keyframe_tracks["mode"]["value_type"] == "option"


def test_render_snapshot_interpolation_uses_param_metadata():
    evaluator = object.__new__(BaseEvaluatorMixin)
    snapshot = type("Snapshot", (), {
        "keyframe_tracks": {
            "position": {
                "value_type": "vec2",
                "keyframes": [
                    {"frame": 0, "value": [0.0, 2.0]},
                    {"frame": 10, "value": [10.0, 6.0]},
                ],
                "min_val": -100.0,
                "max_val": 100.0,
            },
            "enabled": {
                "value_type": "bool",
                "keyframes": [
                    {"frame": 0, "value": False},
                    {"frame": 10, "value": True},
                ],
            },
        },
        "param_defs_meta": {
            "position": {"ptype": "vec2", "min_val": -100.0, "max_val": 100.0},
            "enabled": {"ptype": "bool", "min_val": 0.0, "max_val": 1.0},
        },
        "parameters": {"position": (0.0, 0.0), "enabled": False},
    })()

    values = evaluator._interpolate_parameters(snapshot, 5)
    assert values["position"] == pytest.approx((5.0, 4.0))
    assert values["enabled"] is False


def test_edit_service_validates_and_applies_discrete_and_vector_values():
    graph = NodeGraph()
    node = TypedNode(node_id="typed-edit")
    graph.add_node(node)
    service = CompositeEditService(graph)

    plan = {
        "plan_id": "typed-values",
        "operations": [
            {
                "op": "add_keyframe", "node_id": node.id,
                "param_name": "enabled", "frame": 0, "value": False,
            },
            {
                "op": "add_keyframe", "node_id": node.id,
                "param_name": "enabled", "frame": 10, "value": True,
            },
            {
                "op": "set_keyframes", "node_id": node.id,
                "param_name": "mode", "keyframes": [
                    {"frame": 0, "value": "linear"},
                    {"frame": 10, "value": "hold"},
                ],
            },
            {
                "op": "set_keyframes", "node_id": node.id,
                "param_name": "position", "keyframes": [
                    {"frame": 0, "value": [0.0, 0.0]},
                    {"frame": 10, "value": [10.0, 20.0]},
                ],
            },
        ],
    }
    result = service.apply(plan)
    assert result.applied
    assert node.keyframe_tracks["enabled"]["value_type"] == "bool"
    assert node.keyframe_tracks["mode"]["value_type"] == "option"
    assert node.keyframe_tracks["position"]["value_type"] == "vec2"

    invalid = {
        "plan_id": "bad-discrete",
        "operations": [{
            "op": "add_keyframe", "node_id": node.id,
            "param_name": "mode", "frame": 20, "value": "unsupported",
        }],
    }
    assert not service.apply(invalid).applied


def test_properties_panel_exposes_stopwatches_for_typed_values(qapp):
    from src.features.composite.ui.node_properties_panel import NodePropertiesPanel

    graph = NodeGraph()
    node = TypedNode(node_id="typed-ui")
    graph.add_node(node)
    panel = NodePropertiesPanel()
    panel.set_node_graph(graph)
    panel.set_node(
        node.id, node.node_type, node.node_title, dict(node.parameters),
        node.get_param_options(), node.get_param_defs())
    qapp.processEvents()

    widgets = panel._sections[node.id]._param_widgets
    assert widgets["enabled"]["keyframe_btn"].isCheckable()
    assert widgets["mode"]["keyframe_btn"].isCheckable()
    assert widgets["position"]["keyframe_btn"].isCheckable()

    widgets["enabled"]["keyframe_btn"].click()
    widgets["mode"]["keyframe_btn"].click()
    widgets["position"]["keyframe_btn"].click()
    assert node.keyframe_tracks["enabled"]["keyframes"][0]["value"] is False
    assert node.keyframe_tracks["mode"]["keyframes"][0]["value"] == "linear"
    assert node.keyframe_tracks["position"]["keyframes"][0]["value"] == [0.0, 0.0]

    panel.close()
