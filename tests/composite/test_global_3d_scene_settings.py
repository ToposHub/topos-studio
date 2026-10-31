"""v9 重构测试：全局「3D Scene Settings」注入机制。

Scene3D 作为纯渲染器，所有场景功能（相机/灯光/背景/IBL/网格/MSAA/输出控制/
活动相机）统一来自 eval_ctx.global_3d_scene_settings（由 viewer_state[
"default_3d_scene"] 注入）。本测试验证：

1. DEFAULT_VIEWER3D_SCENE_SETTINGS 包含新增字段（HDRI/灯光颜色/输出控制/活动相机）
2. global_3d_scene_settings 字段在 CompositeEvalContext / RenderRequest 中存在
3. _sp() 辅助函数的三层优先级（view_mode_overrides > global > node params）
4. migrate_v8_to_v9 把 Scene3D 场景参数迁移到 viewer_state["default_3d_scene"]
5. 虚拟 viewer 场景仍正常（回归）

注：不真正建 GL context（CI 无 GPU）；用静态/反射 + mock 验证数据流。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock


# ── 1. DEFAULT_VIEWER3D_SCENE_SETTINGS 字段完整性 ──────────────────────

class TestDefaultSettingsFields:
    """全局默认设置包含 v9 新增的所有场景功能字段。"""

    def test_has_hdri_ibl_fields(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "hdri_preset" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert "use_ibl" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert "env_intensity" in DEFAULT_VIEWER3D_SCENE_SETTINGS

    def test_has_light_color_fields(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "light_color_r" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert "light_color_g" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert "light_color_b" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        # 默认白色（与旧行为一致）
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["light_color_r"] == 1.0
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["light_color_g"] == 1.0
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["light_color_b"] == 1.0

    def test_has_output_control_fields(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "output_bg_opaque" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert "output_show_grid" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        # 默认干净透明（合成输出不带回背景/网格）
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["output_bg_opaque"] is False
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["output_show_grid"] is False

    def test_has_aov_fields(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        for key in (
                "aov_depth", "aov_normal", "aov_position", "aov_uv",
                "aov_material_id",
                "aov_motion_vector", "aov_object_id"):
            assert key in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["aov_uv"] is False
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["aov_material_id"] is False

    def test_has_render_region_mask_fields(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        for key in (
            "render_region_enabled",
            "render_region_mask_enabled",
            "render_region_mask_opacity",
            "render_region_mask_r",
            "render_region_mask_g",
            "render_region_mask_b",
            "render_region_border_enabled",
            "render_region_border_opacity",
            "render_region_border_width",
            "render_region_border_r",
            "render_region_border_g",
            "render_region_border_b",
        ):
            assert key in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["render_region_enabled"] is True
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["render_region_mask_enabled"] is True

    def test_has_grid_param_fields(self):
        """地面网格视觉参数有默认键。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        for key in (
            "grid_cell_size",
            "grid_opacity",
            "grid_line_width",
            "grid_minor_color_r",
            "grid_minor_color_g",
            "grid_minor_color_b",
            "grid_major_color_r",
            "grid_major_color_g",
            "grid_major_color_b",
        ):
            assert key in DEFAULT_VIEWER3D_SCENE_SETTINGS, f"{key} missing"
        # 默认采用更细、更弱的 DCC 视口网格观感。
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_cell_size"] == 0.35
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_opacity"] == 0.25
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_line_width"] == 0.1
        for key in (
            "grid_minor_fade_start",
            "grid_minor_fade_end",
            "grid_minor_fade_slope",
            "grid_major_fade_start",
            "grid_major_fade_end",
            "grid_major_fade_slope",
        ):
            assert key not in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_minor_color_r"] == 0.10
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_minor_color_g"] == 0.10
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_minor_color_b"] == 0.12
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_major_color_r"] == 0.20
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_major_color_g"] == 0.20
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["grid_major_color_b"] == 0.23

    def test_has_viewport_bg_fields(self):
        """视口外框底色（viewport_bg_r/g/b）有默认键。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        for key in ("viewport_bg_r", "viewport_bg_g", "viewport_bg_b"):
            assert key in DEFAULT_VIEWER3D_SCENE_SETTINGS, f"{key} missing"
        # 默认 0.15 = 原 paintGL 硬编码值，保证现有观感不变
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["viewport_bg_r"] == 0.15
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["viewport_bg_g"] == 0.15
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["viewport_bg_b"] == 0.15

    def test_render_region_opacity_defaults(self):
        """render region mask/border opacity 默认值。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["render_region_mask_opacity"] == 0.25
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["render_region_border_opacity"] == 0.1

    def test_has_active_camera_id(self):
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        assert "active_camera_id" in DEFAULT_VIEWER3D_SCENE_SETTINGS
        assert DEFAULT_VIEWER3D_SCENE_SETTINGS["active_camera_id"] == ""

    def test_keeps_existing_fields(self):
        """已有的相机/灯光/背景/网格/MSAA 字段保持不变。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            DEFAULT_VIEWER3D_SCENE_SETTINGS)
        for key in ("cam_fov", "cam_dist", "cam_orbit_yaw", "cam_orbit_pitch",
                    "cam_target_x", "cam_target_y", "cam_target_z",
                    "light_dir_x", "light_dir_y", "light_dir_z", "light_intensity",
                    "bg_r", "bg_g", "bg_b", "bg_opaque",
                    "show_grid", "show_axes", "msaa"):
            assert key in DEFAULT_VIEWER3D_SCENE_SETTINGS, f"{key} missing"

    def test_merged_function_merges_defaults(self):
        """merged_viewer3d_scene_settings 用默认值补齐缺失键。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            merged_viewer3d_scene_settings, DEFAULT_VIEWER3D_SCENE_SETTINGS)
        # 空 dict → 全部默认
        merged = merged_viewer3d_scene_settings({})
        assert merged == DEFAULT_VIEWER3D_SCENE_SETTINGS
        # 部分覆盖
        merged = merged_viewer3d_scene_settings({"cam_fov": 90.0})
        assert merged["cam_fov"] == 90.0
        assert merged["msaa"] == DEFAULT_VIEWER3D_SCENE_SETTINGS["msaa"]


# ── 1b. reset_to_defaults() 重置全部参数（含新增 grid/viewport_bg） ──────

class TestResetToDefaults:
    """3D Scene Settings 面板的「重置」能把全部参数恢复成默认值。"""

    def test_reset_restores_all_keys(self, qapp):
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget, DEFAULT_VIEWER3D_SCENE_SETTINGS)
        widget = Viewer3DSettingsWidget()
        # 先把若干参数改成非默认值
        widget._set_value("grid_cell_size", 2.0)
        widget._set_value("viewport_bg_r", 0.9)
        widget._set_value("render_region_mask_opacity", 0.5)
        assert widget._settings["grid_cell_size"] == 2.0
        # 重置
        widget.reset_to_defaults()
        # 全部键恢复默认（含新增的 grid / viewport_bg / render_region）
        assert widget._settings == DEFAULT_VIEWER3D_SCENE_SETTINGS

    def test_reset_emits_settings_changed(self, qapp):
        """重置必须 emit 信号，否则 page 收不到、viewer_state 不回写。"""
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsWidget, DEFAULT_VIEWER3D_SCENE_SETTINGS)
        widget = Viewer3DSettingsWidget()
        emitted = []
        widget.settings_changed.connect(lambda s: emitted.append(s))
        widget.reset_to_defaults()
        assert len(emitted) == 1
        assert emitted[0] == DEFAULT_VIEWER3D_SCENE_SETTINGS


# ── 2. eval_ctx / RenderRequest 字段存在性 ──────────────────────────────

class TestContextFields:
    """全局设置经 eval_ctx / RenderRequest 透传到 Scene3D。"""

    def test_eval_ctx_has_global_3d_scene_settings(self):
        from src.features.composite.core.eval_context import CompositeEvalContext
        ctx = CompositeEvalContext(gl=None)
        assert hasattr(ctx, "global_3d_scene_settings")
        assert ctx.global_3d_scene_settings is None  # 默认 None（向后兼容）

    def test_eval_ctx_has_view_mode_overrides(self):
        from src.features.composite.core.eval_context import CompositeEvalContext
        ctx = CompositeEvalContext(gl=None)
        assert hasattr(ctx, "view_mode_overrides")
        assert ctx.view_mode_overrides is None

    def test_render_request_has_global_3d_scene_settings(self):
        from src.features.composite.render.render_scheduler import RenderRequest
        from src.features.composite.core.graph_snapshot import GraphSnapshot
        snap = GraphSnapshot(nodes={}, connections=[], eval_order=[], generation=0)
        req = RenderRequest(snapshot=snap, request_id=0, timestamp=0.0)
        assert hasattr(req, "global_3d_scene_settings")
        assert req.global_3d_scene_settings is None

    def test_render_request_has_view_mode_overrides(self):
        from src.features.composite.render.render_scheduler import RenderRequest
        from src.features.composite.core.graph_snapshot import GraphSnapshot
        snap = GraphSnapshot(nodes={}, connections=[], eval_order=[], generation=0)
        req = RenderRequest(snapshot=snap, request_id=0, timestamp=0.0)
        assert hasattr(req, "view_mode_overrides")
        assert req.view_mode_overrides is None


# ── 3. Scene3D 不再持有场景参数（PARAM_DEFS 精简） ────────────────────

class TestScene3DParamDefs:
    """Scene3D PARAM_DEFS 已移除场景/几何参数，只保留渲染器必需项。"""

    def test_scene_params_removed_from_param_defs(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        keys = {p.name for p in Scene3DNode.PARAM_DEFS}
        # 场景环境参数已移除（移至全局设置），几何/材质移至 Geo3D/Model3D。
        for removed in ("cam_fov", "cam_dist", "bg_opaque", "bg_r", "show_grid",
                        "show_axes", "msaa", "hdri_preset", "use_ibl",
                        "active_camera_id", "output_show_grid", "output_bg_opaque",
                        "light_dir_x", "light_intensity",
                        "primitive", "mesh_scale", "metallic", "roughness",
                        "object_color_r", "object_pos_x"):
            assert removed not in keys, f"{removed} should be removed from PARAM_DEFS"

    def test_renderer_params_kept(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        keys = {p.name for p in Scene3DNode.PARAM_DEFS}
        # 渲染器必需参数保留
        for kept in ("width", "height", "format_name"):
            assert kept in keys, f"{kept} should remain"


# ── 4. v8→v9 迁移：Scene3D 场景参数 → 全局设置 ────────────────────────

class TestV9Migration:
    """migrate_v8_to_v9 把 Scene3D 场景参数迁移到 viewer_state。"""

    def _make_v8_scene3d(self, with_scene_params=True):
        """构造一个 v8 schema 的 graph dict（Scene3D 持有场景参数）。"""
        params = {
            "width": 1920, "height": 1080, "format_name": "Scene3D",
            "primitive": "none",
        }
        if with_scene_params:
            params.update({
                "cam_fov": 65.0, "cam_dist": 8.0,
                "bg_opaque": False, "bg_r": 0.1, "bg_g": 0.2, "bg_b": 0.3,
                "show_grid": False, "show_axes": False,
                "msaa": 8, "hdri_preset": "studio.hdr",
                "active_camera_id": "cam_0",
                "light_dir_x": 0.5, "light_dir_y": -0.5, "light_dir_z": 0.7,
                "light_intensity": 3.0,
            })
        return {
            "composite_schema_version": 8,
            "nodes": [{"id": "s1", "type": "scene3d", "position": [0, 0],
                       "parameters": params, "disabled": False}],
            "connections": [],
        }

    def test_migrates_scene_params_to_global(self):
        from src.features.composite.core.graph_migration import migrate_graph
        result = migrate_graph(self._make_v8_scene3d())
        default_scene = result.get("viewer_state", {}).get("default_3d_scene", {})
        # 场景参数应在全局设置里
        assert default_scene.get("cam_fov") == 65.0
        assert default_scene.get("msaa") == 8
        assert default_scene.get("hdri_preset") == "studio.hdr"
        assert default_scene.get("active_camera_id") == "cam_0"

    def test_removes_scene_params_from_node(self):
        from src.features.composite.core.graph_migration import migrate_graph
        result = migrate_graph(self._make_v8_scene3d())
        scene3d = [n for n in result["nodes"] if n["type"] == "scene3d"][0]
        keys = set(scene3d["parameters"].keys())
        # 场景参数与 legacy 几何参数从节点上删除
        for removed in ("cam_fov", "bg_opaque", "show_grid", "msaa",
                        "hdri_preset", "active_camera_id", "primitive"):
            assert removed not in keys, f"{removed} should be removed from node"
        # 渲染器参数保留
        for kept in ("width", "height", "format_name"):
            assert kept in keys, f"{kept} should remain"

    def test_bumps_schema_version_to_current(self):
        from src.features.composite.core.graph_migration import (
            migrate_graph, COMPOSITE_SCHEMA_VERSION)
        # B4 bumped the schema to v11 (user_label); keep the ceiling check
        # loose so future additive bumps do not rewrite this test.
        assert COMPOSITE_SCHEMA_VERSION >= 10
        result = migrate_graph(self._make_v8_scene3d())
        assert result["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    def test_v9_to_v10_normalises_layer_stacks(self):
        """v9→v10：layer_stacks/layers 的 None 与损坏值被规范化。"""
        from src.features.composite.core.graph_migration import migrate_graph
        data = {
            "composite_schema_version": 9,
            "nodes": [{"id": "m1", "type": "merge", "position": [0, 0],
                       "parameters": {}, "disabled": False}],
            "connections": [],
            "layer_stacks": None,
            "layers": "corrupt-not-a-dict",
        }
        result = migrate_graph(data)
        assert result["composite_schema_version"] >= 10
        assert "layer_stacks" not in result  # None → 移除
        assert "layers" not in result        # 非 dict 损坏值 → 移除
        # 合法 dict 保留
        data_ok = dict(data, layer_stacks={"stack_0": {"name": "main"}})
        result_ok = migrate_graph(data_ok)
        assert result_ok["layer_stacks"] == {"stack_0": {"name": "main"}}

    def test_no_scene3d_skips_migration(self):
        """无 Scene3D 节点时不影响 viewer_state。"""
        from src.features.composite.core.graph_migration import migrate_graph
        data = {
            "composite_schema_version": 8,
            "nodes": [{"id": "m1", "type": "merge", "position": [0, 0],
                       "parameters": {}, "disabled": False}],
            "connections": [],
        }
        result = migrate_graph(data)
        # 无 Scene3D → default_3d_scene 不存在（或空）
        assert not result.get("viewer_state", {}).get("default_3d_scene")

    def test_first_scene3d_used_as_global_default(self):
        """多个 Scene3D 时，取第一个的场景参数作为全局默认。"""
        from src.features.composite.core.graph_migration import migrate_graph
        data = {
            "composite_schema_version": 8,
            "nodes": [
                {"id": "s1", "type": "scene3d", "position": [0, 0],
                 "parameters": {"width": 1920, "height": 1080,
                                "cam_fov": 45.0, "msaa": 2},
                 "disabled": False},
                {"id": "s2", "type": "scene3d", "position": [200, 0],
                 "parameters": {"width": 1280, "height": 720,
                                "cam_fov": 90.0, "msaa": 8},
                 "disabled": False},
            ],
            "connections": [],
        }
        result = migrate_graph(data)
        default_scene = result.get("viewer_state", {}).get("default_3d_scene", {})
        # 取第一个 Scene3D 的值
        assert default_scene.get("cam_fov") == 45.0
        assert default_scene.get("msaa") == 2
        # 两个 Scene3D 都不再有场景参数
        for nd in result["nodes"]:
            if nd["type"] == "scene3d":
                assert "cam_fov" not in nd["parameters"]
                assert "msaa" not in nd["parameters"]

    def test_existing_global_settings_preserved(self):
        """v9 前已存在的全局设置优先（仅补缺失键）。"""
        from src.features.composite.core.graph_migration import migrate_graph
        data = {
            "composite_schema_version": 8,
            "viewer_state": {"default_3d_scene": {"cam_fov": 100.0}},
            "nodes": [{"id": "s1", "type": "scene3d", "position": [0, 0],
                       "parameters": {"width": 1920, "height": 1080,
                                      "cam_fov": 50.0, "msaa": 4},
                       "disabled": False}],
            "connections": [],
        }
        result = migrate_graph(data)
        default_scene = result.get("viewer_state", {}).get("default_3d_scene", {})
        # 已有值优先（100.0，不是节点的 50.0）
        assert default_scene.get("cam_fov") == 100.0
        # 缺失键由节点补齐
        assert default_scene.get("msaa") == 4


# ── 5. 虚拟 viewer 场景回归 ─────────────────────────────────────────────

class TestVirtualViewerScene:
    """虚拟 viewer 场景（__viewer3d_scene__）仍正常注入快照。"""

    def test_virtual_node_id_constant(self):
        from src.features.composite.ui.composite_page import CompositePage
        assert CompositePage._VIEWER3D_VIRTUAL_NODE_ID == "__viewer3d_scene__"

    def test_inject_uses_global_settings(self):
        """_inject_viewer3d_scene_snapshot 用 merged_viewer3d_scene_settings。"""
        import inspect
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._inject_viewer3d_scene_snapshot)
        assert "merged_viewer3d_scene_settings" in src
        assert "default_3d_scene" in src


class TestProjectResolutionSync:
    """Project resolution should seed renderer-style scene outputs."""

    def test_project_settings_updates_scene3d_resolution(self):
        from src.features.composite.ui.composite_page import CompositePage

        output = SimpleNamespace(
            id="out1", node_type="output",
            parameters={"width": 1920, "height": 1080, "format_mode": "input"},
        )
        scene = SimpleNamespace(
            id="scene1", node_type="scene3d",
            parameters={"width": 1920, "height": 1080},
        )
        updates = []

        class Graph:
            nodes = {"out1": output, "scene1": scene}

            def set_node_parameter(self, node_id, key, value):
                updates.append((node_id, key, value))
                self.nodes[node_id].parameters[key] = value

        page = CompositePage.__new__(CompositePage)
        page._node_graph = Graph()
        page._emit_user_edit_once = MagicMock()
        page._evaluate_graph = MagicMock()

        page._apply_project_settings({
            "width": 2048,
            "height": 858,
        })

        assert ("scene1", "width", 2048) in updates
        assert ("scene1", "height", 858) in updates
        assert scene.parameters["width"] == 2048
        assert scene.parameters["height"] == 858

    def test_new_scene3d_uses_project_output_size(self):
        from src.features.composite.ui.composite_page import CompositePage

        output = SimpleNamespace(
            id="out1", node_type="output",
            parameters={"width": 4096, "height": 1716},
        )
        scene = SimpleNamespace(
            node_type="scene3d",
            parameters={"width": 1920, "height": 1080},
        )
        scene.set_parameter = lambda key, value: scene.parameters.__setitem__(key, value)
        page = CompositePage.__new__(CompositePage)
        page._node_graph = SimpleNamespace(nodes={"out1": output})

        page._initialize_new_3d_helper_node(scene)

        assert scene.parameters["width"] == 4096
        assert scene.parameters["height"] == 1716
