"""Procedural masks for native two-source transitions.

The masks are deliberately numpy-only so the real-time CPU preview and the
frame export path use exactly the same geometry.  ``wipe`` keeps its legacy
direction/angle behaviour; the other masks reveal a scaled polygon from the
centre of the frame.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from typing import Any, Mapping

import numpy as np

from src.features.edit.core.transition_catalog import (
    FULL_FRAME_TRANSITION_TYPES,
    RESAMPLE_TRANSITION_TYPES,
    SPATIAL_TRANSITION_TYPES,
    TRANSITION_EFFECT_TYPES,
)
from src.features.edit.core.transition_wipe import compute_wipe_mask

__all__ = ["compute_transition_mask", "is_spatial_transition"]

#: 无几何 mask 的溶解类：generic mask consumers 传入这些类型时必须得到
#: 常数 progress mask，而不是被误渲染成方向/形状 wipe（AUD-19207：
#: 此前 cross_dissolve / dip_to_color 不在保护集内，直接调用本函数会
#: 得到"从左擦入"的硬边 wipe，与 GPU 侧同类输入返回 None 不一致）。
_FULL_FRAME_MASK_TYPES = frozenset(FULL_FRAME_TRANSITION_TYPES | {
    "cross_dissolve",
    "dip_to_color",
})


def is_spatial_transition(transition_type: str) -> bool:
    """Return whether ``transition_type`` is rendered with a spatial mask."""
    return transition_type in SPATIAL_TRANSITION_TYPES


# ---------------------------------------------------------------------------
# AUD-19208: pure-function caches for the polygon mask path.
#
# ``compute_transition_mask`` 的多边形分支此前每帧重算：48 顶点的心形
# 逐边 Python 循环 × 多个全帧临时数组，4K 导出的转场区间是确定性热点。
# 几何仅依赖 (width, height, type, progress)——两级缓存：
#   1. 归一化坐标网格按 (width, height) 缓存（mgrid/缩放每帧重做）；
#   2. even/odd 判定的布尔结果按完整输入键 LRU 缓存（重复求值同一
#      progress 的帧——预览重绘/双路合成——直接命中）。
# 缓存均为纯函数复用（键精确匹配，位级一致）；对外返回的 float32 数组
# 每次 astype 新拷贝，调用方修改不会污染缓存。容量有界，防多尺寸/多
# 转场并存的工程无界增长。
# ---------------------------------------------------------------------------
_COORD_GRID_CACHE_MAX = 16
_MASK_CACHE_MAX = 64

_coord_grid_cache: "OrderedDict[tuple[int, int], tuple[np.ndarray, np.ndarray]]" = OrderedDict()
_polygon_mask_cache: "OrderedDict[tuple[str, int, int, float], np.ndarray]" = OrderedDict()
_polygon_mask_cache_lock = threading.Lock()


def _get_coord_grid(width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Cached normalised coordinate grid for (width, height)."""
    key = (width, height)
    with _polygon_mask_cache_lock:
        cached = _coord_grid_cache.get(key)
        if cached is not None:
            _coord_grid_cache.move_to_end(key)
            return cached
    yy, xx = np.mgrid[0:height, 0:width]
    x = (xx.astype(np.float32) + 0.5) / max(1.0, float(width)) * 2.0 - 1.0
    y = (yy.astype(np.float32) + 0.5) / max(1.0, float(height)) * 2.0 - 1.0
    # Correct circles/ellipses for non-square output frames.
    x *= float(width) / max(1.0, float(height))
    grid = (x, y)
    with _polygon_mask_cache_lock:
        _coord_grid_cache[key] = grid
        while len(_coord_grid_cache) > _COORD_GRID_CACHE_MAX:
            _coord_grid_cache.popitem(last=False)
    return grid


def _polygon_mask(x: np.ndarray, y: np.ndarray, vertices: np.ndarray) -> np.ndarray:
    """Vectorised even/odd polygon test for a normalised coordinate grid."""
    inside = np.zeros(x.shape, dtype=bool)
    x0 = vertices[:, 0]
    y0 = vertices[:, 1]
    x1 = np.roll(x0, -1)
    y1 = np.roll(y0, -1)
    for ax, ay, bx, by in zip(x0, y0, x1, y1):
        crosses = (ay > y) != (by > y)
        denominator = by - ay
        safe_denominator = denominator if abs(float(denominator)) > 1e-8 else 1e-8
        edge_x = (bx - ax) * (y - ay) / safe_denominator + ax
        inside ^= crosses & (x < edge_x)
    return inside


def _polygon_mask_cached(
    width: int, height: int, progress: float, transition_type: str,
) -> np.ndarray:
    """Even/odd polygon interior test, memoised on the full input key (AUD-19208)."""
    key = (transition_type, width, height, float(progress))
    with _polygon_mask_cache_lock:
        cached = _polygon_mask_cache.get(key)
        if cached is not None:
            _polygon_mask_cache.move_to_end(key)
            return cached
    x, y = _get_coord_grid(width, height)
    vertices = _shape_vertices(transition_type)
    if vertices.size == 0:
        return np.empty((0, 0), dtype=bool)
    scale = max(0.0, progress)
    inside = _polygon_mask(x, y, vertices * scale)
    with _polygon_mask_cache_lock:
        _polygon_mask_cache[key] = inside
        while len(_polygon_mask_cache) > _MASK_CACHE_MAX:
            _polygon_mask_cache.popitem(last=False)
    return inside


def _shape_vertices(transition_type: str) -> np.ndarray:
    """Return a polygon whose full-size version covers the viewport."""
    if transition_type == "iris_diamond":
        return np.array(((0.0, -1.55), (1.55, 0.0), (0.0, 1.55), (-1.55, 0.0)))
    if transition_type == "iris_square":
        return np.array(((-1.55, -1.55), (1.55, -1.55), (1.55, 1.55), (-1.55, 1.55)))
    if transition_type == "iris_hexagon":
        angles = np.linspace(0.0, 2.0 * np.pi, 6, endpoint=False)
        return np.column_stack((np.cos(angles) * 1.7, np.sin(angles) * 1.7))
    if transition_type == "iris_triangle":
        return np.array(((0.0, -1.8), (1.65, 1.55), (-1.65, 1.55)))
    if transition_type == "iris_arrow":
        return np.array(((-1.7, -0.55), (0.15, -0.55), (0.15, -1.45),
                         (1.7, 0.0), (0.15, 1.45), (0.15, 0.55), (-1.7, 0.55)))
    if transition_type == "iris_plus":
        return np.array(((-0.45, -1.6), (0.45, -1.6), (0.45, -0.45),
                         (1.6, -0.45), (1.6, 0.45), (0.45, 0.45),
                         (0.45, 1.6), (-0.45, 1.6), (-0.45, 0.45),
                         (-1.6, 0.45), (-1.6, -0.45), (-0.45, -0.45)))
    if transition_type == "iris_star":
        angles = np.linspace(-np.pi / 2.0, 3.0 * np.pi / 2.0, 10, endpoint=False)
        radii = np.tile((1.65, 0.72), 5)
        return np.column_stack((np.cos(angles) * radii, np.sin(angles) * radii))
    if transition_type == "iris_heart":
        angles = np.linspace(0.0, 2.0 * np.pi, 48, endpoint=False)
        # Parametric heart, shifted so the point is centred in the frame.
        hx = 16.0 * np.sin(angles) ** 3 / 16.0
        hy = (13.0 * np.cos(angles) - 5.0 * np.cos(2.0 * angles)
              - 2.0 * np.cos(3.0 * angles) - np.cos(4.0 * angles)) / 16.0
        return np.column_stack((hx * 1.25, -hy * 1.25))
    return np.empty((0, 2), dtype=np.float32)


def compute_transition_mask(
    width: int,
    height: int,
    progress: float,
    transition_type: str,
    params: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Return an incoming-clip mask in the range ``[0, 1]``."""
    progress = max(0.0, min(1.0, float(progress)))

    # C2: 用户自定义转场 → 解析 base 并合并参数（实例 params 优先）
    from src.features.edit.core.custom_transitions import (
        merge_user_transition_params,
    )
    resolved = merge_user_transition_params(transition_type, params)
    if resolved is not None:
        base_type, merged_params = resolved
        return compute_transition_mask(
            width, height, progress, base_type, merged_params)

    if transition_type in _FULL_FRAME_MASK_TYPES:
        # Full-frame dissolves are composed by transition_effects.py.  This
        # constant mask keeps generic mask consumers from accidentally
        # turning them into a directional or shape wipe.
        return np.full((height, width), progress, dtype=np.float32)

    if transition_type == "wipe":
        return compute_wipe_mask(width, height, progress, params)

    if transition_type == "lib1_clock_wipe":
        # LIB1/X01（L03）：时钟极角 mask，生产核在 transition_packs
        from src.features.edit.transition_packs.library_v1.cores import (
            clock_wipe_mask,
        )
        return clock_wipe_mask(width, height, progress, params)

    if transition_type in {
        "lib1_venetian_blinds", "lib1_checker_reveal", "lib1_luma_reveal",
        "lib1_noise_dissolve", "lib1_halftone_reveal",
    }:
        # LIB1/L13 mask 家族：生产核在 transition_packs（暂存区再导出）。
        from src.features.edit.transition_packs.library_v1.cores import (
            checker_reveal_mask,
            halftone_reveal_mask,
            luma_reveal_mask,
            noise_dissolve_mask,
            venetian_blinds_mask,
        )
        if transition_type == "lib1_venetian_blinds":
            return venetian_blinds_mask(width, height, progress, params)
        if transition_type == "lib1_checker_reveal":
            return checker_reveal_mask(width, height, progress, params)
        if transition_type == "lib1_luma_reveal":
            return luma_reveal_mask(width, height, progress, params)
        if transition_type == "lib1_noise_dissolve":
            return noise_dissolve_mask(width, height, progress, params)
        return halftone_reveal_mask(width, height, progress, params)

    if transition_type == "lib1_voronoi_reveal":
        # LIB1/X07（L14）：网格 hash 扰动种子最近前沿 mask（生产核在
        # transition_packs，暂存区再导出）。
        from src.features.edit.transition_packs.library_v1.cores import (
            voronoi_reveal_mask,
        )
        return voronoi_reveal_mask(width, height, progress, params)

    if transition_type in RESAMPLE_TRANSITION_TYPES:
        # LIB1 双源重采样/复合家族（X09 + L14/L15 十家族）：真实合成在
        # CPU/GPU 的 resample/复合分支；通用 mask 消费方拿到常数
        # progress（与 full-frame 家族同语义，禁被误渲染成 wipe）。
        return np.full((height, width), progress, dtype=np.float32)

    if transition_type in {"slide_left", "slide_right", "slide_up", "slide_down"}:
        direction = transition_type.removeprefix("slide_")
        return compute_wipe_mask(
            width, height, progress,
            {**(params or {}), "direction": direction},
        )

    if transition_type in TRANSITION_EFFECT_TYPES:
        if transition_type in {
            "zoom_blur_in", "zoom_blur_out", "spin_blur_cw",
            "spin_blur_ccw",
        }:
            return compute_transition_mask(
                width, height, progress, "iris_circle", params,
            )
        direction = transition_type.rsplit("_", 1)[-1]
        return compute_wipe_mask(
            width, height, progress,
            {**(params or {}), "direction": direction},
        )

    # AUD-19208：坐标网格与多边形 interior 判定走纯函数缓存（见
    # _get_coord_grid/_polygon_mask_cached），避免转场区间内每帧重算。
    x, y = _get_coord_grid(width, height)

    if transition_type in {"iris_circle", "iris_ellipse"}:
        # AUD-16900 CPU/GPU 同步：与 gpu_transition_shaders.procedural_mask
        # 使用同一自适应公式。旧常数半径 1.55 在宽画幅下盖不住左右边缘
        # 中点（|x|=aspect>1.55）与四角，终点处 progress>=1.0 端点早退造成
        # 全帧跳变。圆半径上界取对角距 sqrt(aspect^2+1)；椭圆保持 0.62
        # 纵横比并对对角距做尺度补偿（GPU 侧 point.x 同样已乘 aspect）。
        aspect = float(width) / max(1.0, float(height))
        if transition_type == "iris_ellipse":
            ellipse_scale = progress * math.sqrt(
                aspect * aspect + 1.0 / (0.62 * 0.62)
            )
            inside = (
                (x / max(ellipse_scale, 1e-6)) ** 2
                + (y / max(ellipse_scale * 0.62, 1e-6)) ** 2
            ) <= 1.0
        else:
            radius = progress * math.sqrt(aspect * aspect + 1.0)
            inside = (x * x + y * y) <= radius * radius
    else:
        vertices = _shape_vertices(transition_type)
        if vertices.size == 0:
            # AUD-19207：未知/无顶点类型不得回退成方向 wipe——与 GPU 侧
            # build_gpu_transition_mask_spec 对同类输入返回 None（无 mask）
            # 保持一致；返回常数 progress mask（等价普通溶解）。
            return np.full((height, width), progress, dtype=np.float32)
        inside = _polygon_mask_cached(width, height, progress, transition_type)

    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        # Every supported full-size polygon is intentionally oversized, but
        # clamping endpoints avoids corner leaks from floating point rounding.
        return np.ones((height, width), dtype=np.float32)
    return inside.astype(np.float32)
