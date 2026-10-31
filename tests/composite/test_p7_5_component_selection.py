"""P7-5：component selection（面级 picking）测试。

覆盖：
- mesh_picking 模块的 ray-triangle intersection（Möller–Trumbore）
- pick_face：对 mesh 做最近命中查询
- screen_to_world_ray：屏幕坐标 → 世界射线
- ComponentMode.POLYGON 启用（之前禁用）
- eval_ctx.selected_faces_3d 字段

注：完整 UI 接线（viewer 点击 → face pick → Scene3D 高亮）作为后续阶段。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest


# ──────────────────────────────────────────────────────────────────────────
# mesh_picking：ray-triangle intersection
# ──────────────────────────────────────────────────────────────────────────

class TestRayTriangleIntersect:
    """Möller–Trumbore 单三角形 intersection。"""

    def test_basic_hit(self):
        from src.features.composite.render3d.mesh_picking import ray_triangle_intersect
        # XY 平面的三角形，z=0
        v0 = np.array([0, 0, 0], dtype=np.float64)
        v1 = np.array([1, 0, 0], dtype=np.float64)
        v2 = np.array([0, 1, 0], dtype=np.float64)
        # 射线从上方射下
        origin = np.array([0.25, 0.25, 1.0])
        direction = np.array([0, 0, -1.0])
        t, u, v = ray_triangle_intersect(origin, direction, v0, v1, v2)
        assert t > 0
        assert abs(t - 1.0) < 1e-6

    def test_parallel_no_hit(self):
        from src.features.composite.render3d.mesh_picking import ray_triangle_intersect
        v0 = np.array([0, 0, 0], dtype=np.float64)
        v1 = np.array([1, 0, 0], dtype=np.float64)
        v2 = np.array([0, 1, 0], dtype=np.float64)
        # 射线平行于 XY 平面
        origin = np.array([0, 0, 1.0])
        direction = np.array([1, 0, 0])
        t, u, v = ray_triangle_intersect(origin, direction, v0, v1, v2)
        assert t < 0

    def test_miss_outside(self):
        from src.features.composite.render3d.mesh_picking import ray_triangle_intersect
        v0 = np.array([0, 0, 0], dtype=np.float64)
        v1 = np.array([1, 0, 0], dtype=np.float64)
        v2 = np.array([0, 1, 0], dtype=np.float64)
        # 射线打在三角形外
        origin = np.array([2.0, 2.0, 1.0])
        direction = np.array([0, 0, -1.0])
        t, u, v = ray_triangle_intersect(origin, direction, v0, v1, v2)
        assert t < 0


# ──────────────────────────────────────────────────────────────────────────
# mesh_picking：pick_face（mesh 级最近命中）
# ──────────────────────────────────────────────────────────────────────────

class TestPickFace:
    """pick_face：对 mesh 做最近命中查询。"""

    def test_cube_face_pick(self):
        """单位立方体，从上方射线命中顶面。"""
        from src.features.composite.render3d.mesh_picking import pick_face
        verts = np.array([
            [0,0,0],[1,0,0],[1,1,0],[0,1,0],
            [0,0,1],[1,0,1],[1,1,1],[0,1,1],
        ], dtype=np.float32)
        indices = np.array([0,1,2, 0,2,3, 4,5,6, 4,6,7], dtype=np.uint32)
        origin = np.array([0.5, 0.5, 5.0])
        direction = np.array([0, 0, -1.0])
        hit = pick_face(origin, direction, verts, indices)
        assert hit.hit
        assert hit.triangle_index >= 2  # 顶面三角形（index 2 或 3）
        assert abs(hit.distance - 4.0) < 0.01
        assert abs(hit.point[2] - 1.0) < 0.01  # 命中顶面 z=1

    def test_miss_returns_no_hit(self):
        """射线偏离 mesh → 未命中。"""
        from src.features.composite.render3d.mesh_picking import pick_face
        verts = np.array([[0,0,0],[1,0,0],[0,1,0]], dtype=np.float32)
        indices = np.array([0,1,2], dtype=np.uint32)
        origin = np.array([5.0, 5.0, 5.0])
        direction = np.array([0, 0, -1.0])
        hit = pick_face(origin, direction, verts, indices)
        assert not hit.hit
        assert hit.triangle_index == -1

    def test_model_matrix_transform(self):
        """model_matrix 变换：mesh 局部坐标 → 世界。"""
        from src.features.composite.render3d.mesh_picking import pick_face
        verts = np.array([[0,0,0],[1,0,0],[0,1,0]], dtype=np.float32)
        indices = np.array([0,1,2], dtype=np.uint32)
        # 平移矩阵：mesh 从原点移到 (10, 0, 0)
        model = np.eye(4, dtype=np.float64)
        model[0, 3] = 10.0
        # 射线命中平移后的三角形
        origin = np.array([10.25, 0.25, 1.0])
        direction = np.array([0, 0, -1.0])
        hit = pick_face(origin, direction, verts, indices, model_matrix=model)
        assert hit.hit
        assert abs(hit.point[0] - 10.25) < 0.01

    def test_empty_mesh_no_crash(self):
        """空 mesh 不崩溃。"""
        from src.features.composite.render3d.mesh_picking import pick_face
        hit = pick_face(np.zeros(3), np.array([0,0,-1.0]),
                        np.array([]), np.array([]))
        assert not hit.hit

    def test_ray_hit_properties(self):
        """RayHit 属性。"""
        from src.features.composite.render3d.mesh_picking import RayHit
        hit = RayHit()
        assert not hit.hit
        assert hit.triangle_index == -1
        hit2 = RayHit(triangle_index=5, distance=2.0,
                      point=(1, 2, 3), barycentric=(0.3, 0.3, 0.4))
        assert hit2.hit
        assert hit2.triangle_index == 5


# ──────────────────────────────────────────────────────────────────────────
# mesh_picking：screen_to_world_ray
# ──────────────────────────────────────────────────────────────────────────

class TestScreenToWorldRay:
    """屏幕坐标 → 世界射线。"""

    def test_center_returns_forward(self):
        """屏幕中心 → 射线朝 -Z（标准相机）。"""
        from src.features.composite.render3d.mesh_picking import screen_to_world_ray
        proj = np.eye(4, dtype=np.float64)
        view = np.eye(4, dtype=np.float64)
        origin, direction = screen_to_world_ray((400, 300), 800, 600, proj, view)
        assert direction.shape == (3,)
        # 方向应归一化
        norm = float(np.linalg.norm(direction))
        assert abs(norm - 1.0) < 1e-6

    def test_y_flip(self):
        """屏幕 y 从顶部 → NDC y 翻转（底部原点）。

        验证：顶部 (y=0) 的 NDC y > 0；底部 (y=600) 的 NDC y < 0。
        screen_to_world_ray 内部 y = 1.0 - 2.0*py/h。
        """
        from src.features.composite.render3d.mesh_picking import screen_to_world_ray
        proj = np.eye(4, dtype=np.float64)
        view = np.eye(4, dtype=np.float64)
        # 顶部 y=0 → NDC y = 1.0 - 0 = 1.0 → 世界近平面 y=1
        # 底部 y=600 → NDC y = 1.0 - 2.0 = -1.0 → 世界近平面 y=-1
        o_top, _ = screen_to_world_ray((400, 0), 800, 600, proj, view)
        o_bot, _ = screen_to_world_ray((400, 600), 800, 600, proj, view)
        # 近平面（identity view 下 origin=z=-1 平面）的 y 坐标应相反
        assert o_top[1] > 0   # 顶部 → y > 0
        assert o_bot[1] < 0   # 底部 → y < 0


# ──────────────────────────────────────────────────────────────────────────
# ComponentMode 启用 + eval_ctx 字段
# ──────────────────────────────────────────────────────────────────────────

class TestComponentModeEnabled:
    """POLYGON mode 启用（之前禁用，P7-5 面级 picking 就绪）。"""

    def test_polygon_enabled_in_viewport(self):
        from src.features.composite.ui.composite_viewport import _3D_COMPONENT_ENABLED
        from src.features.composite.ui.viewport3d_state import ComponentMode
        assert _3D_COMPONENT_ENABLED[ComponentMode.POLYGON] is True

    def test_polygon_enabled_in_toolbar(self):
        from src.features.composite.ui.viewport3d_toolbar import _COMPONENT_ENABLED
        from src.features.composite.ui.viewport3d_state import ComponentMode
        assert _COMPONENT_ENABLED[ComponentMode.POLYGON] is True

    def test_object_still_enabled(self):
        from src.features.composite.ui.composite_viewport import _3D_COMPONENT_ENABLED
        from src.features.composite.ui.viewport3d_state import ComponentMode
        assert _3D_COMPONENT_ENABLED[ComponentMode.OBJECT] is True


class TestEvalContextSelectedFaces:
    """eval_ctx.selected_faces_3d 字段（P7-5）。"""

    def test_field_exists(self):
        from src.features.composite.core.eval_context import CompositeEvalContext
        ctx = CompositeEvalContext()
        assert hasattr(ctx, "selected_faces_3d")
        assert ctx.selected_faces_3d is None  # 默认无选中

    def test_field_assignable(self):
        from src.features.composite.core.eval_context import CompositeEvalContext
        ctx = CompositeEvalContext()
        ctx.selected_faces_3d = {"node1": [0, 5, 12]}
        assert ctx.selected_faces_3d == {"node1": [0, 5, 12]}
