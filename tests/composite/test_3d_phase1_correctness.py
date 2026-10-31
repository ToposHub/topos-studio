"""Phase 1 正确性热修测试。

覆盖 docs/composite_3d_module_audit_optimization_plan_2026-06-29.md 的 P0 修复：
1. 轴线可见（show_axes 真正接线，不再是死参数）
2. payload signature 进 content_sig（上游变更触发重渲染）
3. Light3D IBL None 黑色回退（防渲染线程崩溃）
4. Geo3D VAO 缓存含 instance_enabled（切换实例化重建 VAO）
5. 端口感知 payload 收集（断开的场景节点不影响渲染）
6. view-mode override 门控 show_axes（轴线预览辅助，不进合成输出）

注：不真正建 GL context（CI 无 GPU）；用静态/反射 + mock 验证逻辑分支。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.render3d.scene_payload import (
    CameraPayload, LightPayload, GeometryPayload, ModelPayload, empty_light_payload)
from src.features.composite.render3d import math_utils as m3d
from src.features.composite.core.graph_snapshot import ConnectionSnapshot
from src.features.composite.nodes.model3d_node import Model3DNode
from src.features.composite.render3d.scene_hierarchy import SceneHierarchyItem


# ──────────────────────────────────────────────────────────────────────────
# Part 1: 轴线可见（show_axes 真正接线）
# ──────────────────────────────────────────────────────────────────────────

class TestAxesVisible:
    """show_axes 参数真正生效：helper draw gate 含 show_axes，且传入 show_axes 值。"""

    def test_draw_gate_includes_show_axes(self):
        """_do_evaluate 的 helper draw gate 应为 `if show_grid or show_axes:`。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "if show_grid or show_axes:" in src, \
            "helper draw gate 应含 show_axes（轴线在 show_grid=False 时也能画）"

    def test_draw_passes_show_axes_value(self):
        """_gizmo.draw 调用应传入 show_axes=show_axes（而非硬编码 False）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "show_axes=show_axes" in src, \
            "_gizmo.draw 应传入 show_axes=show_axes，而非硬编码 False"

    def test_axes_param_default_true(self):
        """show_axes 默认 True（开箱见世界坐标轴）。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["show_axes"] is True

    def test_gizmo_renderer_supports_show_axes(self):
        """GizmoRenderer.draw 接受 show_axes 参数并按需绘制。"""
        from src.features.composite.render3d.gizmo_renderer import GizmoRenderer
        sig = inspect.signature(GizmoRenderer.draw)
        assert "show_axes" in sig.parameters

    def test_view_mode_override_gates_show_axes(self):
        """_apply_view_mode_overrides 门控 show_axes：3D 预览显，其余隐藏。

        v9：返回 overrides 字典而非修改快照。3D 预览目标的 show_axes 来自
        全局 show_axes（默认 True），其余场景 show_axes=False。
        """
        from types import SimpleNamespace
        from src.features.composite.ui.composite_page import CompositePage

        # 3D 预览 scene3d → show_axes 来自全局默认（True）
        viewer = SimpleNamespace(get_view_mode=lambda: "3d")
        page = SimpleNamespace(
            _viewer=viewer,
            _resolve_viewer_node_id=lambda: "s1",
            _resolve_preview_node_id=lambda: "s1",
            _node_graph=SimpleNamespace(_viewer_state={}),
        )
        page._global_3d_scene_settings = (
            CompositePage._global_3d_scene_settings.__get__(page))
        snap3d = SimpleNamespace(
            nodes={
                "s1": SimpleNamespace(node_type="scene3d", parameters={})
            }
        )
        overrides = CompositePage._apply_view_mode_overrides(page, snap3d)
        assert overrides["s1"]["show_axes"] is True

        # 2D 模式 → show_axes 强制 False（不进合成输出）
        viewer2 = SimpleNamespace(get_view_mode=lambda: "2d")
        page2 = SimpleNamespace(
            _viewer=viewer2,
            _resolve_viewer_node_id=lambda: None,
            _resolve_preview_node_id=lambda: None,
            _node_graph=SimpleNamespace(_viewer_state={}),
        )
        page2._global_3d_scene_settings = (
            CompositePage._global_3d_scene_settings.__get__(page2))
        snap2d = SimpleNamespace(
            nodes={
                "s1": SimpleNamespace(node_type="scene3d", parameters={})
            }
        )
        overrides2 = CompositePage._apply_view_mode_overrides(page2, snap2d)
        assert overrides2["s1"]["show_axes"] is False


# ──────────────────────────────────────────────────────────────────────────
# Part 2: payload signature 进 content_sig
# ──────────────────────────────────────────────────────────────────────────

class TestPayloadSignature:
    """4 个 payload dataclass 有 signature() 方法，且进 Scene3D content_sig。"""

    def test_camera_payload_has_signature(self):
        cam = CameraPayload(proj=np.eye(4), view=np.eye(4),
                            eye=(0, 0, 5), target=(1, 2, 3), fov=60.0)
        sig = cam.signature()
        assert sig[0] == "camera_v2"
        assert sig[7] == 60.0  # fov（index 7）

    def test_camera_signature_changes_with_proj(self):
        """proj 变化 → signature 变化（相机移动/转角度）。"""
        c1 = CameraPayload(proj=np.eye(4), view=np.eye(4),
                           eye=(0, 0, 5), target=(0, 0, 0))
        proj2 = m3d.perspective(90.0, 16/9, 0.1, 1000.0)
        c2 = CameraPayload(proj=proj2, view=np.eye(4),
                           eye=(0, 0, 5), target=(0, 0, 0))
        assert c1.signature() != c2.signature()

    def test_light_payload_has_signature(self):
        lit = LightPayload(dir=(0, 1, 0), color=(1, 1, 1), intensity=2.0)
        sig = lit.signature()
        assert sig[0] == "light_v2"
        assert sig[5] == 2.0  # intensity

    def test_light_signature_changes_with_intensity(self):
        """灯光强度变化 → signature 变化。"""
        l1 = LightPayload(dir=(0, 1, 0), color=(1, 1, 1), intensity=1.0)
        l2 = LightPayload(dir=(0, 1, 0), color=(1, 1, 1), intensity=2.0)
        assert l1.signature() != l2.signature()

    def test_geo_payload_has_signature(self):
        geo = GeometryPayload(vao=None, model_matrix=np.eye(4),
                              material={"albedo": (0.8, 0.8, 0.8),
                                        "metallic": 0.0, "roughness": 0.5})
        sig = geo.signature()
        assert sig[0] == "geo"

    def test_geo_signature_changes_with_revision(self):
        """content_revision 变化（Geo3D 参数改了）→ signature 变化。"""
        g1 = GeometryPayload(vao=None, model_matrix=np.eye(4), content_revision=1)
        g2 = GeometryPayload(vao=None, model_matrix=np.eye(4), content_revision=2)
        assert g1.signature() != g2.signature()

    def test_model_payload_has_signature(self):
        mod = ModelPayload(has_model=True, model_path="/x.gltf",
                           model_mtime=1.5, anim_time=2.0)
        sig = mod.signature()
        assert sig[0] == "model"

    def test_model_signature_changes_with_anim_time(self):
        """动画帧变化 → signature 变化（骨骼动画每帧重渲染）。"""
        m1 = ModelPayload(has_model=True, model_path="/x.gltf",
                          model_mtime=1.5, anim_time=1.0)
        m2 = ModelPayload(has_model=True, model_path="/x.gltf",
                          model_mtime=1.5, anim_time=2.0)
        assert m1.signature() != m2.signature()

    def test_model_signature_changes_with_outliner_state(self):
        """Outliner lock/solo changes must invalidate the Scene3D content cache."""
        base = ModelPayload(has_model=True)
        locked = ModelPayload(has_model=True, locked_mesh_ids=("mesh-a",))
        solo = ModelPayload(has_model=True, solo_mesh_id="mesh-a")

        assert base.signature() != locked.signature()
        assert base.signature() != solo.signature()

    def test_model_solo_resolves_group_to_effective_hidden_mesh_ids(self):
        """Soloing a hierarchy group keeps its descendants and hides siblings."""
        hierarchy = SceneHierarchyItem(
            node_id="root", name="Scene", children=[
                SceneHierarchyItem(node_id="body", name="Body",
                                   node_type="mesh"),
                SceneHierarchyItem(
                    node_id="parts", name="Parts", node_type="group",
                    children=[SceneHierarchyItem(
                        node_id="head", name="Head", node_type="mesh")]),
            ])
        flat_meshes = [{"node_id": "body"}, {"node_id": "head"}]

        hidden = Model3DNode._effective_hidden_mesh_ids(
            (), "parts", hierarchy, flat_meshes)

        assert hidden == ("body",)

    def test_scene3d_content_sig_includes_payload_signatures(self):
        """Scene3D content_sig 元组含 4 个 payload signature 分量。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "_cam_sig" in src, "content_sig 应含 camera payload signature"
        assert "_light_sig" in src, "content_sig 应含 light payload signature"
        assert "_geo_sig" in src, "content_sig 应含 geo payload signature"
        assert "_model_sig" in src, "content_sig 应含 model payload signature"

    def test_geo3d_increments_content_revision_each_eval(self):
        """Geo3D 每次 _do_evaluate 递增 content_revision（捕获参数变化）。"""
        # Geo3D 真实求值需要 GL；验证逻辑：source 含 content_revision 递增
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        src = inspect.getsource(Geo3DNode._do_evaluate)
        assert "content_revision" in src
        assert "_geo_revision" in src

    def test_model3d_passes_resource_id_to_payload(self):
        """Model3D emit payload 时带 model_path/model_mtime/anim_time（signature 用）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        assert "model_path=" in src
        assert "model_mtime=" in src
        assert "anim_time=" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 3: IBL None 黑色回退
# ──────────────────────────────────────────────────────────────────────────

class TestIblNoneFallback:
    """Light3D bake 失败残留 None IBL 时，Scene3D 防御性降级为黑色占位。"""

    def test_scene3d_defends_against_none_ibl(self):
        """Scene3D 在 _light3d_ibl IBL 为 None 时降级黑色占位（源码含防御逻辑）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # 关键防御：检测任一 cube 为 None → 调 _ensure_ibl(ctx, False, "")
        assert "irradiance_cube is None" in src or "is None" in src
        assert "_ensure_ibl(ctx, False, \"\")" in src, \
            "IBL None 时应降级为 _ensure_ibl 黑色占位"

    def test_empty_light_payload_factory_is_black(self):
        """empty_light_payload 构造全黑无 IBL 的降级 payload。"""
        p = empty_light_payload()
        assert p.irradiance_cube is None
        assert p.prefilter_cube is None
        assert p.brdf_lut is None
        assert p.use_ibl is False


# ──────────────────────────────────────────────────────────────────────────
# Part 4: Geo3D VAO 缓存含 instance_enabled
# ──────────────────────────────────────────────────────────────────────────

class TestGeo3dVaoInstanceRebuild:
    """Geo3D 切换 instance_enabled 时强制重建 VAO（不复用错误属性的 VAO）。"""

    def test_ensure_mesh_geometry_checks_instance_enabled(self):
        """_ensure_mesh_geometry 复用条件含 instance_enabled 比较。"""
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        src = inspect.getsource(Geo3DNode._ensure_mesh_geometry)
        assert "_mesh_instance_enabled == instance_enabled" in src, \
            "复用条件必须含 instance_enabled，否则切换实例化复用错误 VAO"

    def test_geo3d_has_instance_enabled_field(self):
        """Geo3DNode 有 _mesh_instance_enabled 字段（跟踪当前 VAO 状态）。"""
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        node = Geo3DNode()
        assert hasattr(node, "_mesh_instance_enabled")
        assert node._mesh_instance_enabled is False  # 初始未实例化


# ──────────────────────────────────────────────────────────────────────────
# Part 5: 端口感知 payload 收集
# ──────────────────────────────────────────────────────────────────────────

class _MockGlowCube:
    """伪 GL 对象（用于 payload，有 .use() 方法占位，但不真实调 GL）。"""
    def use(self, location=0):
        pass


class TestPortAwarePayloadCollection:
    """端口感知收集：只读连接到本节点的上游 payload，断开的不影响渲染。"""

    def test_collect_connected_returns_only_connected(self):
        """连接的 Camera3D payload 被收集，未连接的不收集。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={}, scene_connections={
            "scene1": {"camera_in": "cam_connected"}
        })
        # 两个 CameraPayload 在旁路：一个连接、一个未连接
        ctx.scene_payloads["cam_connected"] = {"display": CameraPayload(
            proj=np.eye(4), view=np.eye(4), eye=(0, 0, 5), target=(0, 0, 0), fov=50.0)}
        ctx.scene_payloads["cam_orphan"] = {"display": CameraPayload(
            proj=np.eye(4), view=np.eye(4), eye=(9, 9, 9), target=(0, 0, 0), fov=99.0)}

        scene3d = NODE_REGISTRY["scene3d"]("scene1")
        result = scene3d._collect_connected_scene_payloads(ctx, CameraPayload)
        assert len(result) == 1, "只应收集连接的 payload"
        assert result[0].fov == 50.0  # 是 connected 那个，不是 orphan

    def test_collect_connected_multiple_geo_in_order(self):
        """v8：多个 Geo3D 连到单 `in` 端口 → 全部返回（按连接顺序）。

        scene_connections value 是列表（支持多连接）：`in` 端口可接多个源。
        """
        ctx = CompositeEvalContext(gl="fake", scene_payloads={}, scene_connections={
            "scene1": {"in": ["cam1", "geo_a", "geo_b"]}  # list：多连接
        })
        ctx.scene_payloads["geo_a"] = {"display": GeometryPayload(
            vao=None, model_matrix=np.eye(4), content_revision=1)}
        ctx.scene_payloads["geo_b"] = {"display": GeometryPayload(
            vao=None, model_matrix=np.eye(4), content_revision=2)}
        scene3d = NODE_REGISTRY["scene3d"]("scene1")
        result = scene3d._collect_connected_scene_payloads(ctx, GeometryPayload)
        # 多输入：两个 geo payload 都应被收集，按连接顺序
        assert len(result) == 2, "多输入端口应收集所有连接的 payload"
        assert result[0].content_revision == 1  # geo_a 先连
        assert result[1].content_revision == 2  # geo_b 后连

    def test_collect_connected_returns_empty_when_no_connections(self):
        """scene_connections 未启用 → 退化为全局扫描（向后兼容）。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={
            "cam": {"display": CameraPayload(
                proj=np.eye(4), view=np.eye(4), eye=(0, 0, 5), target=(0, 0, 0))}
        })  # scene_connections=None
        scene3d = NODE_REGISTRY["scene3d"]("scene1")
        result = scene3d._collect_connected_scene_payloads(ctx, CameraPayload)
        # 退化：全局扫描返回 1 个（向后兼容）
        assert len(result) == 1

    def test_collect_connected_skips_disconnected_payload(self):
        """断开的场景节点（在旁路但无连接记录）不被收集。"""
        ctx = CompositeEvalContext(gl="fake", scene_payloads={}, scene_connections={
            "scene1": {}  # 无任何连接
        })
        ctx.scene_payloads["orphan_light"] = {"display": LightPayload(
            dir=(0, 1, 0), color=(1, 1, 1), intensity=1.0)}
        scene3d = NODE_REGISTRY["scene3d"]("scene1")
        result = scene3d._collect_connected_scene_payloads(ctx, LightPayload)
        assert len(result) == 0, "断开的 light payload 不应被收集"

    def test_eval_context_has_scene_connections_field(self):
        """CompositeEvalContext 有 scene_connections 字段（端口感知收集用）。"""
        ctx = CompositeEvalContext(gl=None)
        assert hasattr(ctx, "scene_connections")
        assert ctx.scene_connections is None  # 默认未启用


# ──────────────────────────────────────────────────────────────────────────
# Part 6: 端到端 — 端口感知收集在真实 evaluate 流程中工作
# ──────────────────────────────────────────────────────────────────────────

class TestEndToEndPortAware:
    """Camera3D emit payload + Scene3D 端口感知收集的端到端数据流。"""

    def test_connected_camera_payload_reaches_scene3d(self):
        """连接的 Camera3D payload 能被 Scene3D 端口感知收集到。"""
        eval_ctx = CompositeEvalContext(gl="fake", scene_payloads={},
                                        scene_connections={"scene1": {"camera_in": "cam1"}})
        cam = NODE_REGISTRY["camera3d"]("cam1")
        cam.parameters["cam_fov"] = 70.0
        cam.evaluate({}, eval_ctx, None)
        assert "cam1" in eval_ctx.scene_payloads

        scene3d = NODE_REGISTRY["scene3d"]("scene1")
        result = scene3d._collect_connected_scene_payloads(eval_ctx, CameraPayload)
        assert len(result) == 1
        assert result[0].fov == 70.0  # 连接的相机参数正确传递

    def test_disconnected_camera_does_not_affect_scene3d(self):
        """未连接的 Camera3D emit 的 payload 不影响 Scene3D（端口感知）。"""
        eval_ctx = CompositeEvalContext(gl="fake", scene_payloads={},
                                        scene_connections={"scene1": {}})  # scene1 无连接
        cam = NODE_REGISTRY["camera3d"]("cam1")
        cam.parameters["cam_fov"] = 70.0
        cam.evaluate({}, eval_ctx, None)

        scene3d = NODE_REGISTRY["scene3d"]("scene1")
        result = scene3d._collect_connected_scene_payloads(eval_ctx, CameraPayload)
        assert len(result) == 0, "未连接的 camera payload 不应被 Scene3D 收集"
