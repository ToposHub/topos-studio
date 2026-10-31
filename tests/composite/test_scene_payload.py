"""阶段七：Scene3D 节点拆分 — scene payload 旁路 + payload 构造单测。

覆盖：
- 双轨制旁路（_emit_scene_payload / _collect_scene_payload_by_type）
- CameraPayload 构造 + 与 Scene3D 数学 parity
- LightPayload legacy fallback（light0_intensity==0 → 旧主灯）+ 多灯启用/禁用
- ModelPayload 降级（无路径 / 不存在文件 → has_model=False）
- 4 节点注册 + scene 端口 + capability 表一致性
- 向后兼容（ctx 无 scene_payloads 时 emit/collect no-op）

注：Geo3D/Scene3D 真实渲染需要 GL context，本测试用 headless 环境验证数据流，
不触及 GL。端到端 GL 渲染由现有 scene3d_node 集成覆盖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES
from src.features.composite.core.node_capabilities import (
    get_node_capability, is_source_node_type)
from src.features.composite.render3d.scene_payload import (
    CameraPayload, LightPayload, GeometryPayload, ModelPayload, empty_light_payload)
from src.features.composite.render3d import math_utils as m3d


# ── 注册 + 端口 ──────────────────────────────────────────────────────────

class TestNodeRegistration:
    """4 个 scene 数据源节点 + Scene3D 聚合器的注册完整性。"""

    def test_all_3d_nodes_in_category(self):
        """3D 分类含 8 个节点：5 个 scene 源 + deformer3d 中间变形器 +
        Phase 2 的 volume3d / pointcloud3d（B1/B2）。"""
        types = {t for t, _ in NODE_CATEGORIES["3D"]}
        assert types == {"scene3d", "camera3d", "light3d", "geo3d",
                         "model3d", "deformer3d",
                         "volume3d", "pointcloud3d"}

    def test_all_registered_as_source(self):
        for nt in ("scene3d", "camera3d", "light3d", "geo3d", "model3d"):
            assert is_source_node_type(nt), f"{nt} should be source"

    def test_scene_source_nodes_output_scene_port(self):
        for nt in ("camera3d", "light3d", "geo3d", "model3d"):
            n = NODE_REGISTRY[nt]("x")
            assert len(n.outputs) == 1
            assert n.outputs[0].data_type == "scene", f"{nt} output not scene"
            assert n.outputs[0].name == "display"

    def test_scene3d_has_single_scene_input(self):
        """Scene3D 使用单一 in scene 输入端口。"""
        n = NODE_REGISTRY["scene3d"]("x")
        assert len(n.inputs) == 1
        in_types = {p.data_type for p in n.inputs}
        assert in_types == {"scene"}, f"Scene3D inputs should all be scene, got {in_types}"
        in_names = {p.name for p in n.inputs}
        assert in_names == {"in"}

    def test_capabilities_own_color_false(self):
        """所有 3D 源节点 owns_color_texture=False（避免 CacheManager 双重释放）。"""
        for nt in ("scene3d", "camera3d", "light3d", "geo3d", "model3d"):
            cap = get_node_capability(nt)
            assert cap.owns_color_texture is False, f"{nt} must own its color texture"


# ── 双轨制旁路 ────────────────────────────────────────────────────────────

class TestBypassMechanism:
    """_emit_scene_payload / _collect_scene_payload_by_type 旁路。"""

    def test_emit_and_collect_roundtrip(self):
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        cam = NODE_REGISTRY["camera3d"]("cam1")
        cam.parameters["cam_fov"] = 60.0
        cam.evaluate({}, ctx, None)

        agg = NODE_REGISTRY["scene3d"]("agg")
        collected = agg._collect_scene_payload_by_type(ctx, CameraPayload)
        assert isinstance(collected, CameraPayload)
        assert collected.fov == 60.0

    def test_collect_returns_none_when_empty(self):
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        agg = NODE_REGISTRY["scene3d"]("agg")
        assert agg._collect_scene_payload_by_type(ctx, CameraPayload) is None

    def test_collect_returns_none_when_payload_is_none(self):
        """占位 payload（None）不应被 collect 当作有效数据。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={"x": {"display": None}})
        agg = NODE_REGISTRY["scene3d"]("agg")
        assert agg._collect_scene_payload_by_type(ctx, CameraPayload) is None

    def test_no_scene_payloads_backward_compat(self):
        """ctx.scene_payloads=None（旧测试环境）时 emit/collect 静默 no-op。"""
        ctx = CompositeEvalContext(gl="fake")  # scene_payloads=None
        assert ctx.scene_payloads is None
        cam = NODE_REGISTRY["camera3d"]("cam1")
        # emit 应不抛异常
        cam.evaluate({}, ctx, None)
        agg = NODE_REGISTRY["scene3d"]("agg")
        assert agg._collect_scene_payload_by_type(ctx, CameraPayload) is None

    def test_collect_by_type_is_type_specific(self):
        """按 payload dataclass 类型匹配，不串扰。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={
            "cam": {"display": CameraPayload(proj=np.eye(4), view=np.eye(4),
                                             eye=(0, 0, 5), target=(0, 0, 0))},
            "model": {"display": ModelPayload(has_model=True)},
        })
        agg = NODE_REGISTRY["scene3d"]("agg")
        cam = agg._collect_scene_payload_by_type(ctx, CameraPayload)
        model = agg._collect_scene_payload_by_type(ctx, ModelPayload)
        light = agg._collect_scene_payload_by_type(ctx, LightPayload)
        assert isinstance(cam, CameraPayload)
        assert isinstance(model, ModelPayload)
        assert light is None  # no LightPayload in bypass

    def test_connected_collect_does_not_scan_unconnected_camera(self):
        """端口感知启用后，未连接 Camera3D 不能影响 Scene3D。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={
            "cam": {"display": CameraPayload(proj=np.eye(4), view=np.eye(4),
                                             eye=(0, 0, 5), target=(0, 0, 0))},
        })
        ctx.scene_connections = {"agg": {}}
        agg = NODE_REGISTRY["scene3d"]("agg")

        assert agg._collect_connected_scene_payloads(ctx, CameraPayload) == []

    def test_connected_collect_only_uses_declared_scene_inputs(self):
        """端口感知收集只接受连接映射中声明的上游节点。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={
            "cam1": {"display": CameraPayload(proj=np.eye(4), view=np.eye(4),
                                              eye=(0, 0, 5), target=(0, 0, 0))},
            "cam2": {"display": CameraPayload(proj=np.eye(4), view=np.eye(4),
                                              eye=(1, 0, 5), target=(0, 0, 0))},
        })
        ctx.scene_connections = {"agg": {"in": ["cam2"]}}
        agg = NODE_REGISTRY["scene3d"]("agg")

        payloads = agg._collect_connected_scene_payloads(ctx, CameraPayload)

        assert len(payloads) == 1
        assert payloads[0].node_id == "cam2"


# ── CameraPayload 构造 ────────────────────────────────────────────────────

class TestCameraPayload:
    """Camera3D 矩阵计算与 Scene3D 自身数学 parity。"""

    def test_camera_payload_shape_and_fields(self):
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        cam = NODE_REGISTRY["camera3d"]("c")
        cam.evaluate({}, ctx, None)
        p = ctx.scene_payloads["c"]["display"]
        assert p.proj.shape == (4, 4)
        assert p.view.shape == (4, 4)
        assert len(p.eye) == 3
        assert len(p.target) == 3
        assert p.near == 0.1 and p.far == 1000.0

    def test_proj_matches_scene3d_math(self):
        """Camera3D 的 proj 必须与 Scene3D 拆分前的 perspective 调用一致。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        cam = NODE_REGISTRY["camera3d"]("c")
        cam.parameters["cam_fov"] = 65.0
        cam.parameters["width"] = 2048
        cam.parameters["height"] = 858
        cam.evaluate({}, ctx, None)
        p = ctx.scene_payloads["c"]["display"]
        expected = m3d.perspective(65.0, 2048 / 858, 0.1, 1000.0)
        assert np.allclose(p.proj, expected)

    def test_eye_matches_orbit_math(self):
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        cam = NODE_REGISTRY["camera3d"]("c")
        cam.parameters["cam_dist"] = 10.0
        cam.parameters["cam_orbit_yaw"] = 0.0
        cam.parameters["cam_orbit_pitch"] = 0.0
        cam.parameters["cam_target_x"] = 0.0
        cam.parameters["cam_target_y"] = 0.0
        cam.parameters["cam_target_z"] = 0.0
        cam.evaluate({}, ctx, None)
        p = ctx.scene_payloads["c"]["display"]
        # yaw=0,pitch=0,target=0 → eye 应在 +Z 轴 dist 处
        assert abs(p.eye[2] - 10.0) < 1e-4, f"eye z should be ~10.0, got {p.eye[2]}"
        assert abs(p.eye[0]) < 1e-4 and abs(p.eye[1]) < 1e-4


# ── LightPayload 单灯结构 ────────────────────────────────────────────────

class _FakeHdriBaker:
    """跳过 GL 的 IBL baker 替身。"""
    def bake(self, ctx, path):
        return (None, None, None)
    def release(self):
        pass


class TestLightPayload:
    """Light3D 产出单灯 LightPayload。"""

    def _make_light(self, node_id="l"):
        light = NODE_REGISTRY["light3d"](node_id)
        light._hdri_baker = _FakeHdriBaker()  # 跳过 GL
        return light

    def test_default_directional_light(self):
        """默认 Light3D 产出一盏 directional 白光。"""
        ctx = CompositeEvalContext(gl=None, scene_payloads={})
        light = self._make_light()
        light.evaluate({}, ctx, None)
        p = ctx.scene_payloads["l"]["display"]
        assert p.light_type == "directional"
        assert p.intensity == 2.0
        assert p.color == (1.0, 1.0, 1.0)

    def test_color_and_intensity(self):
        """颜色和强度写入单灯 payload。"""
        ctx = CompositeEvalContext(gl=None, scene_payloads={})
        light = self._make_light()
        light.parameters["light_intensity"] = 3.0
        light.parameters["light_color_r"] = 0.5
        light.parameters["light_color_g"] = 0.5
        light.parameters["light_color_b"] = 0.5
        light.evaluate({}, ctx, None)
        p = ctx.scene_payloads["l"]["display"]
        assert p.intensity == 3.0
        assert p.color == (0.5, 0.5, 0.5)

    def test_point_light_position(self):
        """point light 写入位置与 range。"""
        ctx = CompositeEvalContext(gl=None, scene_payloads={})
        light = self._make_light()
        light.parameters["light_type"] = "point"
        light.parameters["light_pos_x"] = 1.0
        light.parameters["light_pos_y"] = 2.0
        light.parameters["light_pos_z"] = 3.0
        light.parameters["light_range"] = 12.0
        light.evaluate({}, ctx, None)
        p = ctx.scene_payloads["l"]["display"]
        assert p.light_type == "point"
        assert p.pos == (1.0, 2.0, 3.0)
        assert p.range == 12.0

    def test_dirs_are_normalized(self):
        """方向光方向应被归一化（normalize）。"""
        ctx = CompositeEvalContext(gl=None, scene_payloads={})
        light = self._make_light()
        light.parameters["light_dir_x"] = 1.0
        light.parameters["light_dir_y"] = 1.0
        light.parameters["light_dir_z"] = 1.0
        light.evaluate({}, ctx, None)
        p = ctx.scene_payloads["l"]["display"]
        d = p.dir
        norm = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
        assert abs(norm - 1.0) < 1e-4, f"direction not normalized: norm={norm}"

    def test_empty_light_payload_factory(self):
        """empty_light_payload() 构造全黑无 IBL 的降级 payload。"""
        p = empty_light_payload()
        assert p.intensity == 0.0
        assert p.color == (0.0, 0.0, 0.0)
        assert p.use_ibl is False
        assert p.env_intensity == 0.0


# ── ModelPayload 降级 ────────────────────────────────────────────────────

class TestModelPayload:
    """Model3D 在无模型 / 加载失败时降级为 has_model=False。"""

    def test_no_path_emits_empty_payload(self):
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        model = NODE_REGISTRY["model3d"]("m")
        model.evaluate({}, ctx, None)
        p = ctx.scene_payloads["m"]["display"]
        assert isinstance(p, ModelPayload)
        assert p.has_model is False
        assert p.flat_meshes == []

    def test_disabled_with_path_emits_empty(self):
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        model = NODE_REGISTRY["model3d"]("m")
        model.parameters["model_path"] = "/nonexistent.gltf"
        model.parameters["use_imported_model"] = False
        model.evaluate({}, ctx, None)
        p = ctx.scene_payloads["m"]["display"]
        assert p.has_model is False

    def test_nonexistent_path_degrades_gracefully(self):
        """不存在的文件路径 → 加载失败，has_model=False，error 设置（不抛异常）。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        model = NODE_REGISTRY["model3d"]("m")
        model.parameters["model_path"] = "/nonexistent.gltf"
        model.parameters["use_imported_model"] = True
        # 不应抛异常
        model.evaluate({}, ctx, None)
        p = ctx.scene_payloads["m"]["display"]
        assert p.has_model is False
        assert p.model_error is not None
        assert p.model_error.startswith("asset.missing:")


# ── Scene3D 聚合器降级 ───────────────────────────────────────────────────

class TestScene3DDelegation:
    """Scene3D 在无 scene 输入时退化为旧参数（过渡期兼容）。"""

    def test_scene3d_collect_returns_none_without_payload(self):
        """无 payload 时 collect-by-type 返回 None（Scene3D 走旧参数路径）。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        agg = NODE_REGISTRY["scene3d"]("agg")
        for cls in (CameraPayload, LightPayload, GeometryPayload, ModelPayload):
            assert agg._collect_scene_payload_by_type(ctx, cls) is None

    def test_payload_collect_uses_eval_ctx_not_unwrapped_gl(self):
        """回归：Scene3D 解包 CompositeEvalContext 后仍须能读 scene_payloads 旁路。

        根因：_do_evaluate 开头 ``ctx = ctx.gl`` 把 CompositeEvalContext 解包成
        裸 moderngl.Context（无 scene_payloads 属性），导致 payload 收集返回 None
        → Scene3D 触发纯背景 early-return → 画面灰色空白。
        修复：保留 eval_ctx 引用，payload 收集用 eval_ctx 而非解包后的 ctx。
        """
        # 模拟真实渲染：Camera3D emit payload 到 CompositeEvalContext 旁路
        eval_ctx = CompositeEvalContext(gl="fake_gl", scene_payloads={})
        cam = NODE_REGISTRY["camera3d"]("cam1")
        cam.parameters["cam_fov"] = 50.0
        cam.evaluate({}, eval_ctx, None)
        assert "cam1" in eval_ctx.scene_payloads

        # 模拟 Scene3D 解包后的状态
        unwrapped_ctx = eval_ctx.gl  # 裸 ctx（无 scene_payloads）
        assert not hasattr(unwrapped_ctx, "scene_payloads"), "裸 GL ctx 不应有旁路属性"

        # 错误用法（bug 复现）：用解包后的裸 ctx → 读不到 payload
        agg = NODE_REGISTRY["scene3d"]("agg")
        wrong = agg._collect_scene_payload_by_type(unwrapped_ctx, CameraPayload)
        assert wrong is None, "裸 ctx 读不到旁路（这就是灰色根因）"

        # 正确用法（修复后）：用保留的 eval_ctx → 能读到 payload
        correct = agg._collect_scene_payload_by_type(eval_ctx, CameraPayload)
        assert correct is not None, "eval_ctx 应能读到旁路 payload"
        assert correct.fov == 50.0

    def test_scene3d_picks_correct_variant_from_payloads(self):
        """4 种 payload 同时在旁路时，按类型各自正确解析。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={
            "cam": {"display": CameraPayload(proj=np.eye(4), view=np.eye(4),
                                             eye=(1, 2, 3), target=(0, 0, 0), fov=80.0)},
            "light": {"display": LightPayload(dir=(0, 1, 0), color=(1, 1, 1),
                                              intensity=1.0)},
            "model": {"display": ModelPayload(has_model=True)},
        })
        agg = NODE_REGISTRY["scene3d"]("agg")
        cam = agg._collect_scene_payload_by_type(ctx, CameraPayload)
        light = agg._collect_scene_payload_by_type(ctx, LightPayload)
        model = agg._collect_scene_payload_by_type(ctx, ModelPayload)
        geo = agg._collect_scene_payload_by_type(ctx, GeometryPayload)
        assert cam.fov == 80.0 and cam.eye == (1, 2, 3)
        assert light.light_type == "directional"
        assert light.intensity == 1.0
        assert model.has_model is True
        assert geo is None  # no GeometryPayload provided


# ── v7 图迁移 ─────────────────────────────────────────────────────────────

from src.features.composite.core.graph_migration import (
    migrate_graph, COMPOSITE_SCHEMA_VERSION, _SCENE3D_KEEP)


class TestV7Migration:
    """v6→v7 迁移：旧 Scene3D 单节点 → spawn 4 子节点 + 连线。"""

    def _make_v6_scene3d(self, scene3d_id="scene1", with_model=False):
        params = {
            "width": 1920, "height": 1080, "format_name": "Scene3D",
            "bg_opaque": True, "bg_r": 0.05, "bg_g": 0.05, "bg_b": 0.05,
            "show_grid": True, "show_axes": True,
            # camera
            "cam_fov": 50.0, "cam_dist": 5.0, "cam_orbit_yaw": 30.0,
            "cam_orbit_pitch": 20.0,
            # light legacy
            "light_dir_x": -0.6, "light_dir_y": 0.7, "light_dir_z": 0.4,
            "light_intensity": 2.0,
            "light1_intensity": 1.0,
            "hdri_path": "", "use_ibl": True, "env_intensity": 1.0,
            # geometry
            "primitive": "sphere", "mesh_scale": 2.0,
            "object_pos_x": 1.5, "object_rot_y": 45.0,
            "metallic": 0.3, "roughness": 0.7,
            # model
            "model_path": "/path/model.gltf" if with_model else "",
            "use_imported_model": with_model,
            "anim_enabled": True, "anim_time": 2.0, "anim_speed": 1.5,
        }
        return {
            "composite_schema_version": 6,
            "nodes": [{"id": scene3d_id, "type": "scene3d",
                       "position": [400, 0], "parameters": params, "disabled": False}],
            "connections": [],
        }

    def test_migrates_to_v7_schema_version(self):
        result = migrate_graph(self._make_v6_scene3d())
        assert result["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    def test_spawns_camera_light_geo_without_model(self):
        """无模型时 spawn 3 子节点（camera3d/light3d/geo3d），无 model3d。"""
        result = migrate_graph(self._make_v6_scene3d(with_model=False))
        types = sorted(n["type"] for n in result["nodes"])
        assert types == ["camera3d", "geo3d", "light3d", "scene3d"]

    def test_spawns_all_4_children_with_model(self):
        """有模型时 spawn 4 子节点（含 model3d）。"""
        result = migrate_graph(self._make_v6_scene3d(with_model=True))
        types = sorted(n["type"] for n in result["nodes"])
        assert types == ["camera3d", "geo3d", "light3d", "model3d", "scene3d"]

    def test_establishes_scene_connections(self):
        """3 条 scene 连接（无模型）都指向单一 in 端口。"""
        result = migrate_graph(self._make_v6_scene3d(with_model=False))
        in_ports = {c["dst_port_name"] for c in result["connections"]}
        assert in_ports == {"in"}
        # 所有连接 src 都是 display，dst 都是 scene1
        for c in result["connections"]:
            assert c["src_port_name"] == "display"
            assert c["dst_node_id"] == "scene1"

    def test_establishes_model_connection_with_model(self):
        """有模型时 4 条连接；Model3D 也连单一 in 端口。"""
        result = migrate_graph(self._make_v6_scene3d(with_model=True))
        in_ports = {c["dst_port_name"] for c in result["connections"]}
        # v8+ 合并为单一 in 端口，无 model_in
        assert in_ports == {"in"}
        assert len(result["connections"]) == 4  # camera + light + geo + model 都各一条
        # Model3D 的连接目标是单一 in 端口
        model_conn = [c for c in result["connections"]
                      if c["src_node_id"].endswith("_model3d_0")]
        assert len(model_conn) == 1
        assert model_conn[0]["dst_port_name"] == "in"

    def test_scene3d_keeps_only_canvas_params(self):
        """迁移后 scene3d 只剩画幅/元数据。

        v9 把场景环境参数（cam_*/bg_*/light_*/hdri_preset/show_grid/show_axes/
        output_*/msaa/active_camera_id）也移到全局 viewer_state["default_3d_scene"]。
        Scene3D 只保留 width/height/format_name；几何/材质由 Geo3D/Model3D 提供。

        注：v6 工程的 cam_*/light_* 等在 v6→v7 迁移时已移到 Camera3D/Light3D
        子节点；v9 进一步把残留的 Scene3D 场景参数（v7 hotfix 保留的 cam_*/bg_*
        等）移到全局。对于全新 v9 工程，场景参数直接在全局设置里。
        """
        result = migrate_graph(self._make_v6_scene3d())
        scene3d = [n for n in result["nodes"] if n["type"] == "scene3d"][0]
        actual = set(scene3d["parameters"].keys())
        # 应该是 _SCENE3D_KEEP 的子集（format_name 可能不在原始数据里）
        assert actual.issubset(_SCENE3D_KEEP), f"extra params: {actual - _SCENE3D_KEEP}"
        # v9：场景环境与 legacy 几何材质参数全部不在 Scene3D 上。
        for migrated in ("light_intensity", "model_path",
                         "cam_fov", "cam_dist", "bg_opaque", "show_grid",
                         "hdri_preset", "msaa", "active_camera_id",
                         "primitive", "mesh_scale", "object_pos_x",
                         "metallic", "roughness", "object_color_r"):
            assert migrated not in actual, f"{migrated} should be migrated away"

    def test_params_migrated_to_correct_child(self):
        """相机/灯光/几何参数分别迁移到对应子节点。"""
        result = migrate_graph(self._make_v6_scene3d())
        cam3d = [n for n in result["nodes"] if n["type"] == "camera3d"][0]
        geo3d = [n for n in result["nodes"] if n["type"] == "geo3d"][0]
        light3d = [n for n in result["nodes"] if n["type"] == "light3d"][0]
        assert cam3d["parameters"]["cam_fov"] == 50.0
        assert cam3d["parameters"]["cam_dist"] == 5.0
        assert geo3d["parameters"]["primitive"] == "sphere"
        assert geo3d["parameters"]["mesh_scale"] == 2.0
        assert geo3d["parameters"]["object_rot_y"] == 45.0
        assert light3d["parameters"]["light_intensity"] == 2.0
        assert light3d["parameters"]["light1_intensity"] == 1.0

    def test_model_params_migrated_when_model_present(self):
        """模型参数迁移到 model3d。"""
        result = migrate_graph(self._make_v6_scene3d(with_model=True))
        model3d = [n for n in result["nodes"] if n["type"] == "model3d"][0]
        assert model3d["parameters"]["model_path"] == "/path/model.gltf"
        assert model3d["parameters"]["anim_time"] == 2.0
        assert model3d["parameters"]["anim_speed"] == 1.5

    def test_migration_is_idempotent(self):
        """迁移 v7 图应是 no-op（不再 spawn 子节点）。"""
        result = migrate_graph(self._make_v6_scene3d())
        re_result = migrate_graph(result)
        assert len(re_result["nodes"]) == len(result["nodes"])
        assert len(re_result["connections"]) == len(result["connections"])

    def test_child_node_positions_offset_from_scene3d(self):
        """子节点位置在 scene3d 左侧纵向错开（避免重叠）。"""
        result = migrate_graph(self._make_v6_scene3d())
        scene3d = [n for n in result["nodes"] if n["type"] == "scene3d"][0]
        sx, sy = scene3d["position"]
        for child in result["nodes"]:
            if child["type"] == "scene3d":
                continue
            cx, cy = child["position"]
            # 子节点应在 scene3d 左侧（cx < sx）且纵向有偏移
            assert cx < sx, f"{child['type']} not to the left of scene3d"
            assert cy != sy, f"{child['type']} not vertically offset"

    def test_non_scene3d_nodes_untouched(self):
        """非 scene3d 节点不应被迁移影响。"""
        graph = self._make_v6_scene3d()
        graph["nodes"].append({"id": "blur1", "type": "blur",
                               "position": [0, 0], "parameters": {"radius": 5.0},
                               "disabled": False})
        result = migrate_graph(graph)
        blur = [n for n in result["nodes"] if n["type"] == "blur"][0]
        assert blur["parameters"]["radius"] == 5.0
        assert blur["id"] == "blur1"

    def test_multiple_scene3d_nodes_each_migrated(self):
        """多个 scene3d 节点各自独立迁移。"""
        graph = {
            "composite_schema_version": 6,
            "nodes": [
                {"id": "s1", "type": "scene3d", "position": [0, 0],
                 "parameters": {"width": 1920, "height": 1080,
                                "cam_fov": 50.0, "primitive": "cube"},
                 "disabled": False},
                {"id": "s2", "type": "scene3d", "position": [800, 0],
                 "parameters": {"width": 1280, "height": 720,
                                "cam_fov": 80.0, "primitive": "sphere"},
                 "disabled": False},
            ],
            "connections": [],
        }
        result = migrate_graph(graph)
        scene3ds = [n for n in result["nodes"] if n["type"] == "scene3d"]
        assert len(scene3ds) == 2
        # 每个 scene3d 应各自 spawn 自己的子节点（子节点 id 带 scene3d id 前缀）
        s1_children = [n for n in result["nodes"] if n["id"].startswith("s1_")]
        s2_children = [n for n in result["nodes"] if n["id"].startswith("s2_")]
        assert len(s1_children) == 3  # camera + light + geo
        assert len(s2_children) == 3
