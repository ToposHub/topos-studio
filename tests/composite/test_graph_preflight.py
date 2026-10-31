"""GraphPreflight tests — plan P0-03 (+ P0-06 placeholder governance).

The service is pure (no Qt/GL), so all checks are tested over serialized
graph data (``NodeGraph.to_dict()`` form) plus one live-graph round-trip.
"""

import sys
import os
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.graph_preflight import (
    MODE_EXPORT,
    MODE_PREVIEW,
    PreflightIssue,
    PreflightReport,
    run_preflight,
    run_preflight_data,
)
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_stack_controller import (
    LayerStackController,
)
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.compare_node import CompareNode
from src.features.composite.nodes.blur_node import BlurNode

pytestmark = pytest.mark.composite_core


def _codes(report, severity=None):
    return {i.code for i in report.issues
            if severity is None or i.severity == severity}


def _gd(nodes, connections):
    return {"nodes": nodes, "connections": connections}


def _node(nid, ntype, **params):
    return {"id": nid, "type": ntype, "parameters": params, "disabled": False}


def _conn(src, dst, src_port_name="result", dst_port_name="image"):
    return {"src_node_id": src, "src_port_name": src_port_name,
            "dst_node_id": dst, "dst_port_name": dst_port_name}


class TestPreflightTopology(unittest.TestCase):

    def test_empty_graph_is_error(self):
        report = run_preflight_data(None)
        self.assertFalse(report.ok)
        self.assertIn("graph.empty", _codes(report))

    def test_cycle_is_error(self):
        data = _gd(
            [_node("a", "blur"), _node("b", "blur"), _node("out", "output")],
            [_conn("a", "b"), _conn("b", "a"), _conn("a", "out")])
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("graph.cycle", _codes(report))

    def test_output_without_input_is_error(self):
        data = _gd([_node("out", "output")], [])
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("output.no_input", _codes(report, "error"))

    def test_file_output_is_a_valid_terminal(self):
        data = _gd(
            [_node("c", "constant"),
             _node("write", "file_output", file_path="/tmp/composite.png")],
            [_conn("c", "write")])
        report = run_preflight_data(data, mode=MODE_EXPORT, check_fs=False)
        self.assertTrue(report.ok)
        self.assertNotIn("output.missing", _codes(report))
        self.assertNotIn("node.placeholder", _codes(report))

    def test_file_output_without_path_blocks_export(self):
        data = _gd(
            [_node("c", "constant"), _node("write", "file_output")],
            [_conn("c", "write")])
        report = run_preflight_data(data, mode=MODE_EXPORT, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("output.missing_path", _codes(report, "error"))

    def test_missing_output_is_warning(self):
        data = _gd([_node("blur1", "blur")], [])
        report = run_preflight_data(data, check_fs=False)
        self.assertTrue(report.ok)  # warning only, not blocking
        self.assertIn("output.missing", _codes(report, "warning"))

    def test_multiple_outputs_is_info(self):
        data = _gd(
            [_node("c", "constant"), _node("o1", "output"),
             _node("o2", "output")],
            [_conn("c", "o1"), _conn("c", "o2")])
        report = run_preflight_data(data, check_fs=False)
        self.assertTrue(report.ok)
        self.assertIn("output.multiple", _codes(report, "info"))

    def test_named_output_dependency_report_and_duplicate_name_diagnostic(self):
        data = _gd(
            [_node("c", "constant"), _node("o1", "output"),
             _node("o2", "output")],
            [_conn("c", "o1"), _conn("c", "o2")])
        data["nodes"][1]["parameters"] = {"output_name": "beauty"}
        data["nodes"][2]["parameters"] = {"output_name": "beauty"}

        report = run_preflight_data(data, check_fs=False)

        assert report.output_dependencies[0]["dependency_node_ids"] == ["c"]
        assert report.output_dependencies[1]["dependency_node_ids"] == ["c"]
        self.assertIn("output.duplicate_name", _codes(report, "warning"))

    def test_dangling_connection_is_warning(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output")],
            [_conn("c", "out"), _conn("ghost", "out")])
        report = run_preflight_data(data, check_fs=False)
        self.assertIn("connection.dangling", _codes(report, "warning"))

    def test_orphan_and_unreachable_are_info(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output"),
             _node("lone", "blur"), _node("branch", "blur"),
             _node("floating", "blur")],
            [_conn("c", "out"), _conn("branch", "lone")])
        report = run_preflight_data(data, check_fs=False)
        codes = _codes(report, "info")
        # floating: no connections at all → orphan.
        # branch→lone: connected but never reaches the Output → unreachable.
        self.assertIn("node.orphan", codes)
        self.assertIn("node.unreachable", codes)
        for issue in report.by_severity("info"):
            if issue.code == "node.orphan":
                self.assertEqual(issue.node_id, "floating")
            if issue.code == "node.unreachable":
                self.assertIn(issue.node_id, {"branch", "lone"})

    def test_healthy_graph_is_ok(self):
        data = _gd(
            [_node("c", "constant"), _node("b", "blur"),
             _node("out", "output")],
            [_conn("c", "b"), _conn("b", "out")])
        report = run_preflight_data(data, check_fs=False)
        self.assertEqual(report.issues, [])
        self.assertTrue(report.ok)


class TestPreflightPorts(unittest.TestCase):

    def test_missing_source_port_blocks_render(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output")],
            [_conn("c", "out", src_port_name="does_not_exist")],
        )
        report = run_preflight_data(data, mode=MODE_EXPORT, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("connection.source_port_missing", _codes(report, "error"))

    def test_wrong_direction_is_locatable(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output")],
            [_conn("out", "c", src_port_name="image",
                   dst_port_name="result")],
        )
        report = run_preflight_data(data, mode=MODE_EXPORT, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn(
            "connection.source_wrong_direction", _codes(report, "error"))
        self.assertIn(
            "connection.destination_wrong_direction", _codes(report, "error"))

    def test_incompatible_port_types_block_render(self):
        data = _gd(
            [_node("c", "constant"), _node("scene", "scene3d"),
             _node("out", "output")],
            [_conn("c", "scene", dst_port_name="in"),
             _conn("scene", "out", src_port_name="display")],
        )
        report = run_preflight_data(data, mode=MODE_EXPORT, check_fs=False)
        self.assertFalse(report.ok)
        issue = next(i for i in report.issues
                     if i.code == "connection.type_mismatch")
        self.assertEqual(issue.node_id, "scene")

    def test_dynamic_switch_ports_use_saved_input_count(self):
        data = _gd(
            [_node("c", "constant"), _node("switch", "switch", input_count=3),
             _node("out", "output")],
            [_conn("c", "switch", dst_port_name="input2"),
             _conn("switch", "out")],
        )
        report = run_preflight_data(data, check_fs=False)
        self.assertNotIn("connection.destination_port_missing", _codes(report))


class TestPreflightExpressions(unittest.TestCase):

    def _expression_graph(self, expressions, nodes=None):
        graph_nodes = nodes or [
            _node("source", "constant", value=1.0),
            _node("control", "blur", radius=2.0),
            _node("out", "output"),
        ]
        for node in graph_nodes:
            if node["id"] in expressions:
                node["expressions"] = expressions[node["id"]]
        return _gd(graph_nodes, [_conn("source", "out")])

    def test_invalid_syntax_is_blocking_and_locatable(self):
        report = run_preflight_data(
            self._expression_graph({"control": {"radius": "1 + ("}}),
            check_fs=False)

        self.assertFalse(report.ok)
        issue = next(i for i in report.issues
                     if i.code == "expression.syntax_error")
        self.assertEqual(issue.node_id, "control")

    def test_missing_node_and_parameter_are_blocking(self):
        report = run_preflight_data(
            self._expression_graph({
                "control": {"radius": "Missing.radius + source.nope"},
            }), check_fs=False)

        self.assertFalse(report.ok)
        codes = _codes(report, "error")
        self.assertIn("expression.node_not_found", codes)
        self.assertIn("expression.param_not_found", codes)

    def test_self_and_indirect_cycles_are_blocking(self):
        nodes = [
            _node("a", "blur", radius=1.0),
            _node("b", "blur", radius=1.0),
            _node("out", "output"),
        ]
        nodes[0]["expressions"] = {"radius": "b.radius + 1"}
        nodes[1]["expressions"] = {"radius": "a.radius + 1"}
        nodes[0]["expressions"]["size"] = "this.size + 1"
        report = run_preflight_data(
            _gd(nodes, [_conn("a", "out")]), check_fs=False)

        self.assertFalse(report.ok)
        cycle_issues = [i for i in report.issues
                        if i.code == "expression.circular_dependency"]
        self.assertEqual({i.node_id for i in cycle_issues}, {"a", "b"})

    def test_valid_alias_reference_is_accepted(self):
        nodes = [
            _node("abc123", "blur", radius=1.0),
            _node("control", "blur", radius=1.0),
            _node("out", "output"),
        ]
        nodes[1]["expressions"] = {"radius": "n_abc123.radius * 2"}
        report = run_preflight_data(
            _gd(nodes, [_conn("abc123", "out")]), check_fs=False)

        self.assertFalse(any(i.code.startswith("expression.")
                             for i in report.issues))


class TestPreflightPlaceholders(unittest.TestCase):

    def _tree_with_placeholder(self):
        return _gd(
            [_node("c", "constant"), _node("cmp", "ai_generate"),
             _node("out", "output")],
            [_conn("cmp", "out", src_port_name="image")])

    def test_placeholder_in_tree_warns_in_preview(self):
        report = run_preflight_data(self._tree_with_placeholder(),
                                    mode=MODE_PREVIEW, check_fs=False)
        self.assertTrue(report.ok)  # preview: warning only
        self.assertIn("node.placeholder", _codes(report, "warning"))

    def test_placeholder_in_tree_blocks_export(self):
        report = run_preflight_data(self._tree_with_placeholder(),
                                    mode=MODE_EXPORT, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("node.placeholder", _codes(report, "error"))

    def test_placeholder_outside_tree_is_info(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output"),
             _node("cmp", "ai_generate")],
            [_conn("c", "out")])
        report = run_preflight_data(data, mode=MODE_EXPORT, check_fs=False)
        self.assertTrue(report.ok)
        self.assertIn("node.placeholder", _codes(report, "info"))

    def test_unknown_type_is_warning(self):
        data = _gd(
            [_node("c", "constant"), _node("x", "totally_fake_node"),
             _node("out", "output")],
            [_conn("c", "x"), _conn("x", "out")])
        report = run_preflight_data(data, check_fs=False)
        self.assertIn("node.unknown_type", _codes(report, "warning"))

    def test_matte_cleanup_is_renderable(self):
        from src.features.composite.core.node_capabilities import (
            is_placeholder_node_type,
        )
        self.assertFalse(is_placeholder_node_type("matte_cleanup"))


class TestPreflightGroups(unittest.TestCase):

    def _valid_group_graph(self):
        return _gd(
            [_node("src", "constant"), _node("member", "blur"),
             {"id": "group", "type": "group", "parameters": {},
              "member_node_ids": ["member"],
              "input_mappings": {"source": ["member", "image"]},
              "output_mappings": {"result": ["member", "result"]}},
             _node("out", "output")],
            [{"src_node_id": "src", "src_port_name": "result",
              "dst_node_id": "group", "dst_port_name": "source"},
             {"src_node_id": "group", "src_port_name": "result",
              "dst_node_id": "out", "dst_port_name": "image"}])

    def test_valid_group_boundary_reaches_output(self):
        report = run_preflight_data(self._valid_group_graph(), check_fs=False)
        self.assertNotIn("group.invalid_mapping", _codes(report))
        self.assertNotIn("group.missing_member", _codes(report))
        self.assertNotIn("node.unreachable", {
            issue.code for issue in report.issues
            if issue.node_id == "member"})

    def test_missing_group_member_blocks_render(self):
        data = self._valid_group_graph()
        data["nodes"][1]["id"] = "different-member"
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("group.missing_member", _codes(report, "error"))

    def test_mapping_outside_group_blocks_render(self):
        data = self._valid_group_graph()
        data["nodes"][2]["input_mappings"]["source"] = ["src", "result"]
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("group.mapping_outside_members", _codes(report, "error"))

    def test_nested_group_membership_cycle_blocks_render(self):
        data = _gd(
            [{"id": "g1", "type": "group", "parameters": {},
              "member_node_ids": ["g2"], "input_mappings": {},
              "output_mappings": {}},
             {"id": "g2", "type": "group", "parameters": {},
              "member_node_ids": ["g1"], "input_mappings": {},
              "output_mappings": {}}], [])
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("group.membership_cycle", _codes(report, "error"))


class TestPreflightPrecomps(unittest.TestCase):

    def _valid_nested_graph(self):
        return {
            "nodes": [
                _node("src", "constant"),
                _node("precomp", "precomp", child_stack_id="child"),
                _node("out", "output"),
            ],
            "connections": [
                {"src_node_id": "src", "src_port_name": "result",
                 "dst_node_id": "precomp", "dst_port_name": "image"},
                {"src_node_id": "precomp", "src_port_name": "result",
                 "dst_node_id": "out", "dst_port_name": "image"},
            ],
            "layer_stacks": {
                "main": {"stack_id": "main", "output_node_id": "out",
                         "layer_ids": ["precomp-layer"]},
                "child": {"stack_id": "child", "output_node_id": "precomp",
                          "layer_ids": ["child-layer"]},
            },
            "layers": {
                "precomp-layer": {"layer_id": "precomp-layer",
                                   "root_node_id": "precomp",
                                   "layer_type": "precomp"},
                "child-layer": {"layer_id": "child-layer",
                                 "root_node_id": "src",
                                 "source_node_id": "src"},
            },
        }

    def test_valid_nested_stack_has_no_precomp_error(self):
        report = run_preflight_data(self._valid_nested_graph(), check_fs=False)
        self.assertFalse({"precomp.missing_child_stack",
                          "precomp.output_mismatch",
                          "precomp.stack_cycle"} & _codes(report))

    def test_missing_child_stack_blocks_render(self):
        data = self._valid_nested_graph()
        data["nodes"][1]["parameters"]["child_stack_id"] = "ghost"
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("precomp.missing_child_stack", _codes(report, "error"))

    def test_nested_stack_cycle_blocks_render(self):
        data = self._valid_nested_graph()
        data["nodes"].append(_node("nested", "precomp", child_stack_id="main"))
        data["layer_stacks"]["child"]["layer_ids"].append("nested-layer")
        data["layers"]["nested-layer"] = {
            "layer_id": "nested-layer", "root_node_id": "nested",
            "layer_type": "precomp",
        }
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("precomp.stack_cycle", _codes(report, "error"))


class TestPreflightLayerChain(unittest.TestCase):

    def test_external_merge_input_is_reported(self):
        data = {
            "nodes": [_node("root", "constant"), _node("merge", "merge"),
                      _node("intruder", "constant"), _node("out", "output")],
            "connections": [
                {"src_node_id": "root", "src_port_name": "result",
                 "dst_node_id": "merge", "dst_port_name": "A"},
                {"src_node_id": "intruder", "src_port_name": "result",
                 "dst_node_id": "merge", "dst_port_name": "B"},
                {"src_node_id": "merge", "src_port_name": "result",
                 "dst_node_id": "out", "dst_port_name": "image"},
            ],
            "layer_stacks": {
                "main": {"stack_id": "main", "managed": True,
                         "output_node_id": "out", "layer_ids": ["layer"],
                         "generated_merge_node_ids": ["merge"]},
            },
            "layers": {
                "layer": {"layer_id": "layer", "root_node_id": "root"},
            },
        }
        report = run_preflight_data(data, check_fs=False)
        self.assertIn("layer.managed_chain_conflict",
                      _codes(report, "warning"))


class TestPreflightTrackMattes(unittest.TestCase):

    def _data(self, layers):
        return {
            "nodes": [_node("a", "constant"), _node("b", "constant"),
                      _node("out", "output")],
            "connections": [_conn("a", "out")],
            "layers": layers,
            "layer_stacks": {
                "main": {"stack_id": "main", "layer_ids": ["a", "b"]},
            },
        }

    def test_self_matte_blocks_render(self):
        report = run_preflight_data(self._data({
            "a": {"layer_id": "a", "root_node_id": "a",
                  "track_matte_layer_id": "a"},
        }), check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("layer.track_matte_self", _codes(report, "error"))

    def test_matte_cycle_blocks_render(self):
        report = run_preflight_data(self._data({
            "a": {"layer_id": "a", "root_node_id": "a",
                  "track_matte_layer_id": "b"},
            "b": {"layer_id": "b", "root_node_id": "b",
                  "track_matte_layer_id": "a"},
        }), check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("layer.track_matte_cycle", _codes(report, "error"))

    def test_external_matte_is_warning(self):
        data = self._data({
            "a": {"layer_id": "a", "root_node_id": "a",
                  "track_matte_layer_id": "b"},
            "b": {"layer_id": "b", "root_node_id": "b"},
        })
        data["layer_stacks"]["other"] = {
            "stack_id": "other", "layer_ids": ["b"]}
        report = run_preflight_data(data, check_fs=False)
        self.assertIn("layer.track_matte_external", _codes(report, "warning"))


class TestPreflightSources(unittest.TestCase):

    def test_missing_path_is_error(self):
        data = _gd(
            [_node("v", "video_frame", file_path=""), _node("out", "output")],
            [_conn("v", "out", src_port_name="image")])
        report = run_preflight_data(data, check_fs=False)
        self.assertFalse(report.ok)
        self.assertIn("source.missing_path", _codes(report, "error"))

    def test_missing_file_on_disk_is_warning(self):
        data = _gd(
            [_node("v", "video_frame", file_path="/no/such/file.mov"),
             _node("out", "output")],
            [_conn("v", "out", src_port_name="image")])
        report = run_preflight_data(data, check_fs=True)
        self.assertTrue(report.ok)
        self.assertIn("source.file_not_found", _codes(report, "warning"))

    def test_existing_file_no_issue(self):
        data = _gd(
            [_node("v", "video_frame", file_path=__file__),
             _node("out", "output")],
            [_conn("v", "out", src_port_name="image")])
        report = run_preflight_data(data, check_fs=True)
        self.assertNotIn("source.file_not_found", _codes(report))

    def test_model_planned_format_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "asset.usd")
            with open(path, "wb") as handle:
                handle.write(b"placeholder")
            data = _gd(
                [_node("m", "model3d", use_imported_model=True,
                       model_path=path)], [])
            report = run_preflight_data(data, check_fs=True)
            self.assertIn("source.format_not_implemented", _codes(report, "warning"))
            self.assertNotIn("source.file_not_found", _codes(report))

    def test_model_relink_candidate_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            moved_dir = os.path.join(temp_dir, "moved")
            os.makedirs(moved_dir)
            moved_path = os.path.join(moved_dir, "asset.usd")
            with open(moved_path, "wb") as handle:
                handle.write(b"placeholder")
            old_path = os.path.join(temp_dir, "old", "asset.usd")
            data = _gd(
                [_node("m", "model3d", use_imported_model=True,
                       model_path=old_path)], [])
            report = run_preflight_data(
                data, check_fs=True, asset_search_roots=(moved_dir,))
            self.assertIn("source.relink_candidate", _codes(report, "info"))
            self.assertIn("source.format_not_implemented", _codes(report, "warning"))
            self.assertNotIn("source.file_not_found", _codes(report))


class TestPreflightLayers(unittest.TestCase):

    def test_layer_reference_to_missing_node(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output")],
            [_conn("c", "out")],
        )
        data["layers"] = {
            "l1": {"layer_id": "l1", "name": "A",
                   "root_node_id": "ghost_root", "source_node_id": "ghost_src"},
        }
        report = run_preflight_data(data, check_fs=False)
        self.assertIn("layer.invalid_reference", _codes(report, "warning"))

    def test_layer_inverted_range(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output")],
            [_conn("c", "out")],
        )
        data["layers"] = {
            "l1": {"layer_id": "l1", "name": "A", "root_node_id": "c",
                   "in_frame": 100, "out_frame": 10},
        }
        report = run_preflight_data(data, check_fs=False)
        self.assertIn("layer.time_range_inverted", _codes(report, "warning"))

    def test_track_matte_missing_layer(self):
        data = _gd(
            [_node("c", "constant"), _node("out", "output")],
            [_conn("c", "out")],
        )
        data["layers"] = {
            "l1": {"layer_id": "l1", "name": "A", "root_node_id": "c",
                   "track_matte_layer_id": "ghost_matte"},
        }
        report = run_preflight_data(data, check_fs=False)
        self.assertIn("layer.track_matte_missing", _codes(report, "warning"))


class TestPreflightLiveGraph(unittest.TestCase):

    def test_live_graph_roundtrip(self):
        g = NodeGraph()
        g.add_node(OutputNode(node_id="out"))
        ctrl = LayerStackController(g, undo_stack=None)
        g.add_node(ConstantNode(node_id="src0"))
        layer_id = ctrl.add_media_layer("Media", "src0")
        report = run_preflight(g, check_fs=False)
        self.assertTrue(report.ok)
        # layer now references real nodes → no invalid reference
        self.assertNotIn("layer.invalid_reference", _codes(report))

    def test_summary_formats_codes(self):
        report = PreflightReport()
        report.issues.append(PreflightIssue(
            code="x.y", severity="error", message="boom", node_id="n1"))
        text = report.format_summary()
        self.assertIn("[x.y]", text)
        self.assertIn("n1", text)


if __name__ == "__main__":
    unittest.main()
