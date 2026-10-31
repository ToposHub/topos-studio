"""B1 批次（图编辑与动画语义）focused 回归测试。

对应 2026-10-14 复合页审计计划的 U-06/07/08/10/14/15/16。断言用户预期
最终状态：装饰载入可见、启停/设置可撤销、上下游闭包完整、连线替换/
多输入断线/粘贴共用原子事务，model/visual/undo 三方一致。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core


def _make_page(qapp, monkeypatch):
    from unittest.mock import MagicMock
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage(parent=None)
    for name in (
        "_evaluate_graph", "_evaluate_graph_now", "update_viewer_handles",
        "_refresh_asset_dependency_panel", "_update_minimap",
        "_refresh_keyframes_panel", "_refresh_keyframes_and_spline_for_node",
        "_sync_layer_stack_after_node_edit",
    ):
        monkeypatch.setattr(page, name, lambda *a, **k: None, raising=False)
    page._properties_panel = MagicMock()
    return page


def _reset(page, nodes):
    page.node_graph.clear()
    for n in nodes:
        page.node_graph.add_node(n)
    page.node_editor.build_from_graph(page.node_graph)
    page._undo_stack.clear()


def _model_edges(page):
    return {(c.src_node_id, c.dst_node_id) for c in page.node_graph.connections}


def _visual_edges(page):
    return {(c[1], c[3]) for c in page.node_editor._connection_items}


# ── U-06：装饰载入重建视觉 ─────────────────────────────────────────────


class TestDecorationRestore:
    def test_u06_backdrop_and_sticky_rebuilt_on_load(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode

        page = _make_page(qapp, monkeypatch)
        try:
            payload = {
                "composite_id": "A",
                "nodes": [ConstantNode(node_id="c").to_dict()],
                "connections": [],
                "backdrops": {
                    "bd1": {"id": "bd1", "title": "Notes",
                            "position": [0, 0], "size": [200, 120]}},
                "sticky_notes": {
                    "sn1": {"id": "sn1", "text": "hello",
                            "position": [10, 10], "size": [120, 80]}},
            }
            page.load_composite_data(payload)

            assert len(page.node_graph.backdrops) == 1
            assert len(page.node_graph.get_sticky_notes()) == 1
            # 视觉对象必须与模型一同恢复
            assert len(page.node_editor._backdrop_items) == 1
            assert len(page.node_editor._sticky_note_items) == 1
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u06_repeated_load_no_duplicates(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode

        page = _make_page(qapp, monkeypatch)
        try:
            payload = {
                "composite_id": "A",
                "nodes": [ConstantNode(node_id="c").to_dict()],
                "connections": [],
                "backdrops": {"bd1": {"id": "bd1", "position": [0, 0],
                                      "size": [200, 120]}},
                "sticky_notes": {"sn1": {"id": "sn1", "position": [10, 10],
                                         "size": [120, 80]}},
            }
            page.load_composite_data(payload)
            page.load_composite_data(payload)
            assert len(page.node_editor._backdrop_items) == 1
            assert len(page.node_editor._sticky_note_items) == 1
        finally:
            page.cleanup()
            page.deleteLater()


# ── U-10：上下游闭包 ───────────────────────────────────────────────────


class TestUpDownstreamClosure:
    def test_u10_four_node_chain_full_closure(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.output_node import OutputNode
        from src.features.composite.nodes.blur_node import BlurNode
        from src.features.composite.nodes.grade_node import GradeNode

        page = _make_page(qapp, monkeypatch)
        try:
            a = ConstantNode(node_id="a")
            b = GradeNode(node_id="b")
            c = BlurNode(node_id="c")
            d = OutputNode(node_id="d")
            _reset(page, [a, b, c, d])
            act = page.node_editor._actions
            assert act.connect("a", "result", "b", "image")
            assert act.connect("b", "result", "c", "image")
            assert act.connect("c", "result", "d", "image")

            upstream = page.node_editor._get_upstream_nodes("d")
            downstream = page.node_editor._get_downstream_nodes("a")

            assert upstream == {"a", "b", "c"}
            assert downstream == {"b", "c", "d"}
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u10_fork_and_join_closure(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.blend_node import MergeNode

        page = _make_page(qapp, monkeypatch)
        try:
            a = ConstantNode(node_id="a")
            b = ConstantNode(node_id="b")
            m = MergeNode(node_id="m")
            _reset(page, [a, b, m])
            act = page.node_editor._actions
            assert act.connect("a", "result", "m", "A")
            assert act.connect("b", "result", "m", "B")

            assert page.node_editor._get_upstream_nodes("m") == {"a", "b"}
            assert page.node_editor._get_downstream_nodes("a") == {"m"}
        finally:
            page.cleanup()
            page.deleteLater()


# ── U-07：启停/旁路撤销 ────────────────────────────────────────────────


class TestBypassUndo:
    def test_u07_single_toggle_undo(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode

        page = _make_page(qapp, monkeypatch)
        try:
            _reset(page, [ConstantNode(node_id="t")])
            page.node_editor._actions.toggle_bypass("t")
            assert page.node_graph.get_node("t").disabled is True
            assert page._undo_stack.can_undo()

            page.undo()

            assert page.node_graph.get_node("t").disabled is False
            item = page.node_editor._node_items["t"]
            assert item._disabled is False
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u07_batch_toggle_is_one_undo_entry(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode

        page = _make_page(qapp, monkeypatch)
        try:
            _reset(page, [ConstantNode(node_id="n1"),
                          ConstantNode(node_id="n2")])
            for it in page.node_editor._node_items.values():
                it.set_selected(True)

            page.node_editor._actions.toggle_bypass_selected()

            assert page.node_graph.get_node("n1").disabled is True
            assert page.node_graph.get_node("n2").disabled is True

            page.undo()

            assert page.node_graph.get_node("n1").disabled is False
            assert page.node_graph.get_node("n2").disabled is False
            # 一步撤销后回到根：整组是一条历史
            assert not page._undo_stack.can_undo()
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u07_properties_toggle_undo(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode

        page = _make_page(qapp, monkeypatch)
        try:
            _reset(page, [ConstantNode(node_id="t")])

            page._on_props_node_enabled_toggled("t", False)
            assert page.node_graph.get_node("t").disabled is True

            page.undo()

            assert page.node_graph.get_node("t").disabled is False
        finally:
            page.cleanup()
            page.deleteLater()


# ── U-08：Project Settings 撤销 ────────────────────────────────────────


class TestProjectSettingsUndo:
    def test_u08_settings_undo_and_redo(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.output_node import OutputNode

        page = _make_page(qapp, monkeypatch)
        try:
            const = ConstantNode(node_id="bg")
            const.set_parameter("is_project_background", True)
            out = OutputNode(node_id="o")
            _reset(page, [const, out])
            assert page._composite_fps == 24.0

            page._apply_project_settings({
                "width": 640, "height": 360, "fps": 30.0,
                "duration_seconds": 5.0,
                "background_color": (0.1, 0.2, 0.3, 1.0),
            })

            assert page._composite_fps == 30.0
            assert page._composite_duration_frames == 150
            assert page.node_graph.get_node("o").parameters["width"] == 640
            assert page._undo_stack.can_undo()

            page.undo()

            assert page._composite_fps == 24.0
            assert page._composite_duration_frames == 0
            assert page.node_graph.get_node("o").parameters["width"] == 1920
            assert page.node_graph.get_node("o").parameters["height"] == 1080

            page.redo()

            assert page._composite_fps == 30.0
            assert page.node_graph.get_node("o").parameters["width"] == 640
        finally:
            page.cleanup()
            page.deleteLater()


# ── U-14 / U-15：连线事务 ──────────────────────────────────────────────


class TestConnectionTransactions:
    def test_u14_replace_undo_restores_old_edge(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.output_node import OutputNode

        page = _make_page(qapp, monkeypatch)
        try:
            a = ConstantNode(node_id="a")
            b = ConstantNode(node_id="b")
            o = OutputNode(node_id="o")
            _reset(page, [a, b, o])
            act = page.node_editor._actions
            assert act.connect("a", "result", "o", "image")
            page._undo_stack.clear()

            assert act.connect("b", "result", "o", "image")
            assert _model_edges(page) == {("b", "o")}

            page.undo()

            assert _model_edges(page) == {("a", "o")}
            assert _visual_edges(page) == {("a", "o")}

            page.redo()

            assert _model_edges(page) == {("b", "o")}
            assert _visual_edges(page) == {("b", "o")}
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u14_duplicate_connect_no_history(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.output_node import OutputNode

        page = _make_page(qapp, monkeypatch)
        try:
            a = ConstantNode(node_id="a")
            o = OutputNode(node_id="o")
            _reset(page, [a, o])
            act = page.node_editor._actions
            assert act.connect("a", "result", "o", "image")
            page._undo_stack.clear()

            # actions.connect 的重复守卫直接拒绝，不进页面
            assert act.connect("a", "result", "o", "image") is False

            assert not page._undo_stack.can_undo()
            assert _model_edges(page) == {("a", "o")}
            assert _visual_edges(page) == {("a", "o")}
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u15_multi_input_delete_keeps_other_edges(self, qapp, monkeypatch):
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        page = _make_page(qapp, monkeypatch)
        try:
            g1 = Geo3DNode(node_id="g1")
            g2 = Geo3DNode(node_id="g2")
            s = Scene3DNode(node_id="s")
            _reset(page, [g1, g2, s])
            act = page.node_editor._actions
            assert act.connect("g1", "display", "s", "in")
            assert act.connect("g2", "display", "s", "in")
            page._undo_stack.clear()

            act.disconnect("g2", "display", "s", "in")

            # 删第二条：第一条 model/visual 都必须在
            assert _model_edges(page) == {("g1", "s")}
            assert _visual_edges(page) == {("g1", "s")}

            page.undo()

            assert _model_edges(page) == {("g1", "s"), ("g2", "s")}
            assert _visual_edges(page) == {("g1", "s"), ("g2", "s")}

            page.redo()

            assert _model_edges(page) == {("g1", "s")}
            assert _visual_edges(page) == {("g1", "s")}
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u15_reverse_drag_disconnect_exact(self, qapp, monkeypatch):
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        page = _make_page(qapp, monkeypatch)
        try:
            g1 = Geo3DNode(node_id="g1")
            g2 = Geo3DNode(node_id="g2")
            s = Scene3DNode(node_id="s")
            _reset(page, [g1, g2, s])
            act = page.node_editor._actions
            assert act.connect("g1", "display", "s", "in")
            assert act.connect("g2", "display", "s", "in")
            page._undo_stack.clear()

            # 反拖入口：只断第一条（当前遍历序），另一条完好
            src_info = act.disconnect_by_dst("s", "in")

            assert src_info == ("g1", "display")
            assert _model_edges(page) == {("g2", "s")}
            assert _visual_edges(page) == {("g2", "s")}

            page.undo()

            assert _model_edges(page) == {("g1", "s"), ("g2", "s")}
            assert _visual_edges(page) == {("g1", "s"), ("g2", "s")}
        finally:
            page.cleanup()
            page.deleteLater()


# ── U-16：粘贴单事务 ───────────────────────────────────────────────────


class TestPasteSingleTransaction:
    def test_u16_paste_with_edges_is_one_history_entry(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.output_node import OutputNode

        page = _make_page(qapp, monkeypatch)
        try:
            a = ConstantNode(node_id="a")
            o = OutputNode(node_id="o")
            _reset(page, [a, o])
            act = page.node_editor._actions
            assert act.connect("a", "result", "o", "image")
            page._undo_stack.clear()

            for it in page.node_editor._node_items.values():
                it.set_selected(True)
            act.copy_selected_nodes()
            new_ids = act.paste_nodes()

            assert len(new_ids) == 2
            assert len(page.node_graph.nodes) == 4
            # 历史顶是一条 Paste nodes 组记录
            assert "Paste nodes" in page._undo_stack.undo_description()

            page.undo()

            # 一步撤销撤掉全部新节点与内部连线，旧图保持
            assert len(page.node_graph.nodes) == 2
            assert _model_edges(page) == {("a", "o")}
            assert _visual_edges(page) == {("a", "o")}
            # 下一条历史为空：没有游离的 Connect 记录
            assert page._undo_stack.undo_description() == ""
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u16_undo_then_redo_restores_paste(self, qapp, monkeypatch):
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.output_node import OutputNode

        page = _make_page(qapp, monkeypatch)
        try:
            a = ConstantNode(node_id="a")
            o = OutputNode(node_id="o")
            _reset(page, [a, o])
            act = page.node_editor._actions
            assert act.connect("a", "result", "o", "image")

            for it in page.node_editor._node_items.values():
                it.set_selected(True)
            act.copy_selected_nodes()
            act.paste_nodes()
            edges_before_undo = _model_edges(page)

            page.undo()
            page.redo()

            assert _model_edges(page) == edges_before_undo
            assert len(page.node_graph.nodes) == 4
        finally:
            page.cleanup()
            page.deleteLater()
