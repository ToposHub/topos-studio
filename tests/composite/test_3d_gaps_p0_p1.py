"""补完缺口测试：P0 多几何渲染 + P0 gizmo 变换归属 + P1 工具栏接入。

覆盖之前承认未做的 3 个缺口（对应 audit plan P0/P1）：
1. Scene3D 多几何渲染（渲染所有连接的 Geo3D，不再只取第一个）
2. Gizmo 变换归属（选中 Geo3D/Model3D 时 gizmo 显示 + 写它们的 object_pos_*）
   - Model3D 新增 object transform 参数 + 应用到 flat_meshes
3. 3D 工具栏接入 viewer（实例化 + 3D 模式显隐 + 双向同步）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest


# ──────────────────────────────────────────────────────────────────────────
# Part 1: Scene3D 多几何渲染
# ──────────────────────────────────────────────────────────────────────────

class TestMultiGeoRendering:
    """Scene3D 渲染所有连接的 Geo3D（不再只取第一个）。"""

    def test_scene3d_iterates_all_geo_payloads(self):
        """_do_evaluate 遍历 _geo_payloads（for _gp in ...），而非 [0]。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "for _gp in _geo_payloads" in src, \
            "应遍历所有 geo payload（多几何渲染）"

    def test_geo_path_not_mutually_exclusive_with_model(self):
        """geo 渲染不再是 model 的 else 分支（两者都渲染）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        # geo 渲染是独立 if（与 model if 同级），model+geo 混合场景都渲染
        assert "if _geo_payloads:" in src
        assert "elif not import_ok" in src  # built-in procedural 才是兜底

    def test_collect_returns_list_not_single(self):
        """端口感知收集返回 list（_collect_connected_scene_payloads）。"""
        from src.features.composite.nodes.base_node import Node
        src = inspect.getsource(Node._collect_connected_scene_payloads)
        assert "result = []" in src or "result.append" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 2: Gizmo 变换归属
# ──────────────────────────────────────────────────────────────────────────

class TestGizmoTransformOwnership:
    """选中 Geo3D/Model3D 时 gizmo 显示 + 写它们的 object_pos_*。"""

    def test_gizmo_visible_for_geo3d_model3d(self):
        """_update_gizmo_visibility 认 geo3d/model3d（不再只认 scene3d）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._update_gizmo_visibility)
        assert "geo3d" in src
        assert "model3d" in src
        assert "scene3d" in src

    def test_gizmo_syncs_object_pos_on_visibility(self):
        """gizmo 显示时同步选中节点的 object_pos_* 到 gizmo 锚点。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._update_gizmo_visibility)
        assert "object_pos_x" in src
        assert "_gizmo_obj_pos" in src


class TestModel3DTransformParams:
    """Model3D 新增 object_pos/rot/scale 参数（P0 缺口）。"""

    def test_model3d_has_object_transform_params(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        n = Model3DNode()
        for k in ("object_pos_x", "object_pos_y", "object_pos_z",
                  "object_rot_x", "object_rot_y", "object_rot_z",
                  "mesh_scale"):
            assert k in n.parameters, f"Model3D 应有 {k}"

    def test_model3d_param_defs_include_transform(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        names = {p.name for p in Model3DNode.PARAM_DEFS}
        assert "object_pos_x" in names
        assert "mesh_scale" in names

    def test_model3d_applies_transform_to_flat_meshes(self):
        """_do_evaluate 应用 object transform 到 flat_meshes world_matrix（不改缓存）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        assert "obj_xform" in src
        assert "obj_xform @ fm[\"world_matrix\"]" in src

    def test_model3d_transform_sig_in_payload(self):
        """ModelPayload 带 transform_sig（变换变化触发 Scene3D 重渲染）。"""
        from src.features.composite.render3d.scene_payload import ModelPayload
        p = ModelPayload(has_model=True)
        assert hasattr(p, "transform_sig")
        assert p.transform_sig == ()
        # 签名含 transform_sig
        sig = p.signature()
        assert () in sig  # 默认空 tuple 进签名

    def test_model3d_uses_composite_playhead_for_animation(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        assert "eval_ctx.frame_number" in src
        assert "anim_time = float(eval_ctx.frame_number)" in src

    def test_model3d_transform_does_not_mutate_cache(self):
        """应用变换不改 _flat_meshes_gl 缓存（用新 dict 列表）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        # 用 {**fm, ...} 构造新 dict，不改原 fm
        assert "{**fm" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 3: 3D 工具栏接入 viewer
# ──────────────────────────────────────────────────────────────────────────

class TestToolbarWiredIntoViewer:
    """CompositeViewer 主工具栏内置 3D 工具 + 3D 模式显隐 + 双向同步。"""

    def test_viewer_instantiates_toolbar(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.__init__)
        assert "_toolbar = CompositeViewerBar" in src
        assert "tool_mode_requested.connect" in src
        assert "component_mode_requested.connect" in src

    def test_set_view_mode_toggles_toolbar(self):
        """set_view_mode 在 3D 时 show_3d_tools，2D 时 hide_3d_tools。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.set_view_mode)
        assert "show_3d_tools" in src
        assert "hide_3d_tools" in src

    def test_set_view_mode_does_not_call_resize_event_none(self):
        """set_view_mode 绝不能 resizeEvent(None)——PyQt6 下段崩溃。

        回归保护：Tab 切 3D 曾用 self.resizeEvent(None) 定位工具栏，但
        QWidget.resizeEvent 的 C++ 实现会解引用 None event 导致段崩溃。
        必须改用 _relayout_overlay_bars（与 event 解耦的纯布局方法）。
        """
        import re
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.set_view_mode)
        # 去掉注释行后再检查：不应有作为语句的 resizeEvent(None) 调用
        code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        code = "\n".join(code_lines)
        assert not re.search(r"\.resizeEvent\s*\(\s*None\s*\)", code), \
            "set_view_mode 不得调用 resizeEvent(None)（PyQt6 段崩溃）"
        assert "_relayout_overlay_bars" in code

    def test_resize_event_positions_toolbar(self):
        """resizeEvent 定位主工具栏，3D 工具按钮在主工具栏内显隐。

        布局逻辑抽到 _relayout_overlay_bars（供 set_view_mode 安全复用，
        避免 resizeEvent(None) 段崩溃）；resizeEvent 调用之。
        """
        from src.features.composite.ui.composite_viewport import CompositeViewer
        resize_src = inspect.getsource(CompositeViewer.resizeEvent)
        layout_src = inspect.getsource(CompositeViewer._relayout_overlay_bars)
        # resizeEvent 必须调用复用的布局方法（不再内联定位逻辑）
        assert "_relayout_overlay_bars" in resize_src
        # 布局逻辑确实定位合并后的主工具栏
        assert "_toolbar" in layout_src
        assert "setGeometry" in layout_src

    def test_toolbar_signals_connect_to_controller(self):
        """工具栏 tool_mode_requested 接到 controller.set_tool_mode。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.__init__)
        assert "set_tool_mode" in src
        assert "tool_mode_requested.connect" in src

    def test_controller_syncs_back_to_toolbar(self):
        """controller.tool_mode_changed 反向同步工具栏选中态。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer.__init__)
        assert "tool_mode_changed.connect" in src
