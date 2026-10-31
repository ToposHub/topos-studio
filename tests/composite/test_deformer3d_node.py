"""Deformer3D 节点单测（master_plan §16.9 显式节点链路）。

覆盖：
1. 节点注册（NODE_REGISTRY / NODE_CATEGORIES / capability）。
2. 端口定义（in:scene 输入 + display:scene 输出）。
3. 参数定义（deformer_type/enable/strength/axis/angle/amount/center/noise）。
4. 无上游时 emit 空 ModelPayload（不崩）。
5. 串联数据流：Model3D → Deformer3D 的 scene payload 传递（eval_ctx 旁路）。
6. 变形后 emit 新 ModelPayload（flat_meshes 含变形后 mesh）。
7. transform_sig 含变形参数（Scene3D 据此重渲染）。
8. 变形缓存：参数不变时复用（_deform_sig 命中）。
9. cleanup 释放 GL 资源。

注：真实 GL 渲染需 ModernGL context；本测试用 mock ctx 验证数据流和变形逻辑，
不触及 GL 对象创建（变形算法本身是纯 numpy，已有 test_3d_deformers.py 覆盖）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES
from src.features.composite.core.node_capabilities import (
    get_node_capability, is_source_node_type)
from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.render3d.scene_payload import ModelPayload
from src.features.composite.render3d import primitives as prim

pytestmark = pytest.mark.composite_core


# ── 注册 ─────────────────────────────────────────────────────────────

class TestDeformer3DRegistration:
    def test_in_registry(self):
        assert "deformer3d" in NODE_REGISTRY

    def test_in_3d_category(self):
        types = {t for t, _ in NODE_CATEGORIES["3D"]}
        assert "deformer3d" in types

    def test_capability(self):
        cap = get_node_capability("deformer3d")
        assert cap.category == "3D"
        assert cap.requires_gl is True
        assert cap.owns_color_texture is False

    def test_is_source_node(self):
        """Deformer3D 走 source 轨道（emit scene payload）。"""
        assert is_source_node_type("deformer3d")


# ── 端口 + 参数 ──────────────────────────────────────────────────────

class TestDeformer3DPorts:
    def _make(self):
        return NODE_REGISTRY["deformer3d"]("d1")

    def test_input_port_is_scene_in(self):
        """输入端口名必须是 'in'（base_evaluator _scene_ports 据此登记连接）。"""
        n = self._make()
        assert len(n.inputs) == 1
        assert n.inputs[0].name == "in"
        assert n.inputs[0].data_type == "scene"

    def test_output_port_is_scene_display(self):
        n = self._make()
        assert len(n.outputs) == 1
        assert n.outputs[0].name == "display"
        assert n.outputs[0].data_type == "scene"

    def test_parameters_complete(self):
        n = self._make()
        assert "deformer_type" in n.parameters
        assert "enabled" in n.parameters
        assert "strength" in n.parameters
        assert "axis" in n.parameters
        assert "angle" in n.parameters
        assert "center_x" in n.parameters
        # 默认值
        assert n.parameters["deformer_type"] == "bend"
        assert n.parameters["strength"] == 1.0

    def test_param_defs_exist(self):
        """PARAM_DEFS 应覆盖所有参数（UI 自动生成用）。"""
        n = self._make()
        param_names = {pd.name for pd in n.PARAM_DEFS}
        for key in n.parameters:
            assert key in param_names, f"参数 {key} 缺少 ParamDef"


# ── 无上游降级 ───────────────────────────────────────────────────────

class TestNoUpstream:
    def test_no_upstream_emits_empty(self):
        """无上游连接时 emit has_model=False（不崩）。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        ctx.scene_connections = {}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.evaluate({}, ctx, None)
        p = ctx.scene_payloads["d1"]["display"]
        assert isinstance(p, ModelPayload)
        assert p.has_model is False

    def test_upstream_no_model_emits_empty(self):
        """上游有连接但无模型 → 透传 has_model=False。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={
            "m1": {"display": ModelPayload(has_model=False)},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.evaluate({}, ctx, None)
        p = ctx.scene_payloads["d1"]["display"]
        assert p.has_model is False


# ── 变形参数签名 ─────────────────────────────────────────────────────

class TestDeformerParamSignature:
    def test_param_sig_changes_with_strength(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        sig1 = n._deformer_param_sig()
        n.parameters["strength"] = 2.0
        sig2 = n._deformer_param_sig()
        assert sig1 != sig2

    def test_param_sig_changes_with_type(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        sig1 = n._deformer_param_sig()
        n.parameters["deformer_type"] = "twist"
        sig2 = n._deformer_param_sig()
        assert sig1 != sig2

    def test_param_sig_stable_same_params(self):
        n1 = NODE_REGISTRY["deformer3d"]("d1")
        n2 = NODE_REGISTRY["deformer3d"]("d2")
        assert n1._deformer_param_sig() == n2._deformer_param_sig()


# ── DeformerSpec 构建 ────────────────────────────────────────────────

class TestBuildDeformerSpec:
    def test_bend_spec(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["deformer_type"] = "bend"
        n.parameters["angle"] = 60.0
        spec = n._build_deformer_spec()
        assert spec.deformer_type == "bend"
        assert spec.params["angle"] == 60.0

    def test_twist_spec(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["deformer_type"] = "twist"
        spec = n._build_deformer_spec()
        assert spec.deformer_type == "twist"
        assert "angle" in spec.params

    def test_taper_spec(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["deformer_type"] = "taper"
        n.parameters["amount"] = 2.0
        spec = n._build_deformer_spec()
        assert spec.deformer_type == "taper"
        assert spec.params["amount"] == 2.0

    def test_noise_spec(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["deformer_type"] = "noise"
        n.parameters["frequency"] = 5.0
        spec = n._build_deformer_spec()
        assert spec.deformer_type == "noise"
        assert spec.params["frequency"] == 5.0

    def test_center_from_params(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["center_x"] = 1.0
        n.parameters["center_y"] = 2.0
        n.parameters["center_z"] = 3.0
        spec = n._build_deformer_spec()
        assert np.allclose(spec.center, [1, 2, 3])

    def test_axis_param(self):
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["axis"] = "x"
        spec = n._build_deformer_spec()
        assert spec.axis == "x"


# ── 串联数据流（纯 numpy，mock GL）────────────────────────────────────

class _MockBuffer:
    def __init__(self):
        self.released = False
    def release(self):
        self.released = True


class _MockVAO:
    def __init__(self):
        self.released = False
    def release(self):
        self.released = True


class _MockProgram:
    def release(self):
        pass


class _MockGLCtx:
    """最小化 mock：buffer()/vertex_array() 返回占位对象。"""
    def __init__(self):
        self._buffers = []
        self._vaos = []
    def buffer(self, data):
        b = _MockBuffer()
        self._buffers.append(b)
        return b
    def vertex_array(self, prog, contents, index_buffer=None):
        v = _MockVAO()
        self._vaos.append(v)
        return v
    def program(self, vertex_shader="", fragment_shader=""):
        return _MockProgram()


class TestScenePayloadChain:
    """验证 Model3D → Deformer3D 的 scene payload 传递（mock GL）。"""

    def _make_upstream_payload(self):
        """构造一个含真实 MeshData 的上游 ModelPayload（flat_meshes）。"""
        cube = prim.cube(1.0)
        return ModelPayload(
            flat_meshes=[{
                'mesh': cube,
                'index_count': int(cube.indices.shape[0]),
                'is_skinned': False,
                'has_tangent': False,
                'can_instance': False,
                'world_matrix': np.eye(4, dtype=np.float32),
                'material': None,
                'skin': None,
                'node_name': 'body',
            }],
            has_model=True,
            model_path="/test.glb",
            model_mtime=12345.0,
            transform_sig=("upstream_sig",),
        )

    def test_collect_upstream_payload(self):
        """Deformer3D 能从 eval_ctx 旁路收集上游 ModelPayload。"""
        upstream = self._make_upstream_payload()
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.evaluate({}, ctx, None)
        # Deformer3D 应 emit 变形后的 payload
        p = ctx.scene_payloads["d1"]["display"]
        assert isinstance(p, ModelPayload)
        assert p.has_model is True
        assert len(p.flat_meshes) == 1

    def test_deformed_mesh_differs_from_source(self):
        """变形后的 mesh 顶点应与源 mesh 不同（strength>0 的 bend）。"""
        upstream = self._make_upstream_payload()
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["deformer_type"] = "bend"
        n.parameters["strength"] = 1.0
        n.parameters["angle"] = 90.0
        n.evaluate({}, ctx, None)
        p = ctx.scene_payloads["d1"]["display"]
        deformed_mesh = p.flat_meshes[0]['mesh']
        source_mesh = upstream.flat_meshes[0]['mesh']
        assert not np.allclose(deformed_mesh.vertices, source_mesh.vertices)

    def test_transform_sig_includes_deform_params(self):
        """emit 的 ModelPayload.transform_sig 应含变形参数（Scene3D 重渲染用）。"""
        upstream = self._make_upstream_payload()
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["strength"] = 2.0
        n.evaluate({}, ctx, None)
        p = ctx.scene_payloads["d1"]["display"]
        # transform_sig 应包含上游 sig + 变形参数（含 strength=2.0）
        assert "upstream_sig" in p.transform_sig
        assert 2.0 in p.transform_sig

    def test_disabled_deformer_passes_through(self):
        """enabled=False 时仍 emit 有效 payload（透传，不变形）。"""
        upstream = self._make_upstream_payload()
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["enabled"] = False
        n.evaluate({}, ctx, None)
        p = ctx.scene_payloads["d1"]["display"]
        assert p.has_model is True  # 仍 emit 有效模型

    def test_preserves_material_and_world_matrix(self):
        """变形后 flat_mesh 应保留上游的 material/world_matrix/node_name。"""
        from src.features.composite.render3d import scene_graph as sg
        cube = prim.cube(1.0)
        mat = sg.MaterialData()
        world = np.eye(4, dtype=np.float32) * 2.0
        world[3, 3] = 1.0
        upstream = ModelPayload(
            flat_meshes=[{
                'mesh': cube,
                'index_count': int(cube.indices.shape[0]),
                'is_skinned': False, 'has_tangent': False,
                'can_instance': False,
                'world_matrix': world,
                'material': mat, 'skin': None, 'node_name': 'part0',
                'node_id': 'node-part0', 'material_id': 'mat-part0',
            }],
            has_model=True,
        )
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.evaluate({}, ctx, None)
        p = ctx.scene_payloads["d1"]["display"]
        fm = p.flat_meshes[0]
        assert fm['node_name'] == 'part0'
        assert fm['node_id'] == 'node-part0'
        assert fm['material_id'] == 'mat-part0'
        assert fm['material'] is mat
        assert np.allclose(fm['world_matrix'], world)

    def test_skinned_mesh_is_posed_then_deformed(self):
        """蒙皮 mesh 应先应用动画姿态，再进入 DeformerStack。"""
        from src.features.composite.render3d.scene_graph import SkinData

        vertices = np.array([
            [0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 2.0, 0.0],
        ], dtype=np.float32)
        mesh = prim.MeshData(
            vertices=vertices,
            normals=np.tile([0.0, 1.0, 0.0], (3, 1)),
            uvs=np.zeros((3, 2), dtype=np.float32),
            indices=np.array([0, 1, 2], dtype=np.uint32),
            bone_indices=np.zeros((3, 4), dtype=np.uint32),
            bone_weights=np.column_stack([
                np.ones(3, dtype=np.float32),
                np.zeros((3, 3), dtype=np.float32),
            ]),
        )
        animated_root = np.eye(4, dtype=np.float32)
        animated_root[0, 3] = 1.0
        skin = SkinData(
            bone_names=["root"],
            inverse_bind_matrices=np.eye(4, dtype=np.float32)[None, ...],
            bone_indices=mesh.bone_indices,
            bone_weights=mesh.bone_weights,
        )
        upstream = ModelPayload(
            flat_meshes=[{
                'mesh': mesh,
                'index_count': 3,
                'is_skinned': True,
                'has_tangent': False,
                'can_instance': False,
                'world_matrix': np.eye(4, dtype=np.float32),
                'material': None,
                'skin': skin,
                'node_name': 'body',
            }],
            anim_worlds={"root": animated_root},
            has_model=True,
        )
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters.update({
            "deformer_type": "stretch",
            "amount": 2.0,
            "axis": "y",
        })

        n.evaluate({}, ctx, None)

        payload = ctx.scene_payloads["d1"]["display"]
        result = payload.flat_meshes[0]
        assert result["is_skinned"] is False
        assert result["skin"] is None
        assert payload.anim_worlds == {}
        # The x translation comes from skinning; the y scale comes from the
        # deformer. This catches both skipped skinning and wrong operation order.
        assert np.allclose(result["mesh"].vertices[:, 0], 1.0 / np.sqrt(2.0))
        assert np.allclose(result["mesh"].vertices[:, 1], [0.0, 2.0, 4.0])
        assert "deformer.skinned_passthrough" not in payload.diagnostics

    def test_preserves_model_metadata_after_cpu_skinning(self):
        """Deformer 链路不得丢失材质编辑器和动画面板需要的 payload 元数据。"""
        from src.features.composite.render3d.scene_graph import SkinData

        mesh = prim.MeshData(
            vertices=np.zeros((3, 3), dtype=np.float32),
            normals=np.tile([0.0, 0.0, 1.0], (3, 1)),
            uvs=np.zeros((3, 2), dtype=np.float32),
            indices=np.array([0, 1, 2], dtype=np.uint32),
            bone_indices=np.zeros((3, 4), dtype=np.uint32),
            bone_weights=np.column_stack([
                np.ones(3, dtype=np.float32),
                np.zeros((3, 3), dtype=np.float32),
            ]),
        )
        skin = SkinData(
            bone_names=["root"],
            inverse_bind_matrices=np.eye(4, dtype=np.float32)[None, ...],
            bone_indices=mesh.bone_indices,
            bone_weights=mesh.bone_weights,
        )
        animation_clips = (("walk", 1.5),)
        import_report = {"format": "glb", "skin_count": 1}
        upstream = ModelPayload(
            flat_meshes=[{
                "mesh": mesh, "index_count": 3, "is_skinned": True,
                "has_tangent": False, "can_instance": False,
                "world_matrix": np.eye(4, dtype=np.float32),
                "material": None, "skin": skin, "node_name": "body",
                "node_id": "node-body", "material_id": "body",
            }],
            anim_worlds={"root": np.eye(4, dtype=np.float32)},
            animation_clips=animation_clips,
            import_report=import_report,
            material_overrides={"body": {"target_material_id": "body"}},
            materials=({"material_id": "body", "name": "Body"},),
            node_id="model-1",
            has_model=True,
        )
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.evaluate({}, ctx, None)

        payload = ctx.scene_payloads["d1"]["display"]
        assert payload.material_overrides == upstream.material_overrides
        assert payload.materials == upstream.materials
        assert payload.animation_clips == animation_clips
        assert payload.import_report == import_report
        # _collect_connected_scene_payloads stamps the real upstream node id
        # (m1) at the graph boundary; Deformer3D must preserve that stamp.
        assert payload.node_id == "m1"


# ── 缓存 ─────────────────────────────────────────────────────────────

class TestDeformerCache:
    def test_cache_reuses_on_same_params(self):
        """参数不变时复用变形结果（_deform_sig 命中）。"""
        upstream = ModelPayload(
            flat_meshes=[{
                'mesh': prim.cube(1.0),
                'index_count': 36, 'is_skinned': False,
                'has_tangent': False, 'can_instance': False,
                'world_matrix': np.eye(4, dtype=np.float32),
                'material': None, 'skin': None, 'node_name': 'm',
            }],
            has_model=True,
        )
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        # 第一次求值：变形 + 缓存
        n.evaluate({}, ctx, None)
        first_meshes = n._deformed_meshes
        assert len(first_meshes) == 1
        first_sig = n._deform_sig
        # 第二次求值（参数不变）：应命中缓存，复用同一列表对象
        n.evaluate({}, ctx, None)
        assert n._deformed_meshes is first_meshes  # 同一对象（缓存命中）
        assert n._deform_sig == first_sig

    def test_cache_invalidates_on_param_change(self):
        """参数变化时重新变形（_deform_sig 不匹配）。"""
        upstream = ModelPayload(
            flat_meshes=[{
                'mesh': prim.cube(1.0),
                'index_count': 36, 'is_skinned': False,
                'has_tangent': False, 'can_instance': False,
                'world_matrix': np.eye(4, dtype=np.float32),
                'material': None, 'skin': None, 'node_name': 'm',
            }],
            has_model=True,
        )
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.parameters["strength"] = 1.0
        n.evaluate({}, ctx, None)
        first_sig = n._deform_sig
        # 改参数
        n.parameters["strength"] = 2.0
        n.evaluate({}, ctx, None)
        assert n._deform_sig != first_sig  # 签名变了 → 重新变形


# ── cleanup ──────────────────────────────────────────────────────────

class TestDeformerCleanup:
    def test_cleanup_releases_meshes(self):
        """cleanup 应清空 _deformed_meshes。"""
        upstream = ModelPayload(
            flat_meshes=[{
                'mesh': prim.cube(1.0),
                'index_count': 36, 'is_skinned': False,
                'has_tangent': False, 'can_instance': False,
                'world_matrix': np.eye(4, dtype=np.float32),
                'material': None, 'skin': None, 'node_name': 'm',
            }],
            has_model=True,
        )
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.evaluate({}, ctx, None)
        assert len(n._deformed_meshes) == 1
        n.cleanup()
        assert n._deformed_meshes == []
        assert n._deform_sig == ()

    def test_cleanup_caches_and_uses_gl_ctx(self):
        """cleanup 用 evaluate 时缓存的 _last_gl_ctx 释放 VBO 池引用（回归 MODERATE）。

        之前 cleanup() 无 ctx → pool.release 永不调用 → 节点删除时池引用泄漏。
        """
        upstream = ModelPayload(
            flat_meshes=[{
                'mesh': prim.cube(1.0),
                'index_count': 36, 'is_skinned': False,
                'has_tangent': False, 'can_instance': False,
                'world_matrix': np.eye(4, dtype=np.float32),
                'material': None, 'skin': None, 'node_name': 'm',
            }],
            has_model=True,
        )
        ctx = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": upstream},
        })
        ctx.scene_connections = {"d1": {"in": ["m1"]}}
        n = NODE_REGISTRY["deformer3d"]("d1")
        n.evaluate({}, ctx, None)
        # evaluate 后应缓存了 gl_ctx
        assert n._last_gl_ctx is not None
        # 记录 vbo_hash（cleanup 需用它调 pool.release）
        vbo_hash = n._deformed_meshes[0].get('vbo_hash')
        assert vbo_hash is not None
        n.cleanup()
        # cleanup 后 _last_gl_ctx 被清空（防悬空引用）
        assert n._last_gl_ctx is None


# ── scene_input_sig cache key 联动（回归：调参无反应根因）─────────────

class TestSceneInputSigCacheKey:
    """回归测试：Deformer3D 调参后 Scene3D cache key 必须变化。

    根因：evaluator 的 _collect_scene_input_sig 最初用 NodePlan(np) 调用
    _collect_connected_scene_payloads（Node 基类方法），但 NodePlan 没有此方法
    → 抛异常被 try/except 吞掉 → scene_input_sig 恒为空 → Scene3D cache key
    不含上游签名 → 参数变化时 cache hit → _do_evaluate 不执行 → 无反应。

    修复：用节点实例（self._render_nodes.get(node_id)）而非 NodePlan。
    """

    def _make_eval_and_scene(self, upstream_payload):
        """构造 evaluator-like 对象 + Scene3D 实例 + 填充 scene_payloads。"""
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        scene3d = NODE_REGISTRY["scene3d"]("s1")
        ctx = CompositeEvalContext(gl="fake", scene_payloads={
            "d1": {"display": upstream_payload},
        })
        ctx.scene_connections = {"s1": {"in": ["d1"]}}
        ev = BaseEvaluatorMixin.__new__(BaseEvaluatorMixin)
        ev._eval_ctx = ctx
        return ev, scene3d

    def test_sig_non_empty_with_upstream(self):
        """有上游 payload 时 scene_input_sig 应非空。"""
        upstream = ModelPayload(
            flat_meshes=[{'mesh': prim.cube(1.0), 'index_count': 36,
                          'is_skinned': False, 'has_tangent': False,
                          'can_instance': False,
                          'world_matrix': np.eye(4, dtype=np.float32),
                          'material': None, 'skin': None, 'node_name': 'm'}],
            has_model=True, model_path='/test.glb',
        )
        ev, scene3d = self._make_eval_and_scene(upstream)
        sig = ev._collect_scene_input_sig(scene3d)
        assert sig != "", "scene_input_sig 为空——_collect_scene_input_sig 没收集到上游 payload"

    def test_sig_changes_with_deformer_params(self):
        """Deformer3D 参数变化时 scene_input_sig 应不同（cache key 失效）。"""
        # strength=1.0 的上游 payload
        cube = prim.cube(1.0)
        d = NODE_REGISTRY["deformer3d"]("d1")
        d.parameters["strength"] = 1.0
        d.parameters["angle"] = 90.0
        ctx1 = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": ModelPayload(
                flat_meshes=[{'mesh': cube, 'index_count': 36, 'is_skinned': False,
                              'has_tangent': False, 'can_instance': False,
                              'world_matrix': np.eye(4, dtype=np.float32),
                              'material': None, 'skin': None, 'node_name': 'm'}],
                has_model=True, model_path='/test.glb')},
        })
        ctx1.scene_connections = {"d1": {"in": ["m1"]}, "s1": {"in": ["d1"]}}
        d.evaluate({}, ctx1, None)
        upstream1 = ctx1.scene_payloads["d1"]["display"]

        # strength=2.0 的上游 payload
        d2 = NODE_REGISTRY["deformer3d"]("d1")
        d2.parameters["strength"] = 2.0
        d2.parameters["angle"] = 90.0
        ctx2 = CompositeEvalContext(gl=_MockGLCtx(), scene_payloads={
            "m1": {"display": ModelPayload(
                flat_meshes=[{'mesh': cube, 'index_count': 36, 'is_skinned': False,
                              'has_tangent': False, 'can_instance': False,
                              'world_matrix': np.eye(4, dtype=np.float32),
                              'material': None, 'skin': None, 'node_name': 'm'}],
                has_model=True, model_path='/test.glb')},
        })
        ctx2.scene_connections = {"d1": {"in": ["m1"]}, "s1": {"in": ["d1"]}}
        d2.evaluate({}, ctx2, None)
        upstream2 = ctx2.scene_payloads["d1"]["display"]

        ev1, scene1 = self._make_eval_and_scene(upstream1)
        ev2, scene2 = self._make_eval_and_scene(upstream2)
        sig1 = ev1._collect_scene_input_sig(scene1)
        sig2 = ev2._collect_scene_input_sig(scene2)
        assert sig1 != sig2, "strength 变化但 scene_input_sig 没变——cache key 不会失效"
