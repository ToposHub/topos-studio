"""AE-style expression language and snapshot integration tests."""

import math

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.core.layer_model import Layer, LayerStack
from src.features.composite.utils.expression_parser import (
    ExpressionContext,
    ExpressionParser,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


def test_ae_time_context_and_interpolation_helpers():
    parser = ExpressionParser()
    context = ExpressionContext(frame=12, time=0.5, fps=24.0,
                                project_width=3840, project_height=2160)

    assert parser.evaluate("frame + time * fps", context).value == pytest.approx(24.0)
    assert parser.evaluate("linear(5, 0, 10, 0, 100)", context).value == pytest.approx(50.0)
    assert parser.evaluate("ease(5, 0, 10, 0, 100)", context).value == pytest.approx(50.0)
    assert parser.evaluate("comp.width / comp.height", context).value == pytest.approx(3840 / 2160)


def test_value_at_time_and_random_are_deterministic():
    parser = ExpressionParser()
    context = ExpressionContext(
        frame=12,
        time=0.5,
        current_value=20.0,
        value_at_time=lambda t: 10.0 + t * 4.0,
        random_seed=1234,
    )

    assert parser.evaluate("valueAtTime(2) + value", context).value == pytest.approx(38.0)
    first = parser.evaluate("random(-1, 1) + noise(2)", context).value
    second = parser.evaluate("random(-1, 1) + noise(2)", context).value
    assert first == pytest.approx(second)


def test_layer_namespace_is_available_in_parser_context():
    parser = ExpressionParser()
    context = ExpressionContext(
        layer_attrs={"index": 2, "opacity": 0.75, "inPoint": 10,
                     "outPoint": 90, "enabled": True})

    result = parser.evaluate(
        "layer.index + layer.opacity + thisLayer.inPoint / 10",
        context)
    assert result.success is True
    assert result.value == pytest.approx(3.75)


def test_snapshot_evaluates_keyframes_before_expression_without_mutating_node():
    graph = NodeGraph()
    node = TransformNode(node_id="Transform1")
    graph.add_node(node)

    track = KeyframeTrack("scale_x", min_val=0.0, max_val=10.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(24, 2.0))
    node.set_keyframe_track("scale_x", track)
    assert node.set_expression("rotation", "frame * 2")

    snapshot = graph.snapshot(frame_number=12, fps=24.0)
    node_snapshot = snapshot.nodes[node.id]
    assert node_snapshot.parameters["rotation"] == pytest.approx(24.0)
    assert node_snapshot.parameters["scale_x"] == pytest.approx(1.0)
    assert node_snapshot.expressions["rotation"] == "frame * 2"
    assert node.parameters["rotation"] == pytest.approx(0.0)


def test_snapshot_expression_can_reference_keyframed_upstream_node():
    graph = NodeGraph()
    source = TransformNode(node_id="Source")
    driven = TransformNode(node_id="Driven")
    graph.add_node(source)
    graph.add_node(driven)

    track = KeyframeTrack("rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 10.0))
    track.add_keyframe(Keyframe(24, 50.0))
    source.set_keyframe_track("rotation", track)
    assert driven.set_expression("rotation", "Source.rotation * 2")

    snapshot = graph.snapshot(frame_number=12, fps=24.0)
    assert snapshot.nodes["Source"].parameters["rotation"] == pytest.approx(30.0)
    assert snapshot.nodes["Driven"].parameters["rotation"] == pytest.approx(60.0)


def test_snapshot_supplies_layer_namespace_without_mutating_layer_state():
    graph = NodeGraph()
    graph.duration_frames = 120
    node = TransformNode(node_id="LayerRoot")
    graph.add_node(node)
    layer = Layer(layer_id="layer-1", name="Foreground",
                  root_node_id=node.id, opacity=0.5, in_frame=10,
                  out_frame=90)
    stack = LayerStack(stack_id="main", output_node_id="",
                       layer_ids=[layer.layer_id])
    graph.set_layer_stack(stack)
    graph.upsert_layer(stack.stack_id, layer)
    assert node.set_expression(
        "rotation", "layer.index + layer.opacity + layer.inPoint")

    snapshot = graph.snapshot(frame_number=20)
    assert snapshot.nodes[node.id].parameters["rotation"] == pytest.approx(10.5)
    assert snapshot.nodes[node.id].expression_errors == {}
    assert layer.opacity == pytest.approx(0.5)


def test_snapshot_exposes_runtime_expression_errors_and_keeps_base_value():
    """Runtime failures must be locatable without poisoning GPU parameters."""
    graph = NodeGraph()
    node = TransformNode(node_id="Driven")
    graph.add_node(node)
    assert node.set_expression("rotation", "Missing.radius")

    snapshot = graph.snapshot(frame_number=12)
    node_snapshot = snapshot.nodes[node.id]

    assert node_snapshot.parameters["rotation"] == pytest.approx(0.0)
    assert "rotation" in node_snapshot.expression_errors
    assert "Missing" in node_snapshot.expression_errors["rotation"]
    assert graph.get_expression_errors(frame_number=12)[node.id]["rotation"]


def test_non_finite_expression_is_reported_and_falls_back_to_base_value():
    graph = NodeGraph()
    node = TransformNode(node_id="Driven")
    graph.add_node(node)
    assert node.set_expression("rotation", "1 / 0")

    snapshot = graph.snapshot()
    node_snapshot = snapshot.nodes[node.id]

    assert node_snapshot.parameters["rotation"] == pytest.approx(0.0)
    assert "rotation" in node_snapshot.expression_errors
    assert "non-finite" in node_snapshot.expression_errors["rotation"]


def test_disabled_expression_is_persisted_but_not_evaluated():
    graph = NodeGraph()
    node = TransformNode(node_id="Driven")
    graph.add_node(node)
    assert node.set_expression("rotation", "frame * 2")
    assert graph.set_node_expression_enabled("Driven", "rotation", False) == (
        True, None)

    snapshot = graph.snapshot(frame_number=12)
    node_snapshot = snapshot.nodes[node.id]
    assert node_snapshot.parameters["rotation"] == pytest.approx(0.0)
    assert node_snapshot.expressions["rotation"] == "frame * 2"
    assert node_snapshot.expression_errors == {}

    restored = NodeGraph.from_dict(graph.to_dict())
    restored_node = restored.get_node("Driven")
    assert restored_node is not None
    assert restored_node.is_expression_enabled("rotation") is False
    assert restored.snapshot(frame_number=12).nodes["Driven"].parameters[
        "rotation"] == pytest.approx(0.0)


def test_parameter_change_marks_expression_dependents_dirty():
    graph = NodeGraph()
    source = TransformNode(node_id="Source")
    driven = TransformNode(node_id="Driven")
    graph.add_node(source)
    graph.add_node(driven)
    success, error = graph.set_node_expression(
        "Driven", "rotation", "Source.rotation * 2")
    assert (success, error) == (True, None)

    driven._dirty = False
    assert graph.set_node_parameter("Source", "rotation", 3.0)
    assert driven._dirty is True


def test_expression_limits_are_reported_before_evaluation():
    parser = ExpressionParser()
    result = parser.validate("1 + " * 2000 + "1")
    assert result.success is False
    assert result.error is not None
    assert "too" in result.error.message.lower()
