"""C4 contract tests — node-graph topology operations in the shared
Composite edit protocol (script / MCP / AI all speak the same ops).

Covers the plan's verification items:
- script builds a graph (constant -> transform -> blur -> output) in one
  plan; snapshot topology and parameter state assert the result
- illegal operations (unknown type / cycle / bad port / type mismatch /
  unknown node / unknown param) return structured errors and leave the
  graph untouched
- one plan = ONE composite undo group via the page's
  record_external_composite_edit hook
"""

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.edit_service import CompositeEditService
from src.features.scripting.composite_api import CompositeScriptAPI


def make_service():
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="output-1"))
    return graph, CompositeEditService(graph)


BUILD_PLAN = {
    "plan_id": "build",
    "operations": [
        {"op": "add_node", "node_type": "constant", "node_id": "src",
         "position": [120, 300], "parameters": {"width": 640, "height": 480}},
        {"op": "add_node", "node_type": "transform", "node_id": "xf"},
        {"op": "add_node", "node_type": "blur", "node_id": "bl"},
        {"op": "connect", "from_node": "src", "from_port": "result",
         "to_node": "xf", "to_port": "image"},
        {"op": "connect", "from_node": "xf", "from_port": "result",
         "to_node": "bl", "to_port": "image"},
        {"op": "connect", "from_node": "bl", "from_port": "result",
         "to_node": "output-1", "to_port": "image"},
        {"op": "set_param", "node_id": "xf", "param_name": "scale_x", "value": 1.5},
    ],
}


# ── Happy path: build a graph in one plan ───────────────────────────────────

class TestScriptGraphBuild:
    def test_build_graph_topology(self):
        graph, service = make_service()
        result = service.apply(BUILD_PLAN)
        assert result.applied, result.error
        assert result.generation_after > result.generation_before

        snapshot = service.snapshot()
        ids = {n["id"] for n in snapshot["nodes"]}
        assert ids == {"src", "xf", "bl", "output-1"}
        connections = {
            (c["src_node_id"], c["src_port_name"], c["dst_node_id"], c["dst_port_name"])
            for c in snapshot["connections"]
        }
        assert connections == {
            ("src", "result", "xf", "image"),
            ("xf", "result", "bl", "image"),
            ("bl", "result", "output-1", "image"),
        }

    def test_build_graph_parameters_applied(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        # set_param landed…
        assert float(graph.get_node("xf").parameters["scale_x"]) == 1.5
        # …and add_node's inline parameters landed.
        assert int(graph.get_node("src").parameters["width"]) == 640

    def test_report_notes_created_node_ids(self):
        graph, service = make_service()
        result = service.apply({
            "plan_id": "auto-id",
            "operations": [{"op": "add_node", "node_type": "text"}],
        })
        assert result.applied
        infos = [d.message for d in result.report.diagnostics if d.severity == "info"]
        assert len(infos) == 1 and infos[0].startswith("created node text_")
        assert any(n["type"] == "text" for n in service.snapshot()["nodes"])

    def test_mixed_plan_add_connect_disconnect_remove(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        result = service.apply({
            "plan_id": "rewire",
            "operations": [
                {"op": "add_node", "node_type": "noise", "node_id": "nz"},
                {"op": "connect", "from_node": "nz", "from_port": "result",
                 "to_node": "xf", "to_port": "image"},
                {"op": "disconnect", "from_node": "nz", "from_port": "result",
                 "to_node": "xf", "to_port": "image"},
                {"op": "remove_node", "node_id": "nz"},
            ],
        })
        assert result.applied, result.error
        assert graph.get_node("nz") is None

    def test_keyframe_ops_mix_with_node_ops(self):
        graph, service = make_service()
        result = service.apply({
            "plan_id": "mixed",
            "operations": BUILD_PLAN["operations"] + [
                {"op": "add_keyframe", "node_id": "xf",
                 "param_name": "scale_x", "frame": 0, "value": 1.0},
            ],
        })
        assert result.applied, result.error
        assert "scale_x" in graph.get_node("xf").keyframe_tracks


# ── Structured errors leave the graph untouched ─────────────────────────────

class TestGraphOpErrors:
    def _assert_rejected(self, service, graph, operation):
        before = graph.to_dict()
        result = service.apply({"plan_id": "bad", "operations": [operation]})
        assert not result.applied
        errors = result.report.errors
        assert errors, f"expected a structured error for {operation}"
        assert graph.to_dict() == before
        return errors[0].message

    def test_unknown_node_type(self):
        graph, service = make_service()
        message = self._assert_rejected(service, graph, {
            "op": "add_node", "node_type": "definitely_not_a_node"})
        assert "unknown node_type" in message

    def test_cycle_is_rejected(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        message = self._assert_rejected(service, graph, {
            "op": "connect", "from_node": "bl", "from_port": "result",
            "to_node": "xf", "to_port": "image"})
        assert "cycle" in message

    def test_missing_port_is_rejected(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        message = self._assert_rejected(service, graph, {
            "op": "connect", "from_node": "src", "from_port": "does_not_exist",
            "to_node": "output-1", "to_port": "image"})
        assert "output port not found" in message

    def test_port_type_mismatch_is_rejected(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        assert service.apply({
            "plan_id": "s3d",
            "operations": [{"op": "add_node", "node_type": "scene3d",
                            "node_id": "sc"}],
        }).applied
        message = self._assert_rejected(service, graph, {
            "op": "connect", "from_node": "src", "from_port": "result",
            "to_node": "sc", "to_port": "in"})
        assert "port type mismatch" in message

    def test_unknown_node_reference(self):
        graph, service = make_service()
        message = self._assert_rejected(service, graph, {
            "op": "remove_node", "node_id": "ghost"})
        assert "node not found" in message

    def test_unknown_parameter(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        message = self._assert_rejected(service, graph, {
            "op": "set_param", "node_id": "xf",
            "param_name": "not_a_param", "value": 1.0})
        assert "parameter not found" in message

    def test_out_of_range_value(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        message = self._assert_rejected(service, graph, {
            "op": "set_param", "node_id": "xf",
            "param_name": "scale_x", "value": 99999.0})
        assert "outside parameter range" in message

    def test_duplicate_node_id(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        message = self._assert_rejected(service, graph, {
            "op": "add_node", "node_type": "noise", "node_id": "src"})
        assert "already exists" in message

    def test_self_connection(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        message = self._assert_rejected(service, graph, {
            "op": "connect", "from_node": "xf", "from_port": "result",
            "to_node": "xf", "to_port": "image"})
        assert "self-connection" in message

    def test_node_removed_mid_plan_cannot_be_referenced(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        result = service.apply({
            "plan_id": "use-after-remove",
            "operations": [
                {"op": "remove_node", "node_id": "bl"},
                {"op": "set_param", "node_id": "bl",
                 "param_name": "radius", "value": 4.0},
            ],
        })
        assert not result.applied
        assert "already removed" in result.report.errors[0].message
        assert graph.get_node("bl") is not None  # whole plan rejected

    def test_disconnect_requires_existing_connection(self):
        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied
        message = self._assert_rejected(service, graph, {
            "op": "disconnect", "from_node": "src", "from_port": "result",
            "to_node": "bl", "to_port": "image"})
        assert "connection not found" in message


# ── One plan = one composite undo group (script bridge level) ───────────────

class _Bridge:
    def call_sync(self, fn):
        return fn()


class _Page:
    def __init__(self, graph):
        self.node_graph = graph
        self.edits = []

    def record_external_composite_edit(self, before, after):
        self.edits.append((before, after))


class TestSingleUndoGroup:
    def test_multi_op_plan_records_one_undo_action(self):
        graph, _service = make_service()
        page = _Page(graph)
        api = CompositeScriptAPI(_Bridge(), lambda: page)

        result = api.apply(BUILD_PLAN)
        assert result["applied"]
        # 7 operations → exactly ONE external edit record (one undo group).
        assert len(page.edits) == 1
        before, after = page.edits[0]
        assert "nodes" in before and "nodes" in after
        assert len(after["nodes"]) == 4

    def test_failed_plan_records_no_undo_action(self):
        graph, _service = make_service()
        page = _Page(graph)
        api = CompositeScriptAPI(_Bridge(), lambda: page)

        result = api.apply({
            "plan_id": "bad",
            "operations": [{"op": "add_node", "node_type": "nope"}],
        })
        assert not result["applied"]
        assert page.edits == []


# ── AI contract alignment ────────────────────────────────────────────────────

class TestAIContractAlignment:
    def test_node_ops_match_composite_plan_contract(self):
        from src.features.composite.ai.composite_plan import (
            SUPPORTED_NODE_OPS, NODE_OP_ADD_NODE, NODE_OP_CONNECT,
            NODE_OP_DISCONNECT, NODE_OP_SET_PARAM, NODE_OP_REMOVE_NODE,
        )
        from src.features.composite.edit_service import NODE_OPS
        # edit_service implements the plan contract's apply-able subset.
        assert {NODE_OP_ADD_NODE, NODE_OP_CONNECT, NODE_OP_DISCONNECT,
                NODE_OP_SET_PARAM, NODE_OP_REMOVE_NODE} <= NODE_OPS
        assert NODE_OPS <= SUPPORTED_NODE_OPS

    def test_validate_composite_plan_passes_on_built_graph(self):
        from src.features.composite.ai.composite_plan import (
            CompositePlan, NodeGraphOperation, validate_composite_plan,
            NODE_OP_CONNECT,
        )
        from src.features.composite.core.node_engine import NODE_REGISTRY

        graph, service = make_service()
        assert service.apply(BUILD_PLAN).applied

        plan = CompositePlan(
            plan_id="p1", brief_id="b1", project_id="prj",
            timeline_id="tl", base_generation=0,
            operations=(
                NodeGraphOperation(op_id="o1", kind=NODE_OP_CONNECT,
                                   node_id="src", from_port="result",
                                   to_port="image"),
            ),
        )
        report = validate_composite_plan(
            plan, available_node_types=NODE_REGISTRY.keys())
        assert report.is_deliverable
