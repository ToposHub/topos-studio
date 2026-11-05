"""B03 legacy 无损迁移测试 —— N03 migrate/project + E07 加载优先级。

计划验收：v1 平铺、v2–v4 layers、mask bitmap、tracking、重复名称、
隐藏层、空 layers、缺 LUT、旧 bake 无 digest、future extras；迁移幂等，
ID/资源/关键帧守恒；复杂 DAG 拒绝压平；损坏图显式报错不静默回退。
"""

from __future__ import annotations

import uuid

import json

import numpy as np
import pytest

from src.features.color.core.color_graph_migration import (
    LegacyMigrationError,
    can_project_to_legacy_layers,
    migrate_legacy_stack,
    project_legacy_layers,
)
from src.features.color.core.layer import Layer, MaskShape
from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorNodeData,
    GraphSchemaError,
    NodeType,
)
from src.shared.project.color_stack import ColorStackData
from tests.color.fixtures.color_graph import f03_layer_stack


def _stack_with_layers(layers: list[Layer], active: int = 0) -> ColorStackData:
    stack = ColorStackData.create(asset_id="a1", name="legacy")
    return stack.with_layers([l.to_dict(None) for l in layers],
                             active_layer_index=active)


# ── v2+ layers 迁移 ──────────────────────────────────────────────────────

class TestLayerStackMigration:
    def test_serial_chain_shape(self):
        stack = _stack_with_layers(f03_layer_stack())
        graph = migrate_legacy_stack(stack)
        assert graph.is_valid()
        correctors = [n for n in graph.nodes
                      if n.node_type == NodeType.CORRECTOR.value]
        assert len(correctors) == 3
        # 源 → C0 → C1 → C2 → 输出 单链
        assert len(graph.edges) == 4
        assert can_project_to_legacy_layers(graph)

    def test_layer_payload_conserved_json_exact(self):
        """载荷守恒按持久化（JSON）形态比较——tuple 在 JSON 中即 list。"""
        layers = f03_layer_stack()
        stack = _stack_with_layers(layers)
        graph = migrate_legacy_stack(stack)
        restored = project_legacy_layers(graph)
        assert len(restored) == 3
        for original, back in zip(
                [l.to_dict(None) for l in layers], restored):
            assert (json.loads(json.dumps(original, default=list))
                    == json.loads(json.dumps(back, default=list))), \
                "layer 载荷 JSON 形态不守恒"

    def test_stable_ids_idempotent(self):
        """同一 layer id 集合多次迁移得到相同节点 UUID（uuid5 稳定映射）。"""
        stack = _stack_with_layers(f03_layer_stack())
        g1 = migrate_legacy_stack(stack)
        g2 = migrate_legacy_stack(stack)
        assert [n.node_id for n in g1.nodes] == [n.node_id for n in g2.nodes]

    def test_rerun_on_migrated_stack_returns_same_graph(self):
        stack = _stack_with_layers(f03_layer_stack())
        graph = migrate_legacy_stack(stack)
        migrated_stack = stack.with_color_graph(graph)
        again = migrate_legacy_stack(migrated_stack)
        assert again.to_dict() == graph.to_dict()  # 幂等（无双权威）

    def test_hidden_layer_maps_to_disabled_node(self):
        layers = f03_layer_stack()
        layers[1].visible = False
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        node = next(n for n in graph.nodes if n.label == "Lift Shadows")
        assert node.enabled is False

    def test_duplicate_layer_names_allowed(self):
        l1, l2 = Layer("Same"), Layer("Same")
        graph = migrate_legacy_stack(_stack_with_layers([l1, l2]))
        assert len([n for n in graph.nodes
                    if n.node_type == NodeType.CORRECTOR.value]) == 2

    def test_duplicate_layer_ids_rejected(self):
        l1 = Layer("A")
        bad = l1.to_dict(None)
        stack = _stack_with_layers([l1])
        data = stack.to_dict()
        data["layers"].append(dict(bad))  # 同 id 复制 = 损坏数据
        stack2 = ColorStackData.from_dict(data)
        with pytest.raises(LegacyMigrationError) as ei:
            migrate_legacy_stack(stack2)
        assert ei.value.code in ("duplicate_node_id", "invalid_graph")

    def test_corrupt_layer_entry_located(self):
        stack = _stack_with_layers([Layer("ok")])
        data = stack.to_dict()
        data["layers"].insert(0, {"not": "a layer", "id": None})
        with pytest.raises(LegacyMigrationError) as ei:
            migrate_legacy_stack(ColorStackData.from_dict(data))
        assert ei.value.layer_index is not None


# ── 特殊语义守恒（§4.1/§4.2） ────────────────────────────────────────────

class TestSemanticsConservation:
    def test_key_source_mode_legacy_raw(self):
        graph = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        for node in graph.nodes:
            if node.node_type == NodeType.CORRECTOR.value:
                assert node.params["key_source_mode"] == "legacy_raw"

    def test_source_layer_id_tracking_only(self):
        layers = f03_layer_stack()
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        corrector = next(n for n in graph.nodes
                         if n.node_type == NodeType.CORRECTOR.value)
        assert corrector.params["source_layer_id"] == layers[0].id
        # 身份是 node UUID，不是 source_layer_id / 数组位置
        assert corrector.node_id != layers[0].id

    def test_bake_state_without_digest_flagged(self):
        layers = f03_layer_stack()
        layers[1].is_baked = True
        layers[1].baked_file_name = "baked_x.exr"
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        node = next(n for n in graph.nodes if n.label == "Lift Shadows")
        bake = node.params["bake_state"]
        assert bake["is_baked"] is True
        assert bake["digest_known"] is False   # 旧 bake 无 digest：兼容 + 诊断
        assert bake["prefix_digest"] is None

    def test_ai_override_semantics_carried(self):
        layers = f03_layer_stack()
        layers[2].plugins.append({
            "instance_id": "i1", "plugin_id": "ai_matte_x",
            "params": {}, "enabled": True, "type": "ai"})
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        node = next(n for n in graph.nodes if n.label == "Tone Curve")
        assert node.params["ai_override"]["replaces_input"] is True
        assert "ai_matte_x" in node.params["ai_override"]["plugin_ids"]

    def test_window_shape_ids_and_keyframes_conserved(self):
        layers = f03_layer_stack()
        shape = MaskShape(MaskShape.CIRCLE)
        layers[0].mask_shapes = [shape]
        from src.features.color.core.grade_keyframes import GradeKeyframeTrack
        track = GradeKeyframeTrack(param_name="exposure")
        track.set_keyframe(0, 0.0)
        track.set_keyframe(10, 1.0)
        layers[0].grade_keyframes["exposure"] = track
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        restored = project_legacy_layers(graph)
        assert restored[0]["mask_shapes"][0]["id"] == shape.id
        assert "exposure" in restored[0]["grade_keyframes"]

    def test_missing_lut_path_carried_as_none(self):
        layers = f03_layer_stack()
        layers[0].active_lut_path = None   # 缺 LUT：neutral 路径
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        restored = project_legacy_layers(graph)
        assert restored[0]["active_lut_path"] is None

    def test_lut_absolute_path_allowed_in_legacy_payload(self):
        """legacy_payload 豁免路径检查（B14 资源闭包统一转工程引用）。"""
        layers = f03_layer_stack()
        layers[0].active_lut_path = "/Users/editor/luts/look.cube"
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        assert graph.is_valid()
        restored = project_legacy_layers(graph)
        assert restored[0]["active_lut_path"] == "/Users/editor/luts/look.cube"

    def test_future_extra_fields_in_layer_dict_survive(self):
        layers = f03_layer_stack()
        payload = layers[0].to_dict(None)
        payload["future_layer_field"] = {"keep": [1, 2]}
        stack = ColorStackData.create(asset_id="a1", name="s")
        stack = stack.with_layers([payload] + [l.to_dict(None) for l in layers[1:]])
        graph = migrate_legacy_stack(stack)
        restored = project_legacy_layers(graph)
        assert restored[0]["future_layer_field"] == {"keep": [1, 2]}


# ── v1 平铺迁移 ─────────────────────────────────────────────────────────

class TestV1FlatMigration:
    def test_flat_params_become_single_corrector(self):
        stack = ColorStackData.create(asset_id="a1", name="v1")
        flat = dict(stack.grading_params)
        flat["exposure"] = 1.5
        stack = stack.with_grading_params(flat)
        graph = migrate_legacy_stack(stack)
        correctors = [n for n in graph.nodes
                      if n.node_type == NodeType.CORRECTOR.value]
        assert len(correctors) == 1
        assert correctors[0].legacy_payload["grading_params"]["exposure"] == 1.5
        assert can_project_to_legacy_layers(graph)

    def test_empty_stack_migrates_to_source_output_only(self):
        stack = ColorStackData.create(asset_id="a1", name="empty")
        graph = migrate_legacy_stack(stack)
        assert graph.is_valid()
        assert can_project_to_legacy_layers(graph)
        assert project_legacy_layers(graph) == []

    def test_curves_and_lut_carried(self):
        stack = ColorStackData.create(asset_id="a1", name="v1c")
        stack = stack.with_curves({"master": [(0.0, 0.1), (1.0, 0.9)]})
        stack = stack.with_lut("assets/luts/look.cube", 0.8)
        graph = migrate_legacy_stack(stack)
        projected = project_legacy_layers(graph)[0]
        assert projected["curves_data"]["master"] == [[0.0, 0.1], [1.0, 0.9]]
        assert projected["active_lut_path"] == "assets/luts/look.cube"
        assert projected["lut_intensity"] == 0.8


# ── 严格反向投影 ────────────────────────────────────────────────────────

class TestStrictBackProjection:
    def test_new_serial_without_legacy_payload_has_stable_layer_identity(self):
        from src.features.color.core.color_graph_service import ColorGraphService

        base = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        first = next(n.node_id for n in base.nodes
                     if n.node_type == NodeType.CORRECTOR.value)
        result = ColorGraphService(id_factory=lambda: "new-serial").op_insert_corrector(
            base, first)
        assert result.ok
        assert can_project_to_legacy_layers(result.graph)
        first_view = project_legacy_layers(result.graph)
        second_view = project_legacy_layers(result.graph)
        assert [layer["id"] for layer in first_view] == [
            layer["id"] for layer in second_view]
        assert "new-serial" in [layer["id"] for layer in first_view]

    def _graph_with_extra(self, node: ColorNodeData, edge: ColorEdgeData | None = None):
        base = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        nodes = tuple(base.nodes) + (node,)
        edges = tuple(base.edges) + ((edge,) if edge else ())
        return ColorGraphData(
            nodes=nodes, edges=edges,
            source_node_id=base.source_node_id,
            output_node_id=base.output_node_id)

    def test_mixer_makes_graph_unprojectable(self):
        mixer = ColorNodeData(node_id=str(uuid.uuid4()),
                              node_type=NodeType.PARALLEL_MIXER.value)
        graph = self._graph_with_extra(mixer)
        assert can_project_to_legacy_layers(graph) is False
        with pytest.raises(LegacyMigrationError):
            project_legacy_layers(graph)

    def test_key_edge_makes_graph_unprojectable(self):
        base = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        cs = [n for n in base.nodes if n.node_type == NodeType.CORRECTOR.value]
        key_edge = ColorEdgeData(
            edge_id=str(uuid.uuid4()),
            src_node=cs[0].node_id, src_port="key_out",
            dst_node=cs[1].node_id, dst_port="key_in")
        graph = ColorGraphData(
            nodes=base.nodes, edges=tuple(base.edges) + (key_edge,),
            source_node_id=base.source_node_id,
            output_node_id=base.output_node_id)
        assert graph.is_valid()
        assert can_project_to_legacy_layers(graph) is False

    def test_branch_makes_graph_unprojectable(self):
        base = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        branch = ColorNodeData(node_id=str(uuid.uuid4()),
                               node_type=NodeType.CORRECTOR.value,
                               legacy_payload={"id": str(uuid.uuid4()),
                                               "name": "B", "visible": True})
        branch_edge = ColorEdgeData(
            edge_id=str(uuid.uuid4()),
            src_node=base.source_node_id, src_port="image",
            dst_node=branch.node_id, dst_port="image")
        graph = ColorGraphData(
            nodes=tuple(base.nodes) + (branch,),
            edges=tuple(base.edges) + (branch_edge,),
            source_node_id=base.source_node_id,
            output_node_id=base.output_node_id)
        assert graph.is_valid()
        assert can_project_to_legacy_layers(graph) is False

    def test_unknown_node_type_unprojectable(self):
        mystery = ColorNodeData(node_id=str(uuid.uuid4()),
                                node_type="future_fx")
        graph = self._graph_with_extra(mystery)
        assert can_project_to_legacy_layers(graph) is False

    def test_invalid_graph_unprojectable(self):
        assert can_project_to_legacy_layers(ColorGraphData()) is False


# ── E07 加载优先级（B03.3） ─────────────────────────────────────────────

class TestHydratorGraphPriority:
    def _hydrator(self):
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        return ColorStackHydrator()

    def test_projectable_graph_rebuilds_engine_layers(self, image_engine):
        stack = _stack_with_layers(f03_layer_stack())
        graph = migrate_legacy_stack(stack)
        engine = image_engine
        self._hydrator().apply_stack_to_engine(
            stack.with_color_graph(graph), engine)
        assert [l.name for l in engine.layers] == [
            "Print Warmer", "Lift Shadows", "Tone Curve"]
        # 图是权威：图侧隐藏层投影后 engine 同步 disabled
        assert engine.layers[0].grading_params["exposure"] == 0.5

    def test_corrupt_graph_raises_not_silent_fallback(self, image_engine):
        stack = _stack_with_layers(f03_layer_stack())
        engine = image_engine
        before = [l.id for l in engine.layers]
        bad = migrate_legacy_stack(stack)
        # 构造结构损坏图：边引用不存在的节点（validate → node_not_found）
        broken = ColorGraphData(
            nodes=bad.nodes,
            edges=tuple(bad.edges) + (ColorEdgeData(
                edge_id=str(uuid.uuid4()),
                src_node=bad.source_node_id, src_port="image",
                dst_node="ghost-node", dst_port="image"),),
            source_node_id=bad.source_node_id,
            output_node_id=bad.output_node_id)
        with pytest.raises(GraphSchemaError):
            self._hydrator().apply_stack_to_engine(
                stack.with_color_graph(broken), engine)
        assert [l.id for l in engine.layers] == before  # 不半应用

    def test_complex_graph_leaves_legacy_layers_untouched(self, image_engine):
        """复杂 DAG：不从隐藏 layers 恢复（双权威禁止），诊断后跳过。"""
        stack = _stack_with_layers(f03_layer_stack())
        engine = image_engine
        engine.layers = [Layer("Keep Me")]
        before = [l.id for l in engine.layers]
        base = migrate_legacy_stack(stack)
        mixer = ColorNodeData(node_id=str(uuid.uuid4()),
                              node_type=NodeType.PARALLEL_MIXER.value)
        graph = ColorGraphData(
            nodes=tuple(base.nodes) + (mixer,), edges=base.edges,
            source_node_id=base.source_node_id,
            output_node_id=base.output_node_id)
        assert not can_project_to_legacy_layers(graph)
        self._hydrator().apply_stack_to_engine(
            stack.with_color_graph(graph), engine)
        assert [l.id for l in engine.layers] == before

    def test_graph_priority_over_stale_legacy_layers(self, image_engine):
        """图优先：stale layers 不得覆盖图派生视图。"""
        layers = f03_layer_stack()
        layers[0].grading_params["exposure"] = 2.5
        stack = _stack_with_layers(layers)
        graph = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        engine = image_engine
        self._hydrator().apply_stack_to_engine(
            stack.with_color_graph(graph), engine)
        assert engine.layers[0].grading_params["exposure"] == 0.5  # 图赢

    def test_legacy_path_unchanged_without_graph(self, image_engine):
        """无图栈仍走原 v2/v1 路径（回归保护）。"""
        engine = image_engine
        self._hydrator().apply_stack_to_engine(
            _stack_with_layers(f03_layer_stack()), engine)
        assert len(engine.layers) == 3


# ── 迁移 ↔ B00 像素等价（载荷守恒 ⇒ 渲染等价） ─────────────────────────

class TestMigrationPixelEquivalence:
    def test_layer_render_data_identical_after_roundtrip(self, image_engine):
        """迁移→投影→Layer 对象 的渲染 DTO 与原层一致（B00 等价的 CPU 侧证明）。"""
        from src.features.color.core.image_engine import _layer_render_data
        layers = f03_layer_stack()
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        restored_layers = [
            Layer.from_dict(p) for p in project_legacy_layers(graph)]

        # curves_lut/hsl_curve_lut/warper 是渲染缓存（由 curves_data 等
        # 持久化字段在引擎侧重建，现有 legacy save/load 同语义）——DTO
        # 等价断言排除缓存字段，显式断言其持久化来源守恒。
        CACHE_FIELDS = ("curves_lut", "hsl_curve_lut", "warper_params")

        def dto_equal(a: dict, b: dict) -> bool:
            if set(a) != set(b):
                return False
            for key in a:
                va, vb = a[key], b[key]
                if key in CACHE_FIELDS:
                    continue
                if isinstance(va, np.ndarray) or isinstance(vb, np.ndarray):
                    if not np.array_equal(va, vb):
                        return False
                elif va != vb:
                    return False
            return True

        for original, restored in zip(layers, restored_layers):
            assert dto_equal(_layer_render_data(original),
                             _layer_render_data(restored))
        # 持久化来源守恒：curves_data 原样带回
        assert (json.loads(json.dumps(layers[2].curves_data, default=list))
                == json.loads(json.dumps(restored_layers[2].curves_data,
                                         default=list)))

    def test_graph_survives_stack_roundtrip(self):
        stack = _stack_with_layers(f03_layer_stack())
        graph = migrate_legacy_stack(stack)
        data = stack.with_color_graph(graph).to_dict()
        restored_stack = ColorStackData.from_dict(data)
        assert restored_stack.color_graph is not None
        assert restored_stack.color_graph.to_dict() == graph.to_dict()
