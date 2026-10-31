"""
F6-6 骨骼 overlay 渲染器 —— 把 SkeletonData 画成骨骼线段 + 关节点。

设计（参考 gizmo_renderer.py 的 program/VBO 管理模式）：
- SkeletonOverlayRenderer 持有 line program（GL_LINES）+ point program（GL_POINTS）。
- render(ctx, skeleton, current_worlds, proj, view, model)：
    1. 调 build_skeleton_overlay 取线段/关节点 buffer
    2. 应用 model 变换（骨骼在模型局部空间 → 世界需 model）
    3. 上传 VBO → 绘制
- VBO 每帧重建（骨骼动画时位置变化；静态时可缓存，但第一版简单实现）
- program 一次性编译并缓存；cleanup 释放。

线段着色：parent→child 用渐变色（父端偏蓝、子端偏黄，便于识别层级）。
关节点：根骨骼绿色（稍大），其余黄色。

关联模块：
- skeleton.py：build_skeleton_overlay 产出 buffer
- scene3d_node.py：持有 renderer 实例，在 beauty pass 后调 render
- math_utils.matrix_to_tuple：矩阵上传
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from . import math_utils as m3d
from .skeleton import (
    SkeletonData,
    build_skeleton_overlay,
)


# ── 线段着色器（骨骼连接线）────────────────────────────────────────────

_SKELETON_LINE_VERTEX = """
#version 330
in vec3 in_pos;
in vec3 in_color;
uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
out vec3 v_color;
void main() {
    vec4 world = u_model * vec4(in_pos, 1.0);
    gl_Position = u_proj * u_view * world;
    v_color = in_color;
}
"""

_SKELETON_LINE_FRAGMENT = """
#version 330
in vec3 v_color;
uniform float u_alpha;
out vec4 fragColor;
void main() {
    fragColor = vec4(v_color, u_alpha);
}
"""

# ── 关节点着色器（GL_POINTS）────────────────────────────────────────────

_SKELETON_POINT_VERTEX = """
#version 330
in vec3 in_pos;
in float in_size;
in vec3 in_color;
uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
out vec3 v_color;
void main() {
    vec4 world = u_model * vec4(in_pos, 1.0);
    gl_Position = u_proj * u_view * world;
    v_color = in_color;
    gl_PointSize = in_size;
}
"""

_SKELETON_POINT_FRAGMENT = """
#version 330
in vec3 v_color;
uniform float u_alpha;
out vec4 fragColor;
void main() {
    // 把方点变圆（距离中心 > 0.5 丢弃）
    vec2 coord = gl_PointCoord - vec2(0.5);
    if (dot(coord, coord) > 0.25) discard;
    fragColor = vec4(v_color, u_alpha);
}
"""


# 颜色常量（线性 RGB）
_COLOR_ROOT = np.array([0.2, 0.9, 0.3], dtype=np.float32)   # 根骨骼绿
_COLOR_BONE_PARENT = np.array([0.3, 0.5, 0.9], dtype=np.float32)  # 线段父端蓝
_COLOR_BONE_CHILD = np.array([0.9, 0.7, 0.2], dtype=np.float32)   # 线段子端黄
_COLOR_JOINT = np.array([0.9, 0.7, 0.2], dtype=np.float32)        # 普通关节黄


class SkeletonOverlayRenderer:
    """骨骼 overlay 渲染器（线段 + 关节点）。

    生命周期由 Scene3DNode 管理（cleanup 时调 release）。
    program 一次性编译；VBO 每帧重建（骨骼动画时位置变化）。
    """

    def __init__(self) -> None:
        self._line_prog = None
        self._point_prog = None
        self._line_vbo: Optional[object] = None
        self._line_vao: Optional[object] = None
        self._point_vbo: Optional[object] = None
        self._point_vao: Optional[object] = None
        self._built = False

    def _ensure_programs(self, ctx) -> None:
        """懒编译 line/point program（一次性）。"""
        if self._built:
            return
        self._line_prog = ctx.program(
            vertex_shader=_SKELETON_LINE_VERTEX,
            fragment_shader=_SKELETON_LINE_FRAGMENT)
        self._point_prog = ctx.program(
            vertex_shader=_SKELETON_POINT_VERTEX,
            fragment_shader=_SKELETON_POINT_FRAGMENT)
        self._built = True

    def render(
        self,
        ctx,
        skeleton: SkeletonData,
        proj: np.ndarray,
        view: np.ndarray,
        model: np.ndarray,
        current_worlds: Optional[dict] = None,
        line_alpha: float = 0.85,
        point_alpha: float = 0.95,
        bone_size: float = 8.0,
    ) -> None:
        """在当前绑定的 FBO 上绘制骨骼 overlay。

        Args:
            ctx: ModernGL context（FBO 已 use）。
            skeleton: 骨架数据。
            proj: 4x4 投影矩阵。
            view: 4x4 视图矩阵。
            model: 4x4 模型矩阵（骨骼局部→世界；含 Model3D object_pos/scale）。
            current_worlds: 当前 pose 世界矩阵（None=用 rest_worlds = bind pose）。
            line_alpha: 线段不透明度。
            point_alpha: 关节点不透明度。
            bone_size: 关节点基础大小（像素；根骨骼 ×1.5）。
        """
        if skeleton is None or skeleton.is_empty:
            return
        self._ensure_programs(ctx)

        # 释放上一帧 VBO（骨骼动画时每帧位置变化）
        self._release_vbos()

        line_verts, joint_pos, joint_sizes = build_skeleton_overlay(
            skeleton, current_worlds=current_worlds, bone_size=bone_size)

        # ── 线段：每对端点带颜色（父端蓝、子端黄）──
        if line_verts.shape[0] >= 2:
            n_segments = line_verts.shape[0] // 2
            # (n_segments*2, 3) pos + (n_segments*2, 3) color interleave
            colors = np.empty((line_verts.shape[0], 3), dtype=np.float32)
            for i in range(n_segments):
                colors[i * 2] = _COLOR_BONE_PARENT      # 父端
                colors[i * 2 + 1] = _COLOR_BONE_CHILD   # 子端
            line_data = np.empty((line_verts.shape[0], 6), dtype=np.float32)
            line_data[:, :3] = line_verts
            line_data[:, 3:] = colors
            self._line_vbo = ctx.buffer(line_data.tobytes())
            self._line_vao = ctx.vertex_array(
                self._line_prog,
                [(self._line_vbo, '3f 3f', 'in_pos', 'in_color')])
            self._line_prog['u_proj'].value = m3d.matrix_to_tuple(proj)
            self._line_prog['u_view'].value = m3d.matrix_to_tuple(view)
            self._line_prog['u_model'].value = m3d.matrix_to_tuple(model)
            self._line_prog['u_alpha'].value = float(line_alpha)
            self._line_vao.render(__import__('moderngl').LINES)

        # ── 关节点：GL_POINTS，根骨骼绿色稍大，其余黄色 ──
        if joint_pos.shape[0] > 0:
            n = joint_pos.shape[0]
            root_set = set(skeleton.root_indices)
            point_colors = np.empty((n, 3), dtype=np.float32)
            for i in range(n):
                point_colors[i] = _COLOR_ROOT if i in root_set else _COLOR_JOINT
            # interleave: pos(3) + size(1) + color(3) = 7 floats per vertex
            point_data = np.empty((n, 7), dtype=np.float32)
            point_data[:, :3] = joint_pos
            point_data[:, 3] = joint_sizes
            point_data[:, 4:] = point_colors
            self._point_vbo = ctx.buffer(point_data.tobytes())
            self._point_vao = ctx.vertex_array(
                self._point_prog,
                [(self._point_vbo, '3f 1f 3f', 'in_pos', 'in_size', 'in_color')])
            self._point_prog['u_proj'].value = m3d.matrix_to_tuple(proj)
            self._point_prog['u_view'].value = m3d.matrix_to_tuple(view)
            self._point_prog['u_model'].value = m3d.matrix_to_tuple(model)
            self._point_prog['u_alpha'].value = float(point_alpha)
            self._point_vao.render(__import__('moderngl').POINTS)

    def _release_vbos(self) -> None:
        """释放上一帧的 VBO/VAO（每帧重建，因为骨骼动画时位置变化）。"""
        for attr in ('_line_vao', '_point_vao'):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
                setattr(self, attr, None)
        for attr in ('_line_vbo', '_point_vbo'):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
                setattr(self, attr, None)

    def release(self) -> None:
        """释放所有 GL 资源（program + VBO/VAO）。"""
        self._release_vbos()
        for attr in ('_line_prog', '_point_prog'):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._built = False
