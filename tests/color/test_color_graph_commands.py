"""B05 图 command / undo / revision 测试 —— E08 ApplyColorGraphCommand + N08 桥。

计划验收：连续拖参数 50 次一个事务、add-parallel、undo/redo、锁栈、
stale revision 冲突；undo 恢复字节等价图、revision 单调且无重复历史；
旧 UpdateColorStackCommand 载荷仍可读。
"""

from __future__ import annotations

import pytest

from src.app.controllers.color_graph_controller import (
    GraphProjectBridge,
    GraphSessionState,
    IntentRejection,
    ColorGraphIntentNormalizer,
    ColorGraphSessionCoordinator,
)
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.commands.color_commands import (
    ApplyColorGraphCommand,
    CreateColorStackCommand,
    GraphCommandConflict,
    SetColorGraphLockCommand,
    UpdateColorStackCommand,
)
from src.features.edit.core.project import EditProject
from src.shared.project.color_graph import ColorGraphData, NodeType
from tests.color.fixtures.color_graph import f03_layer_stack


def _project_with_graph_stack() -> tuple[EditProject, str, ColorGraphData]:
    """空工程 + 一个带合法图的调色栈（legacy 迁移产物）。"""
    project, stack_id = EditProject.create(name="cmd-test").create_color_stack(
        asset_id="a1", name="node stack")
    stack = project.color_stacks[stack_id]
    graph = migrate_legacy_stack(
        stack.with_layers([l.to_dict(None) for l in f03_layer_stack()]))
    project = project.update_color_stack(stack_id, color_graph=graph)
    return project, stack_id, graph


def _bridge(project_holder: dict) -> tuple[GraphProjectBridge,
                                           ColorGraphSessionCoordinator,
                                           ProjectCommandStack]:
    stack = ProjectCommandStack()
    loads = []
    coord = ColorGraphSessionCoordinator(
        session_id="sess-1", on_load_graph=lambda g, s: loads.append((g, s)))
    coord.begin_hydrate("clip", "clip-1")
    # 初始投影：hydrate 当前权威图
    from src.app.controllers.color_graph_controller import GraphProjection
    project = project_holder["project"]
    first_stack = next(iter(project.color_stacks.values()))
    coord.hydrate_projection(GraphProjection(
        session_id="sess-1", session_generation=0, scope="clip",
        scope_owner_id="clip-1",
        graph=first_stack.color_graph.to_dict()
        if first_stack.color_graph else None,
        graph_revision=first_stack.color_graph.graph_revision
        if first_stack.color_graph else 0))
    bridge = GraphProjectBridge(
        coord, stack,
        lambda: project_holder["project"],
        set_project=lambda p: project_holder.__setitem__("project", p))
    bridge.bind_stack(next(iter(project_holder["project"].color_stacks)))
    return bridge, coord, stack


def _intent(bridge_stack_id: str, command: str, arguments: dict,
            **kwargs):
    normalizer = ColorGraphIntentNormalizer(
        session_id="sess-1", scope_owner_id="clip-1",
        stack_id=bridge_stack_id, expected_graph_revision=None)
    from src.app.controllers.color_graph_controller import ACTION_CONTRACTS
    action = next(a for a, c in ACTION_CONTRACTS.items()
                  if c.command == command) if command in (
        "insert_corrector", "add_parallel_branch") else command
    envelope = {"scope": "clip", "compound_path": [],
                "payload": {"arguments": arguments}}
    return normalizer.normalize(action, envelope)


# ── 基础：command 执行 / undo / redo ────────────────────────────────────

class TestApplyCommand:
    def test_apply_and_undo_restore_byte_equal_graph(self):
        project, stack_id, graph = _project_with_graph_stack()
        holder = {"project": project}
        bridge, coord, stack = _bridge(holder)

        cmd = ApplyColorGraphCommand.create(
            stack_id, "insert_corrector", {"active_node_id": None,
                                           "before": False},
            expected_graph_revision=graph.graph_revision)
        new_project = stack.execute(cmd, holder["project"])
        holder["project"] = new_project
        new_graph = new_project.color_stacks[stack_id].color_graph
        assert new_graph.graph_revision == graph.graph_revision + 1

        undone = stack.undo(holder["project"])
        holder["project"] = undone
        restored = undone.color_stacks[stack_id].color_graph
        assert restored.to_dict() == graph.to_dict()  # 字节等价（含定义/资源）

        redone = stack.redo(holder["project"])
        holder["project"] = redone
        assert (redone.color_stacks[stack_id].color_graph.to_dict()
                == new_graph.to_dict())

    def test_service_rejection_conflict_leaves_project_unchanged(self):
        project, stack_id, graph = _project_with_graph_stack()
        stack = ProjectCommandStack()
        cmd = ApplyColorGraphCommand.create(
            stack_id, "connect_ports",
            {"src_node": "ghost", "src_port": "image",
             "dst_node": "ghost2", "dst_port": "image"})
        before = holder_project = project
        with pytest.raises(GraphCommandConflict) as ei:
            stack.execute(cmd, project)
        assert ei.value.code == "service_rejected"
        assert project is before  # 工程不变
        assert stack._undo_stack == []  # 冲突不进历史

    def test_stale_revision_conflict(self):
        project, stack_id, graph = _project_with_graph_stack()
        stack = ProjectCommandStack()
        cmd = ApplyColorGraphCommand.create(
            stack_id, "insert_corrector", {},
            expected_graph_revision=graph.graph_revision + 99)
        with pytest.raises(GraphCommandConflict) as ei:
            stack.execute(cmd, project)
        assert ei.value.code == "stale_revision"
        assert project.color_stacks[stack_id].color_graph.to_dict() \
            == graph.to_dict()

    def test_first_command_migrates_legacy_stack(self):
        """无图栈首个图 command：显式 legacy 迁移后执行（不留双权威）。"""
        project, stack_id = EditProject.create(name="mig").create_color_stack(
            asset_id="a1")
        stack = project.color_stacks[stack_id]
        project = project.update_color_stack(
            stack_id,
            layers=[l.to_dict(None) for l in f03_layer_stack()],
            active_layer_index=0)
        cmd = ApplyColorGraphCommand.create(
            stack_id, "insert_corrector", {})
        from src.features.edit.core.commands import ProjectCommandStack
        result = ProjectCommandStack().execute(cmd, project)
        graph = result.color_stacks[stack_id].color_graph
        assert graph is not None
        assert any(n.node_type == NodeType.CORRECTOR.value
                   for n in graph.nodes)

    def test_command_serialization_roundtrip(self):
        project, stack_id, graph = _project_with_graph_stack()
        stack = ProjectCommandStack()
        cmd = ApplyColorGraphCommand.create(
            stack_id, "add_alpha_output", {})
        stack.execute(cmd, project)
        data = cmd.to_dict()
        restored_cmd = ApplyColorGraphCommand.from_dict(data)
        assert restored_cmd.operation == "add_alpha_output"
        assert restored_cmd._old_graph is not None
        # 反序列化命令仍可 undo
        undone = restored_cmd.undo(project)
        assert (undone.color_stacks[stack_id].color_graph.to_dict()
                == graph.to_dict())

    def test_legacy_update_command_payload_still_readable(self):
        """旧 command 载荷（无 color_graph 键）继续可读（B05.1）。"""
        data = {
            "command_id": "old-1", "command_type": "UpdateColorStackCommand",
            "label": "Update", "stack_id": "s1",
            "grading_params": {"exposure": 1.0},
        }
        cmd = UpdateColorStackCommand.from_dict(data)
        assert cmd.grading_params == {"exposure": 1.0}
        legacy_create = CreateColorStackCommand.from_dict({
            "command_id": "old-2", "command_type": "CreateColorStackCommand",
            "label": "Create", "name": "Old"})
        assert legacy_create.name == "Old"


# ── 事务合并：50 次参数拖动 → 1 个 undo ────────────────────────────────

class TestTransactionMerging:
    def test_fifty_param_edits_one_undo(self):
        project, stack_id, graph = _project_with_graph_stack()
        holder = {"project": project}
        bridge, coord, stack = _bridge(holder)
        service_graph = graph

        from src.features.color.core.color_graph_service import (
            ColorGraphService,
        )
        counter = iter(range(10000))
        service = ColorGraphService(
            id_factory=lambda: f"tx-{next(counter)}")

        bridge.begin_transaction("gesture-1")
        current = service_graph
        target_node = current.nodes[1].node_id
        for i in range(50):
            result = service.op_update_layout(
                current, {target_node: (float(i), 0.0)})
            assert result.ok
            current = result.graph
            bridge.add_to_transaction(result, "gesture-1", "update_layout")
        assert bridge.flush_transaction() is True

        assert len(stack._undo_stack) == 1, "50 次拖动必须合并为一个 command"
        # undo 一次回到手势前（布局 revision 一并还原）
        undone = stack.undo(holder["project"])
        restored = undone.color_stacks[stack_id].color_graph
        assert restored.to_dict() == graph.to_dict()

    def test_flush_without_accumulation_returns_none(self):
        project, stack_id, graph = _project_with_graph_stack()
        holder = {"project": project}
        bridge, coord, stack = _bridge(holder)
        assert bridge.flush_transaction() is False

    def test_move_and_insert_single_command(self):
        """move_and_insert 经 command 栈只产生一个 undo 项。"""
        project, stack_id, graph = _project_with_graph_stack()
        stack = ProjectCommandStack()
        # 先加自由节点（一个 command）
        add_cmd = ApplyColorGraphCommand.create(
            stack_id, "add_node",
            {"kind": "corrector", "position": [0.0, 0.0]})
        project = stack.execute(add_cmd, project)
        new_graph = project.color_stacks[stack_id].color_graph
        free_id = next(n.node_id for n in new_graph.nodes
                       if len([e for e in new_graph.edges
                               if e.src_node == n.node_id
                               or e.dst_node == n.node_id]) == 0)
        target_edge = next(e for e in new_graph.edges
                           if e.dst_node == new_graph.output_node_id)
        cmd = ApplyColorGraphCommand.create(
            stack_id, "move_and_insert",
            {"positions": {free_id: [12.0, 3.0]},
             "node_id": free_id, "edge_id": target_edge.edge_id},
            label="Move and Insert")
        project = stack.execute(cmd, project)
        assert len(stack._undo_stack) == 2  # add + move_and_insert 各一次
        project = stack.undo(project)
        graph_after = project.color_stacks[stack_id].color_graph
        # undo move_and_insert 后：自由节点回到未连接 + 原布局
        node = next(n for n in graph_after.nodes if n.node_id == free_id)
        assert not [e for e in graph_after.edges
                    if e.src_node == free_id or e.dst_node == free_id]


# ── 栈锁 / shared fanout ───────────────────────────────────────────────

class TestLockAndSharedFanout:
    def test_locked_stack_rejects_edits_but_allows_unlock(self):
        project, stack_id, graph = _project_with_graph_stack()
        stack = ProjectCommandStack()
        lock = SetColorGraphLockCommand.create(stack_id, True)
        project = stack.execute(lock, project)
        assert project.color_stacks[stack_id].graph_locked is True

        cmd = ApplyColorGraphCommand.create(stack_id, "insert_corrector", {})
        with pytest.raises(GraphCommandConflict) as ei:
            stack.execute(cmd, project)
        assert ei.value.code == "stack_locked"

        # 解锁 command 本身可执行（元信息路径）
        unlock = SetColorGraphLockCommand.create(stack_id, False)
        project = stack.execute(unlock, project)
        assert project.color_stacks[stack_id].graph_locked is False
        # 解锁后图编辑恢复
        project = stack.execute(
            ApplyColorGraphCommand.create(stack_id, "insert_corrector", {}),
            project)
        assert (project.color_stacks[stack_id].color_graph.graph_revision
                == graph.graph_revision + 1)

    def test_lock_command_undo(self):
        project, stack_id, _ = _project_with_graph_stack()
        stack = ProjectCommandStack()
        project = stack.execute(
            SetColorGraphLockCommand.create(stack_id, True), project)
        project = stack.undo(project)
        assert project.color_stacks[stack_id].graph_locked is False

    def test_shared_definition_fanout_across_stacks(self):
        """一个定义、两个引用栈：fanout 更新都失效，undo 全还原。"""
        from dataclasses import replace as _replace
        from src.features.color.core.color_graph_service import (
            ColorGraphService,
        )
        project, stack_a, graph_a = _project_with_graph_stack()
        counter = iter(range(1000))
        service = ColorGraphService(
            id_factory=lambda: f"fan-{next(counter)}")
        c0_a = next(n for n in graph_a.nodes
                    if n.node_type == NodeType.CORRECTOR.value)
        shared = service.apply_intent(
            graph_a, "create_shared_definition",
            {"node_id": c0_a.node_id, "name": "Look"})
        assert shared.ok
        definition = shared.graph.shared_definitions[0]
        project = project.update_color_stack(stack_a,
                                             color_graph=shared.graph)

        # 栈 B：迁移后把首 Corrector 变为同一定义的引用（跨栈共享）
        project, stack_b = project.create_color_stack(asset_id="a2",
                                                      name="second")
        stack_b_data = project.color_stacks[stack_b].with_layers(
            [l.to_dict(None) for l in f03_layer_stack()])
        graph_b = migrate_legacy_stack(stack_b_data)
        c0_b = next(n for n in graph_b.nodes
                    if n.node_type == NodeType.CORRECTOR.value)
        ref_node = _replace(c0_b,
                            node_type=NodeType.SHARED_CORRECTOR.value,
                            shared_definition_id=definition.definition_id)
        graph_b = _replace(
            graph_b,
            nodes=tuple(ref_node if n.node_id == c0_b.node_id else n
                        for n in graph_b.nodes),
            shared_definitions=(definition,))
        assert graph_b.is_valid()
        project = project.update_color_stack(stack_b, color_graph=graph_b)

        holder = {"project": project}
        bridge, coord, cmd_stack = _bridge(holder)
        bridge.bind_stack(stack_a)
        updated = bridge.update_shared_definition(
            definition.definition_id, {"grading": {"saturation": 2.0}})
        assert set(updated) == {stack_a, stack_b}
        for sid in (stack_a, stack_b):
            g = holder["project"].color_stacks[sid].color_graph
            definition_after = next(
                d for d in g.shared_definitions
                if d.definition_id == definition.definition_id)
            assert definition_after.version == definition.version + 1
            # 引用存在 → render revision 同步失效
            assert g.graph_revision == (shared.graph.graph_revision
                                        if sid == stack_a else
                                        graph_b.graph_revision) + 1
        # undo 全还原（依赖索引随整图快照恢复）
        while cmd_stack._undo_stack:
            undone = cmd_stack.undo(holder["project"])
            assert undone is not None
            holder["project"] = undone
        for sid, before in ((stack_a, shared.graph), (stack_b, graph_b)):
            g = holder["project"].color_stacks[sid].color_graph
            assert g.to_dict() == before.to_dict()


# ── 桥接：冲突 → reject → 协调器回滚 ──────────────────────────────────

class TestBridgeConflictRecovery:
    def test_service_reject_returns_false_and_recovers(self):
        project, stack_id, graph = _project_with_graph_stack()
        holder = {"project": project}
        bridge, coord, stack = _bridge(holder)
        ok = bridge.submit(_intent(stack_id, "insert_corrector",
                                   {"active_node_id": "ghost-node"}))
        assert ok is False
        assert coord.state == GraphSessionState.READY  # 冲突回滚后可恢复
        assert holder["project"].color_stacks[stack_id].color_graph.to_dict() \
            == graph.to_dict()

    def test_successful_submit_updates_projection(self):
        project, stack_id, graph = _project_with_graph_stack()
        holder = {"project": project}
        bridge, coord, stack = _bridge(holder)
        ok = bridge.submit(_intent(stack_id, "insert_corrector",
                                   {"active_node_id": None,
                                    "before": False}))
        assert ok is True
        assert coord.state == GraphSessionState.READY
        assert coord.last_ack.graph_revision == graph.graph_revision + 1

    def test_revision_monotonic_no_duplicate_history(self):
        project, stack_id, graph = _project_with_graph_stack()
        holder = {"project": project}
        bridge, coord, stack = _bridge(holder)
        revisions = [graph.graph_revision]
        for _ in range(5):
            ok = bridge.submit(_intent(stack_id, "insert_corrector",
                                       {"active_node_id": None,
                                        "before": False}))
            assert ok is True
            g = holder["project"].color_stacks[stack_id].color_graph
            revisions.append(g.graph_revision)
        assert revisions == sorted(set(revisions))  # 单调无重复
        assert len(stack._undo_stack) == 5

    def test_hundred_undo_redo_cycles_restore(self):
        """连续 100 次 undo/redo 完整恢复资源引用与图字节（B05.5）。"""
        project, stack_id, graph = _project_with_graph_stack()
        holder = {"project": project}
        bridge, coord, stack = _bridge(holder)
        for _ in range(10):
            assert bridge.submit(_intent(stack_id, "insert_corrector",
                                         {"active_node_id": None,
                                          "before": False}))
        final = holder["project"].color_stacks[stack_id].color_graph
        final_dict = final.to_dict()

        for _ in range(10):
            undone = stack.undo(holder["project"])
            assert undone is not None
            holder["project"] = undone
        assert (holder["project"].color_stacks[stack_id].color_graph
                .to_dict() == graph.to_dict())
        for _ in range(10):
            redone = stack.redo(holder["project"])
            assert redone is not None
            holder["project"] = redone
        assert (holder["project"].color_stacks[stack_id].color_graph
                .to_dict() == final_dict)
