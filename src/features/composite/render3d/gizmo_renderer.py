"""
Scene3D 辅助元素渲染器 —— 无限地面网格 + 世界坐标轴。

C4D 风格：
- 地面网格不是固定线段几何，而是 shader 在大平面上实时计算固定世界尺寸网格，
  远处按相机距离平滑淡出。
- 世界坐标轴（原点红X/绿Y/蓝Z）恒在最前，提供朝向参考。

实现：
- 网格 = 一个大的 XZ 平面 mesh（覆盖足够远），fragment shader 用 fract()
  在世界 XZ 上画网格线，并用相机距离衰减 alpha 实现无限延伸的视觉。
- 坐标轴 = 三条彩色线段，关深度测试恒在最前。
"""

import numpy as np
import moderngl
import logging
from typing import Optional

from . import math_utils as m3d

logger = logging.getLogger(__name__)


# ── 坐标轴着色器（线段，固定颜色）───────────────────────────────────────────
_AXES_VERTEX = """
#version 330
in vec3 in_pos;
in vec3 in_color;
uniform mat4 u_proj;
uniform mat4 u_view;
out vec3 v_color;
void main() {
    gl_Position = u_proj * u_view * vec4(in_pos, 1.0);
    v_color = in_color;
}
"""

_AXES_FRAGMENT = """
#version 330
in vec3 v_color;
out vec4 fragColor;
void main() {
    fragColor = vec4(v_color, 1.0);
}
"""


# ── 无限网格着色器 ─────────────────────────────────────────────────────────
# 大平面 mesh，vertex 传世界坐标，fragment 用 fract 画线 + 距离淡出。
# cell 控制格距；near_cell 是近原点的高亮大格。

_GRID_VERTEX = """
#version 330
in vec3 in_pos;
uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
out vec3 v_world_pos;
out vec3 v_view_pos;
void main() {
    vec4 world = u_model * vec4(in_pos, 1.0);
    vec4 view_pos = u_view * world;
    v_world_pos = world.xyz;
    v_view_pos = view_pos.xyz;
    gl_Position = u_proj * view_pos;
}
"""

_GRID_FRAGMENT = """
#version 330
in vec3 v_world_pos;
in vec3 v_view_pos;
uniform float u_cell_size;     // 小格间距（世界单位）
uniform float u_major_every;   // 每 N 个小格画一条粗线
uniform vec3  u_minor_color;   // 小格颜色
uniform vec3  u_major_color;   // 大格颜色
uniform float u_minor_alpha;   // 小格基础 alpha
uniform float u_major_alpha;   // 大格基础 alpha
uniform float u_line_width;    // 线宽系数（像素空间，<1 更细，>1 更粗）
uniform int   u_grid_plane;    // 网格平面：0=XZ（地面），1=XY（前视图），2=YZ（右视图）
uniform float u_minor_fade_start; // 小格开始淡出距离
uniform float u_minor_fade_end;   // 小格完全淡出距离
uniform float u_minor_fade_slope; // 小格淡出曲线（<1 更缓，>1 更陡）
uniform float u_major_fade_start; // 大格开始淡出距离
uniform float u_major_fade_end;   // 大格完全淡出距离
uniform float u_major_fade_slope; // 大格淡出曲线（<1 更缓，>1 更陡）
out vec4 fragColor;

float distanceFade(float start_dist, float end_dist, float dist, float slope) {
    float span = max(end_dist - start_dist, 0.001);
    float t = clamp((dist - start_dist) / span, 0.0, 1.0);
    float eased = smoothstep(0.0, 1.0, t);
    return pow(1.0 - eased, max(slope, 0.001));
}

float gridLineAlpha(vec2 coord, float width) {
    vec2 dcoord = max(fwidth(coord), vec2(0.0001));
    vec2 g = abs(fract(coord - 0.5) - 0.5) / dcoord;
    float line = min(g.x, g.y);
    return 1.0 - smoothstep(width, width + 0.90, line);
}

float gridCellPixels(vec2 coord) {
    vec2 dcoord = max(fwidth(coord), vec2(0.0001));
    return 1.0 / max(dcoord.x, dcoord.y);
}

void main() {
    // 按平面取两个坐标轴画网格（正交视图适配：TOP→XZ, FRONT→XY, RIGHT→YZ）
    vec2 p;
    if (u_grid_plane == 1) {
        p = v_world_pos.xy / u_cell_size;       // XY 平面（前视图地面）
    } else if (u_grid_plane == 2) {
        p = v_world_pos.yz / u_cell_size;       // YZ 平面（右视图地面）
    } else {
        p = v_world_pos.xz / u_cell_size;       // XZ 平面（透视/顶视图地面，默认）
    }
    // Fixed-size DCC-style grid. Cell size stays stable in world space; screen
    // LOD only suppresses sub-pixel shimmer instead of changing grid scale.
    float minor_width = max(u_line_width * 0.55, 0.020);
    float major_width = max(u_line_width * 0.85, 0.040);

    float minor_px = gridCellPixels(p);
    float minor_lod = smoothstep(0.75, 4.0, minor_px);
    float minor_a = gridLineAlpha(p, minor_width) * minor_lod;

    vec2 major_coord = p / max(u_major_every, 2.0);
    float major_px = gridCellPixels(major_coord);
    float major_lod = smoothstep(2.0, 9.0, major_px);
    float major_a = gridLineAlpha(major_coord, major_width) * major_lod;

    // 相机距离淡出。不要用 length(world_xz) 做径向淡出；那会在拉远时
    // 暴露一个以 target/origin 为中心的圆盘，不像 DCC 视口里的参考网格。
    // 也不要用 view-space z 深度；旋转视角会大幅改变同一网格点的 z，
    // 造成同距离网格忽明忽暗。欧氏相机距离更稳定。
    float fade_dist = length(v_view_pos);
    float minor_dist_fade = mix(
        0.12,
        1.0,
        distanceFade(u_minor_fade_start, u_minor_fade_end, fade_dist, u_minor_fade_slope));
    float major_dist_fade = mix(
        0.28,
        1.0,
        distanceFade(u_major_fade_start, u_major_fade_end, fade_dist, u_major_fade_slope));
    minor_a *= minor_dist_fade;
    major_a *= major_dist_fade;

    // 颜色：major 优先。
    vec3 color = mix(u_minor_color, u_major_color, major_a);
    float alpha = max(minor_a * u_minor_alpha, major_a * u_major_alpha);

    if (alpha < 0.001) discard;
    fragColor = vec4(color, alpha);
}
"""


class GizmoRenderer:
    """绘制 Scene3D 的无限地面网格和世界坐标轴。

    生命周期由 Scene3DNode 管理（它在 cleanup 时调 release）。
    """

    # 网格平面覆盖范围（足够大即可，远处会淡出）
    _PLANE_EXTENT = 5000.0

    def __init__(self) -> None:
        self._grid_prog: Optional[moderngl.Program] = None
        self._grid_vao: Optional[moderngl.VertexArray] = None
        self._axes_prog: Optional[moderngl.Program] = None
        self._axes_vao: Optional[moderngl.VertexArray] = None
        self._grid_vbo: Optional[moderngl.Buffer] = None
        self._axes_vbo: Optional[moderngl.Buffer] = None
        self._built: bool = False
        # AUD-88040：记录 _build 用的 context；draw 换 ctx 时先 release 再重建，
        # 避免继续使用旧 context 的陈旧 GL 对象。
        self._ctx: Optional[object] = None

    def _build(self, ctx) -> None:
        """懒编译 program + 上传网格平面 & 坐标轴几何（一次性）。

        AUD-88040 异常安全：
        - 中途失败（如坐标轴 program 编译抛错）时，先释放已创建的 GL 对象
          再重抛，下次重试不会覆盖/泄漏半成品引用；
        - 记录 ctx，外层更换 context 后首次 draw 自动 release 旧资源重建。
        """
        if self._built:
            if self._ctx is not ctx:
                self.release()
            else:
                return
        try:
            # ── 无限网格：大的 XZ 平面 quad ──
            self._grid_prog = ctx.program(vertex_shader=_GRID_VERTEX,
                                           fragment_shader=_GRID_FRAGMENT)
            e = self._PLANE_EXTENT
            plane = np.array([
                -e, 0.0, -e,
                 e, 0.0, -e,
                -e, 0.0,  e,
                 e, 0.0,  e,
            ], dtype=np.float32)
            vbo = ctx.buffer(plane.tobytes())
            self._grid_vao = ctx.vertex_array(
                self._grid_prog, [(vbo, '3f', 'in_pos')])
            # 保留 vbo 引用避免 GC（VAO 不强持有）
            self._grid_vbo = vbo

            # ── 世界坐标轴 ──
            self._axes_prog = ctx.program(vertex_shader=_AXES_VERTEX,
                                           fragment_shader=_AXES_FRAGMENT)
            L = 2.0  # 轴长（世界单位）
            ah = 0.18  # 箭头回退长度
            aw = 0.08  # 箭头半宽
            axes = np.array([
                # X 红：主轴 + 端点 V 形箭头
                0,0,0, 1.0,0.0,0.0,   L,0,0, 1.0,0.0,0.0,
                L,0,0, 1.0,0.0,0.0,   L-ah, aw,0, 1.0,0.0,0.0,
                L,0,0, 1.0,0.0,0.0,   L-ah,-aw,0, 1.0,0.0,0.0,
                L,0,0, 1.0,0.0,0.0,   L-ah,0, aw, 1.0,0.0,0.0,
                L,0,0, 1.0,0.0,0.0,   L-ah,0,-aw, 1.0,0.0,0.0,
                # Y 绿：主轴 + 端点 V 形箭头
                0,0,0, 0.0,1.0,0.0,   0,L,0, 0.0,1.0,0.0,
                0,L,0, 0.0,1.0,0.0,   aw,L-ah,0, 0.0,1.0,0.0,
                0,L,0, 0.0,1.0,0.0,  -aw,L-ah,0, 0.0,1.0,0.0,
                0,L,0, 0.0,1.0,0.0,   0,L-ah, aw, 0.0,1.0,0.0,
                0,L,0, 0.0,1.0,0.0,   0,L-ah,-aw, 0.0,1.0,0.0,
                # Z 蓝：主轴 + 端点 V 形箭头
                0,0,0, 0.0,0.0,1.0,   0,0,L, 0.0,0.0,1.0,
                0,0,L, 0.0,0.0,1.0,   aw,0,L-ah, 0.0,0.0,1.0,
                0,0,L, 0.0,0.0,1.0,  -aw,0,L-ah, 0.0,0.0,1.0,
                0,0,L, 0.0,0.0,1.0,   0, aw,L-ah, 0.0,0.0,1.0,
                0,0,L, 0.0,0.0,1.0,   0,-aw,L-ah, 0.0,0.0,1.0,
            ], dtype=np.float32)
            avbo = ctx.buffer(axes.tobytes())
            self._axes_vao = ctx.vertex_array(
                self._axes_prog, [(avbo, '3f 3f', 'in_pos', 'in_color')])
            self._axes_vbo = avbo
        except Exception:
            # AUD-88040：中途失败立即释放半成品（grid program/VAO/VBO 已创建、
            # 坐标轴段失败是典型路径），否则重入 _build 会覆盖引用 → 泄漏旧
            # GL 对象直到 release()。释放后原样重抛，由调用方决定降级策略。
            logger.warning("GizmoRenderer._build 失败，释放已创建的 GL 对象",
                           exc_info=True)
            self.release()
            raise

        self._built = True
        self._ctx = ctx

    def draw(self, ctx, proj: np.ndarray, view: np.ndarray,
             cam_target=None, show_grid: bool = True, show_axes: bool = True,
             cam_pitch_deg: float = 20.0, grid_plane: str = "xz",
             grid_cell_size: float = 0.35, grid_opacity: float = 0.25,
             grid_line_width: float = 0.1,
             grid_minor_fade_start: float = 120.0,
             grid_minor_fade_end: float = 1200.0,
             grid_minor_fade_slope: float = 0.55,
             grid_major_fade_start: float = 80.0,
             grid_major_fade_end: float = 520.0,
             grid_major_fade_slope: float = 0.75,
             grid_minor_color: tuple[float, float, float] = (0.10, 0.10, 0.12),
             grid_major_color: tuple[float, float, float] = (0.20, 0.20, 0.23)) -> None:
        """在当前绑定的 FBO 上叠加绘制网格和坐标轴。

        Args:
            ctx: ModernGL context（FBO 已 use）。
            proj: 4x4 投影矩阵。
            view: 4x4 视图矩阵。
            cam_target: 相机注视点（用于把网格平面中心移到 target 下方，使
                平移时网格跟着场景走）。None 则原点。
            show_grid / show_axes: 显隐控制。
            cam_pitch_deg: 兼容旧调用保留；网格亮度不再按俯仰角额外压暗。
            grid_plane: 网格平面朝向——"xz"（地面，默认）/ "xy"（前视图）/
                "yz"（右视图）。正交视图各自显示对应方向的网格。
            grid_cell_size: 细格间距（世界单位）。
            grid_opacity: 整体网格不透明度倍率（缩放 minor/major alpha，1.0=默认）。
            grid_line_width: 线宽系数（<1 更细，>1 更粗）。
            grid_minor_fade_*: 细格距离淡出控制。
            grid_major_fade_*: 大格距离淡出控制。
            grid_minor_color / grid_major_color: 细格/大格颜色。
        """
        self._build(ctx)

        # ── 网格：开启混合，参与深度测试（被物体遮挡），但不写入深度 ──
        # 网格是辅助叠加层：它应该被物体遮挡（DEPTH_TEST 开），但自身不应写入深度
        # 缓冲（glDepthMask=GL_FALSE）。否则网格 quad 的 500×500 平面会写入整个地面
        # 的深度值，当物体贴近网格平面时与物体底面深度冲突（z-fighting）/ 在网格线
        # 位置挡住物体，导致物体"靠近网格线时突然消失"。
        if show_grid:
            # 网格平面中心移到 target（沿平面两轴跟随场景，法线轴归零）。
            tx = ty = tz = 0.0
            if cam_target is not None:
                if grid_plane == "xy":
                    tx, ty, tz = float(cam_target[0]), float(cam_target[1]), 0.0
                elif grid_plane == "yz":
                    tx, ty, tz = 0.0, float(cam_target[1]), float(cam_target[2])
                else:  # xz（默认地面）
                    tx, ty, tz = float(cam_target[0]), 0.0, float(cam_target[2])
            # 网格几何是 XZ 平面 quad，旋转到目标平面（绕法线外的轴旋转）：
            # xy: 绕 X 轴 -90°（法线 Y→Z）→ XY 平面
            # yz: 绕 Z 轴 +90°（法线 Y→X）→ YZ 平面
            # xz: 不旋转（原地，法线 Y）
            plane_rot = (m3d.rotate_x(-90.0) if grid_plane == "xy"
                         else (m3d.rotate_z(90.0) if grid_plane == "yz"
                               else np.eye(4, dtype=np.float32)))
            grid_model = m3d.translate(tx, ty, tz) @ plane_rot
            prog = self._grid_prog
            prog['u_proj'].value = m3d.matrix_to_tuple(proj)
            prog['u_view'].value = m3d.matrix_to_tuple(view)
            prog['u_model'].value = m3d.matrix_to_tuple(grid_model)
            # 精致参数：细格更密、大格更稀疏、次级线比大格略暗、远处淡化。
            # cell_size / opacity / line_width 来自全局「3D Scene Settings」；
            # 其余参数保持默认视觉。opacity 作为倍率缩放 minor/major alpha。
            _op = max(0.0, float(grid_opacity))
            prog['u_cell_size'].value = max(0.1, float(grid_cell_size))
            prog['u_major_every'].value = 10.0   # 每 10 格一条大线
            prog['u_minor_color'].value = tuple(
                max(0.0, min(1.0, float(c))) for c in grid_minor_color)
            prog['u_major_color'].value = tuple(
                max(0.0, min(1.0, float(c))) for c in grid_major_color)
            prog['u_minor_alpha'].value = min(1.0, 0.10 * _op)
            prog['u_major_alpha'].value = min(1.0, 0.26 * _op)
            prog['u_line_width'].value = max(0.01, float(grid_line_width))
            _minor_start = max(0.0, float(grid_minor_fade_start))
            _minor_end = max(_minor_start + 0.1, float(grid_minor_fade_end))
            _major_start = max(0.0, float(grid_major_fade_start))
            _major_end = max(_major_start + 0.1, float(grid_major_fade_end))
            prog['u_minor_fade_start'].value = _minor_start
            prog['u_minor_fade_end'].value = _minor_end
            prog['u_minor_fade_slope'].value = max(0.001, float(grid_minor_fade_slope))
            prog['u_major_fade_start'].value = _major_start
            prog['u_major_fade_end'].value = _major_end
            prog['u_major_fade_slope'].value = max(0.001, float(grid_major_fade_slope))
            # 网格平面 uniform：0=xz, 1=xy, 2=yz（shader 据此取坐标轴画线）。
            # 防御：旧缓存 program 可能无此 uniform（shader 升级前编译）→ 检查存在再设。
            if 'u_grid_plane' in prog:
                prog['u_grid_plane'].value = (1 if grid_plane == "xy"
                                              else (2 if grid_plane == "yz" else 0))
            ctx.enable(moderngl.BLEND)
            ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
            # 关深度写入：网格只做深度测试（被物体遮挡），不污染深度缓冲。
            # ModernGL 没有高层 depth-mask API；PyOpenGL 在某些 shared-context
            # 渲染线程里会抛 GL 错误。失败时退化为普通绘制，不能让辅助网格拖垮
            # 整个 3D Viewer。
            depth_mask_disabled = False
            try:
                from OpenGL.GL import glDepthMask
                glDepthMask(False)
                depth_mask_disabled = True
            except Exception:
                depth_mask_disabled = False
            try:
                self._grid_vao.render(moderngl.TRIANGLE_STRIP)
            finally:
                if depth_mask_disabled:
                    try:
                        from OpenGL.GL import glDepthMask
                        glDepthMask(True)
                    except Exception:
                        pass
            ctx.disable(moderngl.BLEND)

        # ── 坐标轴：关深度测试，恒在最前 ──
        if show_axes:
            ctx.disable(moderngl.DEPTH_TEST)
            prog = self._axes_prog
            prog['u_proj'].value = m3d.matrix_to_tuple(proj)
            prog['u_view'].value = m3d.matrix_to_tuple(view)
            self._axes_vao.render(moderngl.LINES, vertices=30)
            ctx.enable(moderngl.DEPTH_TEST)

    def release(self) -> None:
        """释放所有 GL 资源。"""
        for attr in ('_grid_vao', '_axes_vao'):
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)
        for attr in ('_grid_vbo', '_axes_vbo'):
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)
        for attr in ('_grid_prog', '_axes_prog'):
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)
        self._built = False
        # AUD-88040：context 绑定一并失效（下次 draw 用新 ctx 重建）。
        self._ctx = None
