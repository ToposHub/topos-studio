"""Contract tests for the shared AI/script Composite edit protocol."""

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode
from src.features.composite.edit_service import CompositeEditPlan, CompositeEditService


def make_graph():
    graph = NodeGraph()
    transform = TransformNode(node_id="transform-1")
    output = OutputNode(node_id="output-1")
    graph.add_node(transform)
    graph.add_node(output)
    graph.connect(transform.id, "result", output.id, "image")
    return graph, transform


def test_snapshot_exposes_animatable_params_without_file_paths():
    graph, transform = make_graph()
    snapshot = CompositeEditService(graph).snapshot()
    node = next(item for item in snapshot["nodes"] if item["id"] == transform.id)
    scale = next(item for item in node["param_defs"] if item["name"] == "scale_x")

    assert scale["type"] == "float"
    assert scale["animated"] is False
    assert "file_path" not in node["parameters"]


def test_same_plan_is_shared_by_validation_and_apply():
    graph, transform = make_graph()
    plan = CompositeEditPlan.from_dict({
        "plan_id": "scale-ease",
        "base_generation": graph.generation,
        "operations": [{
            "op": "set_keyframes", "node_id": transform.id, "param_name": "scale_x",
            "keyframes": [
                {"frame": 0, "value": 1.0, "interpolation": "bezier",
                 "handle_right": [12, 0.0]},
                {"frame": 24, "value": 1.8, "interpolation": "bezier",
                 "handle_left": [-12, 0.0]},
            ],
            "pre_extrap": "constant", "post_extrap": "linear",
        }],
    })
    service = CompositeEditService(graph)
    assert service.validate(plan).ok
    result = service.apply(plan)

    assert result.applied
    track = transform.keyframe_tracks["scale_x"]
    assert [kf["frame"] for kf in track["keyframes"]] == [0, 24]
    assert track["keyframes"][0]["interpolation"] == "bezier"
    assert track["post_extrap"] == "linear"


def test_generation_conflict_does_not_mutate_graph():
    graph, transform = make_graph()
    service = CompositeEditService(graph)
    graph.set_node_parameter(transform.id, "scale_x", 1.2)
    plan = {
        "plan_id": "stale",
        "base_generation": 0,
        "operations": [{
            "op": "add_keyframe", "node_id": transform.id, "param_name": "scale_x",
            "frame": 0, "value": 1.0,
        }],
    }

    result = service.apply(plan)
    assert not result.applied
    assert "scale_x" not in transform.keyframe_tracks


def test_script_and_ai_operation_shapes_are_identical():
    graph, transform = make_graph()
    service = CompositeEditService(graph)
    operation = {
        "op": "add_keyframe", "node_id": transform.id, "param_name": "scale_x",
        "frame": 12, "value": 1.4, "interpolation": "ease_in_out",
        "tangent_mode": "smooth", "handle_right": [6, 0.2],
    }
    assert service.validate({"plan_id": "shape", "operations": [operation]}).ok
    assert service.apply({"plan_id": "shape", "operations": [operation]}).applied
    assert transform.keyframe_tracks["scale_x"]["keyframes"][0]["tangent_mode"] == "smooth"


def test_color_keyframes_are_stored_as_one_rgba_track():
    graph = NodeGraph()
    constant = ConstantNode(node_id="constant-1")
    graph.add_node(constant)
    service = CompositeEditService(graph)
    plan = {
        "plan_id": "atomic-color",
        "operations": [{
            "op": "set_keyframes", "node_id": constant.id,
            "param_name": "color",
            "keyframes": [
                {"frame": 0, "value": [0.0, 0.2, 0.4, 1.0]},
                {"frame": 10, "value": [1.0, 0.8, 0.6, 0.5]},
            ],
        }],
    }

    assert service.apply(plan).applied
    assert set(constant.keyframe_tracks) == {"color"}
    assert constant.keyframe_tracks["color"]["value_type"] == "color"
    assert constant.keyframe_tracks["color"]["keyframes"][1]["value"] == [
        1.0, 0.8, 0.6, 0.5]


def test_roto_point_channels_use_shared_edit_and_snapshot_contract():
    graph = NodeGraph()
    roto = RotoShapeNode(node_id="roto-1")
    graph.add_node(roto)
    point_x = roto._point_param_names(0)["x"]
    service = CompositeEditService(graph)

    result = service.apply({
        "plan_id": "animate-roto-point",
        "operations": [{
            "op": "set_keyframes", "node_id": roto.id,
            "param_name": point_x,
            "keyframes": [
                {"frame": 0, "value": 0.25},
                {"frame": 10, "value": 0.75},
            ],
        }],
    })

    assert result.applied
    assert point_x in roto.get_param_defs()
    assert roto.keyframe_tracks[point_x]["value_type"] == "float"
    snapshot = graph.snapshot(frame_number=5)
    node_snapshot = snapshot.nodes[roto.id]
    assert node_snapshot.parameters[point_x] == 0.5
    assert node_snapshot.param_defs_meta[point_x]["ptype"] == "float"


def test_roto_point_animation_survives_graph_round_trip():
    graph = NodeGraph()
    roto = RotoShapeNode(node_id="roto-1")
    graph.add_node(roto)
    point_y = roto._point_param_names(0)["y"]
    service = CompositeEditService(graph)
    assert service.apply({
        "plan_id": "roundtrip-roto-point",
        "operations": [{
            "op": "add_keyframe", "node_id": roto.id,
            "param_name": point_y, "frame": 12, "value": 0.6,
        }],
    }).applied

    restored = NodeGraph.from_dict(graph.to_dict())
    restored_roto = restored.get_node(roto.id)
    assert restored_roto is not None
    assert point_y in restored_roto.get_param_defs()
    assert restored_roto.is_param_animated(point_y)
    assert restored.snapshot(frame_number=12).nodes[roto.id].parameters[
        point_y] == 0.6


def test_roto_point_feather_animation_survives_snapshot():
    graph = NodeGraph()
    roto = RotoShapeNode(node_id="roto-feather")
    graph.add_node(roto)
    point_feather = roto._point_param_names(0)["feather"]
    service = CompositeEditService(graph)

    assert service.apply({
        "plan_id": "animate-roto-feather",
        "operations": [{
            "op": "set_keyframes", "node_id": roto.id,
            "param_name": point_feather,
            "keyframes": [
                {"frame": 0, "value": 0.0},
                {"frame": 10, "value": 0.2},
            ],
        }],
    }).applied

    snapshot = graph.snapshot(frame_number=5)
    assert snapshot.nodes[roto.id].parameters[point_feather] == 0.1
    assert snapshot.nodes[roto.id].param_defs_meta[point_feather][
        "max_val"] == 0.5
