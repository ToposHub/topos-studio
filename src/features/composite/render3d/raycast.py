"""
3D 射线求交工具库 — Transform Gizmo 拾取与物体拾取的几何核心。

纯数学实现（仅依赖 numpy），不引入 GL/PyQt 依赖，方便单元测试。

坐标系约定（与 math_utils 一致）：OpenGL 右手系，相机看向 -Z。

提供：
- screen_to_ray      : 屏幕/视口坐标 → 世界射线（gluUnProject 等价）
- ray_axis_intersect : 射线与「轴射线」最近点（拖单轴移动用）
- ray_plane_intersect: 射线与平面求交（拖动时取地面/屏幕平面投影）
- ray_mesh_intersect : 射线与三角网格求交（Möller–Trumbore，物体拾取用）
"""

from typing import Optional, Tuple

import numpy as np


def screen_to_ray(ndc_x: float, ndc_y: float,
                  proj: np.ndarray, view: np.ndarray,
                  ) -> Tuple[np.ndarray, np.ndarray]:
    """屏幕 NDC 坐标 → 世界空间射线 (origin, direction)。

    标准 gluUnProject 算法：把近裁剪面与远裁剪面上的两个点反投影到世界
    空间，origin = 近点，direction = normalize(远点 - 近点)。

    Args:
        ndc_x: 视口归一化设备坐标 X（-1..1，左负右正）。
        ndc_y: 视口归一化设备坐标 Y（-1..1，下负上正，OpenGL 习惯）。
        proj: 4x4 投影矩阵（行主序，与 math_utils 一致）。
        view: 4x4 视图矩阵（行主序）。

    Returns:
        (origin, direction)：世界空间射线起点 (3,) 与方向 (3,)（已归一化）。
    """
    # 联合反投影矩阵：(proj @ view)^-1 把 clip → world
    vp = np.asarray(proj, dtype=np.float64) @ np.asarray(view, dtype=np.float64)
    vp_inv = np.linalg.inv(vp)

    def _unproject(z: float) -> np.ndarray:
        clip = np.array([ndc_x, ndc_y, z, 1.0], dtype=np.float64)
        world = vp_inv @ clip
        # 齐次坐标除以 w
        w = world[3]
        if abs(w) < 1e-12:
            w = 1e-12
        return (world[:3] / w).astype(np.float32)

    near_pt = _unproject(-1.0)
    far_pt = _unproject(1.0)
    direction = far_pt - near_pt
    norm = np.linalg.norm(direction)
    if norm < 1e-12:
        direction = np.array([0.0, 0.0, -1.0], dtype=np.float32)
    else:
        direction = (direction / norm).astype(np.float32)
    return near_pt.astype(np.float32), direction


def ray_axis_intersect(origin: np.ndarray, direction: np.ndarray,
                       axis_point: np.ndarray, axis_dir: np.ndarray,
                       ) -> float:
    """鼠标射线与「坐标轴射线」的最近点 → 沿轴的参数 t。

    用于单轴拖动：把鼠标位置投影到选中轴射线上，取鼠标射线与轴射线的
    公共垂线中点，再投影到轴上得到 t。物体新位置 = axis_point + t*axis_dir。

    推导：令 w = r + t*D1 - s*D2（r=P1-P2）为两射线上点连线，最近点处
    w 同时垂直于 D1 与 D2，解 2x2 线性方程组得：

        r = P1 - P2
        a = D1·D1, b = D1·D2, c = D2·D2
        d = D1·r,  e = D2·r
        denom = a*c - b*b
        s = (a*e - b*d) / denom

    s 即沿 axis_dir 的世界单位偏移。返回 0 当射线近于平行（无法投影）。

    Args:
        origin, direction: 鼠标射线（direction 无需归一化，但建议归一）。
        axis_point: 轴射线的起点（通常是物体当前位置）。
        axis_dir: 轴方向（建议归一化）。

    Returns:
        沿 axis_dir 的参数 t（世界单位）。若鼠标几乎平行于轴返回 0.0。
    """
    p1 = np.asarray(origin, dtype=np.float64)
    d1 = np.asarray(direction, dtype=np.float64)
    p2 = np.asarray(axis_point, dtype=np.float64)
    d2 = np.asarray(axis_dir, dtype=np.float64)

    r = p1 - p2
    a = float(np.dot(d1, d1))
    b = float(np.dot(d1, d2))
    c = float(np.dot(d2, d2))
    d = float(np.dot(d1, r))
    e = float(np.dot(d2, r))
    denom = a * c - b * b
    if abs(denom) < 1e-9:
        # 鼠标射线与轴近于平行，无法稳定投影，保持原地
        return 0.0
    s = (a * e - b * d) / denom
    return float(s)


def ray_plane_intersect(origin: np.ndarray, direction: np.ndarray,
                        plane_point: np.ndarray, plane_normal: np.ndarray,
                        ) -> Optional[float]:
    """射线与平面求交 → 参数 t（origin + t*direction 为交点）。

    平面方程：dot(P - plane_point, plane_normal) = 0。

    Args:
        origin, direction: 射线（direction 建议归一化，则 t 为真实距离）。
        plane_point: 平面上任一点。
        plane_normal: 平面法线（无需归一，内部处理）。

    Returns:
        参数 t（方向沿 direction），无交点（射线平行于平面）返回 None。
    """
    o = np.asarray(origin, dtype=np.float64)
    d = np.asarray(direction, dtype=np.float64)
    pp = np.asarray(plane_point, dtype=np.float64)
    pn = np.asarray(plane_normal, dtype=np.float64)
    denom = float(np.dot(d, pn))
    if abs(denom) < 1e-9:
        return None
    t = float(np.dot(pp - o, pn)) / denom
    return t


def ray_mesh_intersect(origin: np.ndarray, direction: np.ndarray,
                       vertices: np.ndarray, indices: np.ndarray,
                       cull_backface: bool = True,
                       t_max: float = 1e9,
                       ) -> Optional[Tuple[float, np.ndarray]]:
    """射线与三角网格求交（Möller–Trumbore 算法）→ 最近交点。

    遍历网格全部三角形，返回最近的命中。用于「点选物体」拾取。

    Args:
        origin, direction: 射线（direction 建议归一化，则 t 为真实距离）。
        vertices: (N,3) 顶点位置数组，float32。
        indices:  (M,) 三角形索引（每 3 个一组），uint32。
        cull_backface: 是否剔除背向射线的面（提升速度且符合可见性）。
        t_max: 最大命中距离（剔除远处误判）。

    Returns:
        (t, hit_point)：t 为参数，hit_point (3,) 为世界交点。
        未命中返回 None。
    """
    o = np.asarray(origin, dtype=np.float64)
    d = np.asarray(direction, dtype=np.float64)
    verts = np.asarray(vertices, dtype=np.float64)
    idx = np.asarray(indices).reshape(-1)
    if idx.shape[0] < 3:
        return None

    best_t = t_max
    best_hit: Optional[np.ndarray] = None
    eps = 1e-9

    for i in range(0, idx.shape[0], 3):
        i0, i1, i2 = int(idx[i]), int(idx[i + 1]), int(idx[i + 2])
        v0 = verts[i0]
        edge1 = verts[i1] - v0
        edge2 = verts[i2] - v0
        h = np.cross(d, edge2)
        a = float(np.dot(edge1, h))
        if cull_backface:
            if a < eps:
                continue  # 背面或平行
        else:
            if abs(a) < eps:
                continue  # 平行
        f = 1.0 / a
        s = o - v0
        u = f * float(np.dot(s, h))
        if u < 0.0 or u > 1.0:
            continue
        q = np.cross(s, edge1)
        v = f * float(np.dot(d, q))
        if v < 0.0 or (u + v) > 1.0:
            continue
        t = f * float(np.dot(edge2, q))
        if eps < t < best_t:
            best_t = t
            best_hit = o + d * t

    if best_hit is None:
        return None
    return float(best_t), best_hit.astype(np.float32)
