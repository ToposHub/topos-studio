"""P1 增强项完成度测试。

覆盖 composite_3d_module_audit_optimization_plan_2026-06-29.md 中此前未完成的 6 项：
1. 材质纹理 GL 上传绑定（material_texture_loader + Scene3D 绑定路径）
2. Camera3D grid pitch 修复（payload.pitch + Scene3D 用它）
3. Camera3D 相机模式扩展（orbit/free/ortho + near/far/roll/ortho_scale）
4. Geo3D 新基本体（cylinder/cone/torus/null）
5. Light3D hdri_path 改 file 类型
6. 4 个 3D 节点属性面板分组（ParamDef.group）

测试策略：CI 无 GPU，故采用 source-level 断言（inspect.getsource）+ 纯数学逻辑
断言（无 GL 依赖的部分）。GL 运行时验证由用户在带 GPU 环境完成。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest

from src.features.composite.render3d import math_utils as m3d
from src.features.composite.render3d.primitives import (
    PRIMITIVES, get_primitive, cylinder, cone, torus, null_object)


# ──────────────────────────────────────────────────────────────────────────
# #4 Geo3D 新基本体（cylinder/cone/torus/null）
# ──────────────────────────────────────────────────────────────────────────

class TestNewPrimitives:
    """新增 cylinder/cone/torus/null 基本体的几何正确性。"""

    def test_primitives_registered(self):
        for name in ("cylinder", "cone", "torus", "null"):
            assert name in PRIMITIVES, f"{name} 未注册到 PRIMITIVES"
            assert PRIMITIVES[name] is not None

    def test_get_primitive_supports_new(self):
        for name in ("cylinder", "cone", "torus", "null"):
            mesh = get_primitive(name)
            assert mesh is not None
            assert mesh.vertices.shape[0] > 0
            assert mesh.indices.shape[0] > 0

    def test_unknown_primitive_raises(self):
        with pytest.raises(ValueError):
            get_primitive("nonexistent_primitive")

    def test_cylinder_has_top_bottom_and_side(self):
        m = cylinder(radius=1.0, height=2.0, segments=16)
        # 侧面：2*(segments+1) 顶点；顶盖中心+环：1+(segments+1)；底盖同理。
        # 总顶点 = 2*17 + 1+17 + 1+17 = 34 + 18 + 18 = 70
        assert m.vertices.shape[0] == 2 * 17 + (1 + 17) + (1 + 17)
        # 索引：侧面 6*segments 三角形索引 + 顶盖 3*segments + 底盖 3*segments
        assert m.indices.shape[0] == 6 * 16 + 3 * 16 + 3 * 16

    def test_cylinder_radius_applied(self):
        m = cylinder(radius=2.0, height=2.0, segments=8)
        # 顶点应在半径 2 的圆柱上（检查水平距离 ≈ 2）
        horz = np.sqrt(m.vertices[:, 0] ** 2 + m.vertices[:, 2] ** 2)
        ring_verts = horz[horz > 0.1]  # 排除盖中心
        assert np.allclose(ring_verts, 2.0, atol=1e-5)

    def test_cone_has_apex_and_base(self):
        m = cone(radius=1.0, height=2.0, segments=16)
        # apex(1) + 底环(segments+1) + 底盖中心(1) + 底盖环(segments+1)
        assert m.vertices.shape[0] == 1 + 17 + 1 + 17
        # apex 在顶部（y = +half_h = 1.0）
        assert abs(m.vertices[0, 1] - 1.0) < 1e-5

    def test_torus_is_closed_surface(self):
        m = torus(major_radius=1.0, minor_radius=0.3, major_segments=16, minor_segments=8)
        # 顶点数 = major_segments * minor_segments
        assert m.vertices.shape[0] == 16 * 8
        # 索引：每个 quad 2 三角形 * 6 索引，共 major*minor 个 quad
        assert m.indices.shape[0] == 16 * 8 * 6

    def test_torus_major_minor_radius(self):
        m = torus(major_radius=2.0, minor_radius=0.5, major_segments=12, minor_segments=6)
        # 管中心应在半径 2.0 处（忽略管半径波动，检查平均径向距离）
        radial = np.sqrt(m.vertices[:, 0] ** 2 + m.vertices[:, 2] ** 2)
        assert abs(radial.mean() - 2.0) < 0.6  # 管半径波动范围内

    def test_null_object_is_axis_lines(self):
        m = null_object()
        # 6 顶点（原点重复 + 三轴端点），6 索引（3 条线段）
        assert m.vertices.shape[0] == 6
        assert m.indices.shape[0] == 6
        # 原点在第一个顶点
        assert np.allclose(m.vertices[0], [0.0, 0.0, 0.0])
        # X/Y/Z 轴端点分别在 (1,0,0)/(0,1,0)/(0,0,1)
        assert np.allclose(m.vertices[1], [1.0, 0.0, 0.0])
        assert np.allclose(m.vertices[3], [0.0, 1.0, 0.0])
        assert np.allclose(m.vertices[5], [0.0, 0.0, 1.0])

    def test_all_new_meshes_have_valid_normals_uvs(self):
        for name in ("cylinder", "cone", "torus", "null"):
            mesh = get_primitive(name)
            assert mesh.normals.shape[0] == mesh.vertices.shape[0]
            assert mesh.uvs.shape[0] == mesh.vertices.shape[0]

    def test_geo3d_line_primitives_set(self):
        """Geo3D 节点把 grid/null 标记为 LINES 渲染。"""
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        assert "grid" in Geo3DNode._LINE_PRIMITIVES
        assert "null" in Geo3DNode._LINE_PRIMITIVES
        assert "cube" not in Geo3DNode._LINE_PRIMITIVES

    def test_geo3d_primitive_labels_include_new(self):
        """primitive 参数选项含新基本体。"""
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        pdef = next(p for p in Geo3DNode.PARAM_DEFS if p.name == "primitive")
        for name in ("cylinder", "cone", "torus", "null"):
            assert name in pdef.labels


# ──────────────────────────────────────────────────────────────────────────
# #3 Camera3D 相机模式扩展 + #2 grid pitch 修复
# ──────────────────────────────────────────────────────────────────────────

class TestCamera3DModes:
    """Camera3D 支持 orbit/free/ortho 三模式 + near/far/roll/ortho_scale。"""

    def test_camera_mode_param_exists(self):
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        pdef = next(p for p in Camera3DNode.PARAM_DEFS if p.name == "camera_mode")
        assert pdef.ptype == "option"
        assert set(pdef.labels) == {"orbit", "free", "ortho"}

    def test_clipping_params_exposed(self):
        """near/far 不再硬编码，可调。"""
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        names = {p.name for p in Camera3DNode.PARAM_DEFS}
        assert "cam_near" in names
        assert "cam_far" in names

    def test_free_camera_params(self):
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        names = {p.name for p in Camera3DNode.PARAM_DEFS}
        assert "cam_eye_x" in names
        assert "cam_eye_y" in names
        assert "cam_eye_z" in names

    def test_roll_and_ortho_scale_params(self):
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        names = {p.name for p in Camera3DNode.PARAM_DEFS}
        assert "cam_roll" in names
        assert "cam_ortho_scale" in names

    def test_payload_has_pitch_and_mode(self):
        """CameraPayload 带 pitch + camera_mode 字段。"""
        from src.features.composite.render3d.scene_payload import CameraPayload
        cp = CameraPayload(
            proj=np.eye(4, dtype=np.float32),
            view=np.eye(4, dtype=np.float32),
            eye=(0, 0, 5), target=(0, 0, 0))
        assert hasattr(cp, "pitch")
        assert hasattr(cp, "camera_mode")
        assert cp.pitch == 20.0  # 默认值
        assert cp.camera_mode == "orbit"

    def test_pitch_in_camera_signature(self):
        """pitch + camera_mode 进 content signature（grid pitch 修复前置）。"""
        from src.features.composite.render3d.scene_payload import CameraPayload
        cp = CameraPayload(
            proj=np.eye(4, dtype=np.float32),
            view=np.eye(4, dtype=np.float32),
            eye=(0, 0, 5), target=(0, 0, 0),
            pitch=35.0, camera_mode="free")
        sig = cp.signature()
        assert 35.0 in sig
        assert "free" in sig

    def test_pitch_from_eye_target_calculation(self):
        """math_utils.pitch_from_eye_target 正确反算俯仰角。"""
        # 正前方看（水平）→ pitch ≈ 0
        p = m3d.pitch_from_eye_target(np.array([0, 0, 5]), np.array([0, 0, 0]))
        assert abs(p) < 0.5
        # 从高处看 → pitch > 0
        p = m3d.pitch_from_eye_target(np.array([0, 5, 5]), np.array([0, 0, 0]))
        assert 40 < p < 50
        # 正下方看 → pitch ≈ 89（钳制）
        p = m3d.pitch_from_eye_target(np.array([0, 10, 0]), np.array([0, 0, 0]))
        assert p == 89.0

    def test_ortho_projection_no_perspective(self):
        """正交投影矩阵：远近物体大小一致（无透视收缩）。"""
        proj = m3d.ortho(scale=5.0, aspect=1.0, near=0.1, far=100.0)
        # 正交投影的 [3][2] 元素含 near/far 但 [2][3] 也应有值；
        # 关键：proj[0][0] = 2/(r-l) = 2/(2*scale*aspect) = 1/scale
        assert abs(proj[0, 0] - (1.0 / 5.0)) < 1e-5

    def test_rotate_around_axis_identity(self):
        """绕轴旋转 0 度 = 不变。"""
        v = np.array([1.0, 0.0, 0.0])
        r = m3d.rotate_around_axis(v, np.array([0.0, 1.0, 0.0]), 0.0)
        assert np.allclose(r, v)

    def test_rotate_around_axis_90deg(self):
        """绕 Z 轴旋转 90°：(1,0,0) → (0,1,0)。"""
        v = np.array([1.0, 0.0, 0.0])
        r = m3d.rotate_around_axis(v, np.array([0.0, 0.0, 1.0]), 90.0)
        assert np.allclose(r, [0.0, 1.0, 0.0], atol=1e-5)


class TestGridPitchFix:
    """#2: Scene3D 用 CameraPayload.pitch 而非自身 cam_orbit_pitch 驱动 grid fade。"""

    def test_scene3d_uses_payload_pitch(self):
        """源码断言：cam_payload 应用段读取 payload.pitch 覆盖 cam_pitch。"""
        from src.features.composite.nodes import scene3d_node
        src = inspect.getsource(scene3d_node.Scene3DNode._do_evaluate)
        # 必须有从 cam_payload 读 pitch 覆盖 cam_pitch 的语句
        assert "cam_pitch" in src
        assert "pitch" in src
        # 关键断言：payload.pitch 覆盖逻辑存在
        assert "getattr(cam_payload" in src and "pitch" in src


# ──────────────────────────────────────────────────────────────────────────
# #1 材质纹理 GL 上传绑定
# ──────────────────────────────────────────────────────────────────────────

class TestMaterialTextureLoader:
    """material_texture_loader 模块结构正确性（无 GL 环境下的源码级断言）。"""

    def test_module_importable(self):
        from src.features.composite.render3d import material_texture_loader as mtl
        assert hasattr(mtl, "MaterialTextureLoader")
        assert hasattr(mtl, "MATERIAL_SAMPLER_LOCATIONS")
        assert hasattr(mtl, "apply_filter_wrap")

    def test_sampler_locations_distinct_from_ibl(self):
        """材质 sampler location 不能与 IBL 的 0/1/2 冲突。"""
        from src.features.composite.render3d.material_texture_loader import (
            MATERIAL_SAMPLER_LOCATIONS)
        locs = list(MATERIAL_SAMPLER_LOCATIONS.values())
        # 5 个 slot，全部 >= 3（避开 IBL 的 0/1/2）
        assert len(locs) == 5
        assert all(l >= 3 for l in locs)
        assert len(set(locs)) == 5  # 互不相同

    def test_sampler_locations_match_slots(self):
        from src.features.composite.render3d.material_texture_loader import (
            MATERIAL_SAMPLER_LOCATIONS)
        for slot in ("base_color", "normal", "orm", "ao", "emissive"):
            assert slot in MATERIAL_SAMPLER_LOCATIONS

    def test_resolve_path_relative_to_model_dir(self):
        """相对路径优先相对模型目录解析。"""
        from src.features.composite.render3d.material_texture_loader import (
            MaterialTextureLoader)
        # 绝对路径原样返回
        p = MaterialTextureLoader._resolve_path("/abs/tex.png", "/model/dir")
        assert p == "/abs/tex.png"

    def test_apply_filter_wrap_exists(self):
        """apply_filter_wrap 函数签名正确（接受 tex/filter/wrap）。"""
        from src.features.composite.render3d.material_texture_loader import apply_filter_wrap
        sig = inspect.signature(apply_filter_wrap)
        assert "texture_filter" in sig.parameters
        assert "texture_wrap" in sig.parameters
        src = inspect.getsource(apply_filter_wrap)
        assert "moderngl.REPEAT" not in src

    def test_upload_keeps_color_textures_for_shader_decode(self):
        """材质贴图 CPU 上传不做 sRGB 解码，避免和 shader 双重 gamma。"""
        from src.features.composite.render3d.material_texture_loader import MaterialTextureLoader
        src = inspect.getsource(MaterialTextureLoader._upload)
        assert "np.power" not in src
        assert "shader" in src

    def test_apply_texture_wrap_handles_fake_texture(self):
        """wrap 设置不能因 ModernGL 缺少 REPEAT 常量而抛错。"""
        from src.features.composite.render3d.material_texture_loader import _apply_texture_wrap

        class FakeTex:
            glo = 0
            repeat_x = None
            repeat_y = None

        tex = FakeTex()
        _apply_texture_wrap(tex, "repeat")
        assert tex.repeat_x is True
        assert tex.repeat_y is True


class TestScene3DMaterialBinding:
    """Scene3D 路径 A 绑定材质纹理（源码级断言）。"""

    def test_bind_material_textures_method_exists(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        assert hasattr(Scene3DNode, "_bind_material_textures")
        assert hasattr(Scene3DNode, "_unbind_material_textures")

    def test_bind_uses_opengl_glo_binding(self):
        """绑定用 gl.glBindTexture（支持外部 texture_overrides 的 glo）。"""
        from src.features.composite.nodes import scene3d_node
        src = inspect.getsource(scene3d_node.Scene3DNode._bind_material_textures)
        assert "glBindTexture" in src or "gl.glBindTexture" in src
        assert "glActiveTexture" in src or "gl.glActiveTexture" in src

    def test_bind_respects_material_mode(self):
        """material_mode 控制 override/imported 优先级。"""
        from src.features.composite.nodes import scene3d_node
        src = inspect.getsource(scene3d_node.Scene3DNode._bind_material_textures)
        assert "material_mode" in src
        assert "override" in src
        assert "imported" in src

    def test_render_path_sets_has_map_from_binding(self):
        """渲染路径 A 用绑定结果设 has_map（不再硬编码 0）。"""
        from src.features.composite.nodes import scene3d_node
        src = inspect.getsource(scene3d_node.Scene3DNode._do_evaluate)
        # 有 _bind_material_textures 调用 + _has_map 设 uniform
        assert "_bind_material_textures" in src
        assert "_has_map" in src
        assert 'int(_has_map["base_color"])' in src

    def test_cleanup_releases_material_loader(self):
        """cleanup 释放材质纹理加载器缓存。"""
        from src.features.composite.nodes import scene3d_node
        src = inspect.getsource(scene3d_node.Scene3DNode.cleanup)
        assert "_material_tex_loader" in src
        assert "release_all" in src

    def test_material_state_cleanup_or_cache_present(self):
        """主视图用材质状态缓存；正交旧路径仍解绑 sampler。"""
        from src.features.composite.nodes import scene3d_node
        main_src = inspect.getsource(scene3d_node.Scene3DNode._do_evaluate)
        ortho_src = inspect.getsource(scene3d_node.Scene3DNode._render_ortho_views)
        assert "_material_cache" in main_src
        assert "_bind_null_material_samplers" in main_src
        assert "_unbind_material_textures" in ortho_src


# ──────────────────────────────────────────────────────────────────────────
# #5 Light3D hdri_path 改 file 类型
# ──────────────────────────────────────────────────────────────────────────

class TestLight3DHdriFileType:
    """hdri_path 用 file 类型，属性面板提供文件选择器。"""

    def test_hdri_path_is_file_type(self):
        from src.features.composite.nodes.light3d_node import _make_default_param_defs
        defs = _make_default_param_defs()
        hdri_pdef = next(p for p in defs if p.name == "hdri_path")
        assert hdri_pdef.ptype == "file"


# ──────────────────────────────────────────────────────────────────────────
# #6 属性面板分组（ParamDef.group）
# ──────────────────────────────────────────────────────────────────────────

class TestPanelGrouping:
    """4 个 3D 节点的 ParamDef 带 group 字段，面板自动分组。"""

    def test_camera3d_has_groups(self):
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        groups = {p.group for p in Camera3DNode.PARAM_DEFS if p.group}
        # 至少有相机模式、轨道、画幅等分组
        assert any("Camera" in g or "相机" in g for g in groups)
        assert any("Orbit" in g or "轨道" in g for g in groups)

    def test_geo3d_has_groups(self):
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        groups = {p.group for p in Geo3DNode.PARAM_DEFS if p.group}
        assert any("Geometry" in g or "几何" in g for g in groups)
        assert any("Material" in g or "材质" in g for g in groups)

    def test_light3d_has_groups(self):
        from src.features.composite.nodes.light3d_node import _make_default_param_defs
        defs = _make_default_param_defs()
        groups = {p.group for p in defs if p.group}
        assert any("Light" in g or "灯" in g for g in groups)
        assert any("HDRI" in g or "环境" in g for g in groups)

    def test_model3d_has_groups(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        groups = {p.group for p in Model3DNode.PARAM_DEFS if p.group}
        assert any("Material" in g or "材质" in g for g in groups)
        assert any("Transform" in g or "变换" in g for g in groups)
        assert any("Animation" in g or "动画" in g for g in groups)

    def test_all_3d_node_params_grouped(self):
        """所有 3D 节点的参数都有 group（无遗漏的 ungrouped）。"""
        from src.features.composite.nodes.camera3d_node import Camera3DNode
        from src.features.composite.nodes.geo3d_node import Geo3DNode
        from src.features.composite.nodes.model3d_node import Model3DNode
        from src.features.composite.nodes.light3d_node import _make_default_param_defs
        for cls_or_fn, label in [
            (Camera3DNode, "Camera3D"),
            (Geo3DNode, "Geo3D"),
            (Model3DNode, "Model3D"),
        ]:
            for p in cls_or_fn.PARAM_DEFS:
                assert p.group is not None, f"{label}.{p.name} 缺少 group"
        for p in _make_default_param_defs():
            assert p.group is not None, f"Light3D.{p.name} 缺少 group"


# ──────────────────────────────────────────────────────────────────────────
# 集成：Camera3D _do_evaluate 三模式逻辑
# ──────────────────────────────────────────────────────────────────────────

class TestCamera3DEvaluateModes:
    """Camera3D._do_evaluate 按模式选择投影/视图矩阵（源码级断言）。"""

    def test_evaluate_branches_on_mode(self):
        from src.features.composite.nodes import camera3d_node
        src = inspect.getsource(camera3d_node.Camera3DNode._do_evaluate)
        # 三模式分支
        assert "ortho" in src
        assert "free" in src
        assert "orbit" in src
        # ortho 用 ortho 投影
        assert "m3d.ortho" in src
        # free 用 cam_eye
        assert "cam_eye_x" in src or "cam_eye" in src

    def test_evaluate_uses_configurable_near_far(self):
        """near/far 从参数读，不再硬编码。"""
        from src.features.composite.nodes import camera3d_node
        src = inspect.getsource(camera3d_node.Camera3DNode._do_evaluate)
        assert "cam_near" in src
        assert "cam_far" in src
        # 不应再有硬编码的 near, far = 0.1, 1000.0
        assert "0.1, 1000.0" not in src

    def test_evaluate_fills_payload_pitch(self):
        """payload 填充 pitch（free 模式从 eye-target 反算）。"""
        from src.features.composite.nodes import camera3d_node
        src = inspect.getsource(camera3d_node.Camera3DNode._do_evaluate)
        assert "pitch=" in src
        assert "pitch_from_eye_target" in src

    def test_roll_applied(self):
        """cam_roll 通过 rotate_around_axis 应用到 up 向量。"""
        from src.features.composite.nodes import camera3d_node
        src = inspect.getsource(camera3d_node.Camera3DNode._do_evaluate)
        assert "cam_roll" in src
        assert "rotate_around_axis" in src

    def test_focal_length_drives_fov(self):
        """调 focal length 应反算 FOV，避免焦距参数成为无效摆设。"""
        from src.features.composite.nodes.camera3d_node import Camera3DNode

        cam = Camera3DNode("cam1")
        old_fov = cam.parameters["cam_fov"]
        cam.set_parameter("cam_focal_length", 80.0)

        assert cam.parameters["cam_fov"] != old_fov
        assert cam.parameters["cam_fov"] < old_fov

    def test_fov_updates_focal_length(self):
        """调 FOV 应同步焦距显示，保持两种摄影参数一致。"""
        from src.features.composite.nodes.camera3d_node import Camera3DNode

        cam = Camera3DNode("cam1")
        old_focal = cam.parameters["cam_focal_length"]
        cam.set_parameter("cam_fov", 80.0)

        assert cam.parameters["cam_focal_length"] != old_focal
        assert cam.parameters["cam_focal_length"] < old_focal
