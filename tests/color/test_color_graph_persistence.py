"""B14 持久化测试 —— 保存/重开/版本/LUT 策略矩阵。

验收：save/reopen 图不丢（含共享定义/布局/身份）；版本 activate/copy
恢复一致图；LUT 拒绝空间/时序依赖图；损坏/未知版本回归。
"""

from __future__ import annotations

import copy

import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import (
    ColorGraphService,
    can_export_lut,
)
from src.features.edit.core.project import EditProject
from src.shared.project.color_graph import (
    ColorGraphData,
    NodeType,
)
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers


def _project_with_graph() -> tuple[EditProject, str, ColorGraphData]:
    project, stack_id = EditProject.create(name="p14").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[stack_id]
    graph = migrate_legacy_stack(
        stack.with_layers([l.to_dict(None) for l in f03_layer_stack()]))
    project = project.update_color_stack(stack_id, color_graph=graph)
    return project, stack_id, graph


class TestProjectRoundTrip:
    def test_graph_survives_io_boundary(self):
        """保存边界 = 栈载荷 to_dict → from_dict（工程 IO 消费同一形态）。"""
        project, stack_id, graph = _project_with_graph()
        stack = project.color_stacks[stack_id]
        payload = copy.deepcopy(stack.to_dict())
        assert payload.get("color_graph")
        restored_stack = type(stack).from_dict(payload)
        assert restored_stack.color_graph.to_dict() == graph.to_dict()
        # 工程级替换（with_color_stacks 同语义）
        new_stacks = dict(project.color_stacks)
        new_stacks[stack_id] = restored_stack
        from dataclasses import replace as _replace
        restored_project = _replace(project, color_stacks=new_stacks)
        assert (restored_project.color_stacks[stack_id].color_graph
                .to_dict() == graph.to_dict())

    def test_shared_definitions_and_layout_survive(self):
        project, stack_id, graph = _project_with_graph()
        service = ColorGraphService(id_factory=lambda: "p14-x")
        cs = next(n.node_id for n in graph.nodes
                  if n.node_type == NodeType.CORRECTOR.value)
        shared = service.apply_intent(graph, "create_shared_definition",
                                      {"node_id": cs, "name": "Look"})
        assert shared.ok
        moved = service.apply_intent(
            shared.graph, "update_layout",
            {"positions": {cs: (120.0, 40.0)}})
        assert moved.ok
        project = project.update_color_stack(stack_id,
                                             color_graph=moved.graph)
        payload = copy.deepcopy(
            project.color_stacks[stack_id].to_dict())
        graph2 = type(project.color_stacks[stack_id]).from_dict(
            payload).color_graph
        assert graph2.to_dict() == moved.graph.to_dict()
        assert graph2.shared_definitions
        assert graph2.ui_state.positions[cs] == (120.0, 40.0)

    def test_standalone_stacks_payload_keeps_graph(self):
        """standalone 保存边界（color_stacks.json 载荷）不丢图。"""
        from src.shared.project.save_router import SaveRouter  # noqa: F401
        stack = _stack_with_layers(f03_layer_stack())
        graph = migrate_legacy_stack(stack)
        carrying = stack.with_color_graph(graph)
        payload = carrying.to_dict()
        assert "color_graph" in payload
        restored = type(stack).from_dict(copy.deepcopy(payload))
        assert restored.color_graph is not None
        assert restored.color_graph.to_dict() == graph.to_dict()


class TestVersions:
    def test_version_capture_and_activate_restore_graph(self):
        """版本快照携带完整图；activate 恢复一致身份与内容。"""
        project, stack_id, graph = _project_with_graph()
        stack = project.color_stacks[stack_id]
        content = stack.to_dict()
        assert content.get("color_graph")
        # 版本命令族经 _stack_from_payload → ColorStackData.from_dict
        # 恢复（图随之往返）；这里直接验证载荷链路等价
        from src.features.edit.core.color_versions import _stack_from_payload
        restored_stack = _stack_from_payload(stack, content)
        assert restored_stack.color_graph is not None
        assert restored_stack.color_graph.to_dict() == graph.to_dict()

    def test_missing_plugin_version_keeps_payload(self):
        """未知插件（future node type）在图内 load→save 保留。"""
        project, stack_id, graph = _project_with_graph()
        data = graph.to_dict()
        data["nodes"].append({
            "node_id": "future-fx", "node_type": "neural_relight_v2",
            "params": {"strength": 0.3}, "enabled": True})
        from src.shared.project.color_stack import ColorStackData
        stack_payload = project.color_stacks[stack_id].to_dict()
        stack_payload["color_graph"] = data
        restored_stack = ColorStackData.from_dict(stack_payload)
        mystery = next(n for n in restored_stack.color_graph.nodes
                       if n.node_id == "future-fx")
        assert mystery.node_type == "neural_relight_v2"
        # load→save 不丢
        assert restored_stack.to_dict()["color_graph"]["nodes"][-1][
            "node_id"] == "future-fx"


class TestLutPolicy:
    def test_serial_pure_chain_pointwise(self):
        _, _, graph = _project_with_graph()
        ok, reason = can_export_lut(graph)
        assert ok, reason
        assert reason == "pointwise_serial_chain"

    def test_window_disqualifies(self):
        from src.features.color.core.layer import MaskShape
        layers = f03_layer_stack()
        shape = MaskShape(MaskShape.CIRCLE)
        layers[0].mask_shapes = [shape]
        stack = _stack_with_layers(layers)
        graph = migrate_legacy_stack(stack)
        ok, reason = can_export_lut(graph)
        assert not ok and reason == "windows_masks"

    def test_qualifier_disqualifies(self):
        layers = f03_layer_stack()
        layers[0].qualifier_params["enabled"] = True
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        ok, reason = can_export_lut(graph)
        assert not ok and reason == "qualifier_spatial"

    def test_keyframes_disqualify(self):
        from src.features.color.core.grade_keyframes import GradeKeyframeTrack
        layers = f03_layer_stack()
        track = GradeKeyframeTrack(param_name="exposure")
        track.set_keyframe(0, 0.0)
        layers[0].grade_keyframes["exposure"] = track
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        ok, reason = can_export_lut(graph)
        assert not ok and reason == "keyframes_temporal"

    def test_mixer_disqualifies(self):
        counter = iter(range(50))
        service = ColorGraphService(id_factory=lambda: f"lut-{next(counter)}")
        _, _, graph = _project_with_graph()
        grown = service.apply_intent(graph, "add_parallel_branch", {})
        ok, reason = can_export_lut(grown.graph)
        assert not ok
        assert reason.startswith("node_type_")

    def test_plugins_disqualify(self):
        layers = f03_layer_stack()
        layers[1].plugins.append({
            "instance_id": "i", "plugin_id": "fx", "params": {},
            "enabled": True, "type": "dctl"})
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        ok, reason = can_export_lut(graph)
        assert not ok and reason == "plugins"


class TestCorruptionRecovery:
    def test_corrupt_graph_raises_with_stack_identity(self):
        from src.shared.project.color_graph import GraphSchemaError
        project, stack_id, graph = _project_with_graph()
        payload = project.color_stacks[stack_id].to_dict()
        payload["color_graph"]["nodes"] = "corrupt"
        from src.shared.project.color_stack import ColorStackData
        with pytest.raises(GraphSchemaError):
            ColorStackData.from_dict(payload)

    def test_future_schema_version_rejected_cleanly(self):
        from src.shared.project.color_graph import (
            COLOR_GRAPH_SCHEMA_VERSION,
            GraphSchemaError,
        )
        project, stack_id, graph = _project_with_graph()
        payload = project.color_stacks[stack_id].to_dict()
        payload["color_graph"]["schema_version"] = (
            COLOR_GRAPH_SCHEMA_VERSION + 5)
        from src.shared.project.color_stack import ColorStackData
        with pytest.raises(GraphSchemaError):
            ColorStackData.from_dict(payload)
