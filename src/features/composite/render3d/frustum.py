"""
视锥剔除（Frustum Culling）—— 纯 numpy，无 GL/PyQt 依赖，可独立单测。

用途：Scene3D 渲染循环里，在 CPU 端对每个 mesh 的世界 AABB 做视锥可见性测试，
不可见的跳过 draw（节省 GPU draw call）。

算法：
- extract_frustum_planes：Gribb-Hartmann 矩阵行提取法，从 proj@view 提 6 平面。
- aabb_in_frustum：AABB 8 角点 vs 6 平面测试（保守策略：部分相交算可见）。

参考：Gribb & Hartmann 2001 "Fast Extraction of Viewing Frustum Planes from the
World-View-Projection Matrix"。
"""

from typing import Tuple

import numpy as np


# 平面索引顺序（与 Gribb-Hartmann 一致）
PLANE_LEFT = 0
PLANE_RIGHT = 1
PLANE_BOTTOM = 2
PLANE_TOP = 3
PLANE_NEAR = 4
PLANE_FAR = 5


def extract_frustum_planes(proj_view: np.ndarray) -> np.ndarray:
    """从 proj@view 矩阵提取 6 个视锥平面。

    每平面 (a, b, c, d) 满足：a*x + b*y + c*z + d = 0，法向 (a,b,c) 指向视锥内部，
    内部点满足 a*x+b*y+c*z+d >= 0。

    Args:
        proj_view: 4x4 列主序或行主序矩阵（proj @ view 的结果）。
                   本项目 math_utils 用行主序（v_world = M @ v），故这里按行主序处理。

    Returns:
        (6, 4) float32 平面数组，行序：left, right, bottom, top, near, far。
    """
    m = np.asarray(proj_view, dtype=np.float32).reshape(4, 4)
    # 本项目 math_utils 用行主序（clip = M @ v，v 列向量）。
    # 此时 clip_i = sum_j m[i][j] * v[j]，即每行贡献一个 clip 分量。
    # Gribb-Hartmann 平面 = 行的线性组合（行主序 M@v 下用 row[i]）。
    row = [m[i, :] for i in range(4)]  # 4 个行向量
    planes = np.array([
        row[3] + row[0],  # left
        row[3] - row[0],  # right
        row[3] + row[1],  # bottom
        row[3] - row[1],  # top
        row[3] + row[2],  # near
        row[3] - row[2],  # far
    ], dtype=np.float32)
    # 归一化（让法向单位长度，便于 AABB 距离测试）
    normals = planes[:, :3]
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths = np.where(lengths < 1e-12, 1.0, lengths)
    planes = planes / lengths
    return planes.astype(np.float32)


def aabb_in_frustum(planes: np.ndarray, bmin: np.ndarray, bmax: np.ndarray) -> bool:
    """AABB 是否（部分）在视锥内。

    保守策略：AABB 只要有任一部分在视锥内就返回 True（部分相交算可见），
    避免边界数值抖动导致物体闪烁。

    实现：对每个平面，取 AABB 的「正角点」（p-vertex，法向同号为 max，异号为 min），
    若正角点也在平面外侧（<0），则整个 AABB 在该平面外 → 剔除。

    Args:
        planes: (6,4) 平面数组。
        bmin, bmax: AABB 最小/最大角 (3,)。

    Returns:
        True=可见（至少部分在视锥内），False=完全在视锥外（可剔除）。
    """
    bmin = np.asarray(bmin, dtype=np.float32).reshape(3)
    bmax = np.asarray(bmax, dtype=np.float32).reshape(3)
    for p in planes:
        a, b, c, d = p[0], p[1], p[2], p[3]
        # p-vertex：对法向 (a,b,c)，每个分量取 max（若>0）或 min
        px = bmax[0] if a > 0 else bmin[0]
        py = bmax[1] if b > 0 else bmin[1]
        pz = bmax[2] if c > 0 else bmin[2]
        # 若 p-vertex 也在平面外侧（距离<0），整个 AABB 在外
        if a * px + b * py + c * pz + d < 0.0:
            return False
    return True


def aabb_screen_size(bmin: np.ndarray, bmax: np.ndarray,
                     cam_pos: np.ndarray, viewport_height_px: int,
                     cam_fov_deg: float = 50.0) -> float:
    """估算 AABB 在屏幕上的投影高度（像素）—— P8 small object culling 用。

    近似算法：AABB 中心到相机距离 + AABB 对角线尺寸 → 角度 → 像素。
    用于远距离极小物体剔除（屏幕占比 < 阈值则跳过 draw call）。

    Args:
        bmin, bmax: AABB 世界空间角点。
        cam_pos: 相机世界位置。
        viewport_height_px: 视口高度像素。
        cam_fov_deg: 垂直 FOV（度）。

    Returns:
        估算的屏幕高度像素（float），恒 >= 0。本函数只接收相机位置、不接收
        朝向，无法判定"AABB 在相机后"（AUD-88020：旧 docstring 承诺的相机
        后负值语义不存在任何实现路径，已删除）；相机后剔除由上游的
        aabb_in_frustum 视锥测试负责。
    """
    bmin = np.asarray(bmin, dtype=np.float64).reshape(3)
    bmax = np.asarray(bmax, dtype=np.float64).reshape(3)
    cam_pos = np.asarray(cam_pos, dtype=np.float64).reshape(3)
    center = (bmin + bmax) * 0.5
    extent = bmax - bmin
    # AABB 对角线长度（世界单位）
    diag = float(np.linalg.norm(extent))
    if diag < 1e-7:
        return 0.0
    dist_vec = center - cam_pos
    dist = float(np.linalg.norm(dist_vec))
    if dist < 1e-7:
        return float(viewport_height_px)  # 相机在物体内，视为满屏
    # 角度尺寸 = diag / dist（弧度近似，小角度）
    angular_size = diag / dist
    import math as _math
    fov_rad = _math.radians(cam_fov_deg)
    # 屏幕像素 = angular_size / fov * viewport_height
    if fov_rad < 1e-7:
        return 0.0
    return angular_size / fov_rad * float(viewport_height_px)


def transform_aabb(bmin: np.ndarray, bmax: np.ndarray,
                   matrix: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """把 local-space AABB 经 matrix 变换到 world-space，返回新的轴对齐 AABB。

    对 AABB 的 8 角点全部变换后取 min/max（精确，不放大）。
    用于视锥剔除：mesh local AABB 经 effective_model 变换后测试。

    Args:
        bmin, bmax: local AABB (3,)。
        matrix: 4x4 行主序变换矩阵（M @ v 约定）。

    Returns:
        (world_bmin, world_bmax) (3,) float32。
    """
    bmin = np.asarray(bmin, dtype=np.float32).reshape(3)
    bmax = np.asarray(bmax, dtype=np.float32).reshape(3)
    m = np.asarray(matrix, dtype=np.float32).reshape(4, 4)
    # 8 角点
    corners = np.array([
        [bmin[0], bmin[1], bmin[2]],
        [bmin[0], bmin[1], bmax[2]],
        [bmin[0], bmax[1], bmin[2]],
        [bmin[0], bmax[1], bmax[2]],
        [bmax[0], bmin[1], bmin[2]],
        [bmax[0], bmin[1], bmax[2]],
        [bmax[0], bmax[1], bmin[2]],
        [bmax[0], bmax[1], bmax[2]],
    ], dtype=np.float32)
    homog = np.hstack([corners, np.ones((8, 1), dtype=np.float32)])
    world = (m @ homog.T).T[:, :3]
    return world.min(axis=0).astype(np.float32), world.max(axis=0).astype(np.float32)
