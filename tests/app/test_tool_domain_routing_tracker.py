"""N03-d：Tracker 绑定的 typed handle（节点身份捕获 + 投影枚举/结果同步）。

验收（计划 N03）：取色/跟踪任务绑定 node+frame+session；切 clip 后完成
的异步跟踪结果按绑定节点身份同步（不按当时活动层猜）；可跟踪窗口
枚举读选中节点合并投影（compound 内成员正确）；旧工程绑定（无身份
字段）向后兼容。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.core.layer import Layer
from src.shared.tracking.track_data import TrackingBinding
from tests.app.test_color_graph_app_host import _Harness
from tests.app.test_tool_domain_routing_windows import (
    _WindowEngine, _WindowHost, _seeded_project,
)


@pytest.fixture()
def rig(qapp):
    project, stack_id, graph, node_id = _seeded_project()
    harness = _Harness(qapp, project)
    harness.host.on_clip_stack_loaded(stack_id, scope_owner_id=stack_id)
    # 引擎活动层故意放一个**没有窗口**的层——证明枚举/结果同步读的是
    # 节点投影而非 root active layer
    empty_layer = Layer("stale")
    engine = _WindowEngine(empty_layer)
    host = _WindowHost(harness.host, engine)
    harness.panel.view.state.graph.active_id = node_id
    harness.panel.view._selection_changed()
    return SimpleNamespace(harness=harness, host=host, engine=engine,
                           stack_id=stack_id, node_id=node_id)


def test_binding_identity_roundtrip_and_legacy_compat() -> None:
    binding = TrackingBinding(
        binding_id="b1", clip_id="clip-1", shape_id="win-1",
        node_id="node-9", compound_path=("c1", "c2"), session_id="s-7")
    data = binding.to_dict()
    assert data["node_id"] == "node-9"
    assert data["compound_path"] == ["c1", "c2"]
    restored = TrackingBinding.from_dict(data)
    assert restored.node_id == "node-9"
    assert restored.compound_path == ("c1", "c2")
    assert restored.session_id == "s-7"
    # 旧工程载荷（无身份字段）→ 空默认，照常反序列化
    legacy = TrackingBinding.from_dict(
        {"binding_id": "b2", "clip_id": "clip-1", "shape_id": "win-2"})
    assert legacy.node_id == "" and legacy.compound_path == () \
        and legacy.session_id == ""


def test_trackable_shapes_enumerate_node_projection(rig) -> None:
    """图模式枚举读选中节点投影（引擎活动层无窗口也不受影响）。"""
    shapes = rig.host._iter_trackable_shapes()
    assert [s.id for s in shapes] == ["win-1"]  # 种子窗口来自节点投影
    assert rig.engine.get_active_layer().mask_shapes == []  # 活动层确实为空


def test_tracker_result_layer_resolves_bound_node(rig) -> None:
    """切 clip/换选中后任务完成：按绑定 node_id 解析投影层。"""
    binding = TrackingBinding(binding_id="b1", clip_id="clip-1",
                              shape_id="win-1", node_id=rig.node_id)
    layer = rig.host._tracker_result_layer(binding)
    assert layer is not None
    assert layer.get_mask_item_by_id("win-1") is not None
    # 无身份（legacy 绑定）→ 回落活动层
    legacy_binding = TrackingBinding(binding_id="b2", clip_id="clip-1",
                                     shape_id="win-1")
    fallback = rig.host._tracker_result_layer(legacy_binding)
    assert fallback is rig.engine.get_active_layer()
    # 节点已删（身份失效）→ 回落活动层，不抛
    gone = TrackingBinding(binding_id="b3", clip_id="clip-1",
                           shape_id="win-1", node_id="ghost-node")
    assert rig.host._tracker_result_layer(gone) is rig.engine.get_active_layer()
