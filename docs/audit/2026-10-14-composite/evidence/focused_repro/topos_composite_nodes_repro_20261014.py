"""Read-only targeted Composite audit reproductions. Run with PYTHONPATH=."""
import json
import math
from pathlib import Path

from src.features.composite.core.node_engine import (
    NodeGraph, NODE_REGISTRY, register_plugin_nodes, create_node_by_type,
)
from src.features.composite.core.node_plugin import (
    NodePlugin, NodePluginManager, PluginInfo,
)
from src.features.composite.core.time_remap import TimeRemap
from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
from src.features.composite.nodes.cryptomatte_select_node import CryptomatteSelectNode
from src.features.composite.nodes.noop_node import NoOpNode
from src.features.composite.render3d.cryptomatte import cryptomatte_float_id
from src.features.composite.utils.expression_parser import ExpressionParser, ExpressionContext
from src.shared.keyframe import Keyframe, KeyframeTrack


def track(parameter, end_frame=24, end_value=24.0):
    result = KeyframeTrack(parameter, min_val=-10000.0, max_val=10000.0)
    result.add_keyframe(Keyframe(frame=0, value=0.0))
    result.add_keyframe(Keyframe(frame=end_frame, value=end_value))
    return result


def new_graph(kind="transform", node_id="t"):
    graph = NodeGraph()
    node = NODE_REGISTRY[kind](node_id)
    graph.add_node(node)
    return graph, node


def wire(graph, source, target, target_port=None):
    ok = graph.connect(source.id, source.outputs[0].name, target.id,
                       target_port or target.inputs[0].name)
    assert ok


def run():
    result = {}
    ordered = []
    for order in [("translate_x", "translate_y"), ("translate_y", "translate_x")]:
        graph, node = new_graph()
        expressions = {"translate_x": "this.translate_y * 2", "translate_y": "10"}
        for parameter in order:
            graph.set_node_expression(node.id, parameter, expressions[parameter])
        snapshot = graph.snapshot()
        ordered.append({"set_order": order,
                        "x": snapshot.nodes[node.id].parameters["translate_x"],
                        "y": snapshot.nodes[node.id].parameters["translate_y"],
                        "errors": snapshot.nodes[node.id].expression_errors})
    result["local_expression_order"] = ordered

    graph, node = new_graph()
    node.set_keyframe_track("translate_x", track("translate_x"))
    result["rejected_expression_loses_keys"] = {
        "api_result": graph.set_node_expression(node.id, "translate_x", "t.translate_x+1"),
        "remaining_tracks": node.keyframe_tracks,
        "remaining_expressions": node._expressions,
    }

    graph, node = new_graph()
    node.set_keyframe_track("translate_x", track("translate_x"))
    graph.set_node_expression(node.id, "translate_x", 'loopOut("cycle")')
    result["loop_out_loses_keys"] = {
        "remaining_tracks": node.keyframe_tracks,
        "values": {str(f): graph.snapshot(frame_number=f).nodes[node.id].parameters["translate_x"]
                   for f in [0, 12, 24, 36]},
    }

    graph, source = new_graph("blur", "a")
    target = NODE_REGISTRY["blur"]("b")
    graph.add_node(target)
    graph.set_parameter_binding("b", "radius", "a", "radius")
    result["alias_cycle_accepted"] = {
        "api_result": graph.set_node_expression("a", "radius", "n_b.radius"),
        "snapshot_errors": graph.get_expression_errors(),
    }

    graph, parent = new_graph("transform", "parent")
    child = NODE_REGISTRY["transform"]("child")
    graph.add_node(child)
    parent.set_parameter("translate_x", 100)
    child.set_keyframe_track("translate_x", track("translate_x", 10, 10))
    graph.set_node_parent("child", "parent")
    snapshot = graph.snapshot(frame_number=5)
    evaluator = BaseEvaluatorMixin()
    evaluator._snapshot = snapshot
    result["animated_parent_lost_in_renderer"] = {
        "snapshot_x": snapshot.nodes["child"].parameters["translate_x"],
        "render_x": evaluator._create_ephemeral_node(snapshot.nodes["child"]).parameters["translate_x"],
    }

    graph, source = new_graph("constant", "src")
    transform = NODE_REGISTRY["transform"]("animated")
    hold = NODE_REGISTRY["frame_hold"]("hold")
    output = NODE_REGISTRY["output"]("out")
    for node in [transform, hold, output]:
        graph.add_node(node)
    transform.set_keyframe_track("translate_x", track("translate_x"))
    hold.set_parameter("hold_frame", 5)
    wire(graph, source, transform)
    wire(graph, transform, hold)
    wire(graph, hold, output)
    snapshot = graph.snapshot(frame_number=20)
    evaluator._snapshot = snapshot
    result["hold_does_not_freeze_upstream_animation"] = {
        "override_frame": snapshot.frame_overrides["animated"].apply(20),
        "snapshot_x": snapshot.nodes["animated"].parameters["translate_x"],
        "render_x": evaluator._create_ephemeral_node(snapshot.nodes["animated"]).parameters["translate_x"],
    }

    half = TimeRemap(speed=0.5)
    double = TimeRemap(speed=2.0)
    combined = half.pushed_upstream(double)
    result["retime_rounding_drift"] = [
        {"frame": f, "sequential": double.apply(half.apply(f)), "combined": combined.apply(f)}
        for f in range(7)
    ]

    graph, source = new_graph("video_frame", "video")
    a = NODE_REGISTRY["time_offset"]("offset_a")
    b = NODE_REGISTRY["time_offset"]("offset_b")
    merge = NODE_REGISTRY["merge"]("merge")
    output = NODE_REGISTRY["output"]("out")
    for node in [a, b, merge, output]:
        graph.add_node(node)
    a.set_parameter("frame_offset", 5)
    b.set_parameter("frame_offset", 9)
    wire(graph, source, a)
    wire(graph, source, b)
    wire(graph, a, merge, "A")
    wire(graph, b, merge, "B")
    wire(graph, merge, output)
    snapshot = graph.snapshot(frame_number=10)
    result["shared_source_time_branches"] = {
        "needed_source_frames": [15, 19],
        "only_source_frame": snapshot.frame_overrides["video"].apply(10),
    }

    names = ["object_0", "object_3", "object_440"]
    ids = [cryptomatte_float_id(name) for name in names]
    node = CryptomatteSelectNode()
    node.set_parameter("id_encoding", "float_r")
    for name, value in zip(names[:2], ids[:2]):
        node.add_selection(value, name)
    result["cryptomatte_wrong_id_comparison"] = {
        "input_ids": dict(zip(names[:2], ids[:2])),
        "stored_ids": node.get_selected_ids(),
        "stored_names": node.get_selected_names(),
        "third_id_is_nan": math.isnan(ids[2]),
    }
    bad_payloads = []
    for parameter, value, getter in [
        ("selected_ids", "null", "get_selected_ids"),
        ("selected_names", "null", "get_selected_names"),
        ("manifest", "[]", "get_manifest"),
    ]:
        node = CryptomatteSelectNode()
        node.set_parameter(parameter, value)
        try:
            getattr(node, getter)()
            outcome = "no exception"
        except Exception as error:
            outcome = f"{type(error).__name__}: {error}"
        bad_payloads.append({"parameter": parameter, "payload": value, "outcome": outcome})
    result["cryptomatte_invalid_json_shapes"] = bad_payloads

    parser = ExpressionParser()
    expressions = []
    for expression in ["10 if -1 else 20", "(-1) and 1", "1 or sqrt(-1)",
                       "0 and sqrt(-1)", "input.width > 1000 ? 1.0 : 0.5"]:
        evaluation = parser.evaluate(expression, ExpressionContext(input_attrs={"width": 1920}))
        expressions.append({"expression": expression, "success": evaluation.success,
                            "value": evaluation.value,
                            "error": evaluation.error.message if evaluation.error else None})
    result["expression_truth_and_ternary"] = expressions

    class AuditNode(NoOpNode):
        node_type = "audit_temp_plugin_node"

    class AuditPlugin(NodePlugin):
        @classmethod
        def plugin_info(cls):
            return PluginInfo(name="audit_temp_plugin")

        @classmethod
        def node_types(cls):
            return [AuditNode]

    manager = NodePluginManager()
    manager.register_plugin(AuditPlugin)
    register_plugin_nodes(manager)
    manager.unregister_plugin("audit_temp_plugin")
    result["unloaded_plugin_stays_in_core_registry"] = {
        "plugin_manager_has_node": "audit_temp_plugin_node" in manager.node_types,
        "core_registry_has_node": NODE_REGISTRY.get("audit_temp_plugin_node") is AuditNode,
        "core_factory_still_creates_node": create_node_by_type("audit_temp_plugin_node") is not None,
    }
    return result


if __name__ == "__main__":
    findings = run()
    encoded = json.dumps(findings, ensure_ascii=False, indent=2, allow_nan=False)
    print(encoded)
    Path("/tmp/topos_composite_nodes_repro_20261014.json").write_text(encoded, encoding="utf-8")
