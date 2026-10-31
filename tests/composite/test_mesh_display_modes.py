"""F2 Mesh 显示模式单测（master_plan §5 / §21.5）。

覆盖：
1. mesh_diagnostics.build_unique_edges：去重边、共享边、退化输入。
2. build_normal_lines / build_tangent_lines：线段顶点格式。
3. compute_uv_stretch：面积比、退化三角形。
4. build_point_buffer：降采样。
5. ViewportDisplayMode enum + Viewport3DState.display_mode 默认值。

纯 numpy / dataclass，无 GL/PyQt 依赖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.render3d import mesh_diagnostics as md
from src.features.composite.render3d import primitives as prim
from src.features.composite.ui.viewport3d_state import (
    Viewport3DState, ViewportDisplayMode, DISPLAY_MODE_LABELS)

pytestmark = pytest.mark.composite_core


# ── build_unique_edges ───────────────────────────────────────────────

class TestBuildUniqueEdges:
    def test_single_triangle_three_edges(self):
        """单三角形 → 3 条唯一边。"""
        idx = np.array([0, 1, 2], dtype=np.uint32)
        edges = md.build_unique_edges(idx)
        assert edges.shape == (3, 2)
        # 所有 a < b
        assert np.all(edges[:, 0] < edges[:, 1])

    def test_shared_edge_dedup(self):
        """两个相邻三角形共享一条边 → 去重后 5 条边（非 6）。"""
        # 两个三角形共享边 (0,1)
        idx = np.array([0, 1, 2, 0, 1, 3], dtype=np.uint32)
        edges = md.build_unique_edges(idx)
        assert edges.shape[0] == 5  # (0,1),(0,2),(1,2),(0,3),(1,3)

    def test_two_separate_triangles_six_edges(self):
        """两个不相邻三角形 → 6 条边。"""
        idx = np.array([0, 1, 2, 3, 4, 5], dtype=np.uint32)
        edges = md.build_unique_edges(idx)
        assert edges.shape[0] == 6

    def test_empty_input(self):
        """空输入或不足 3 索引 → 空数组。"""
        assert md.build_unique_edges(np.array([], dtype=np.uint32)).shape == (0, 2)
        assert md.build_unique_edges(np.array([0, 1], dtype=np.uint32)).shape == (0, 2)

    def test_large_vertex_indices_no_overflow(self):
        """大顶点索引（≥ 2^31）不应溢出（回归 MINOR：int64 key 编码溢出）。"""
        # 用接近 uint32 上限的索引
        big = np.uint32(2**31 + 100)
        idx = np.array([big, big + 1, big + 2], dtype=np.uint32)
        edges = md.build_unique_edges(idx)
        assert edges.shape == (3, 2)
        # 所有边应是 (min, max) 排序后的大数
        assert edges[0, 0] == big
        assert edges[0, 1] == big + 1

    def test_cube_edge_count(self):
        """cube（24 顶点 36 索引）边数稳定。"""
        cube = prim.cube(1.0)
        edges = md.build_unique_edges(cube.indices)
        # cube 有 12 条唯一边（6 面 × 但共享边去重后…实际取决于顶点是否独立）
        # cube 用独立顶点（每面 4 顶点），所以邻面不共享顶点索引 → 边较多
        assert edges.shape[0] > 0
        # 稳定性：多次调用结果一致
        edges2 = md.build_unique_edges(cube.indices)
        assert np.array_equal(edges, edges2)

    def test_edge_count_from_indices(self):
        idx = np.array([0, 1, 2], dtype=np.uint32)
        assert md.edge_count_from_indices(idx) == 3


# ── build_normal_lines / build_tangent_lines ─────────────────────────

class TestNormalTangentLines:
    def test_normal_lines_format(self):
        """法线线段：(2K,3)，每对 start/end。"""
        verts = np.array([[0, 0, 0], [1, 0, 0]], dtype=np.float32)
        norms = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
        lines = md.build_normal_lines(verts, norms, length=0.5)
        assert lines.shape == (4, 3)
        # 第一条线段从 (0,0,0) 沿 (1,0,0) 延伸 0.5 → end=(0.5,0,0)
        assert np.allclose(lines[1], [0.5, 0, 0], atol=1e-5)

    def test_normal_lines_stride(self):
        """stride=2 时只画一半顶点。"""
        verts = np.zeros((4, 3), dtype=np.float32)
        norms = np.tile([0, 1, 0], (4, 1)).astype(np.float32)
        lines = md.build_normal_lines(verts, norms, stride=2)
        assert lines.shape == (4, 3)  # 2 顶点 × 2（start+end）

    def test_normal_lines_normalizes(self):
        """未归一化法线应被归一化。"""
        verts = np.array([[0, 0, 0]], dtype=np.float32)
        norms = np.array([[2, 0, 0]], dtype=np.float32)  # 未归一化
        lines = md.build_normal_lines(verts, norms, length=1.0)
        # end 应在 (1,0,0)（归一化后）
        assert np.allclose(lines[1], [1, 0, 0], atol=1e-5)

    def test_normal_lines_empty(self):
        assert md.build_normal_lines(np.zeros((0, 3), dtype=np.float32),
                                     np.zeros((0, 3), dtype=np.float32)).shape == (0, 3)

    def test_tangent_lines_4comp(self):
        """切线 (N,4) 取前 3 分量。"""
        verts = np.array([[0, 0, 0]], dtype=np.float32)
        tans = np.array([[1, 0, 0, 1]], dtype=np.float32)  # xyzw
        lines = md.build_tangent_lines(verts, tans, length=0.5)
        assert lines.shape == (2, 3)
        assert np.allclose(lines[1], [0.5, 0, 0], atol=1e-5)

    def test_tangent_lines_multi_vertex(self):
        """多顶点 mesh 的切线线段（回归：reshape(-1) 把 (N,4) 压成 1D 导致空）。"""
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        tans = np.array([[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1]], dtype=np.float32)
        lines = md.build_tangent_lines(verts, tans, length=0.5)
        # 3 顶点 × 2 端点 = 6 顶点
        assert lines.shape == (6, 3)

    def test_tangent_lines_3comp(self):
        """切线 (N,3) 直接用。"""
        verts = np.array([[0, 0, 0], [1, 1, 1]], dtype=np.float32)
        tans = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
        lines = md.build_tangent_lines(verts, tans, length=1.0)
        assert lines.shape == (4, 3)


# ── compute_uv_stretch ───────────────────────────────────────────────

class TestUVStretch:
    def test_uniform_uv_no_stretch(self):
        """3D 面积 = UV 面积 → 拉伸比 = 1（无拉伸）。"""
        # 单位正方形三角形，UV 也是单位正方形
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        uvs = np.array([[0, 0], [1, 0], [0, 1]], dtype=np.float32)
        idx = np.array([0, 1, 2], dtype=np.uint32)
        stretch = md.compute_uv_stretch(verts, uvs, idx)
        assert stretch.shape == (1,)
        # 3D 面积 = 0.5, UV 面积 = 0.5 → 比值 = 1
        assert abs(stretch[0] - 1.0) < 0.01

    def test_degenerate_uv_zero(self):
        """UV 面积为 0（退化）→ 拉伸系数 = 0。"""
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        uvs = np.array([[0, 0], [0, 0], [0, 0]], dtype=np.float32)  # 共线 UV
        idx = np.array([0, 1, 2], dtype=np.uint32)
        stretch = md.compute_uv_stretch(verts, uvs, idx)
        assert stretch[0] == 0.0

    def test_empty_indices(self):
        stretch = md.compute_uv_stretch(np.zeros((3, 3), dtype=np.float32),
                                        np.zeros((3, 2), dtype=np.float32),
                                        np.array([], dtype=np.uint32))
        assert stretch.shape == (0,)

    def test_stretch_to_color_shape(self):
        """uv_stretch_to_color 返回 (T,3) RGB。"""
        stretch = np.array([1.0, 5.0, 0.0], dtype=np.float32)
        colors = md.uv_stretch_to_color(stretch)
        assert colors.shape == (3, 3)
        # 退化（0）→ 灰色
        assert np.allclose(colors[2], [0.4, 0.4, 0.4], atol=0.01)

    def test_no_stretch_is_green(self):
        """拉伸比=1（无拉伸）→ 绿色（回归：之前映射成蓝色）。"""
        stretch = np.array([1.0, 1.0], dtype=np.float32)
        colors = md.uv_stretch_to_color(stretch)
        # 无拉伸 → t=0 → 绿色 (0,1,0)
        assert np.allclose(colors[0], [0.0, 1.0, 0.0], atol=0.01)

    def test_max_stretch_is_red(self):
        """最大拉伸 → 红色。"""
        stretch = np.array([1.0, 100.0], dtype=np.float32)
        colors = md.uv_stretch_to_color(stretch)
        # 最大拉伸 → t=1 → 红色 (1,0,0)
        assert colors[1, 0] > 0.9  # R 高
        assert colors[1, 1] < 0.1  # G 低


# ── build_point_buffer ───────────────────────────────────────────────

class TestPointBuffer:
    def test_full_points(self):
        verts = np.arange(12, dtype=np.float32).reshape(4, 3)
        pts = md.build_point_buffer(verts)
        assert pts.shape == (4, 3)

    def test_stride_downsample(self):
        verts = np.arange(12, dtype=np.float32).reshape(4, 3)
        pts = md.build_point_buffer(verts, stride=2)
        assert pts.shape == (2, 3)

    def test_empty(self):
        assert md.build_point_buffer(np.zeros((0, 3), dtype=np.float32)).shape == (0, 3)


# ── ViewportDisplayMode + State ──────────────────────────────────────

class TestViewportDisplayMode:
    def test_enum_values(self):
        assert ViewportDisplayMode.TEXTURED.value == "textured"
        assert ViewportDisplayMode.WIREFRAME.value == "wireframe"
        assert ViewportDisplayMode.SHADED_WIRE.value == "shaded_wire"
        assert ViewportDisplayMode.NORMAL_DEBUG.value == "normal_debug"

    def test_labels_complete(self):
        for mode in ViewportDisplayMode:
            assert mode in DISPLAY_MODE_LABELS

    def test_state_default_textured(self):
        state = Viewport3DState()
        assert state.display_mode == ViewportDisplayMode.TEXTURED

    def test_state_overlay_flags_default_off(self):
        state = Viewport3DState()
        assert state.overlay_show_wireframe is False
        assert state.overlay_show_normals is False
        assert state.overlay_show_tangents is False
        assert state.overlay_show_skeleton is False
        assert state.overlay_show_bounds is False

    def test_state_display_mode_changeable(self):
        state = Viewport3DState()
        state.display_mode = ViewportDisplayMode.WIREFRAME
        assert state.display_mode == ViewportDisplayMode.WIREFRAME
