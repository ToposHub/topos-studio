"""N08: Shared 跨 clip 认证（原子 fanout / 一次 undo / Make Local / 锁）。

验收（计划 N08）：A/B 栈同引定义改参数两边更新且 1 undo；Make Local 保留
当时结果；锁定栈显式缺席；跨栈引用闭包（定义随引用栈图持久化）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _two_stack_project():
    project = EditProject.create(name="n08")
    sids = []
    for i in range(2):
        project, sid = project.create_color_stack(asset_id=f"a{i}",
                                                  name=f"s{i}")
        sids.append(sid)
    graphs = []
    for sid in sids:
        stack = project.color_stacks[sid].with_layers(
            [l.to_dict(None) for l in f03_layer_stack()])
        graphs.append(migrate_legacy_stack(stack))
    return project, graphs, sids


def _share(service, graph, definition_id="shared-def-1"):
    """共享化并把 definition_id 归一到指定值（跨栈同引用的模拟形态）。"""
    from dataclasses import replace as _r
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    result = service.apply_intent(graph, "create_shared_definition", {
        "node_id": corrector.node_id, "name": "Look",
        "param_domains": ("grade",), "locked": False})
    assert result.ok, result.diagnostics
    graph = result.graph
    old_id = next(d.definition_id for d in graph.shared_definitions)
    definitions = tuple(
        _r(d, definition_id=definition_id) if d.definition_id == old_id else d
        for d in graph.shared_definitions)
    nodes = tuple(
        _r(n, shared_definition_id=definition_id)
        if n.shared_definition_id == old_id else n
        for n in graph.nodes)
    return _r(graph, shared_definitions=definitions, nodes=nodes), corrector.node_id


def test_atomic_fanout_updates_both_stacks_one_undo() -> None:
    from src.app.controllers.color_graph_controller import GraphProjectBridge
    from src.app.controllers.color_graph_controller import (
        ColorGraphSessionCoordinator, GraphSessionState,
    )
    service = ColorGraphService()
    project, graphs, _sids = _two_stack_project()
    g1, _ = _share(service, graphs[0])
    # 第二栈也持同定义（模拟跨 clip 引用——迁移期各栈独立图 + 同 id 定义）
    g2, _ = _share(service, graphs[1])
    project = project.update_color_stack(
        next(iter(project.color_stacks)), color_graph=g1)
    second = list(project.color_stacks)[1]
    project = project.update_color_stack(second, color_graph=g2)

    holder = {"project": project}
    coordinator = ColorGraphSessionCoordinator(session_id="s",
                                                on_load_graph=lambda *_: None)
    coordinator._set_state(GraphSessionState.READY)
    stack = ProjectCommandStack()
    bridge = GraphProjectBridge(coordinator, stack,
                                 lambda: holder["project"],
                                 lambda p: holder.update(project=p))
    bridge.bind_stack(next(iter(project.color_stacks)))

    definition_id = next(d.definition_id for d in g1.shared_definitions)
    updated = bridge.update_shared_definition(
        definition_id, {"grade": {"exposure": 77.0}})
    assert sorted(updated) == sorted(project.color_stacks)  # 两栈都更新
    # 两栈定义参数都变了
    for sid in project.color_stacks:
        graph = holder["project"].color_stacks[sid].color_graph
        definition = next(d for d in graph.shared_definitions
                          if d.definition_id == definition_id)
        assert definition.params.get("grade", {}).get("exposure") == 77.0
    # 一次 undo 全还原
    undone = stack.undo(holder["project"])
    assert undone is not None
    holder["project"] = undone
    for sid in undone.color_stacks:
        graph = undone.color_stacks[sid].color_graph
        definition = next(d for d in graph.shared_definitions
                          if d.definition_id == definition_id)
        assert definition.params.get("grade", {}).get("exposure") != 77.0


def test_locked_stack_absent_from_fanout() -> None:
    from src.app.controllers.color_graph_controller import GraphProjectBridge
    from src.app.controllers.color_graph_controller import (
        ColorGraphSessionCoordinator, GraphSessionState,
    )
    from src.features.edit.core.commands import color_commands as cc
    service = ColorGraphService()
    project, graphs, _sids = _two_stack_project()
    g1, _ = _share(service, graphs[0])
    g2, _ = _share(service, graphs[1])
    first, second = list(project.color_stacks)[:2]
    project = project.update_color_stack(first, color_graph=g1)
    project = project.update_color_stack(second, color_graph=g2)
    lock = cc.SetColorGraphLockCommand.create(second, True)
    stack = ProjectCommandStack()
    project = stack.execute(lock, project)

    holder = {"project": project}
    coordinator = ColorGraphSessionCoordinator(session_id="s",
                                                on_load_graph=lambda *_: None)
    coordinator._set_state(GraphSessionState.READY)
    bridge = GraphProjectBridge(coordinator, stack,
                                lambda: holder["project"],
                                lambda p: holder.update(project=p))
    bridge.bind_stack(first)
    definition_id = next(d.definition_id for d in g1.shared_definitions)
    updated = bridge.update_shared_definition(
        definition_id, {"grade": {"exposure": 55.0}})
    assert updated == [first]  # 锁定栈显式缺席（不静默失败也不误写）


def test_make_local_preserves_values_at_that_time() -> None:
    service = ColorGraphService()
    project, graphs, _sids = _two_stack_project()
    shared, node_id = _share(service, graphs[0])
    local = service.apply_intent(shared, "make_local_corrector",
                                 {"node_ids": [node_id]})
    assert local.ok, local.diagnostics
    node = next(n for n in local.graph.nodes if n.node_id == node_id)
    assert node.node_type == "corrector"  # 解除引用
    assert not node.shared_definition_id
    assert (node.params.get("grade") or {}) == \
        (next(n for n in shared.nodes if n.node_id == node_id)
         .params.get("grade") or {})  # 当时参数保留
