"""Node Icon Overlay — Camera3D/Light3D 视口图标渲染（P4 阶段四）。

在 3D 操作视口显示摄影机图标（视锥线框）和灯光图标（按类型不同形态），
方便用户在场景中定位、移动、旋转、缩放 Camera3D/Light3D 节点。

设计（与 gizmo_overlay.py 同管道，纯 numpy 数学 + 数据生成）：
    - 工作在"渲染目标（texture）空间"：世界点 → 节点 proj/view → 纹理 NDC → 纹理 UV。
    - CompositeViewer 负责把纹理 UV 经 letterbox 映射到视口 NDC，用原生 GL 画线。
    - 图标不进合成输出纹理（仅视口辅助显示，符合 Nuke/Blender 惯例）。
    - 恒定屏幕像素尺寸：复用 gizmo_overlay 的 world_per_tex_px 公式。

图标几何（世界空间顶点 → 投影到 UV）：
    - 相机：near 平面矩形框 + 4 条视锥线到 far 平面 + 朝向三角形。
    - 灯光：按类型（directional/point/spot/ambient）不同形态。

拾取（hit_test，阶段五用）：纹理像素距离判定，复用 gizmo_overlay 模式。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from . import math_utils as m3d
from .scene_payload import (
    LIGHT_TYPE_DIRECTIONAL, LIGHT_TYPE_POINT, LIGHT_TYPE_SPOT)


# ── 视觉常量 ──
# 图标恒定屏幕像素尺寸（纹理像素，fit 模式 ≈ 屏幕像素）
ICON_PX_SIZE = 58.0
# 拾取阈值（纹理像素，鼠标距图标线段的最大允许距离）
ICON_HIT_PX_THRESHOLD = 14.0
# 视锥线框长度（世界单位由屏幕像素尺寸换算，避免远裁切面把图标撑爆）
FRUSTUM_LENGTH = 2.45

# 相机图标颜色（青蓝，区别于物体 gizmo 的红绿蓝）
CAMERA_COLOR = (0.2, 0.7, 1.0)
CAMERA_SELECTED_COLOR = (1.0, 0.9, 0.2)  # 选中态金色
# 活动渲染相机：醒目的亮白绿（最高优先级，覆盖选中态），让用户一眼看出
# 当前 Scene3D 用的是哪个相机的视角。
CAMERA_ACTIVE_COLOR = (0.4, 1.0, 0.6)
# 灯光图标颜色（暖黄，与 Light 节点 header_color 一致）
LIGHT_COLOR = (0.95, 0.75, 0.25)
LIGHT_SELECTED_COLOR = (1.0, 0.9, 0.2)


@dataclass
class IconLineDraw:
    """图标线段绘制数据（UV 坐标 + 颜色 + 线宽倍率）。

    一条折线由 points_uv 列表定义（≥2 点），用 GL_LINE_STRIP 或细分为
    多段 LINE 绘制。CompositeViewer 的 _draw_overlay_thick_line 负责实际绘制。
    """
    node_id: str                     # 关联的节点 id（拾取/选中用）
    node_type: str                   # "camera3d" | "light3d"
    points_uv: list                  # [(u, v), ...] 纹理 UV（0..1）
    color: tuple                     # (r, g, b) 0..1
    width: float = 2.0               # 线宽倍率（相对默认）
    closed: bool = False             # 是否闭合（首尾相连）


@dataclass
class IconHitTarget:
    """图标拾取命中目标（hit_test 返回）。

    存图标关联的节点信息，供 CompositeViewer 选中节点 + 启动拖动用。
    """
    node_id: str
    node_type: str
    anchor_world: tuple              # (x, y, z) 图标锚点世界坐标（拖动起点）


class NodeIconRenderer:
    """Camera3D/Light3D 视口图标渲染器（纯 numpy 数学）。

    生命周期：由 CompositeViewer 在 3D 模式下持有。每帧 paintGL 前调 update()
    刷新投影；add_*_icon() 注册本帧要画的图标；build_draw_list() 产出绘制数据；
    hit_test() 用于鼠标拾取。

    所有状态在 UI 线程维护，不涉及渲染线程。
    """

    def __init__(self) -> None:
        # 投影状态（每帧 update 刷新）
        self._proj: Optional[np.ndarray] = None
        self._view: Optional[np.ndarray] = None
        self._tex_w_px: int = 1
        self._tex_h_px: int = 1
        self._cam_dist: float = 5.0
        self._cam_fov: float = 50.0
        # 本帧注册的图标（每帧重建）
        self._icons: List[dict] = []
        # 选中节点 id（高亮用）
        self.selected_node_id: Optional[str] = None
        # 活动渲染相机 node_id（当前 Scene3D 实际使用的相机视角，醒目高亮）
        self.active_node_id: Optional[str] = None

    def update(self, proj, view, tex_w_px: int, tex_h_px: int,
               cam_dist: float, cam_fov: float) -> None:
        """刷新投影状态。每帧 paintGL 前调用。

        Args:
            proj/view: 当前查看相机的投影/视图矩阵（行主序 numpy）。proj 的 aspect
                必须与渲染目标一致（= tex_w/tex_h）。
            tex_w_px, tex_h_px: 渲染目标纹理像素尺寸。
            cam_dist: 相机到 target 的距离（世界单位）。
            cam_fov: 相机垂直 FOV（度）。
        """
        self._proj = np.asarray(proj, dtype=np.float64)
        self._view = np.asarray(view, dtype=np.float64)
        self._tex_w_px = max(1, int(tex_w_px))
        self._tex_h_px = max(1, int(tex_h_px))
        self._cam_dist = max(0.001, float(cam_dist))
        self._cam_fov = float(cam_fov)
        # 每帧清空图标（重新注册）
        self._icons = []

    # ── 图标注册 ──

    def add_camera_icon(self, node_id: str, eye: tuple, target: tuple,
                        near: float, far: float, fov: float, aspect: float,
                        selected: bool = False, active: bool = False,
                        camera_mode: str = "orbit", ortho_scale: float = 5.0,
                        roll: float = 0.0, lens_shift_x: float = 0.0,
                        lens_shift_y: float = 0.0,
                        focus_distance: float = 5.0,
                        dof_enable: bool = False,
                        fstop: float = 2.8,
                        focal_length: float = 38.6) -> None:
        """注册一个摄影机图标（视锥线框 + 朝向指示）。

        Args:
            node_id: Camera3D 节点 id。
            eye: 相机世界坐标 (x, y, z)。
            target: 注视中心 (x, y, z)。
            near/far: 裁剪面距离。注意（AUD-9100）：图标是屏幕等像素常量
                尺寸的示意线框，视锥长度由 ``size * FRUSTUM_LENGTH`` 推出，
                near/far **当前存档不参与图标几何**（参数保留供 API 兼容与
                未来视锥按真实裁剪面绘制）。
            fov: 垂直 FOV（度，算视锥张角）。
            aspect: 宽高比（算水平张角）。
            selected: 是否选中（影响颜色）。
            active: 是否为活动渲染相机（当前 Scene3D 使用的视角，最高优先级高亮）。
            camera_mode/ortho_scale: 正交相机用平行视锥和 scale 提示画幅。
            roll/lens_shift_x/y: 旋转图标坐标系并偏移视锥中心。
            focus_distance/dof_enable: 在视锥中绘制焦平面标记。
            fstop/focal_length: 在镜头环内绘制可变化的光圈提示。
        """
        self._icons.append({
            "type": "camera3d",
            "node_id": node_id,
            "eye": eye,
            "target": target,
            "near": near,
            "far": far,
            "fov": fov,
            "aspect": aspect,
            "selected": selected,
            "active": active,
            "camera_mode": camera_mode,
            "ortho_scale": ortho_scale,
            "roll": roll,
            "lens_shift_x": lens_shift_x,
            "lens_shift_y": lens_shift_y,
            "focus_distance": focus_distance,
            "dof_enable": dof_enable,
            "fstop": fstop,
            "focal_length": focal_length,
        })

    def add_light_icon(self, node_id: str, light_type: str,
                       pos: tuple, direction: tuple, color: tuple,
                       icon_pos: Optional[tuple] = None,
                       range: float = 10.0, outer_cone: float = 45.0,
                       selected: bool = False) -> None:
        """注册一个灯光图标（按类型不同形态）。

        Args:
            node_id: Light3D 节点 id。
            light_type: directional/point/spot/ambient。
            pos: 灯光世界坐标（point/spot 用）。
            direction: 灯光方向（directional/spot 用，归一化）。
            color: 灯光颜色（影响图标色调）。
            icon_pos: 图标锚点（directional/ambient 无位置时用）。
            range: 影响范围。注意（AUD-9100）：图标是屏幕等像素常量尺寸的
                示意图形，``range`` **当前存档不参与 point/spot 图标几何**
                （参数保留供 API 兼容与未来按真实衰减半径绘制）。
            outer_cone: 聚光外锥角（spot 图标锥形张角）。
            selected: 是否选中。
        """
        self._icons.append({
            "type": "light3d",
            "node_id": node_id,
            "light_type": light_type,
            "pos": pos,
            "direction": direction,
            "color": color,
            "icon_pos": icon_pos if icon_pos is not None else pos,
            "range": range,
            "outer_cone": outer_cone,
            "selected": selected,
        })

    # ── 投影工具（复用 gizmo_overlay 模式）──

    def _world_to_tex_uv(self, world_pt) -> Optional[tuple]:
        """世界点 → 纹理 UV（0..1）。相机后方的点返回 None（剔除）。"""
        if self._proj is None or self._view is None:
            return None
        p = np.array([world_pt[0], world_pt[1], world_pt[2], 1.0], dtype=np.float64)
        clip = self._proj @ self._view @ p
        w = clip[3]
        if w <= 1e-6:
            return None  # 相机后方或裁剪面外
        nx = float(clip[0] / w)
        ny = float(clip[1] / w)
        return ((nx * 0.5) + 0.5, (ny * 0.5) + 0.5)

    def _world_to_tex_px(self, world_pt) -> Optional[tuple]:
        """世界点 → 纹理像素坐标。"""
        uv = self._world_to_tex_uv(world_pt)
        if uv is None:
            return None
        return (uv[0] * self._tex_w_px, uv[1] * self._tex_h_px)

    def _world_per_tex_px(self) -> float:
        """纹理恒定像素 → 世界长度换算（让图标恒定屏幕大小）。"""
        fov_rad = np.radians(self._cam_fov)
        return (2.0 * self._cam_dist * np.tan(fov_rad * 0.5)) / self._tex_h_px

    # ── 几何生成 ──

    def _build_camera_geometry(self, icon: dict) -> List[Tuple[list, tuple, float, bool]]:
        """生成 DCC 风格相机图标。

        几何由机身、镜头环、取景/顶把手、参数化视锥与焦平面组成。FOV、
        lens shift、正交 scale、focus distance、f-stop 会直接影响线框形态，
        因此属性面板拖动参数时图标会实时反馈。
        """
        eye = np.array(icon["eye"], dtype=np.float32)
        target = np.array(icon["target"], dtype=np.float32)
        fov = float(icon["fov"])
        aspect = float(icon["aspect"])
        camera_mode = str(icon.get("camera_mode", "orbit"))
        # 颜色优先级：活动相机 > 选中 > 默认
        if icon.get("active"):
            color = CAMERA_ACTIVE_COLOR
        elif icon["selected"]:
            color = CAMERA_SELECTED_COLOR
        else:
            color = CAMERA_COLOR

        basis = self._camera_basis(eye, target, float(icon.get("roll", 0.0)))
        if basis is None:
            return []
        forward, right, up = basis

        icon_world_size = self._world_per_tex_px() * ICON_PX_SIZE
        body = self._camera_body_geometry(eye, forward, right, up, icon_world_size, color)
        frustum = self._camera_frustum_geometry(
            eye, forward, right, up, icon_world_size, color, fov, aspect,
            camera_mode, float(icon.get("ortho_scale", 5.0)),
            float(icon.get("lens_shift_x", 0.0)),
            float(icon.get("lens_shift_y", 0.0)),
            float(icon.get("focus_distance", 5.0)),
            bool(icon.get("dof_enable", False)),
        )
        aperture = self._camera_aperture_geometry(
            eye, forward, right, up, icon_world_size, color,
            float(icon.get("fstop", 2.8)), float(icon.get("focal_length", 38.6)))

        lines = []
        lines.extend(body)
        lines.extend(aperture)
        lines.extend(frustum)
        return lines

    def _camera_basis(self, eye, target, roll_deg: float):
        """Return forward/right/up vectors for a Camera3D icon."""
        forward = target - eye
        fl = float(np.linalg.norm(forward))
        if fl < 1e-4:
            return None
        forward = forward / fl
        world_up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        if abs(float(np.dot(forward, world_up))) > 0.99:
            world_up = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        right = m3d.normalize(np.cross(forward, world_up))
        up = m3d.normalize(np.cross(right, forward))
        if abs(float(roll_deg)) > 1e-4:
            right = m3d.normalize(m3d.rotate_around_axis(right, forward, roll_deg))
            up = m3d.normalize(np.cross(right, forward))
        return forward, right, up

    def _camera_box_points(self, center, forward, right, up,
                           half_w: float, half_h: float, half_d: float) -> list:
        """Return the 8 corners of an oriented camera-body box."""
        pts = {}
        for z_name, z_sign in (("front", 1.0), ("back", -1.0)):
            plane_center = center + forward * half_d * z_sign
            pts[z_name] = [
                plane_center + up * half_h - right * half_w,
                plane_center + up * half_h + right * half_w,
                plane_center - up * half_h + right * half_w,
                plane_center - up * half_h - right * half_w,
            ]
        return pts

    def _camera_body_geometry(self, eye, forward, right, up, size, color) -> list:
        """Camera body: box, lens bridge, viewfinder and top handle."""
        body_center = eye - forward * size * 0.14
        body = self._camera_box_points(
            body_center, forward, right, up,
            half_w=size * 0.28, half_h=size * 0.20, half_d=size * 0.18)
        front = body["front"]
        back = body["back"]
        lines = [
            ([tuple(p) for p in front], color, 2.2, True),
            ([tuple(p) for p in back], color, 1.8, True),
        ]
        for i in range(4):
            lines.append(([tuple(front[i]), tuple(back[i])], color, 1.4, False))

        # 镜头桶：两个圆环 + 连接筋，明显表达“看向 forward”。
        lens_center = eye + forward * size * 0.18
        lens_back = lens_center - forward * size * 0.08
        lens_front = lens_center + forward * size * 0.16
        back_ring = self._circle_points(lens_back, forward, size * 0.16, 18)
        front_ring = self._circle_points(lens_front, forward, size * 0.12, 18)
        lines.append((back_ring, color, 1.7, True))
        lines.append((front_ring, color, 2.1, True))
        for idx in (0, 4, 9, 13):
            if idx < len(back_ring) and idx < len(front_ring):
                lines.append(([back_ring[idx], front_ring[idx]], color, 1.2, False))

        # 顶部取景框/把手，给图标一个更接近 C4D/DCC 的轮廓。
        top_y = eye + up * size * 0.31 - forward * size * 0.18
        handle = [
            top_y - right * size * 0.18,
            top_y - right * size * 0.12 + up * size * 0.13,
            top_y + right * size * 0.12 + up * size * 0.13,
            top_y + right * size * 0.18,
        ]
        lines.append(([tuple(p) for p in handle], color, 1.8, False))

        side = [
            eye - right * size * 0.30 - forward * size * 0.02,
            eye - right * size * 0.44 + forward * size * 0.10,
            eye - right * size * 0.38 + forward * size * 0.24,
        ]
        lines.append(([tuple(p) for p in side], color, 1.4, False))
        return lines

    def _camera_aperture_geometry(self, eye, forward, right, up, size,
                                  color, fstop: float, focal_length: float) -> list:
        """Lens aperture cue: smaller f-stop draws a larger iris."""
        fstop = max(0.5, float(fstop))
        focal_norm = min(1.0, max(0.0, (float(focal_length) - 18.0) / 132.0))
        # f/0.5 -> large, f/16+ -> small. Focal length adds a subtle zoom cue.
        aperture_factor = min(1.0, max(0.28, 1.25 / np.sqrt(fstop)))
        radius = size * (0.045 + 0.055 * aperture_factor + 0.018 * focal_norm)
        center = eye + forward * size * 0.35
        iris = self._circle_points(center, forward, radius, 12)
        blade_radius = radius * 1.35
        blades = []
        for i in range(6):
            ang = 2.0 * np.pi * i / 6.0
            p = (center
                 + right * blade_radius * float(np.cos(ang))
                 + up * blade_radius * float(np.sin(ang)))
            blades.append(tuple(p))
        return [
            (iris, color, 1.4, True),
            (blades, color, 1.0, True),
        ]

    def _camera_frustum_geometry(self, eye, forward, right, up, size, color,
                                 fov: float, aspect: float, camera_mode: str,
                                 ortho_scale: float, shift_x: float, shift_y: float,
                                 focus_distance: float, dof_enable: bool) -> list:
        """Projection cue: perspective pyramid or orthographic box.

        AUD-9100：视锥深度由 ``size * 0.55 / size * FRUSTUM_LENGTH`` 常量
        推出，不读取 icon["near"]/icon["far"]（见 add_camera_icon docstring）。
        """
        fov = max(1.0, min(170.0, float(fov)))
        aspect = max(0.1, min(4.0, float(aspect)))
        shift_x = max(-2.0, min(2.0, float(shift_x)))
        shift_y = max(-2.0, min(2.0, float(shift_y)))
        focus_distance = max(0.1, float(focus_distance))

        lens_tip = eye + forward * size * 0.36
        near_d = size * 0.55
        far_d = size * FRUSTUM_LENGTH
        tan_half = float(np.tan(np.radians(fov) * 0.5))
        fov_scale = max(0.20, min(2.2, tan_half / np.tan(np.radians(50.0) * 0.5)))
        ortho_factor = max(0.40, min(1.9, float(ortho_scale) / 5.0))

        if camera_mode == "ortho":
            near_h = size * 0.34 * ortho_factor
            far_h = near_h
        else:
            near_h = size * 0.16
            far_h = size * 0.62 * fov_scale
        near_w = near_h * aspect
        far_w = far_h * aspect
        far_shift = right * (far_w * shift_x) + up * (far_h * shift_y)
        near_shift = right * (near_w * shift_x) + up * (near_h * shift_y)

        near_center = lens_tip + forward * near_d + near_shift
        far_center = lens_tip + forward * far_d + far_shift
        near_rect = self._camera_plane_rect(near_center, right, up, near_w, near_h)
        far_rect = self._camera_plane_rect(far_center, right, up, far_w, far_h)

        lines = [
            ([tuple(p) for p in near_rect], color, 1.2, True),
            ([tuple(p) for p in far_rect], color, 1.7, True),
        ]

        if camera_mode == "ortho":
            for i in range(4):
                lines.append(([tuple(near_rect[i]), tuple(far_rect[i])], color, 1.2, False))
            # Orthographic marker: parallel cross line in the far plane.
            lines.append(([
                tuple(far_center - right * far_w * 0.55),
                tuple(far_center + right * far_w * 0.55),
            ], color, 1.0, False))
        else:
            for p in far_rect:
                lines.append(([tuple(lens_tip), tuple(p)], color, 1.2, False))
            # Center ray shows lens shift immediately.
            lines.append(([tuple(lens_tip), tuple(far_center)], color, 1.0, False))

        # Focus plane marker: always responds to focus_distance, brighter when DOF is on.
        focus_t = max(0.0, min(1.0, focus_distance / 20.0))
        focus_d = near_d + (far_d - near_d) * focus_t
        if camera_mode == "ortho":
            focus_h = near_h
            focus_w = near_w
            focus_shift = near_shift
        else:
            focus_alpha = (focus_d - near_d) / max(far_d - near_d, 1e-4)
            focus_h = near_h + (far_h - near_h) * focus_alpha
            focus_w = near_w + (far_w - near_w) * focus_alpha
            focus_shift = near_shift + (far_shift - near_shift) * focus_alpha
        focus_center = lens_tip + forward * focus_d + focus_shift
        focus_rect = self._camera_plane_rect(focus_center, right, up, focus_w, focus_h)
        focus_color = (1.0, 0.84, 0.22) if dof_enable else (0.55, 0.72, 1.0)
        lines.append(([tuple(p) for p in focus_rect], focus_color, 1.1, True))
        # Small focus tick on the right edge, C4D-like parameter handle hint.
        tick_center = focus_center + right * focus_w
        tick_len = size * 0.09
        lines.append(([
            tuple(tick_center - up * tick_len),
            tuple(tick_center + up * tick_len),
        ], focus_color, 1.5, False))
        return lines

    def _camera_plane_rect(self, center, right, up, half_w: float, half_h: float) -> list:
        """Return TL/TR/BR/BL corners for a camera plane."""
        return [
            center + up * half_h - right * half_w,
            center + up * half_h + right * half_w,
            center - up * half_h + right * half_w,
            center - up * half_h - right * half_w,
        ]

    def _build_light_geometry(self, icon: dict) -> List[Tuple[list, tuple, float, bool]]:
        """生成灯光图标几何（按 light_type 不同形态）。

        directional：圆盘 + 平行射线。
        point：小球（圆）+ 8 向放射星芒。
        spot：锥形线框（pos → 锥底圆）。
        ambient：圆形 + 向内箭头。

        AUD-9100：icon["range"] 不参与几何（见 add_light_icon docstring）；
        spot 锥形张角由 outer_cone 驱动。
        """
        light_type = icon["light_type"]
        direction = np.array(icon["direction"], dtype=np.float32)
        d_len = float(np.linalg.norm(direction))
        if d_len > 1e-4:
            direction = direction / d_len
        else:
            direction = np.array([0.0, -1.0, 0.0], dtype=np.float32)
        color_rgb = icon["color"]
        base_color = LIGHT_SELECTED_COLOR if icon["selected"] else \
            (min(1.0, LIGHT_COLOR[0] + color_rgb[0] * 0.2),
             min(1.0, LIGHT_COLOR[1] + color_rgb[1] * 0.2),
             min(1.0, LIGHT_COLOR[2] + color_rgb[2] * 0.2))

        icon_world_size = self._world_per_tex_px() * ICON_PX_SIZE

        if light_type == LIGHT_TYPE_DIRECTIONAL:
            lines = self._build_directional_icon(
                icon["icon_pos"], direction, base_color, icon_world_size)
            anchor = icon["icon_pos"]
        elif light_type == LIGHT_TYPE_POINT:
            lines = self._build_point_icon(
                icon["pos"], base_color, icon_world_size)
            anchor = icon["pos"]
        elif light_type == LIGHT_TYPE_SPOT:
            lines = self._build_spot_icon(
                icon["pos"], direction, base_color, icon_world_size,
                icon["outer_cone"])
            anchor = icon["pos"]
        else:  # ambient
            lines = self._build_ambient_icon(
                icon["icon_pos"], base_color, icon_world_size)
            anchor = icon["icon_pos"]
        # 世界坐标轴指示器（红X/绿Y/蓝Z），锚在灯光位置，仅显示方向参考
        lines.extend(self._build_axis_indicator(anchor, icon_world_size))
        return lines

    def _build_directional_icon(self, pos, direction, color, size) -> list:
        """平行光图标：圆盘 + 平行射线。"""
        center = np.array(pos, dtype=np.float32)
        # 圆盘半径
        r = size * 0.35
        # 构造垂直于 direction 的圆
        circle_pts = self._circle_points(center, direction, r, 16)
        lines = []
        if len(circle_pts) >= 2:
            lines.append((circle_pts, color, 2.0, True))
        # 平行射线（沿 direction 方向，3 条）
        ray_len = size * 0.5
        # 找两个垂直于 direction 的基向量
        world_up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        if abs(float(np.dot(direction, world_up))) > 0.99:
            world_up = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        right = m3d.normalize(np.cross(direction, world_up))
        up = m3d.normalize(np.cross(right, direction))
        offsets = [right * r * 0.6, -right * r * 0.6, up * r * 0.6]
        for off in offsets:
            start = center + off
            end = start + direction * ray_len
            lines.append(([tuple(start), tuple(end)], color, 1.5, False))
        return lines

    def _build_point_icon(self, pos, color, size) -> list:
        """点光源图标：小球（圆）+ 8 向放射星芒。"""
        center = np.array(pos, dtype=np.float32)
        r = size * 0.3
        lines = []
        # 小球（3 个正交方向的圆，近似球体）
        for axis in [np.array([1, 0, 0], dtype=np.float32),
                     np.array([0, 1, 0], dtype=np.float32),
                     np.array([0, 0, 1], dtype=np.float32)]:
            circle_pts = self._circle_points(center, axis, r, 12)
            if len(circle_pts) >= 2:
                lines.append((circle_pts, color, 1.5, True))
        # 8 向放射星芒
        ray_len = size * 0.5
        dirs = [
            [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0],
            [0, 0, 1], [0, 0, -1],
            [0.7, 0.7, 0], [-0.7, -0.7, 0],
        ]
        for d in dirs:
            dvec = m3d.normalize(np.array(d, dtype=np.float32))
            end = center + dvec * ray_len
            lines.append(([tuple(center), tuple(end)], color, 1.5, False))
        return lines

    def _build_spot_icon(self, pos, direction, color, size, outer_cone_deg) -> list:
        """聚光灯图标：锥形线框（pos → 锥底圆）。"""
        apex = np.array(pos, dtype=np.float32)
        cone_len = size * 0.8
        cone_rad = cone_len * float(np.tan(np.radians(min(outer_cone_deg, 89.0))))
        base_center = apex + direction * cone_len
        # 锥底圆
        circle_pts = self._circle_points(base_center, direction, cone_rad, 16)
        lines = []
        if len(circle_pts) >= 2:
            lines.append((circle_pts, color, 1.5, True))
        # 锥身 4 条线（apex → 圆周 4 点）
        if len(circle_pts) >= 4:
            for i in range(0, len(circle_pts), max(1, len(circle_pts) // 4)):
                lines.append(([tuple(apex), circle_pts[i]], color, 1.5, False))
        return lines

    def _build_ambient_icon(self, pos, color, size) -> list:
        """环境光图标：圆形 + 向内箭头（全景覆盖指示）。"""
        center = np.array(pos, dtype=np.float32)
        r = size * 0.4
        lines = []
        # 大圆
        circle_pts = self._circle_points(center, np.array([0, 1, 0], dtype=np.float32), r, 20)
        if len(circle_pts) >= 2:
            lines.append((circle_pts, color, 2.0, True))
        # 4 个向内箭头
        arrow_len = size * 0.25
        for d in [[1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]]:
            dvec = m3d.normalize(np.array(d, dtype=np.float32))
            outer = center + dvec * r
            inner = center + dvec * (r - arrow_len)
            lines.append(([tuple(outer), tuple(inner)], color, 1.5, False))
        return lines

    def _build_axis_indicator(self, anchor_world, size) -> list:
        """在 anchor_world 画世界坐标轴指示器（红X/绿Y/蓝Z 三条短线）。

        仅显示方向（看清朝向），不可交互。尺寸约图标的 0.5 倍，避免喧宾夺主。
        返回 [(points_world, color_rgb, width, closed), ...] 三条线段组。
        颜色与 gizmo_overlay.AXIS_INFO 一致（去 alpha 通道，IconLineDraw 用 RGB）。
        """
        center = np.asarray(anchor_world, dtype=np.float32)
        axis_len = size * 0.5
        # 红 X / 绿 Y / 蓝 Z（RGB，与 AXIS_INFO 的 RGBA 取前 3 分量）
        axes = [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), (0.95, 0.25, 0.20)),
            (np.array([0.0, 1.0, 0.0], dtype=np.float32), (0.30, 0.90, 0.35)),
            (np.array([0.0, 0.0, 1.0], dtype=np.float32), (0.30, 0.55, 1.00)),
        ]
        lines = []
        for direction, color in axes:
            end = center + direction * axis_len
            lines.append(([tuple(center), tuple(end)], color, 1.5, False))
        return lines

    def _circle_points(self, center, axis, radius, segments) -> list:
        """生成绕 axis 轴的圆周点（世界坐标，center 为圆心）。

        用两个正交基向量构造圆周采样，返回 list of tuple。
        """
        a = m3d.normalize(np.asarray(axis, dtype=np.float32))
        ref = np.array([0.0, 1.0, 0.0], dtype=np.float32) if abs(a[1]) < 0.9 \
            else np.array([1.0, 0.0, 0.0], dtype=np.float32)
        t1 = m3d.normalize(np.cross(a, ref))
        t2 = m3d.normalize(np.cross(a, t1))
        pts = []
        for i in range(segments):
            ang = 2.0 * np.pi * i / segments
            p = center + t1 * radius * float(np.cos(ang)) + t2 * radius * float(np.sin(ang))
            pts.append(tuple(p))
        return pts

    # ── 绘制数据生成 ──

    def build_draw_list(self) -> List[IconLineDraw]:
        """生成本帧所有图标的绘制数据。

        遍历注册的图标，生成世界坐标线段组，投影到纹理 UV，封装为 IconLineDraw。
        相机后方的线段（投影失败）被剔除。
        """
        out: List[IconLineDraw] = []
        for icon in self._icons:
            if icon["type"] == "camera3d":
                geom_groups = self._build_camera_geometry(icon)
            else:
                geom_groups = self._build_light_geometry(icon)
            for points_world, color, width, closed in geom_groups:
                # 投影所有点到 UV
                pts_uv = []
                for pw in points_world:
                    uv = self._world_to_tex_uv(pw)
                    if uv is not None:
                        pts_uv.append(uv)
                if len(pts_uv) >= 2:
                    out.append(IconLineDraw(
                        node_id=icon["node_id"],
                        node_type=icon["type"],
                        points_uv=pts_uv,
                        color=color,
                        width=width,
                        closed=closed,
                    ))
        return out

    # ── 拾取（阶段五用）──

    def hit_test(self, tex_px_x: float, tex_px_y: float) -> Optional[IconHitTarget]:
        """纹理像素坐标拾取测试（返回最近的命中图标）。

        遍历所有图标线段，找鼠标点到线段的最小纹理像素距离，小于阈值则命中。
        复用 gizmo_overlay 的点到线段距离判定模式。

        Args:
            tex_px_x, tex_px_y: 鼠标在纹理像素坐标（原点左下）。

        Returns:
            命中的 IconHitTarget（含 node_id/node_type/anchor），或 None。
        """
        best_dist = ICON_HIT_PX_THRESHOLD
        best_icon: Optional[dict] = None
        for icon in self._icons:
            if icon["type"] == "camera3d":
                geom_groups = self._build_camera_geometry(icon)
                anchor = icon["eye"]
            else:
                geom_groups = self._build_light_geometry(icon)
                anchor = icon.get("icon_pos") or icon["pos"]
            for points_world, _color, _width, closed in geom_groups:
                pts_px = []
                for pw in points_world:
                    px = self._world_to_tex_px(pw)
                    if px is not None:
                        pts_px.append(px)
                if len(pts_px) < 2:
                    continue
                # 检查每条线段
                seg_count = len(pts_px) if closed else len(pts_px) - 1
                for i in range(seg_count):
                    a = pts_px[i]
                    b = pts_px[(i + 1) % len(pts_px)] if closed else pts_px[i + 1]
                    d = _point_to_segment_dist(tex_px_x, tex_px_y, a[0], a[1], b[0], b[1])
                    if d < best_dist:
                        best_dist = d
                        best_icon = icon
        if best_icon is None:
            return None
        anchor = best_icon["eye"] if best_icon["type"] == "camera3d" else \
            (best_icon.get("icon_pos") or best_icon["pos"])
        return IconHitTarget(
            node_id=best_icon["node_id"],
            node_type=best_icon["type"],
            anchor_world=tuple(anchor),
        )


def _point_to_segment_dist(px: float, py: float,
                           ax: float, ay: float,
                           bx: float, by: float) -> float:
    """点到线段的最短距离（2D，纹理像素空间）。"""
    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-6:
        # 退化线段（点）
        return float(np.hypot(px - ax, py - ay))
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return float(np.hypot(px - cx, py - cy))
