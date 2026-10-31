"""B3 tests — group enter/exit edit view wiring (EnterGroupCommand /
ExitGroupCommand through the page-level undo stack) and parameter
promotion (proxy ParamDef on the GroupNode driving members via the
engine's cross-node parameter binding).
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.undo_stack import UndoStack, UndoAction
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.group_node import GroupNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.ui.group_commands import (
    EnterGroupCommand, ExitGroupCommand, create_group_from_selection,
)

pytestmark = pytest.mark.composite_core


def make_graph_with_group():
    graph = NodeGraph()
    graph.add_node(ConstantNode(node_id="src"))
    graph.add_node(BlurNode(node_id="blur"))
    graph.add_node(OutputNode(node_id="out"))
    graph.connect("src", "result", "blur", "image")
    graph.connect("blur", "result", "out", "image")
    gid = create_group_from_selection(graph, ["src", "blur"], title="MyGroup")
    return graph, gid


# ── Enter/Exit command round trip ───────────────────────────────────────────

class TestEnterExitCommands:
    def test_enter_exit_roundtrip_keeps_graph_payload(self):
        graph, gid = make_graph_with_group()
        before = graph.to_dict()
        enter = EnterGroupCommand(graph=graph, group_id=gid)
        assert enter.redo() is True
        assert graph.get_group(gid)["is_expanded"] is True
        exit_cmd = ExitGroupCommand(graph=graph, group_id=gid)
        assert exit_cmd.redo() is True
        assert graph.get_group(gid)["is_expanded"] is False
        # Only the expand marker may differ while inside; after the
        # roundtrip the graph payload is back to the original.
        after = graph.to_dict()
        assert before["nodes"] == after["nodes"]
        assert before["connections"] == after["connections"]

    def test_enter_unknown_group_fails(self):
        graph = NodeGraph()
        assert EnterGroupCommand(graph=graph, group_id="nope").redo() is False

    def test_enter_undo_restores_previous_state(self):
        graph, gid = make_graph_with_group()
        enter = EnterGroupCommand(graph=graph, group_id=gid)
        enter.redo()
        enter.undo()
        assert graph.get_group(gid)["is_expanded"] is False

    def test_undo_stack_roundtrip(self):
        graph, gid = make_graph_with_group()
        stack = UndoStack()
        cmd = EnterGroupCommand(graph=graph, group_id=gid)

        def apply(enter: bool):
            if enter:
                cmd.redo()
            else:
                cmd.undo()

        stack.push(UndoAction(
            "group_enter_view", {"enter": True},
            undo_fn=lambda d: apply(False),
            redo_fn=lambda d: apply(True)))
        # Apply-first convention: push() records, never executes.
        apply(True)
        assert graph.get_group(gid)["is_expanded"] is True
        stack.undo()
        assert graph.get_group(gid)["is_expanded"] is False
        stack.redo()
        assert graph.get_group(gid)["is_expanded"] is True


# ── Parameter promotion (publish) ────────────────────────────────────────────

class TestParameterPromotion:
    def test_promote_creates_proxy_and_binds_member(self):
        graph, gid = make_graph_with_group()
        group = graph.get_node("out")  # placeholder lookup guard below
        group = next(n for n in graph.nodes.values()
                     if isinstance(n, GroupNode)) if group is None else group
        # create_group_from_selection only writes _node_groups metadata —
        # build the GroupNode instance the way a full grouping flow would.
        group = GroupNode(node_id="grp1")
        group.member_node_ids = ["src", "blur"]
        graph.add_node(group)

        ok, proxy = group.promote_param("blur", "radius", graph)
        assert ok, proxy
        # Proxy appears as a numeric param def and holds the member value.
        pdef = group.get_param_defs().get(proxy)
        assert pdef is not None and pdef.ptype == "float"
        assert pdef.group == "Promoted"
        # Member is bound to the group proxy via an expression.
        binding = graph.get_parameter_binding("blur", "radius")
        assert binding is not None
        assert binding["node_id"] == "grp1"
        assert binding["param_name"] == proxy

    def test_editing_proxy_drives_member_expression(self, qapp):
        """The real evaluation path: the member's binding expression
        references the group proxy; running the graph's expression
        resolution with the proxy edited to 33 must yield 33 on the
        member (the plan's "改组参数 → 成员参数联动" assertion)."""
        from src.features.composite.utils.expression_parser import (
            ExpressionManager,
        )

        graph, _gid = make_graph_with_group()
        group = GroupNode(node_id="grp1")
        group.member_node_ids = ["src", "blur"]
        graph.add_node(group)
        ok, proxy = group.promote_param("blur", "radius", graph)
        assert ok, proxy

        graph.set_node_parameter("grp1", proxy, 33.0)
        expr = graph.get_node("blur")._expressions.get("radius")
        assert expr, "member radius must carry a binding expression"

        manager = ExpressionManager()
        manager.set_expression("blur", "radius", expr)
        # Mirror the engine: aliases read n_<node_id> copies of the params.
        raw = {
            "grp1": dict(group.parameters),
            "n_grp1": dict(group.parameters),
            "blur": dict(graph.get_node("blur").parameters),
        }
        resolved = manager.evaluate_all(raw, input_attrs={}, frame=0)
        assert float(resolved["blur"]["radius"]) == pytest.approx(33.0)

    def test_unpromote_removes_proxy_and_binding(self):
        graph, _gid = make_graph_with_group()
        group = GroupNode(node_id="grp1")
        group.member_node_ids = ["src", "blur"]
        graph.add_node(group)
        ok, proxy = group.promote_param("blur", "radius", graph)
        assert ok
        assert group.unpromote_param(proxy, graph) is True
        assert proxy not in group.promoted_params
        assert proxy not in group.parameters
        assert graph.get_parameter_binding("blur", "radius") is None
        assert group.unpromote_param(proxy, graph) is False

    def test_promote_numeric_ok_but_vector_rejected(self):
        graph, _gid = make_graph_with_group()
        group = GroupNode(node_id="grp1")
        group.member_node_ids = ["src"]
        graph.add_node(group)
        # int parameters are promotable.
        ok, proxy = group.promote_param("src", "width", graph)
        assert ok, proxy
        # color is a vector → rejected with a readable error.
        ok, err = group.promote_param("src", "color", graph)
        assert not ok and "float/int" in err

    def test_promote_rejects_non_member(self):
        graph, _gid = make_graph_with_group()
        group = GroupNode(node_id="grp1")
        graph.add_node(group)
        ok, err = group.promote_param("out", "radius", graph)
        assert not ok and "not a member" in err

    def test_promotion_serialization_roundtrip(self):
        graph, _gid = make_graph_with_group()
        group = GroupNode(node_id="grp1")
        group.member_node_ids = ["src", "blur"]
        graph.add_node(group)
        ok, proxy = group.promote_param("blur", "radius", graph)
        assert ok

        clone = GroupNode.from_dict(group.to_dict())
        assert proxy in clone.promoted_params
        assert clone.promoted_params[proxy]["member_id"] == "blur"
        assert clone.get_param_defs().get(proxy) is not None

    def test_legacy_group_dict_without_promoted_key(self):
        clone = GroupNode.from_dict({"id": "g", "type": "group",
                                     "parameters": {}})
        assert clone.promoted_params == {}


# ── Editor group view (canvas filter + breadcrumb signals) ──────────────────

class TestEditorGroupView:
    def _make_editor(self, qapp):
        from src.features.composite.ui.node_editor_widget import (
            NodeEditorWidget,
        )
        editor = NodeEditorWidget()
        graph, gid = make_graph_with_group()
        # build_from_graph attaches the graph reference and builds visuals.
        editor.build_from_graph(graph)
        return editor, graph, gid

    def test_enter_filters_non_members_and_exit_restores(self, qapp):
        editor, graph, gid = self._make_editor(qapp)
        changes = []
        editor.group_view_changed.connect(changes.append)

        assert editor.enter_group_view(gid) is True
        assert editor.is_in_group_view()
        assert changes[-1] == gid
        visible = {nid for nid, item in editor._node_items.items()
                   if item.isVisible()}
        assert visible == {"src", "blur"}

        assert editor.exit_group_view() is True
        assert not editor.is_in_group_view()
        assert changes[-1] is None
        visible = {nid for nid, item in editor._node_items.items()
                   if item.isVisible()}
        assert visible == {"src", "blur", "out"}
        assert graph.get_group(gid)["is_expanded"] is False

    def test_double_click_on_member_requests_enter(self, qapp):
        editor, graph, gid = self._make_editor(qapp)
        requested = []
        editor.group_enter_requested.connect(requested.append)
        editor._on_node_double_clicked("blur")
        assert requested == [gid]
        # Non-grouped node keeps the generic signal path.
        generic = []
        editor.node_double_clicked.connect(generic.append)
        editor._on_node_double_clicked("out")
        assert generic == ["out"] and requested == [gid]
