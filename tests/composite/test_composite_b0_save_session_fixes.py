"""B0 批次（保存与状态安全）focused 回归测试。

对应 2026-10-14 复合页审计计划的 U-01/02/03/04/05/09/11/12/13 与 N-01。
测试断言用户预期的最终状态（复现脚本转换，不重复实现被测函数）：
工程信息不丢失、跨合成撤销隔离、pending 事务顺序正确、空图/非法
payload 的载入边界、表达式提交失败零副作用。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core

import copy


# ── helpers ────────────────────────────────────────────────────────────


def _param(node, name):
    return node.parameters.get(name)


def _track(param: str, end_frame: int = 24, end_value: float = 24.0):
    from src.shared.keyframe import Keyframe, KeyframeTrack

    result = KeyframeTrack(param, min_val=-10000.0, max_val=10000.0)
    result.add_keyframe(Keyframe(frame=0, value=0.0))
    result.add_keyframe(Keyframe(frame=end_frame, value=end_value))
    return result


def _make_page(qapp, monkeypatch):
    """Create a CompositePage with evaluation/UI refresh stubbed off."""
    from src.features.composite.ui.composite_page import CompositePage

    page = CompositePage(parent=None)
    for name in (
        "_evaluate_graph", "_evaluate_graph_now", "update_viewer_handles",
        "_refresh_asset_dependency_panel", "_update_minimap",
        "_refresh_keyframes_panel", "_refresh_keyframes_and_spline_for_node",
    ):
        monkeypatch.setattr(page, name, lambda *a, **k: None, raising=False)
    return page


def _transform_graph(composite_id: str, rotation: float = 0.0) -> dict:
    from src.features.composite.nodes.transform_node import TransformNode

    node = TransformNode(node_id="shared")
    node.set_parameter("rotation", rotation)
    return {
        "composite_id": composite_id,
        "nodes": [node.to_dict()],
        "connections": [],
        "duration_frames": 48,
        "fps": 24.0,
    }


# ── U-02 / U-04 / U-05 / U-09 / U-13：会话边界 ─────────────────────────


class TestCompositeSessionBoundary:
    def test_u02_undo_history_reset_across_compositions(self, qapp, monkeypatch):
        """切合成后 Ctrl+Z 不能用旧图的历史改写新图（U-02）。"""
        page = _make_page(qapp, monkeypatch)
        try:
            page.load_composite_data(_transform_graph("A"))
            page._on_parameter_changed("shared", "rotation", 20.0)
            page._flush_param_changes()
            assert page._undo_stack.can_undo()

            graph_b = _transform_graph("B", rotation=70.0)
            page.load_composite_data(graph_b)

            # 历史已隔离：新图无可撤销动作，Ctrl+Z 是 no-op
            assert not page._undo_stack.can_undo()
            page.undo()
            node_b = page._node_graph.get_node("shared")
            assert _param(node_b, "rotation") == 70.0
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u02_pending_param_cancelled_on_switch(self, qapp, monkeypatch):
        """80ms 窗口内切图：A 的 pending 参数不得写入 B 的历史（U-02）。"""
        page = _make_page(qapp, monkeypatch)
        try:
            page.load_composite_data(_transform_graph("A"))
            page._on_parameter_changed("shared", "rotation", 5.0)
            assert page._pending_param_change is not None

            page.load_composite_data(_transform_graph("B"))
            assert page._pending_param_change is None
            assert not page._undo_stack.can_undo()
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u04_undo_flushes_pending_debounce(self, qapp, monkeypatch):
        """防抖未到期时 Ctrl+Z：先提交 pending 再撤销，不产生悬空分支（U-04）。"""
        page = _make_page(qapp, monkeypatch)
        try:
            page.load_composite_data(_transform_graph("A"))
            node = page._node_graph.get_node("shared")
            # 预置 scale 参数（首次编辑前 parameters 里没有该键，old 为
            # None；预置后 undo 应回到该值）
            page._node_graph.set_node_parameter("shared", "scale", 1.0)
            # 第一步：rotation 已入历史
            page._on_parameter_changed("shared", "rotation", 20.0)
            page._flush_param_changes()
            # 第二步：scale 修改尚在防抖窗口内，立即 undo
            page._on_parameter_changed("shared", "scale", 9.0)
            assert page._pending_param_change is not None

            page.undo()

            assert _param(node, "rotation") == 20.0
            assert _param(node, "scale") == 1.0
            assert page._pending_param_change is None
            # 再一次 undo 恢复 rotation（提交顺序正确）
            page.undo()
            assert _param(node, "rotation") == 0.0
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u05_legal_empty_graph_clears_previous_state(self, qapp, monkeypatch):
        """nodes=[] 是合法空图：清空旧节点并恢复新 fps/duration（U-05）。"""
        page = _make_page(qapp, monkeypatch)
        try:
            page.load_composite_data(_transform_graph("A"))
            assert len(page._node_graph.nodes) == 1
            assert page._composite_fps == 24.0

            page.load_composite_data({
                "composite_id": "empty",
                "nodes": [],
                "connections": [],
                "fps": 30.0,
                "duration_frames": 2,
            })

            assert len(page._node_graph.nodes) == 0
            assert len(page._node_graph.connections) == 0
            assert page._composite_fps == 30.0
            assert page._composite_duration_frames == 2
            assert page._current_composite_id == "empty"
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u05_invalid_payload_keeps_current_graph(self, qapp, monkeypatch):
        """缺失 nodes 字段是格式无效：不更换身份/内容（U-05）。"""
        page = _make_page(qapp, monkeypatch)
        try:
            page.load_composite_data(_transform_graph("A"))
            before = _transform_graph("A2")  # 仅用于制造当前态变化
            page.load_composite_data(before)
            assert page._current_composite_id == "A2"

            page.load_composite_data({"composite_id": "broken"})

            assert page._current_composite_id == "A2"
            assert len(page._node_graph.nodes) == 1
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u09_node_locks_reset_across_compositions(self, qapp, monkeypatch):
        """新合成不得继承上一合成的节点锁（U-09）。"""
        page = _make_page(qapp, monkeypatch)
        try:
            page.load_composite_data(_transform_graph("A"))
            page.node_editor.set_node_locked("shared", True)
            assert page.node_editor.is_node_locked("shared")

            page.load_composite_data(_transform_graph("B"))

            assert not page.node_editor.is_node_locked("shared")
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u13_group_view_reset_across_compositions(self, qapp, monkeypatch):
        """旧分组过滤不得隐藏新合成节点（U-13）。"""
        from src.features.composite.nodes.constant_node import ConstantNode

        page = _make_page(qapp, monkeypatch)
        try:
            b = ConstantNode(node_id="b")
            c = ConstantNode(node_id="c")
            graph_a = {
                "composite_id": "A",
                "nodes": [b.to_dict(), c.to_dict()],
                "connections": [],
            }
            page.load_composite_data(graph_a)
            page._node_graph.create_group("g", ["b", "c"], {"title": "G"})
            assert page.node_editor.enter_group_view("g")
            assert page.node_editor.group_view_stack == ["g"]

            new_only = ConstantNode(node_id="new-only")
            page.load_composite_data({
                "composite_id": "C",
                "nodes": [new_only.to_dict()],
                "connections": [],
            })

            assert page.node_editor.group_view_stack == []
            item = page.node_editor._node_items.get("new-only")
            assert item is not None and item.isVisible()
        finally:
            page.cleanup()
            page.deleteLater()


# ── U-03：批量参数防抖归属 ─────────────────────────────────────────────


class TestBatchParamDebounceOwnership:
    def test_u03_second_param_gets_own_undo_step(self, qapp, monkeypatch):
        """同窗口内两个批量参数必须是两条独立、精确的撤销记录（U-03）。"""
        from src.features.composite.nodes.transform_node import TransformNode

        page = _make_page(qapp, monkeypatch)
        try:
            t1 = TransformNode(node_id="n1")
            t2 = TransformNode(node_id="n2")
            payload = {
                "composite_id": "A",
                "nodes": [t1.to_dict(), t2.to_dict()],
                "connections": [],
            }
            page.load_composite_data(payload)
            n1 = page._node_graph.get_node("n1")
            n2 = page._node_graph.get_node("n2")

            page._on_parameters_changed_batch(
                ["n1", "n2"], "rotation", 10.0, {"n1": 0.0, "n2": 0.0})
            page._on_parameters_changed_batch(
                ["n1", "n2"], "scale", 2.0, {"n1": 1.0, "n2": 1.0})
            page._flush_batch_param_changes()

            page.undo()
            # 第一步撤销 scale：rotation 保持 10，scale 回 1
            assert _param(n1, "rotation") == 10.0
            assert _param(n1, "scale") == 1.0
            assert _param(n2, "rotation") == 10.0
            assert _param(n2, "scale") == 1.0

            page.undo()
            # 第二步撤销 rotation
            assert _param(n1, "rotation") == 0.0
            assert _param(n2, "rotation") == 0.0
        finally:
            page.cleanup()
            page.deleteLater()

    def test_u03_different_node_set_flushes_previous(self, qapp, monkeypatch):
        """不同节点集合的批量修改也不得沿用旧集合的 old_values（U-03）。"""
        from src.features.composite.nodes.transform_node import TransformNode

        page = _make_page(qapp, monkeypatch)
        try:
            t1 = TransformNode(node_id="n1")
            t2 = TransformNode(node_id="n2")
            payload = {
                "composite_id": "A",
                "nodes": [t1.to_dict(), t2.to_dict()],
                "connections": [],
            }
            page.load_composite_data(payload)
            n2 = page._node_graph.get_node("n2")

            page._on_parameters_changed_batch(
                ["n1"], "rotation", 10.0, {"n1": 0.0})
            page._on_parameters_changed_batch(
                ["n2"], "scale", 4.0, {"n2": 1.0})
            page._flush_batch_param_changes()

            page.undo()
            assert _param(n2, "scale") == 1.0
            page.undo()
            assert _param(page._node_graph.get_node("n1"),
                           "rotation") == 0.0
        finally:
            page.cleanup()
            page.deleteLater()


# ── U-01 / U-11 / U-12：保存与同步契约 ─────────────────────────────────


class TestCompositionMergeContract:
    def _controller(self, project):
        from unittest.mock import MagicMock
        from src.app.controllers.composite_controller import CompositeControllerMixin

        class C(CompositeControllerMixin):
            def __init__(self, p):
                self._current_edit_project = p
                self.composite_page = None
                self.edit_project_dirty = False
                self._refresh_edit_preview_after_composite_change = MagicMock()

        return C(project)

    def test_u01_layer_only_change_syncs_to_edit_project(self):
        """仅修改 layers/layer_stacks 也必须触发同步（U-01）。"""
        from src.features.edit.core.project import EditProject

        p = EditProject.create("audit")
        p, _, _, gid = p.create_empty_comp_clip()
        c = self._controller(p)

        incoming = copy.deepcopy(p.compositions[gid])
        incoming["layers"] = {"layer-1": {
            "layer_id": "layer-1", "visible": False}}
        incoming["layer_stacks"] = {"stack-1": {"layer_ids": ["layer-1"]}}

        c._sync_composite_graph_to_edit_project(gid, incoming)

        merged = c._current_edit_project.compositions[gid]
        assert merged.get("layers") == incoming["layers"]
        assert merged.get("layer_stacks") == incoming["layer_stacks"]
        assert c.edit_project_dirty is True

    def test_u01_layer_deletion_to_zero_marks_dirty(self):
        """删到最后一个图层（空 dict）也必须落盘，不能被比较逻辑跳过（U-01）。"""
        from src.features.edit.core.project import EditProject

        p = EditProject.create("audit")
        p, _, _, gid = p.create_empty_comp_clip()
        c = self._controller(p)

        incoming = copy.deepcopy(p.compositions[gid])
        incoming["layers"] = {"layer-1": {"layer_id": "layer-1"}}
        c._sync_composite_graph_to_edit_project(gid, incoming)
        assert c._current_edit_project.compositions[gid].get("layers")

        # 删除所有层：空 dict 与上一记录不同，必须再次同步
        incoming2 = copy.deepcopy(c._current_edit_project.compositions[gid])
        incoming2["layers"] = {}
        incoming2["layer_stacks"] = {}
        c._sync_composite_graph_to_edit_project(gid, incoming2)
        assert c._current_edit_project.compositions[gid].get("layers") == {}

    def test_u11_save_preserves_source_identity_and_extensions(self):
        """合成页保存不得覆盖来源/身份/扩展元信息（U-11）。"""
        from src.shared.project.save_router import SaveRouter

        composition = {
            "composition_id": "comp-1",
            "comp_clip_id": "cc-1",
            "comp_asset_id": "asset-1",
            "source_kind": "timeline_clip",
            "name": "Timeline Comp",
            "width": 1920,
            "height": 1080,
            "timeline_id": "tl-1",
            "clip_id": "clip-1",
            "source_clip_id": "clip-1",
            "custom_extension": {"vendor_field": 42},
            "nodes": [{"id": "n1", "type": "output"}],
            "connections": [],
            "viewer_state": {"selected_node_id": "n1"},
        }
        from src.features.edit.core.project import EditProject
        from dataclasses import replace as dc_replace

        base = EditProject.create("audit")
        project = dc_replace(base, compositions={"comp-1": composition})

        router = SaveRouter()
        updated = []
        router.set_callback("on_edit_project_updated", updated.append)
        router.set_callback("get_ui_state", lambda: None)
        router.set_callback("get_current_timeline_sequences", lambda: None)

        graph_only = {
            "nodes": [{"id": "n2", "type": "constant"}],
            "connections": [],
            "viewer_state": {
                "selected_node_id": "n2",
                "asset_search_roots": ["/tmp/assets"],
                "output_target_name": "matte",
                "output_target_node_id": "n2",
            },
        }
        router._save_composite_to_edit_project(
            project_path="/tmp/never-written.topos",
            edit_project=project,
            composite_graph_data=graph_only,
            composite_viewer_state={"zoom_level": 1.5},
            composite_id="comp-1",
        )

        assert updated, "on_edit_project_updated must fire"
        saved = updated[0].compositions["comp-1"]
        # graph-owned 字段被更新
        assert saved["nodes"] == graph_only["nodes"]
        # 来源/身份/扩展字段全部保留
        assert saved["comp_clip_id"] == "cc-1"
        assert saved["comp_asset_id"] == "asset-1"
        assert saved["source_kind"] == "timeline_clip"
        assert saved["name"] == "Timeline Comp"
        assert saved["timeline_id"] == "tl-1"
        assert saved["clip_id"] == "clip-1"
        assert saved["source_clip_id"] == "clip-1"
        assert saved["custom_extension"] == {"vendor_field": 42}
        assert saved["width"] == 1920 and saved["height"] == 1080
        assert saved["composition_id"] == "comp-1"

    def test_u12_viewer_state_merged_not_replaced(self):
        """viewer_state 按键合并：图编辑器键与显示键同时保留（U-12）。"""
        from src.shared.project.save_router import SaveRouter
        from src.features.edit.core.project import EditProject
        from dataclasses import replace as dc_replace

        composition = {
            "composition_id": "comp-1",
            "nodes": [{"id": "n1", "type": "output"}],
            "connections": [],
            "viewer_state": {"selected_node_id": "b"},
        }
        base = EditProject.create("audit")
        project = dc_replace(base, compositions={"comp-1": composition})

        router = SaveRouter()
        updated = []
        router.set_callback("on_edit_project_updated", updated.append)
        router.set_callback("get_ui_state", lambda: None)
        router.set_callback("get_current_timeline_sequences", lambda: None)

        graph_only = {
            "nodes": composition["nodes"],
            "connections": [],
            "viewer_state": {
                "selected_node_id": "b",
                "asset_search_roots": ["/tmp/assets"],
                "output_target_name": "matte",
                "output_target_node_id": "d",
            },
        }
        router._save_composite_to_edit_project(
            project_path="/tmp/never-written.topos",
            edit_project=project,
            composite_graph_data=graph_only,
            composite_viewer_state={"zoom_level": 1.5},
            composite_id="comp-1",
        )

        merged_vs = updated[0].compositions["comp-1"]["viewer_state"]
        assert merged_vs["selected_node_id"] == "b"
        assert merged_vs["asset_search_roots"] == ["/tmp/assets"]
        assert merged_vs["output_target_name"] == "matte"
        assert merged_vs["output_target_node_id"] == "d"
        assert merged_vs["zoom_level"] == 1.5

    def test_merged_record_is_decoupled_from_live_graph(self):
        """合并结果与 live graph 不共享可变对象（后续编辑不串档）。"""
        from src.shared.project.composition_merge import merge_composition_record

        nodes = [{"id": "n1", "type": "output"}]
        existing = {"composition_id": "c", "nodes": nodes,
                    "custom": {"k": [1]}}
        graph_data = {"nodes": nodes, "connections": []}

        merged = merge_composition_record(existing, graph_data, "c")
        nodes.append({"id": "n2", "type": "constant"})

        assert len(merged["nodes"]) == 1
        assert len(existing["nodes"]) == 2  # 调用方数据不被改动
        assert merged["custom"] == {"k": [1]}
        merged["custom"]["k"].append(2)
        assert existing["custom"]["k"] == [1]


# ── N-01：表达式提交失败零副作用 ────────────────────────────────────────


class TestExpressionCommitRollback:
    def _graph_with_animated_param(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode

        graph = NodeGraph()
        node = TransformNode(node_id="t")
        graph.add_node(node)
        node.set_keyframe_track("translate_x", _track("translate_x"))
        assert "translate_x" in node.keyframe_tracks
        return graph, node

    def test_n01_rejected_expression_keeps_keyframe_track(self):
        """循环表达式被拒后关键帧轨道必须完整保留（N-01）。"""
        graph, node = self._graph_with_animated_param()

        ok, err = graph.set_node_expression(
            "t", "translate_x", "t.translate_x + 1")

        assert ok is False
        assert err is not None
        track = node.keyframe_tracks.get("translate_x")
        assert track is not None, "keyframe track must survive rejection"
        assert len(track["keyframes"]) == 2
        assert node.get_expression("translate_x") is None

    def test_n01_rejected_edit_keeps_previous_expression(self):
        """把已有表达式改成循环式被拒后，旧表达式必须保留（N-01）。"""
        graph, node = self._graph_with_animated_param()
        ok, _ = graph.set_node_expression(
            "t", "translate_x", "this.translate_y + 1")
        assert ok

        ok, err = graph.set_node_expression(
            "t", "translate_x", "t.translate_x * 2")

        assert ok is False
        assert node.get_expression("translate_x") == "this.translate_y + 1"

    def test_n01_valid_expression_still_works(self):
        """合法表达式路径不受回滚逻辑影响。"""
        graph, node = self._graph_with_animated_param()
        ok, err = graph.set_node_expression(
            "t", "translate_x", "this.translate_y + 2")
        assert ok and err is None
        assert node.get_expression("translate_x") == "this.translate_y + 2"
        # N-04 政策（B3 批次）：合法提交不再删除轨道——轨道构成 base，
        # 表达式求值时覆盖 base
        assert "translate_x" in node.keyframe_tracks
