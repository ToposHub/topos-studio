"""
Transform Gizmo（视口叠加层）— 物体原点的可拖动三轴 / 旋转环 / 缩放句柄。

与 gizmo_renderer.py（节点烘焙用的地面网格 + 世界坐标轴）不同，本模块
的 Gizmo 是**视口交互工具**：
- 画在视口叠加层（paintGL 之上），不进入节点输出纹理（符合 Nuke/Blender）
- 三轴恒定「渲染目标纹理像素」长度（fit 模式下等同屏幕像素）
- 按 mode 切换交互：MOVE（拖轴移动）/ ROTATE（拖环旋转）/ SCALE（拖句柄缩放）

坐标系约定（关键）：
- Gizmo 工作在 **渲染目标（texture）空间**，与节点输出纹理一致。
  - 世界点 → 节点投影 → 纹理 NDC（-1..1）→ 纹理 UV（0..1，Y 下负上正翻转为 GL）
- 视口叠加层负责把「纹理 UV」经 letterbox 缩放映射到「视口 NDC」绘制
  （与现有 bbox/format overlay 同款映射，保证对齐）。
- hit_test 在 **纹理像素** 空间判定（viewport 传入鼠标的纹理像素坐标）。
- drag_offset 的鼠标坐标用 **纹理 NDC**（= uv*2-1），直接喂 screen_to_ray。

设计要点：
- 纯数学 + 数据生成，不依赖 GL/ModernGL；绘制交由 viewport 的 overlay 机制
- 屏幕像素 → 世界长度：由相机距离、FOV、**纹理**高度共同决定
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

from . import math_utils as m3d
from . import raycast

# 三轴方向（世界空间单位向量，右手系）
AXIS_X = np.array([1.0, 0.0, 0.0], dtype=np.float32)  # 红
AXIS_Y = np.array([0.0, 1.0, 0.0], dtype=np.float32)  # 绿
AXIS_Z = np.array([0.0, 0.0, 1.0], dtype=np.float32)  # 蓝

# 轴名 → (方向, 颜色 RGBA)
AXIS_INFO = {
    "x": (AXIS_X, (1.00, 0.12, 0.18, 0.96)),   # C4D 红
    "y": (AXIS_Y, (0.00, 0.92, 0.52, 0.96)),   # C4D 绿
    "z": (AXIS_Z, (0.16, 0.48, 1.00, 0.96)),   # C4D 蓝
}

# hover/active 时的高亮色（黄）
HOVER_COLOR = (1.00, 0.90, 0.20, 1.0)

# 轴名 → object_pos 参数名 / 分量索引
_AXIS_PARAM = {"x": "object_pos_x", "y": "object_pos_y", "z": "object_pos_z"}
_AXIS_IDX = {"x": 0, "y": 1, "z": 2}
# 轴名 → object_rot 参数名（Phase 5 旋转）
_AXIS_ROT_PARAM = {"x": "object_rot_x", "y": "object_rot_y", "z": "object_rot_z"}


class GizmoMode(Enum):
    """Gizmo 交互模式（对应 Viewer3DToolMode）。

    MOVE:   拖单轴移动物体（投影约束）。
    ROTATE: 拖环旋转物体（绕世界轴）。
    SCALE:  拖句柄统一缩放（暂用单轴缩放，Phase 6 扩展）。
    """
    MOVE = "move"
    ROTATE = "rotate"
    SCALE = "scale"


@dataclass
class GizmoAxisDraw:
    """单条轴的绘制数据（已投影到纹理 UV）。"""
    name: str            # "x" / "y" / "z"
    start_uv: tuple      # 轴起点纹理 UV (u,v)，v 上正（GL 习惯）
    end_uv: tuple        # 轴端点纹理 UV
    color: tuple         # RGBA（已应用 hover/active 高亮）
    world_start: np.ndarray  # 世界起点（物体原点）
    world_dir: np.ndarray    # 轴世界方向（归一化）
    world_length: float      # 轴世界长度
    head_uv: tuple = field(default_factory=tuple)  # 箭头端点（默认=end_uv）


@dataclass
class GizmoRingDraw:
    """单条旋转环的绘制数据（折线 UV 点，Phase 5）。"""
    name: str            # "x" / "y" / "z"
    points_uv: list      # 折线点纹理 UV 列表 [(u,v), ...]
    color: tuple         # RGBA（已应用 hover/active 高亮）
    thickness_px: float = 2.6  # 视口绘制线宽（像素）


@dataclass
class GizmoPlaneHandleDraw:
    """移动模式的 C4D 平面约束手柄（L 形折线，XY/XZ/YZ）。"""
    name: str            # "xy" / "xz" / "yz"
    points_uv: list      # L 形折线 UV 点 [(u,v), ...]
    color: tuple         # RGBA


@dataclass
class GizmoScaleHandleDraw:
    """单轴缩放句柄的绘制数据（端点方块 UV，Phase 5）。"""
    name: str            # "x" / "y" / "z"
    center_uv: tuple     # 句柄中心纹理 UV
    color: tuple         # RGBA（已应用 hover/active 高亮）
    side_px: float = 9.0


@dataclass
class GizmoCenterDraw:
    """Gizmo 原点的小黄点（C4D 风格）。"""
    center_uv: tuple
    color: tuple = (1.0, 0.92, 0.12, 1.0)
    radius_px: float = 3.2


class TransformGizmo:
    """物体原点的可拖动三轴 Gizmo（视口叠加层）。

    生命周期：由 CompositeViewer 在 3D 模式 + scene3d 选中时持有。
    每帧 paintGL 前调 update() 刷新投影；mousePress/mouseMove 调 hit_test()
    与 drag_offset() 实现拾取与拖动。

    所有状态都在 UI 线程维护，不涉及渲染线程。
    """

    # 三轴在纹理像素上的恒定长度（fit 模式 ≈ 屏幕像素；拉近拉远变化）
    AXIS_PX_LENGTH = 140.0
    # 拾取阈值（纹理像素，鼠标距轴线的最大允许距离）
    HIT_PX_THRESHOLD = 30.0
    AXIS_END_HIT_PX_THRESHOLD = 46.0
    # 旋转环的纹理像素半径（与轴长同量级）
    RING_PX_RADIUS = 96.0
    OUTER_RING_PX_RADIUS = 124.0
    # 旋转环采样段数（画圆用多段折线）
    RING_SEGMENTS = 48
    # 缩放句柄在轴端的纹理像素偏移（缩放方块大小）
    SCALE_HANDLE_PX = 12.0
    SCALE_HANDLE_HIT_PX = 22.0
    # C4D 风格轴箭头与平面约束手柄尺寸
    PLANE_HANDLE_PX = 36.0
    PLANE_HANDLE_OFFSET_PX = 68.0
    # 缩放拖动灵敏度（纹理 NDC → 缩放比）
    SCALE_SENSITIVITY = 8.0

    def __init__(self) -> None:
        self.object_pos: np.ndarray = m3d.vec3(0.0, 0.0, 0.0)
        # Phase 5: 旋转模式需要物体当前旋转（度，Euler XYZ）
        self.object_rot: tuple = (0.0, 0.0, 0.0)
        # Phase 5: Gizmo 模式（MOVE/ROTATE/SCALE），默认 MOVE
        self.mode: GizmoMode = GizmoMode.MOVE
        # 上一帧投影参数（用于 hit_test/drag，update 后填好）
        self._proj: Optional[np.ndarray] = None
        self._view: Optional[np.ndarray] = None
        self._tex_w_px: int = 1
        self._tex_h_px: int = 1
        self._cam_dist: float = 5.0
        self._cam_fov: float = 50.0
        # 当前帧每条轴的世界端点 + 纹理像素端点
        self._axis_world: dict = {}   # name -> (start(3,), dir(3,), length)
        self._axis_tex_px: dict = {}  # name -> (start_px, end_px) in tex pixel
        # Phase 5: 旋转环 / 缩放句柄的纹理像素几何（hit_test 用）
        self._ring_tex_px: dict = {}   # name -> list of (px,py) 折线点
        self._outer_ring_tex_px: list = []  # 屏幕朝向外圈（白/灰）
        self._scale_handles_tex_px: dict = {}  # name -> (cx, cy) 中心 + radius
        self._plane_handles_tex_px: dict = {}  # "xy"/"xz"/"yz" -> 折线点
        # 交互状态
        self.hovered_axis: Optional[str] = None
        self.active_axis: Optional[str] = None

    # ── 状态更新 ──

    def update(self, object_pos, proj, view, tex_w_px: int, tex_h_px: int,
               cam_dist: float, cam_fov: float) -> None:
        """刷新 Gizmo 投影状态。每帧 paintGL 前调用。

        Args:
            object_pos: 物体当前世界位置 (3,)。
            proj/view: 节点相机的投影/视图矩阵（行主序）。注意 proj 的 aspect
                必须与渲染目标一致（= tex_w/tex_h），否则投影失真。
            tex_w_px, tex_h_px: 渲染目标纹理像素尺寸（节点输出宽高）。
            cam_dist: 相机到 target 的距离（世界单位）。
            cam_fov: 相机垂直 FOV（度）。
        """
        self.object_pos = np.asarray(object_pos, dtype=np.float32).reshape(3).copy()
        self._proj = np.asarray(proj, dtype=np.float64)
        self._view = np.asarray(view, dtype=np.float64)
        self._tex_w_px = max(1, int(tex_w_px))
        self._tex_h_px = max(1, int(tex_h_px))
        self._cam_dist = max(0.001, float(cam_dist))
        self._cam_fov = float(cam_fov)

        # 纹理恒定像素 → 世界长度：
        # 纹理高度（世界单位）≈ 2 * dist * tan(fov/2)，按纹理像素均摊。
        fov_rad = np.radians(self._cam_fov)
        world_per_tex_px = (2.0 * self._cam_dist * np.tan(fov_rad * 0.5)) / self._tex_h_px
        world_length = float(world_per_tex_px * self.AXIS_PX_LENGTH)

        self._axis_world = {}
        self._axis_tex_px = {}
        for name, (adir, _color) in AXIS_INFO.items():
            start = self.object_pos
            end = start + adir * world_length
            self._axis_world[name] = (start.astype(np.float32),
                                      adir.astype(np.float32),
                                      world_length)
            self._axis_tex_px[name] = (self._world_to_tex_px(start),
                                       self._world_to_tex_px(end))

        # Phase 5: 旋转环几何（每轴一个垂直于该轴的环，绕物体原点）
        # 环半径 = RING_PX_RADIUS 像素对应的世界长度
        ring_world_r = float(world_per_tex_px * self.RING_PX_RADIUS)
        outer_ring_world_r = float(world_per_tex_px * self.OUTER_RING_PX_RADIUS)
        self._ring_tex_px = {}
        for name, (adir, _color) in AXIS_INFO.items():
            self._ring_tex_px[name] = self._build_ring_tex_px(adir, ring_world_r)
        self._outer_ring_tex_px = self._build_view_facing_ring_tex_px(outer_ring_world_r)

        # Phase 5: 缩放句柄几何（每轴端点一个方块，hit_test 圆形判定）
        self._scale_handles_tex_px = {}
        for name, (adir, _color) in AXIS_INFO.items():
            end = self.object_pos + adir * world_length
            self._scale_handles_tex_px[name] = (
                self._world_to_tex_px(end), self.SCALE_HANDLE_PX)

        # C4D move gizmo 的平面约束 L 形手柄（XY/XZ/YZ）。
        self._plane_handles_tex_px = self._build_plane_handles_tex_px(world_per_tex_px)

    def _build_ring_tex_px(self, axis_dir, world_radius: float) -> list:
        """构建旋转环的纹理像素折线点（绕物体原点，垂直于 axis_dir 的圆）。

        圆所在平面垂直于 axis_dir；用两个正交的切向量构造圆周采样点。
        """
        # 取与 axis_dir 正交的两个向量（切空间基）
        a = np.asarray(axis_dir, dtype=np.float32)
        # 选一个不平行的参考向量构造正交基
        ref = np.array([0.0, 1.0, 0.0], dtype=np.float32) if abs(a[1]) < 0.9 \
            else np.array([1.0, 0.0, 0.0], dtype=np.float32)
        t1 = np.cross(a, ref)
        t1 = t1 / (np.linalg.norm(t1) + 1e-12)
        t2 = np.cross(a, t1)
        t2 = t2 / (np.linalg.norm(t2) + 1e-12)
        center = self.object_pos
        pts = []
        for i in range(self.RING_SEGMENTS):
            theta = 2.0 * np.pi * i / self.RING_SEGMENTS
            wp = center + (t1 * np.cos(theta) + t2 * np.sin(theta)) * world_radius
            pts.append(self._world_to_tex_px(wp))
        return pts

    def _build_view_facing_ring_tex_px(self, world_radius: float) -> list:
        """构建面向相机的外旋转圈（C4D 灰白外圈）。"""
        if self._view is None:
            return []
        inv_view = np.linalg.inv(self._view)
        right = np.asarray(inv_view[:3, 0], dtype=np.float32)
        up = np.asarray(inv_view[:3, 1], dtype=np.float32)
        right = right / (np.linalg.norm(right) + 1e-12)
        up = up / (np.linalg.norm(up) + 1e-12)
        pts = []
        for i in range(self.RING_SEGMENTS):
            theta = 2.0 * np.pi * i / self.RING_SEGMENTS
            wp = self.object_pos + (right * np.cos(theta) + up * np.sin(theta)) * world_radius
            pts.append(self._world_to_tex_px(wp))
        return pts

    def _build_plane_handles_tex_px(self, world_per_tex_px: float) -> dict:
        """构建移动模式 L 形平面手柄的纹理像素折线。"""
        offset = float(world_per_tex_px * self.PLANE_HANDLE_OFFSET_PX)
        size = float(world_per_tex_px * self.PLANE_HANDLE_PX)
        pairs = {
            "xy": (AXIS_X, AXIS_Y, (1.0, 0.10, 0.18, 0.95)),
            "xz": (AXIS_X, AXIS_Z, (0.10, 0.92, 0.52, 0.95)),
            "yz": (AXIS_Y, AXIS_Z, (0.16, 0.48, 1.0, 0.95)),
        }
        out = {}
        for name, (a, b, _color) in pairs.items():
            p0 = self.object_pos + a * offset
            p1 = self.object_pos + a * offset + b * size
            p2 = self.object_pos + b * offset + a * size
            p3 = self.object_pos + b * offset
            out[name] = [
                self._world_to_tex_px(p0),
                self._world_to_tex_px(p1),
                self._world_to_tex_px(p2),
                self._world_to_tex_px(p3),
            ]
        return out

    def set_object_rot(self, rot_xyz: tuple) -> None:
        """设置物体当前旋转（度，Euler XYZ），旋转模式拖动锚点用。"""
        self.object_rot = (float(rot_xyz[0]), float(rot_xyz[1]), float(rot_xyz[2]))

    def _projection_ready(self) -> bool:
        """投影状态是否已就绪（update() 至少成功调用过一次）。

        AUD-88030：构造后 _proj/_view 为 None，此时 build_draw_list /
        begin_drag / _world_to_tex_ndc 会 None@None 抛 TypeError。所有消费
        投影矩阵的入口先经此守卫。
        """
        return self._proj is not None and self._view is not None

    def _world_to_tex_ndc(self, world_pt) -> tuple:
        """世界点 → 渲染目标 NDC（-1..1，节点投影空间）。

        AUD-88030：update() 之前调用（_proj/_view 为 None）时返回 (0.0, 0.0)
        而非抛 TypeError——调用方应先用 _projection_ready() 拦截。
        """
        if not self._projection_ready():
            return (0.0, 0.0)
        p = np.array([world_pt[0], world_pt[1], world_pt[2], 1.0], dtype=np.float64)
        clip = self._proj @ self._view @ p
        w = clip[3]
        if abs(w) < 1e-12:
            w = 1e-12
        return (float(clip[0] / w), float(clip[1] / w))

    def _world_to_tex_uv(self, world_pt) -> tuple:
        """世界点 → 纹理 UV（0..1，v 上正，GL 习惯）。"""
        nx, ny = self._world_to_tex_ndc(world_pt)
        return ((nx * 0.5) + 0.5, (ny * 0.5) + 0.5)

    def _world_to_tex_px(self, world_pt) -> tuple:
        """世界点 → 纹理像素坐标（原点左下，与 GL 一致，用于 hit_test）。"""
        nx, ny = self._world_to_tex_ndc(world_pt)
        return ((nx * 0.5 + 0.5) * self._tex_w_px,
                (ny * 0.5 + 0.5) * self._tex_h_px)

    # ── 绘制数据 ──

    def build_draw_list(self) -> list:
        """生成当前帧绘制数据列表（按 mode 返回轴/环/句柄）。

        MOVE:   返回轴箭头 + 平面手柄 + 中心点。
        ROTATE: 返回三环 + 灰白外圈 + 中心点。
        SCALE:  返回轴线 + 端点方块 + 中心点。

        AUD-88030：update() 之前调用（无投影状态）返回空列表，不抛异常。
        """
        if not self._projection_ready():
            return []
        if self.mode == GizmoMode.ROTATE:
            return self._build_ring_draw_list() + [self._build_center_draw()]
        if self.mode == GizmoMode.SCALE:
            return self._build_axis_draw_list() + self._build_scale_draw_list() + [self._build_center_draw()]
        return self._build_axis_draw_list() + self._build_plane_draw_list() + [self._build_center_draw()]

    def _build_axis_draw_list(self) -> list:
        """三轴绘制数据（MOVE / SCALE 共用）。"""
        if not self._axis_world:
            return []
        out = []
        for name, (start, adir, length) in self._axis_world.items():
            base_color = AXIS_INFO[name][1]
            if name == self.active_axis or name == self.hovered_axis:
                color = HOVER_COLOR
            else:
                color = base_color
            end_world = start + adir * length
            out.append(GizmoAxisDraw(
                name=name,
                start_uv=self._world_to_tex_uv(start),
                end_uv=self._world_to_tex_uv(end_world),
                color=color,
                world_start=start,
                world_dir=adir,
                world_length=length,
                head_uv=self._world_to_tex_uv(end_world),
            ))
        return out

    def _build_ring_draw_list(self) -> list:
        """旋转环绘制数据（Phase 5）。"""
        if not self._ring_tex_px:
            return []
        out = []
        if self._outer_ring_tex_px:
            out.append(GizmoRingDraw(
                name="screen",
                points_uv=[self._tex_px_to_uv(p) for p in self._outer_ring_tex_px],
                color=(0.86, 0.86, 0.82, 0.62),
                thickness_px=1.1,
            ))
        for name, pts_px in self._ring_tex_px.items():
            base_color = AXIS_INFO[name][1]
            if name == self.active_axis or name == self.hovered_axis:
                color = HOVER_COLOR
            else:
                color = base_color
            # 纹理像素 → UV
            pts_uv = [self._tex_px_to_uv(p) for p in pts_px]
            out.append(GizmoRingDraw(name=name, points_uv=pts_uv, color=color))
        return out

    def _build_plane_draw_list(self) -> list:
        """移动模式平面 L 形手柄绘制数据。"""
        if not self._plane_handles_tex_px:
            return []
        colors = {
            "xy": (1.0, 0.10, 0.18, 0.94),
            "xz": (0.10, 0.92, 0.52, 0.94),
            "yz": (0.16, 0.48, 1.0, 0.94),
        }
        out = []
        for name, pts_px in self._plane_handles_tex_px.items():
            color = HOVER_COLOR if name == self.active_axis or name == self.hovered_axis else colors[name]
            out.append(GizmoPlaneHandleDraw(
                name=name,
                points_uv=[self._tex_px_to_uv(p) for p in pts_px],
                color=color,
            ))
        return out

    def _build_scale_draw_list(self) -> list:
        """缩放句柄绘制数据（Phase 5，轴端点方块）。"""
        if not self._scale_handles_tex_px:
            return []
        out = []
        for name, ((cx, cy), r) in self._scale_handles_tex_px.items():
            base_color = AXIS_INFO[name][1]
            if name == self.active_axis or name == self.hovered_axis:
                color = HOVER_COLOR
            else:
                color = base_color
            out.append(GizmoScaleHandleDraw(
                name=name, center_uv=self._tex_px_to_uv((cx, cy)), color=color,
                side_px=self.SCALE_HANDLE_PX))
        return out

    def _build_center_draw(self) -> GizmoCenterDraw:
        """Gizmo 原点小黄点。"""
        return GizmoCenterDraw(center_uv=self._world_to_tex_uv(self.object_pos))

    def _tex_px_to_uv(self, tex_px: tuple) -> tuple:
        """纹理像素 → UV（0..1）。"""
        return (tex_px[0] / max(1, self._tex_w_px),
                tex_px[1] / max(1, self._tex_h_px))

    # ── 拾取 ──

    def hit_test(self, mouse_tex_px: tuple) -> Optional[str]:
        """纹理像素位置 → 命中的轴名（或 None）。按当前 mode 判定。

        MOVE:   鼠标到轴段最近距离 < 阈值。
        ROTATE: 鼠标到旋转环折线最近距离 < 阈值。
        SCALE:  鼠标在缩放句柄圆形（端点方块）半径内。
        """
        if self.mode == GizmoMode.ROTATE:
            return self._hit_test_ring(mouse_tex_px)
        if self.mode == GizmoMode.SCALE:
            return self._hit_test_scale(mouse_tex_px)
        # MOVE（默认）
        if not self._axis_tex_px:
            return None
        mx, my = float(mouse_tex_px[0]), float(mouse_tex_px[1])
        best_name: Optional[str] = None
        best_dist = self.HIT_PX_THRESHOLD
        for name, (s_px, e_px) in self._axis_tex_px.items():
            end_dist = float(np.hypot(mx - e_px[0], my - e_px[1]))
            if end_dist < self.AXIS_END_HIT_PX_THRESHOLD:
                end_score = end_dist * (self.HIT_PX_THRESHOLD / self.AXIS_END_HIT_PX_THRESHOLD)
                if end_score < best_dist:
                    best_dist = end_score
                    best_name = name
            d = _point_to_segment_px((mx, my), s_px, e_px)
            if d < best_dist:
                best_dist = d
                best_name = name
        for name, pts in self._plane_handles_tex_px.items():
            for i in range(max(0, len(pts) - 1)):
                d = _point_to_segment_px((mx, my), pts[i], pts[i + 1])
                if d < best_dist:
                    best_dist = d
                    best_name = name
        return best_name

    def _hit_test_ring(self, mouse_tex_px: tuple) -> Optional[str]:
        """旋转环拾取：鼠标到环折线最近距离 < 阈值。"""
        if not self._ring_tex_px:
            return None
        mx, my = float(mouse_tex_px[0]), float(mouse_tex_px[1])
        best_name: Optional[str] = None
        best_dist = self.HIT_PX_THRESHOLD
        for name, pts in self._ring_tex_px.items():
            for i in range(len(pts)):
                a = pts[i]
                b = pts[(i + 1) % len(pts)]
                d = _point_to_segment_px((mx, my), a, b)
                if d < best_dist:
                    best_dist = d
                    best_name = name
        return best_name

    def _hit_test_scale(self, mouse_tex_px: tuple) -> Optional[str]:
        """缩放句柄拾取：鼠标在端点方块半径内。"""
        if not self._scale_handles_tex_px:
            return None
        mx, my = float(mouse_tex_px[0]), float(mouse_tex_px[1])
        best_name: Optional[str] = None
        best_dist = self.SCALE_HANDLE_HIT_PX
        for name, ((cx, cy), r) in self._scale_handles_tex_px.items():
            d = float(np.hypot(mx - cx, my - cy))
            hit_r = max(float(r), self.SCALE_HANDLE_HIT_PX)
            if d < hit_r and d < best_dist:
                best_dist = d
                best_name = name
        return best_name

    # ── 拖动（投影约束）──

    def drag_offset(self, mouse_tex_ndc: tuple) -> Optional[dict]:
        """拖动时按 mode 计算 object_pos/rot/scale 变更。

        Args:
            mouse_tex_ndc: 鼠标的**渲染目标 NDC**（-1..1，Y 上正）。

        Returns:
            MOVE:   {"object_pos_x": 2.3}（单轴平移）
            ROTATE: {"object_rot_y": 45.0}（绕轴旋转，度）
            SCALE:  {"mesh_scale": 1.2}（统一缩放，Phase 6 扩展为分轴）
            None:   无 active_axis 或投影失败。
        """
        if self.mode == GizmoMode.ROTATE:
            return self.drag_rotate(mouse_tex_ndc)
        if self.mode == GizmoMode.SCALE:
            return self.drag_scale(mouse_tex_ndc)
        return self._drag_move(mouse_tex_ndc)

    def _drag_move(self, mouse_tex_ndc: tuple) -> Optional[dict]:
        """MOVE 模式：鼠标投影到 active_axis 世界射线 → object_pos 增量。"""
        if self.active_axis is None or self._proj is None:
            return None
        if self.active_axis in ("xy", "xz", "yz"):
            return self._drag_move_plane(mouse_tex_ndc)
        adir = AXIS_INFO[self.active_axis][0]
        anchor = getattr(self, "_drag_anchor_pos", self.object_pos)
        ray_o, ray_d = raycast.screen_to_ray(
            mouse_tex_ndc[0], mouse_tex_ndc[1], self._proj, self._view)
        t = raycast.ray_axis_intersect(ray_o, ray_d, anchor, adir)
        start_t = float(getattr(self, "_drag_axis_start_t", 0.0))
        delta_t = t - start_t
        new_val = float(np.asarray(anchor, dtype=np.float32)[_AXIS_IDX[self.active_axis]] + delta_t)
        return {_AXIS_PARAM[self.active_axis]: new_val}

    def _drag_move_plane(self, mouse_tex_ndc: tuple) -> Optional[dict]:
        """MOVE 模式：拖动 C4D 平面手柄，在 XY/XZ/YZ 平面内移动。"""
        if self.active_axis is None or self._proj is None or self._view is None:
            return None
        normals = {
            "xy": AXIS_Z,
            "xz": AXIS_Y,
            "yz": AXIS_X,
        }
        axes = {
            "xy": ("x", "y"),
            "xz": ("x", "z"),
            "yz": ("y", "z"),
        }
        normal = normals.get(self.active_axis)
        axis_names = axes.get(self.active_axis)
        if normal is None or axis_names is None:
            return None
        anchor = np.asarray(getattr(self, "_drag_anchor_pos", self.object_pos), dtype=np.float32)
        ray_o, ray_d = raycast.screen_to_ray(
            mouse_tex_ndc[0], mouse_tex_ndc[1], self._proj, self._view)
        hit = _ray_plane_intersect(ray_o, ray_d, anchor, normal)
        start_hit = getattr(self, "_drag_plane_start_hit", None)
        if hit is None or start_hit is None:
            return None
        delta = np.asarray(hit, dtype=np.float32) - np.asarray(start_hit, dtype=np.float32)
        new_pos = anchor + delta
        return {
            _AXIS_PARAM[axis_names[0]]: float(new_pos[_AXIS_IDX[axis_names[0]]]),
            _AXIS_PARAM[axis_names[1]]: float(new_pos[_AXIS_IDX[axis_names[1]]]),
        }

    def drag_rotate(self, mouse_tex_ndc: tuple) -> Optional[dict]:
        """ROTATE 模式：绕 active_axis 的旋转角增量（度）。

        近似：鼠标沿屏幕切向移动量 → 旋转角度（NDC 单位 × 灵敏度）。
        完整的绕轴旋转需要 trackball，此处先实现单轴增量旋转（可用）。
        """
        if self.active_axis is None:
            return None
        anchor_ndc = getattr(self, "_drag_anchor_ndc", None)
        if anchor_ndc is None:
            return None
        # 鼠标相对拖动起点的 NDC 位移 → 角度（顺时针为正，与 Euler 约定一致）
        dx = mouse_tex_ndc[0] - anchor_ndc[0]
        dy = mouse_tex_ndc[1] - anchor_ndc[1]
        # 用较大的屏幕分量作为旋转驱动（X 轴旋转看 dy，Y 轴看 dx，Z 轴看 dx）
        base_rot = self._drag_start_rot.get(_AXIS_IDX[self.active_axis], 0.0) \
            if hasattr(self, "_drag_start_rot") else 0.0
        # 灵敏度：1.0 NDC ≈ 90°
        delta_deg = (dx + dy) * 90.0
        new_rot = base_rot + delta_deg
        return {_AXIS_ROT_PARAM[self.active_axis]: float(new_rot)}

    def drag_scale(self, mouse_tex_ndc: tuple) -> Optional[dict]:
        """SCALE 模式：拖动 → 统一 mesh_scale（Phase 6 扩展为分轴）。

        近似：鼠标向远离原点方向拖 → 放大；靠近 → 缩小。
        """
        if self.active_axis is None:
            return None
        anchor_ndc = getattr(self, "_drag_anchor_ndc", None)
        if anchor_ndc is None:
            return None
        # 鼠标距拖动起点的位移 → 缩放因子（指数式，手感自然）
        dx = mouse_tex_ndc[0] - anchor_ndc[0]
        dy = mouse_tex_ndc[1] - anchor_ndc[1]
        move = (dx + dy) * self.SCALE_SENSITIVITY
        base_scale = getattr(self, "_drag_start_scale", 1.0)
        new_scale = max(0.01, base_scale * float(np.exp(move)))
        return {"mesh_scale": new_scale}

    def begin_drag(self, axis_name: str, mouse_tex_ndc: Optional[tuple] = None) -> None:
        """拖动开始：记录激活轴 + 锚点。Phase 5: 旋转/缩放还需记录起始 NDC/值。

        AUD-88030：update() 之前调用时跳过需要投影矩阵的 raycast 部分
        （_drag_axis_start_t / _drag_plane_start_hit 保持未设，drag 阶段用
        getattr 默认值兜底），不再对 None 矩阵抛 TypeError。
        """
        self.active_axis = axis_name
        self._drag_anchor_pos = self.object_pos.copy()
        if mouse_tex_ndc is not None:
            self._drag_anchor_ndc = (float(mouse_tex_ndc[0]), float(mouse_tex_ndc[1]))
        # 旋转/缩放：记录起始值用于增量计算
        if self.mode == GizmoMode.ROTATE:
            self._drag_start_rot = {
                _AXIS_IDX[n]: self.object_rot[_AXIS_IDX[n]] for n in AXIS_INFO}
        if self.mode == GizmoMode.SCALE:
            self._drag_start_scale = getattr(self, "_object_scale", 1.0)
        if not self._projection_ready():
            return
        if axis_name in AXIS_INFO and mouse_tex_ndc is not None:
            adir = AXIS_INFO[axis_name][0]
            ray_o, ray_d = raycast.screen_to_ray(
                mouse_tex_ndc[0], mouse_tex_ndc[1], self._proj, self._view)
            self._drag_axis_start_t = raycast.ray_axis_intersect(
                ray_o, ray_d, self._drag_anchor_pos, adir)
        if axis_name in ("xy", "xz", "yz") and mouse_tex_ndc is not None:
            normals = {"xy": AXIS_Z, "xz": AXIS_Y, "yz": AXIS_X}
            ray_o, ray_d = raycast.screen_to_ray(
                mouse_tex_ndc[0], mouse_tex_ndc[1], self._proj, self._view)
            self._drag_plane_start_hit = _ray_plane_intersect(
                ray_o, ray_d, self._drag_anchor_pos, normals[axis_name])

    def set_object_scale(self, scale: float) -> None:
        """设置物体当前 mesh_scale（缩放模式锚点用）。"""
        self._object_scale = float(scale)

    def end_drag(self) -> None:
        self.active_axis = None
        for attr in ("_drag_anchor", "_drag_anchor_pos", "_drag_anchor_ndc",
                     "_drag_start_rot", "_drag_start_scale",
                     "_drag_axis_start_t", "_drag_plane_start_hit"):
            if hasattr(self, attr):
                delattr(self, attr)


def _point_to_segment_px(p: tuple, a: tuple, b: tuple) -> float:
    """点 p 到线段 ab 的最近距离（2D 欧氏，像素单位）。"""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 < 1e-12:
        return float(np.hypot(px - ax, py - ay))
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return float(np.hypot(px - cx, py - cy))


def _ray_plane_intersect(ray_o: np.ndarray, ray_d: np.ndarray,
                         plane_point: np.ndarray,
                         plane_normal: np.ndarray) -> Optional[np.ndarray]:
    """射线和平面相交，返回世界点；平行时返回 None。"""
    n = np.asarray(plane_normal, dtype=np.float32)
    denom = float(np.dot(ray_d, n))
    if abs(denom) < 1e-6:
        return None
    t = float(np.dot(np.asarray(plane_point, dtype=np.float32) - ray_o, n) / denom)
    return np.asarray(ray_o + ray_d * t, dtype=np.float32)
