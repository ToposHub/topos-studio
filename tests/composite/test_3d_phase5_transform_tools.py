"""Phase 5: 变换工具测试（rotate/scale gizmo + mode 切换）。

覆盖 docs/composite_3d_module_audit_optimization_plan_2026-06-29.md 的 Phase 5：
1. TransformGizmo GizmoMode（MOVE/ROTATE/SCALE）
2. rotate 环 + scale 句柄的 build_draw_list / hit_test
3. drag_rotate / drag_scale 增量计算
4. viewer 接线（mode 跟随工具模式、release 通用化提交 pos/rot/scale）

注：gizmo 是纯数学，用单位投影矩阵验证；viewer 用源码级断言。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest

from src.features.composite.render3d.gizmo_overlay import (
    TransformGizmo, GizmoMode, GizmoAxisDraw, GizmoRingDraw, GizmoScaleHandleDraw,
    GizmoPlaneHandleDraw, GizmoCenterDraw,
    AXIS_INFO, HOVER_COLOR)


def _setup_gizmo(mode: GizmoMode = GizmoMode.MOVE) -> TransformGizmo:
    """构造一个已 update 的 gizmo（单位投影矩阵，物体在原点）。"""
    g = TransformGizmo()
    g.mode = mode
    proj = np.eye(4)
    view = np.eye(4)
    g.update(np.array([0.0, 0.0, 0.0]), proj, view, 800, 600,
             cam_dist=5.0, cam_fov=50.0)
    return g


# ──────────────────────────────────────────────────────────────────────────
# Part 1: GizmoMode + build_draw_list
# ──────────────────────────────────────────────────────────────────────────

class TestGizmoModes:
    """GizmoMode enum + build_draw_list 按 mode 返回正确数据。"""

    def test_gizmo_mode_values(self):
        assert {m.value for m in GizmoMode} == {"move", "rotate", "scale"}

    def test_default_mode_is_move(self):
        g = TransformGizmo()
        assert g.mode == GizmoMode.MOVE

    def test_move_returns_axes(self):
        g = _setup_gizmo(GizmoMode.MOVE)
        dl = g.build_draw_list()
        axes = [d for d in dl if isinstance(d, GizmoAxisDraw)]
        planes = [d for d in dl if isinstance(d, GizmoPlaneHandleDraw)]
        centers = [d for d in dl if isinstance(d, GizmoCenterDraw)]
        assert len(axes) == 3
        assert len(planes) == 3
        assert len(centers) == 1

    def test_rotate_returns_rings(self):
        g = _setup_gizmo(GizmoMode.ROTATE)
        dl = g.build_draw_list()
        rings = [d for d in dl if isinstance(d, GizmoRingDraw)]
        centers = [d for d in dl if isinstance(d, GizmoCenterDraw)]
        assert len(rings) == 4  # 三轴旋转环 + C4D 灰白外圈
        assert len(centers) == 1
        # 每个环有 RING_SEGMENTS 个折线点
        for r in rings:
            assert len(r.points_uv) == TransformGizmo.RING_SEGMENTS

    def test_scale_returns_axes_and_handles(self):
        g = _setup_gizmo(GizmoMode.SCALE)
        dl = g.build_draw_list()
        handles = [d for d in dl if isinstance(d, GizmoScaleHandleDraw)]
        axes = [d for d in dl if isinstance(d, GizmoAxisDraw)]
        assert len(handles) == 3  # 三轴各一个缩放句柄
        assert len(axes) == 3     # 三轴本体（SCALE 模式仍画轴）

    def test_hover_highlights_axis(self):
        """hover/active 时对应轴变黄色。"""
        g = _setup_gizmo(GizmoMode.MOVE)
        g.hovered_axis = "x"
        dl = g.build_draw_list()
        x_draw = next(d for d in dl if d.name == "x")
        assert x_draw.color == HOVER_COLOR


# ──────────────────────────────────────────────────────────────────────────
# Part 2: hit_test 按 mode
# ──────────────────────────────────────────────────────────────────────────

class TestHitTestByMode:
    """hit_test 按当前 mode 判定（轴/环/句柄）。"""

    def test_move_hits_axis(self):
        """MOVE 模式：点击轴附近 → 命中该轴。"""
        g = _setup_gizmo(GizmoMode.MOVE)
        # 物体在原点，投影为单位矩阵 → 纹理中心 (400, 300)
        # 点击 X 轴方向（右）应命中 x
        hit = g.hit_test((450, 300))
        assert hit == "x"

    def test_move_hits_axis_arrow_tip_generously(self):
        """MOVE 模式：箭头端点附近也应容易命中。"""
        g = _setup_gizmo(GizmoMode.MOVE)
        _, x_end = g._axis_tex_px["x"]
        hit = g.hit_test((x_end[0] + 32.0, x_end[1]))
        assert hit == "x"

    def test_move_hits_foreshortened_axis_endpoint(self):
        """轴朝向相机投影变短时，端点热点仍应容易命中。"""
        from src.features.composite.render3d import math_utils as m3d

        g = TransformGizmo()
        g.mode = GizmoMode.MOVE
        proj = m3d.perspective(50.0, 800 / 600, 0.1, 1000.0)
        view = m3d.look_at(
            m3d.vec3(6.0, 0.0, 0.05),
            m3d.vec3(0.0, 0.0, 0.0),
            m3d.vec3(0.0, 1.0, 0.0),
        )
        g.update(np.array([0.0, 0.0, 0.0]), proj, view, 800, 600,
                 cam_dist=6.0, cam_fov=50.0)
        _, x_end = g._axis_tex_px["x"]
        hit = g.hit_test((x_end[0] + 38.0, x_end[1]))
        assert hit == "x"

    def test_rotate_hits_ring(self):
        """ROTATE 模式：点击环附近 → 命中某环。"""
        g = _setup_gizmo(GizmoMode.ROTATE)
        # 环半径 75px，中心 (400,300)；点击环上一点
        hit = g.hit_test((470, 300))
        assert hit is not None

    def test_scale_hits_handle(self):
        """SCALE 模式：点击轴端点句柄 → 命中该轴。"""
        g = _setup_gizmo(GizmoMode.SCALE)
        # X 轴端点句柄位置（投影后约 680,300）；用实际算出的中心点击
        x_center = g._scale_handles_tex_px["x"][0]
        hit = g.hit_test((x_center[0], x_center[1]))
        assert hit == "x"

    def test_scale_hits_near_handle_visual_square(self):
        """SCALE 模式：端点方块附近的合理点击也应命中。"""
        g = _setup_gizmo(GizmoMode.SCALE)
        x_center = g._scale_handles_tex_px["x"][0]
        hit = g.hit_test((x_center[0] + 15.0, x_center[1]))
        assert hit == "x"

    def test_miss_returns_none(self):
        """点击远离任何可拾取元素 → None。"""
        g = _setup_gizmo(GizmoMode.MOVE)
        hit = g.hit_test((10, 10))  # 远离中心
        assert hit is None


# ──────────────────────────────────────────────────────────────────────────
# Part 3: drag_rotate / drag_scale 增量计算
# ──────────────────────────────────────────────────────────────────────────

class TestDragRotateScale:
    """旋转/缩放拖动增量计算。"""

    def test_drag_move_returns_pos(self):
        g = _setup_gizmo(GizmoMode.MOVE)
        g.begin_drag("x", (0.0, 0.0))
        result = g.drag_offset((0.5, 0.0))
        assert result is not None
        assert "object_pos_x" in result

    def test_axis_drag_same_mouse_position_does_not_jump(self):
        """按下后鼠标未移动时，物体应保持原地而不是吸到鼠标射线投影点。"""
        g = _setup_gizmo(GizmoMode.MOVE)
        g.begin_drag("x", (0.5, 0.0))
        result = g.drag_offset((0.5, 0.0))
        assert result is not None
        assert result["object_pos_x"] == pytest.approx(0.0)

    def test_drag_rotate_returns_rot(self):
        """ROTATE 模式拖动 → 返回 object_rot_*。"""
        g = _setup_gizmo(GizmoMode.ROTATE)
        g.set_object_rot((10.0, 20.0, 30.0))
        g.begin_drag("y", (0.0, 0.0))
        # 鼠标向右移 0.5 NDC
        result = g.drag_offset((0.5, 0.0))
        assert result is not None
        assert "object_rot_y" in result
        # 旋转应大于起始值（向右拖 → 增大）
        assert result["object_rot_y"] > 20.0

    def test_drag_scale_returns_mesh_scale(self):
        """SCALE 模式拖动 → 返回 mesh_scale。"""
        g = _setup_gizmo(GizmoMode.SCALE)
        g.set_object_scale(1.0)
        g.begin_drag("x", (0.0, 0.0))
        # 鼠标向右移 → 放大
        result = g.drag_offset((0.5, 0.0))
        assert result is not None
        assert "mesh_scale" in result
        assert result["mesh_scale"] > 1.0

    def test_drag_scale_clamped_positive(self):
        """缩放下限保护：mesh_scale 不会 ≤ 0。"""
        g = _setup_gizmo(GizmoMode.SCALE)
        g.set_object_scale(0.5)
        g.begin_drag("x", (0.0, 0.0))
        # 鼠标大幅向左 → 缩小但不会负
        result = g.drag_offset((-5.0, 0.0))
        assert result["mesh_scale"] > 0.0

    def test_begin_drag_records_start_values(self):
        """begin_drag 记录起始 rot/scale（增量计算锚点）。"""
        g = _setup_gizmo(GizmoMode.ROTATE)
        g.set_object_rot((5.0, 10.0, 15.0))
        g.begin_drag("z", (0.0, 0.0))
        assert hasattr(g, "_drag_start_rot")
        # 起始旋转记录正确
        assert g._drag_start_rot[2] == 15.0  # z 分量

    def test_end_drag_clears_state(self):
        """end_drag 清除所有拖动状态。"""
        g = _setup_gizmo(GizmoMode.SCALE)
        g.begin_drag("x", (0.0, 0.0))
        g.end_drag()
        assert g.active_axis is None
        assert not hasattr(g, "_drag_start_scale")


# ──────────────────────────────────────────────────────────────────────────
# Part 4: viewer 接线（源码级断言）
# ──────────────────────────────────────────────────────────────────────────

class TestViewerTransformWiring:
    """CompositeViewer gizmo 模式跟随工具模式 + release 通用化。"""

    def test_press_sets_gizmo_mode_from_tool(self):
        """_handle_3d_left_press 据 tool_mode 设 gizmo.mode。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_3d_left_press)
        assert "GizmoMode" in src
        assert "_viewport3d_ctrl.state.tool_mode" in src
        # 三种模式映射
        assert "rotate3d" in src or "ROTATE" in src
        assert "scale3d" in src or "SCALE" in src

    def test_press_syncs_object_rot_and_scale(self):
        """press 时同步 object_rot/mesh_scale 到 gizmo（锚点用）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_3d_left_press)
        assert "set_object_rot" in src
        assert "set_object_scale" in src

    def test_press_passes_ndc_to_begin_drag(self):
        """begin_drag 传入 mouse NDC（旋转/缩放增量锚点）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_3d_left_press)
        assert "begin_drag(axis, tex_ndc)" in src

    def test_release_commits_generalized_result(self):
        """_handle_gizmo_drag_release 提交最近一次拖动结果（pos/rot/scale 通用）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_gizmo_drag_release)
        assert "_last_gizmo_drag_result" in src
        assert "Phase 5" in src
        assert "else {}" in src

    def test_drag_move_tracks_last_result(self):
        """_handle_gizmo_drag_move 记录最近一次拖动结果（release 提交用）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_gizmo_drag_move)
        assert "_last_gizmo_drag_result" in src

    def test_undo_before_includes_rot_and_scale(self):
        """undo-before 快照包含 object_rot_* 和 mesh_scale（Phase 5 扩展）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_3d_left_press)
        assert "object_rot_x" in src
        assert "mesh_scale" in src


class TestDeformer3DGizmoWiring:
    """F13：Deformer3D gizmo 手柄——选中时显示，拖拽改 center。"""

    def test_gizmo_visible_for_deformer3d(self):
        """_update_gizmo_visibility 白名单含 deformer3d。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._update_gizmo_visibility)
        assert "deformer3d" in src

    def test_gizmo_anchor_uses_center(self):
        """选中 deformer3d 时 gizmo 锚点从 center_x/y/z 取（非 object_pos）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._update_gizmo_visibility)
        assert "center_x" in src
        assert "center_y" in src
        assert "center_z" in src

    def test_deformer_gizmo_result_translation(self):
        """_deformer_gizmo_result_to_params 把 object_pos 翻译成 center。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        assert hasattr(CompositeViewer, "_deformer_gizmo_result_to_params")
        src = inspect.getsource(CompositeViewer._deformer_gizmo_result_to_params)
        assert 'center_x' in src
        assert "object_pos_x" in src

    def test_drag_move_routes_deformer_translation(self):
        """_handle_gizmo_drag_move 对 deformer3d 调翻译方法。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._handle_gizmo_drag_move)
        assert "_deformer_gizmo_result_to_params" in src

    def test_realtime_preview_whitelist_includes_deformer(self):
        """_is_realtime_3d_transform_preview 白名单含 deformer3d（否则拖拽卡顿）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._is_realtime_3d_transform_preview)
        assert "deformer3d" in src

    def test_undo_keys_include_deformer_center(self):
        """_on_viewer3d_nav_started 对 deformer3d 记录 center_x/y/z（undo 用）。"""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_viewer3d_nav_started)
        # deformer3d 分支应记录 center 参数
        assert "deformer3d" in src
        assert "center_x" in src
