"""N03-c：Window/形状写路径的图模式路由（结构操作 + 参数拖动）。

验收（计划 N03）：图编辑上下文中窗口增/删/清空等结构操作经图命令
（一次操作一次 undo，legacy history 不再压栈）；防抖参数队列 patch
并入 gestures 事务收束为一次 command；跨节点窗口复制经 legacy 层
反查落正确节点；legacy 上下文零变更。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PyQt6")

from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_param_source import node_layer_payload
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.layer import Layer, MaskShape
from src.features.edit.core.project import EditProject
from tests.app.test_color_graph_app_host import _Harness
from tests.color.fixtures.color_graph import f03_layer_stack

_SEED_SHAPES = [
    {"id": "win-1", "type": "circle", "name": "C", "center": [0.5, 0.5],
     "size": [0.2, 0.2], "rotation": 0.0, "feather": 2.0, "opacity": 1.0,
     "visible": True, "invert": False, "operation": "add"},
]


def _seeded_project():
    project, stack_id = EditProject.create(name="n03c").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[stack_id].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    seeded = ColorGraphService().apply_intent(
        graph, "set_node_window_shapes",
        {"node_id": node.node_id, "shapes": _SEED_SHAPES})
    assert seeded.ok, seeded.diagnostics
    project = project.update_color_stack(stack_id, color_graph=seeded.graph)
    return project, stack_id, seeded.graph, node.node_id


class _WindowEngine:
    def __init__(self, layer: Layer) -> None:
        self.layers = [layer]
        self.active_layer_index = 0
        self.tracking_store = None
        self.current_grade_frame = None
        self.history = Mock()
        self.mask_shapes_changed = Mock()
        self.get_active_layer = lambda: self.layers[self.active_layer_index]


class _WindowHost(ColorControllerMixin):
    """Mixin 宿主（最小面板桩；窗口路由所需的全部 self.* 属性）。"""

    def __init__(self, graph_host, engine) -> None:
        self.color_graph_host = graph_host
        self.engine = engine
        self.mask_panel = SimpleNamespace(
            update_shapes_list=lambda *_a: None,
            select_shape=lambda *_a: None,
            set_params=lambda *_a, **_k: None,
            btn_brush=SimpleNamespace(setChecked=lambda _v: None),
            btn_sam=SimpleNamespace(setChecked=lambda _v: None))
        self.canvas = SimpleNamespace(
            active_shape_id=None,
            update_mask_overlay=lambda *_a: None,
            set_active_shape=lambda *_a: None,
            set_drawing_mode=lambda *_a: None,
            set_sam_mode=lambda _v: None,
            clear_sam_points=lambda: None,
            logical_size=None)
        self.layer_panel = SimpleNamespace(
            update_layers=lambda *_a, **_k: None)
        self.status_bar = SimpleNamespace(showMessage=lambda *_a, **_k: None)
        self.tracker_panel = SimpleNamespace(
            set_bound_shape=lambda *_a: None,
            get_params=lambda: {})
        self.pending_mask_updates: dict = {}

    def request_render(self) -> None:
        pass

    def _trigger_autosave(self) -> None:
        pass

    def _refresh_tracker_targets(self, *_a) -> None:
        pass

    def _tracker_render_context(self):
        return (None, None)


@pytest.fixture()
def rig(qapp):
    project, stack_id, graph, node_id = _seeded_project()
    harness = _Harness(qapp, project)
    harness.host.on_clip_stack_loaded(stack_id, scope_owner_id=stack_id)
    # 活动层 = 选中 corrector 的合并投影（含种子窗口）
    node = next(n for n in graph.nodes if n.node_id == node_id)
    layer = Layer.from_dict(node_layer_payload(node))
    engine = _WindowEngine(layer)
    host = _WindowHost(harness.host, engine)
    harness.panel.view.state.graph.active_id = node_id
    harness.panel.view._selection_changed()
    return SimpleNamespace(harness=harness, host=host, engine=engine,
                           stack_id=stack_id, node_id=node_id, layer=layer)


def _graph_node(rig):
    return next(
        n for n in rig.harness.holder["project"]
        .color_stacks[rig.stack_id].color_graph.nodes
        if n.node_id == rig.node_id)


def _depth(rig) -> int:
    return len(rig.harness.cmd_stack._undo_stack)


def test_window_attribute_drag_routes_into_gesture(rig) -> None:
    base = _depth(rig)
    shape = rig.layer.get_mask_item_by_id("win-1")
    rig.host.pending_mask_updates = {"win-1": {"feather": 7.5,
                                               "center": [0.4, 0.6]}}
    rig.host.process_mask_update()
    # 活对象先行同值更新（画布/面板即时反馈）
    assert shape.feather == pytest.approx(7.5)
    assert list(shape.center) == [0.4, 0.6]
    # 手势在途：权威图未变
    assert rig.harness.host.grade_gesture_open()
    assert "mask_shapes" not in (_graph_node(rig).legacy_payload or {}) or \
        _graph_node(rig).legacy_payload["mask_shapes"][0]["feather"] != 7.5
    rig.host._flush_tool_gesture()
    assert _depth(rig) == base + 1  # 一次手势一次 undo
    shape_dict = _graph_node(rig).legacy_payload["mask_shapes"][0]
    assert shape_dict["feather"] == pytest.approx(7.5)
    assert list(shape_dict["center"]) == [0.4, 0.6]
    assert shape_dict["id"] == "win-1"


def test_window_add_routes_structure_command(rig) -> None:
    base = _depth(rig)
    rig.host.on_mask_shape_added("rect")
    # legacy history 未压栈（undo 走图命令）
    rig.engine.history.push_state.assert_not_called()
    assert _depth(rig) == base + 1
    shapes = _graph_node(rig).legacy_payload["mask_shapes"]
    assert len(shapes) == 2  # 种子 + 新矩形
    added = next(s for s in shapes if s["id"] != "win-1")
    assert added["type"] == "rect"
    live_ids = {s.id for s in rig.layer.mask_shapes}
    assert added["id"] in live_ids  # 活对象身份保留（选中/绑定稳定）


def test_window_remove_and_clear_route_structure(rig) -> None:
    base = _depth(rig)
    rig.host.on_mask_shape_removed("win-1")
    rig.engine.history.push_state.assert_not_called()
    assert _depth(rig) == base + 1
    assert list(_graph_node(rig).legacy_payload["mask_shapes"]) == []
    # 再加一个再清空（图模式只清活动节点）
    rig.host.on_mask_shape_added("circle")
    rig.host.on_all_masks_cleared()
    assert _depth(rig) == base + 3
    assert list(_graph_node(rig).legacy_payload["mask_shapes"]) == []


def test_undo_restores_window_domain(rig) -> None:
    rig.host.on_mask_shape_removed("win-1")
    assert list(_graph_node(rig).legacy_payload["mask_shapes"]) == []
    undone = rig.harness.cmd_stack.undo(rig.harness.holder["project"])
    rig.harness.holder["project"] = undone
    restored = _graph_node(rig).legacy_payload["mask_shapes"]
    assert restored[0]["id"] == "win-1"  # 图命令 undo 完整恢复


def test_cross_node_copy_resolves_target_node(rig, qapp) -> None:
    """跨节点窗口复制：目标 legacy 层反查节点，命令落目标节点。"""
    # 第二个 corrector（图内既有节点）作为复制目标
    graph = _graph_node(rig).__class__  # noqa: F841 — 仅示类型
    target_node = next(
        n for n in rig.harness.holder["project"]
        .color_stacks[rig.stack_id].color_graph.nodes
        if n.node_type == "corrector" and n.node_id != rig.node_id)
    target_layer = Layer.from_dict(node_layer_payload(target_node))
    rig.engine.layers.append(target_layer)
    route = rig.host._graph_tool_route()
    assert route is not None
    target_id = rig.harness.host.node_id_for_legacy_layer(target_layer.id)
    assert target_id == target_node.node_id
    base = _depth(rig)
    # 直接走提交路径（菜单交互本身是 QMenu.exec 弹窗，不在此驱动）
    for item in rig.layer.mask_shapes:
        target_layer.mask_shapes.append(rig.host._clone_mask_item(item))
    assert rig.host._submit_window_structure(route, target_layer,
                                             node_id=target_id)
    assert _depth(rig) == base + 1
    shapes = next(
        n for n in rig.harness.holder["project"]
        .color_stacks[rig.stack_id].color_graph.nodes
        if n.node_id == target_node.node_id).legacy_payload["mask_shapes"]
    assert len(shapes) == 1  # 克隆落目标节点（身份已重建，非 win-1）
    assert shapes[0]["id"] != "win-1"
