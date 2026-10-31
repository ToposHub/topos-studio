"""LIB1 转场 CPU 核（L 计划 L03，X01 clock_wipe + X09 true_push）。

事实源说明：本模块是**发布实现**；暂存区组件目录
（content_staging/library_v1/transitions/）的 *_core.py 再导出本模块，
保证组件测试与生产行为同一实现（禁双份漂移）。
几何/语义推导与手算向量见暂存区 README/hand_vectors（X01/X09 试点）。

X01 clock_wipe：极角推进 mask（现有 33 转场零极角实现——L00 台账）。
X09 true_push：双源同步平移（slide 是 wipe mask 双源不动——已抽查证实）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

# ── X01 clock_wipe ────────────────────────────────────────────────────

CLOCK_WIPE_DEFAULTS: dict[str, Any] = {
    "center_x": 0.5,
    "center_y": 0.5,
    "start_angle": 0.0,   # 度；0=12 点方向
    "clockwise": True,
    "softness": 0.02,     # 归一角宽（相对整圈 2π；注意与 wipe 的长边比例口径不同）
}


def clock_wipe_mask(width: int, height: int, progress: float,
                    params: Mapping[str, Any] | None = None) -> np.ndarray:
    """时钟极角揭示 mask：北 0°、顺时针为正、前沿羽化、端点精确。"""
    p = dict(CLOCK_WIPE_DEFAULTS)
    if params:
        p.update(params)
    # _safe_float 防御口径（AUD-19210 家族）：坏值回退默认
    def _safe(key: str) -> float:
        try:
            value = float(p[key])
        except (TypeError, ValueError):
            return float(CLOCK_WIPE_DEFAULTS[key])
        return value if value == value and abs(value) != float("inf") \
            else float(CLOCK_WIPE_DEFAULTS[key])

    progress = max(0.0, min(1.0, float(progress) if progress == progress else 0.0))
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    xs = (np.arange(width, dtype=np.float64) + 0.5) / width
    ys = (np.arange(height, dtype=np.float64) + 0.5) / height
    grid_x, grid_y = np.meshgrid(xs, ys)
    aspect = width / height
    # 屏幕坐标 y 向下：北（上方）= 更小行号 → dy = cy - y
    dx = (grid_x - max(0.0, min(1.0, _safe("center_x")))) * aspect
    dy = max(0.0, min(1.0, _safe("center_y"))) - grid_y

    theta = np.arctan2(dx, dy)  # 北 0，顺时针正 ∈ (-π, π]
    delta = theta - np.deg2rad(_safe("start_angle"))
    clockwise = bool(p.get("clockwise", True))
    if not clockwise:
        delta = -delta
    rel = np.mod(delta, 2.0 * np.pi)

    sweep = progress * 2.0 * np.pi
    softness = max(0.0, min(0.5, _safe("softness")))
    feather = max(softness * 2.0 * np.pi, 1e-6)
    return np.clip((sweep - rel) / feather, 0.0, 1.0).astype(np.float32)


# ── X09 true_push ─────────────────────────────────────────────────────

PUSH_DIRECTIONS = ("left", "right", "up", "down")

PUSH_DEFAULTS: dict[str, Any] = {
    "direction": "left",
    "seam_softness": 0.0,  # 归一接缝半宽（0=硬接缝；GL 侧复用 u_softness）
}


def push_frame(a_img: np.ndarray, b_img: np.ndarray, progress: float,
               direction: str = "left",
               seam_softness: float = 0.0) -> np.ndarray:
    """双源同步平移一帧（float64 计算；uint8 输入返回 uint8）。

    A 段采样坐标 = u+p ≤ 1、B 段 = u+p-1 > 0——**恒在界内**，无越界采样
    （L00 §5.6 的无 handles 冻结边帧问题因此不影响本核）；端点 p=0/1 为
    比特级还原。BGR/RGB 任意 3 通道（或单通道）均适用。
    seam_softness>0 时在接缝两侧 2·soft 带内线性混合（权重域）。
    """
    direction = str(direction) if direction in PUSH_DIRECTIONS else "left"
    a = np.asarray(a_img, dtype=np.float64)
    b = np.asarray(b_img, dtype=np.float64)
    progress = max(0.0, min(1.0, float(progress)))
    if progress <= 0.0:
        out = a
    elif progress >= 1.0:
        out = b
    else:
        axis = 1 if direction in {"left", "right"} else 0
        n = a.shape[axis]
        coords = (np.arange(n, dtype=np.float64) + 0.5) / n
        if direction in {"left", "up"}:
            src_a, use_a = coords + progress, coords + progress <= 1.0
            src_b = coords + progress - 1.0
            seam = 1.0 - progress
        else:
            src_a, use_a = coords - progress, coords - progress >= 0.0
            src_b = coords - progress + 1.0
            seam = progress

        def _sample1(img: np.ndarray, src: np.ndarray, axis: int) -> np.ndarray:
            xf = np.clip(src * n - 0.5, 0.0, n - 1.0)
            x0 = np.floor(xf).astype(np.int64)
            x1 = np.minimum(x0 + 1, n - 1)
            # fx 须对齐被采样的那根轴（axis=0 沿行、axis=1 沿列）：
            # 垂直方向 3 通道 fx 形状 (h,1) 会对 (h,w,3) 错位广播
            # （h 对上 w）——非方幅即崩（L18 P39/P40 实测暴露），分轴展开。
            if img.ndim == 3:
                fx = (xf - x0)[..., None] if axis == 1 \
                    else (xf - x0)[..., None, None]
            else:
                fx = (xf - x0)[:, None] if axis == 0 else (xf - x0)
            take0 = np.take(img, x0, axis=axis)
            take1 = np.take(img, x1, axis=axis)
            return take0 * (1.0 - fx) + take1 * fx

        out_a = _sample1(a, src_a, axis)
        out_b = _sample1(b, src_b, axis)
        shape3 = (1, -1, 1) if (axis == 1 and a.ndim == 3) else \
            ((-1, 1, 1) if a.ndim == 3 else ((1, -1) if axis == 1 else (-1, 1)))
        soft = max(0.0, float(seam_softness))
        if soft <= 0.0:
            out = np.where(use_a.reshape(shape3), out_a, out_b)
        else:
            b_sign = 1.0 if direction in {"left", "up"} else -1.0
            w_b = np.clip(
                0.5 + b_sign * (coords - seam) / (2.0 * soft), 0.0, 1.0)
            out = out_a * (1.0 - w_b.reshape(shape3)) + out_b * w_b.reshape(shape3)
    if np.asarray(a_img).dtype == np.uint8:
        return np.clip(np.round(out), 0, 255).astype(np.uint8)
    return out.astype(np.float32)


# ── L13 mask 家族（X02/X03/X05/X06/X08）───────────────────────────────
# 实现与暂存区组件（content_staging/library_v1/transitions/*）逐式同构；
# 暂存 *_core.py 再导出本模块（单一事实源）。语义推导/手算向量见暂存
# README/hand_vectors（X02/X03 有交接语义修正记录）。

from numpy import ndarray as _NdArray  # noqa: E402  (局部别名避免顶部扰动)

VENETIAN_DEFAULTS: dict[str, Any] = {
    "count": 6,
    "angle": 0.0,
    "phase": 0.0,
    "softness": 0.05,
}


def venetian_blinds_mask(width: int, height: int, progress: float,
                         params: Mapping[str, Any] | None = None) -> _NdArray:
    """X02 周期百叶：带内相位 t=mod(s·count+phase,1)，全带同时扫掠。"""
    return _venetian_checker_impl("venetian", width, height, progress, params)


CHECKER_DEFAULTS: dict[str, Any] = {
    "rows": 4,
    "columns": 4,
    "phase": 0.0,
    "softness": 0.05,
}


def checker_reveal_mask(width: int, height: int, progress: float,
                        params: Mapping[str, Any] | None = None) -> _NdArray:
    """X03 棋盘奇偶两波：trig=mod(((r+c)%2)·0.5+phase,1)。"""
    return _venetian_checker_impl("checker", width, height, progress, params)


def _family_safe_float(value: Any, fallback: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if v != v or v == float("inf") or v == float("-inf"):
        return float(fallback)
    return v


def _family_clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _venetian_checker_impl(family: str, width: int, height: int,
                           progress: float,
                           params: Mapping[str, Any] | None) -> _NdArray:
    defaults = VENETIAN_DEFAULTS if family == "venetian" else CHECKER_DEFAULTS
    p = dict(defaults)
    if params:
        p.update(params)
    progress = _family_clamp(
        _family_safe_float(progress, 0.0), 0.0, 1.0)
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    xs = (np.arange(width, dtype=np.float64) + 0.5) / width
    ys = (np.arange(height, dtype=np.float64) + 0.5) / height
    feather = max(_family_clamp(
        _family_safe_float(p["softness"], defaults["softness"]), 0.0, 0.5), 1e-6)
    phase = _family_safe_float(p["phase"], 0.0) % 1.0

    if family == "venetian":
        count = int(round(_family_safe_float(p["count"], defaults["count"])))
        count = int(_family_clamp(count, 2, 32))
        angle = _family_clamp(_family_safe_float(p["angle"], 0.0), -90.0, 90.0)
        grid_x, grid_y = np.meshgrid(xs, ys)
        aspect = width / height
        a = np.deg2rad(angle)
        s = grid_x * np.cos(a) + (grid_y / aspect) * np.sin(a)
        t = np.mod(s * count + phase, 1.0)
    else:
        rows = int(round(_family_safe_float(p["rows"], defaults["rows"])))
        rows = int(_family_clamp(rows, 1, 16))
        columns = int(round(_family_safe_float(p["columns"], defaults["columns"])))
        columns = int(_family_clamp(columns, 1, 16))
        c_idx = np.clip((xs * columns).astype(np.int64), 0, columns - 1)
        r_idx = np.clip((ys * rows).astype(np.int64), 0, rows - 1)
        grid_r, grid_c = np.meshgrid(r_idx, c_idx, indexing="ij")
        parity = np.mod(grid_r + grid_c, 2).astype(np.float64)
        t = np.mod(parity * 0.5 + phase, 1.0)

    return np.clip((progress - t) / feather, 0.0, 1.0).astype(np.float32)


LUMA_REVEAL_DEFAULTS: dict[str, Any] = {
    "source": "A",
    "threshold": 0.5,
    "span": 0.2,
    "invert": False,
}

_LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)  # Rec.709


def _luma_of(img: Any, height: int, width: int) -> _NdArray | None:
    if img is None:
        return None
    try:
        arr = np.asarray(img, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if arr.shape[0] != height or arr.shape[1] != width:
        return None
    if arr.ndim == 2:
        luma = arr
    elif arr.ndim == 3 and arr.shape[2] == 1:
        luma = arr[..., 0]
    elif arr.ndim == 3 and arr.shape[2] >= 3:
        luma = arr[..., :3] @ _LUMA_W
    else:
        return None
    return np.clip(np.nan_to_num(luma, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


def luma_reveal_mask(width: int, height: int, progress: float,
                     params: Mapping[str, Any] | None = None,
                     image_a: Any = None,
                     image_b: Any = None) -> _NdArray:
    """X05 内容驱动揭示（首个非几何 mask）：亮度排序阈值扫描。

    image 缺省/形状不符 → 线性 dissolve 回退（暂存 README 声明口径）。
    """
    p = dict(LUMA_REVEAL_DEFAULTS)
    if params:
        p.update(params)
    source = str(p.get("source", "A"))
    if source not in ("A", "B"):
        source = "A"
    threshold = _family_clamp(_family_safe_float(p["threshold"], 0.5), 0.0, 1.0)
    span = _family_clamp(_family_safe_float(p["span"], 0.2), 0.01, 0.5)
    invert = bool(p.get("invert", False))

    progress = _family_clamp(_family_safe_float(progress, 0.0), 0.0, 1.0)
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    luma = _luma_of(image_a if source == "A" else image_b, height, width)
    if luma is None:
        return np.full((height, width), progress, dtype=np.float32)

    luma_eff = np.clip(luma + (0.5 - threshold), 0.0, 1.0)
    if invert:
        luma_eff = 1.0 - luma_eff
    thr_eff = -span + progress * (1.0 + 2.0 * span)
    return np.clip((thr_eff + 0.5 * span - luma_eff) / span, 0.0, 1.0).astype(np.float32)


NOISE_DISSOLVE_DEFAULTS: dict[str, Any] = {
    "noise_mode": "Value",
    "scale": 16,
    "seed": 0,
    "softness": 0.05,
}

_NOISE_U64 = np.uint64
_NOISE_MASK32 = _NOISE_U64(0xFFFFFFFF)
_NOISE_C1, _NOISE_C2, _NOISE_C3 = (_NOISE_U64(0x9E3779B1),
                                   _NOISE_U64(0x85EBCA77),
                                   _NOISE_U64(0xC2B2AE3D))
_NOISE_C4, _NOISE_C5 = _NOISE_U64(0x2545F491), _NOISE_U64(0x9E3779B1)


def noise_lattice_hash_u32(ix: Any, iy: Any, seed: int) -> _NdArray:
    """整数格点 → [0,1)。uint32 murmur3 风格（与 GLSL 330 uint 逐位一致）。"""
    x = ix.astype(np.int64).astype(_NOISE_U64) & _NOISE_MASK32
    y = iy.astype(np.int64).astype(_NOISE_U64) & _NOISE_MASK32
    s = _NOISE_U64(int(seed) & 0xFFFFFFFF)
    h = (x * _NOISE_C1) ^ (y * _NOISE_C2) ^ (s * _NOISE_C3)
    h &= _NOISE_MASK32
    h ^= h >> _NOISE_U64(15)
    h = (h * _NOISE_C4) & _NOISE_MASK32
    h ^= h >> _NOISE_U64(13)
    h = (h * _NOISE_C5) & _NOISE_MASK32
    h ^= h >> _NOISE_U64(16)
    return h.astype(np.float64) / 4294967296.0


def noise_dissolve_mask(width: int, height: int, progress: float,
                        params: Mapping[str, Any] | None = None) -> _NdArray:
    """X06 rank-field 揭示：Value=双线性 value noise / Cell=最近格点。"""
    p = dict(NOISE_DISSOLVE_DEFAULTS)
    if params:
        p.update(params)
    mode = str(p.get("noise_mode", "Value"))
    if mode not in ("Value", "Cell"):
        mode = "Value"
    scale = int(round(_family_safe_float(p["scale"], 16)))
    scale = int(_family_clamp(scale, 2, 64))
    seed = int(round(_family_safe_float(p["seed"], 0)))
    seed = int(_family_clamp(seed, 0, 100))
    softness = _family_clamp(_family_safe_float(p["softness"], 0.05), 0.0, 0.5)

    progress = _family_clamp(_family_safe_float(progress, 0.0), 0.0, 1.0)
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    xs = (np.arange(width, dtype=np.float64) + 0.5) / width * scale
    ys = (np.arange(height, dtype=np.float64) + 0.5) / height * scale
    gp_x, gp_y = np.meshgrid(xs, ys)

    if mode == "Value":
        x0 = np.floor(gp_x)
        y0 = np.floor(gp_y)
        fx = gp_x - x0
        fy = gp_y - y0
        fade_x = fx * fx * (3.0 - 2.0 * fx)
        fade_y = fy * fy * (3.0 - 2.0 * fy)
        xi = x0.astype(np.int64)
        yi = y0.astype(np.int64)
        n00 = noise_lattice_hash_u32(xi, yi, seed)
        n10 = noise_lattice_hash_u32(xi + 1, yi, seed)
        n01 = noise_lattice_hash_u32(xi, yi + 1, seed)
        n11 = noise_lattice_hash_u32(xi + 1, yi + 1, seed)
        a = n00 + (n10 - n00) * fade_x
        b = n01 + (n11 - n01) * fade_x
        rank = a + (b - a) * fade_y
    else:
        xi = np.floor(gp_x + 0.5).astype(np.int64)
        yi = np.floor(gp_y + 0.5).astype(np.int64)
        rank = noise_lattice_hash_u32(xi, yi, seed)

    feather = max(softness, 1e-6)
    return np.clip((progress - rank) / feather, 0.0, 1.0).astype(np.float32)


HALFTONE_DEFAULTS: dict[str, Any] = {
    "spacing": 16,
    "angle": 45.0,
    "shape": "Dot",
    "softness": 0.1,
}

_HALFTONE_R_MAX = float(np.sqrt(2.0))


def halftone_reveal_mask(width: int, height: int, progress: float,
                         params: Mapping[str, Any] | None = None) -> _NdArray:
    """X08 网点扩张揭示：Dot=径向距 / Square=切比雪夫距。"""
    p = dict(HALFTONE_DEFAULTS)
    if params:
        p.update(params)
    spacing = int(round(_family_safe_float(p["spacing"], 16)))
    spacing = int(_family_clamp(spacing, 4, 64))
    angle = _family_clamp(_family_safe_float(p["angle"], 45.0), -90.0, 90.0)
    shape = str(p.get("shape", "Dot"))
    if shape not in ("Dot", "Square"):
        shape = "Dot"
    softness = _family_clamp(_family_safe_float(p["softness"], 0.1), 0.0, 0.5)

    progress = _family_clamp(_family_safe_float(progress, 0.0), 0.0, 1.0)
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    xs = np.arange(width, dtype=np.float64) + 0.5
    ys = np.arange(height, dtype=np.float64) + 0.5
    grid_x, grid_y = np.meshgrid(xs, ys)
    a = np.deg2rad(angle)
    s = grid_x * np.cos(a) - grid_y * np.sin(a)
    t = grid_x * np.sin(a) + grid_y * np.cos(a)

    sp = float(spacing)
    ds = s - (np.floor(s / sp) + 0.5) * sp
    dt = t - (np.floor(t / sp) + 0.5) * sp
    if shape == "Dot":
        d = np.sqrt(ds * ds + dt * dt) / sp
    else:
        d = np.maximum(np.abs(ds), np.abs(dt)) / sp

    r = progress * _HALFTONE_R_MAX
    feather = 2.0 * max(softness, 1e-6)
    return np.clip((r + softness - d) / feather, 0.0, 1.0).astype(np.float32)


# ── X07 voronoi_reveal（L14，mask 口径）──────────────────────────────

DEFAULTS: dict[str, Any] = {
    "cell_count": "16",   # "8" | "16" | "32"（目标数，实际网格 n²）
    "seed": 7,
    "edge_width": 0.1,    # 归一距离（相对画幅对角线 D）
}
CELL_COUNTS = ("8", "16", "32")

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)

_EPS_SPEED = 1e-6


def _hash01(ix: int, iy: int, seed: int) -> float:
    """整数格点 → [0,1)（uint32 murmur3 风格，同 F07/fixtures 家族）。"""
    x = _U64(int(ix) & 0xFFFFFFFF)
    y = _U64(int(iy) & 0xFFFFFFFF)
    s = _U64(int(seed) & 0xFFFFFFFF)
    h = (x * _C1) ^ (y * _C2) ^ (s * _C3)
    h &= _MASK32
    h ^= h >> _U64(15)
    h = (h * _C4) & _MASK32
    h ^= h >> _U64(13)
    h = (h * _C5) & _MASK32
    h ^= h >> _U64(16)
    return float(h) / 4294967296.0


def _safe_f(p: Mapping[str, Any], key: str, default: float,
            lo: float, hi: float) -> float:
    try:
        v = float(p[key])
    except (KeyError, TypeError, ValueError):
        return default
    if v != v or abs(v) == float("inf"):
        return default
    return min(hi, max(lo, v))


def _safe_i(p: Mapping[str, Any], key: str, default: int,
            lo: int, hi: int) -> int:
    try:
        v = int(round(float(p[key])))
    except (KeyError, TypeError, ValueError, OverflowError):
        return default
    return min(hi, max(lo, v))


def _seeds(n: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (sx, sy, t)：种子归一位置与出生时间（行序展开，m=n²个）。"""
    m = n * n
    sx = np.empty(m, dtype=np.float64)
    sy = np.empty(m, dtype=np.float64)
    t_raw = np.empty(m, dtype=np.float64)
    for jy in range(n):
        for jx in range(n):
            i = jy * n + jx
            sx[i] = (jx + _hash01(jx, jy, seed)) / n
            sy[i] = (jy + _hash01(jx, jy, seed + 1001)) / n
            t_raw[i] = _hash01(jx, jy, seed + 2002)
    rank_pos = np.empty(m, dtype=np.float64)
    order = np.argsort(t_raw, kind="stable")
    for pos, i in enumerate(order):
        rank_pos[i] = pos / m
    return sx, sy, rank_pos


def voronoi_reveal_mask(width: int, height: int, progress: float,
                  params: Mapping[str, Any] | None = None) -> np.ndarray:
    """晶胞距离推进揭示 mask：(H,W) float32，p=0 全 0 / p=1 全 1。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    cell = str(p.get("cell_count", "16"))
    if cell not in CELL_COUNTS:
        raise ValueError(f"cell_count 必须是 {CELL_COUNTS}，收到 {cell!r}")
    try:
        progress = float(progress)
    except (TypeError, ValueError):
        progress = 0.0
    if progress != progress:
        progress = 0.0
    progress = max(0.0, min(1.0, progress))
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    seed = _safe_i(p, "seed", 7, 0, 100)
    edge_width = _safe_f(p, "edge_width", 0.1, 0.0, 0.5)
    n = int(np.ceil(np.sqrt(float(cell))))
    m = n * n

    t_span = max(0.0, 1.0 - 2.0 * edge_width)
    speed = 1.0 / (edge_width + _EPS_SPEED)
    aspect = float(width) / float(height)
    diag = np.sqrt(aspect * aspect + 1.0)

    sx, sy, rank = _seeds(n, seed)
    t_birth = rank * t_span

    us = (np.arange(width, dtype=np.float64) + 0.5) / width
    vs = (np.arange(height, dtype=np.float64) + 0.5) / height
    uu, vv = np.meshgrid(us, vs)

    # 每像素最近种子（宽高比修正距离场）
    d2_min = np.full((height, width), np.inf, dtype=np.float64)
    best = np.zeros((height, width), dtype=np.int64)
    for i in range(m):
        dx = (uu - sx[i]) * aspect
        dy = vv - sy[i]
        d2 = dx * dx + dy * dy
        take = d2 < d2_min
        d2_min = np.where(take, d2, d2_min)
        best = np.where(take, np.int64(i), best)
    d_norm = np.sqrt(d2_min) / diag

    reveal = (progress - t_birth[best]) * speed - d_norm
    return np.clip(reveal, 0.0, 1.0).astype(np.float32)
