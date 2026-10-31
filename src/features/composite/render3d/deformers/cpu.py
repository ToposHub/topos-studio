"""CPU Deformers — F13：Bend / Twist / Bulge / Taper / Stretch / Noise（纯 numpy）。

设计目标（master_plan §21.16）：
- 纯 numpy 向量化实现（不逐顶点 Python 循环）。
- 每个变形器接收 (顶点, 法线, 可选 field 权重)，返回变形后顶点+法线。
- 通过 FieldSpec 实现局部影响（master_plan §16.7 Falloff）。

算法（master_plan §21.16）：
    Bend:  沿 axis 计算高度 t，弯曲角 theta = t/height * angle * weight，
           在垂直平面旋转顶点。
    Twist: 沿 axis 高度 t，theta = t * twist_angle * weight，绕 axis 旋转横截面。
    Bulge: 到 center 距离 → field weight → 沿法线/radial 推出 p += dir * s * w。
    Taper: 沿 axis 线性缩放横截面（上粗下细等）。
    Stretch: 沿 axis 拉伸 + 垂直压缩（体积守恒近似）。
    Noise:  deterministic noise（sin 组合），沿法线位移。

坐标系约定（master_plan §16.8 space）：
- object：在 mesh 局部坐标系变形（默认）。
- world：调用方应先把顶点变换到世界空间再传入（本模块不处理矩阵）。
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from .base import (
    DeformerSpec,
    DEFORMER_BEND, DEFORMER_TWIST, DEFORMER_BULGE,
    DEFORMER_TAPER, DEFORMER_STRETCH, DEFORMER_NOISE,
)


# 轴名 → 索引
_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}

# AUD-8701: Stretch 的体积守恒因子 1/sqrt(stretch_f) 要求 stretch_f > 0；
# 此下界保证 squash_f ≤ 1/sqrt(eps)（eps=1e-6 → ≤1000），在 float32 顶点
# 坐标尺度下不可能溢出。
_STRETCH_FACTOR_EPS = 1e-6


def _axis_index(axis: str) -> int:
    return _AXIS_INDEX.get(axis.lower(), 1)


def _compute_weights(spec: DeformerSpec, vertices: np.ndarray,
                     selection_mask: Optional[np.ndarray]) -> np.ndarray:
    """计算每顶点综合权重 = strength × field × selection。

    Args:
        spec: 变形器规格。
        vertices: (N,3) 顶点。
        selection_mask: (N,) bool 选择集（None=全部）。

    Returns:
        (N,) float32 权重 [0, strength]。
    """
    n = vertices.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    # field 权重
    if spec.field is not None:
        w = spec.field.evaluate(vertices)
    else:
        w = np.ones(n, dtype=np.float32)
    # selection 掩码
    if selection_mask is not None:
        w = w * selection_mask.astype(np.float32)
    # 全局强度
    return (w * float(spec.strength)).astype(np.float32)


def _rotate_around_axis(points: np.ndarray, angles: np.ndarray,
                        axis: int) -> np.ndarray:
    """每顶点绕给定坐标轴旋转不同角度（向量化）。

    Args:
        points: (N,3) 顶点。
        angles: (N,) 每顶点旋转角（弧度）。
        axis: 0=x / 1=y / 2=z。

    Returns:
        (N,3) 旋转后顶点。
    """
    c = np.cos(angles)
    s = np.sin(angles)
    out = points.copy()
    if axis == 1:  # Y 轴：旋转 XZ 平面
        x, z = points[:, 0], points[:, 2]
        out[:, 0] = c * x + s * z
        out[:, 2] = -s * x + c * z
    elif axis == 0:  # X 轴：旋转 YZ 平面
        y, z = points[:, 1], points[:, 2]
        out[:, 1] = c * y - s * z
        out[:, 2] = s * y + c * z
    else:  # Z 轴：旋转 XY 平面
        x, y = points[:, 0], points[:, 1]
        out[:, 0] = c * x - s * y
        out[:, 1] = s * x + c * y
    return out


def _rotate_normals_around_axis(normals: np.ndarray, angles: np.ndarray,
                                axis: int) -> np.ndarray:
    """法线绕轴旋转（与 _rotate_around_axis 同变换，法线是方向向量）。"""
    return _rotate_around_axis(normals, angles, axis)


# ──────────────────────────────────────────────────────────────────────
# 各变形器实现
# ──────────────────────────────────────────────────────────────────────

def _deform_bend(spec: DeformerSpec, vertices: np.ndarray,
                 normals: np.ndarray, weights: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Bend 变形：沿 axis 弯曲。

    算法（master_plan §21.16）：
    1. 沿 axis 计算归一化高度 t（0..1）。
    2. 弯曲角 theta = t * angle * weight。
    3. 在垂直平面旋转顶点（围绕垂直于 axis 且过弯曲中心的轴）。

    params:
        angle: 总弯曲角（度，默认 45）。
    """
    axis = _axis_index(spec.axis)
    angle_deg = float(spec.params.get("angle", 45.0))
    angle_rad = np.deg2rad(angle_deg)
    center = spec.center
    # 沿 axis 的归一化高度（相对 center）
    h = vertices[:, axis] - center[axis]
    h_range = float(np.max(np.abs(h))) if h.shape[0] > 0 else 1.0
    if h_range < 1e-9:
        return vertices, normals
    t = h / h_range  # 归一化高度 [-1,1]
    theta = t * angle_rad * weights  # 每顶点弯曲角
    # 弯曲：在垂直于 axis 的平面内旋转。
    # 简化模型：把顶点在 axis 方向的坐标 + 垂直方向坐标做圆弧变换。
    # 取两个非 axis 方向作为「垂直平面」。
    other_axes = [i for i in range(3) if i != axis]
    a1, _ = other_axes
    # 顶点相对 center 在 (axis, a1) 平面的弯曲
    v_axis = vertices[:, axis] - center[axis]
    v_a1 = vertices[:, a1] - center[a1]
    # 圆弧弯曲：半径 R = h_range / angle，顶点沿圆弧移动
    # 弯曲后：axis 坐标 = R*sin(theta + base)，a1 = R*(1-cos(theta+base)) - R + original_a2
    # 简化为绕 a2 轴旋转（把 axis-a1 平面的点绕 a2 旋转 theta）
    pts = vertices.copy()
    # 构造绕 a2 轴的旋转（即 axis/a1 平面内旋转）
    c = np.cos(theta)
    s = np.sin(theta)
    new_axis = c * v_axis - s * v_a1 + center[axis]
    new_a1 = s * v_axis + c * v_a1 + center[a1]
    pts[:, axis] = new_axis
    pts[:, a1] = new_a1
    # 法线：同样旋转
    nrm = normals.copy()
    n_axis = normals[:, axis]
    n_a1 = normals[:, a1]
    nrm[:, axis] = c * n_axis - s * n_a1
    nrm[:, a1] = s * n_axis + c * n_a1
    return pts.astype(np.float32), nrm.astype(np.float32)


def _deform_twist(spec: DeformerSpec, vertices: np.ndarray,
                  normals: np.ndarray, weights: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Twist 变形：沿 axis 扭转。

    算法（master_plan §21.16）：
    1. 沿 axis 高度 t。
    2. theta = t * twist_angle * weight。
    3. 绕 axis 旋转横截面。

    params:
        angle: 总扭转角（度，默认 90）。
    """
    axis = _axis_index(spec.axis)
    angle_deg = float(spec.params.get("angle", 90.0))
    angle_rad = np.deg2rad(angle_deg)
    center = spec.center
    h = vertices[:, axis] - center[axis]
    h_range = float(np.max(np.abs(h))) if h.shape[0] > 0 else 1.0
    if h_range < 1e-9:
        return vertices, normals
    t = h / h_range
    theta = t * angle_rad * weights
    pts = _rotate_around_axis(vertices - center, theta, axis) + center
    nrm = _rotate_normals_around_axis(normals, theta, axis)
    return pts.astype(np.float32), nrm.astype(np.float32)


def _deform_bulge(spec: DeformerSpec, vertices: np.ndarray,
                  normals: np.ndarray, weights: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Bulge 变形：沿法线/radial 方向膨胀。

    算法（master_plan §21.16）：
    p += normal * strength * weight（沿法线推出）。
    或 p += radial_dir * strength * weight（从 center 向外推）。

    params:
        mode: "normal"（沿法线，默认）/ "radial"（从 center 向外）。
    """
    mode = str(spec.params.get("mode", "normal"))
    if mode == "radial":
        diff = vertices - spec.center
        dist = np.linalg.norm(diff, axis=1, keepdims=True)
        dist[dist < 1e-9] = 1.0
        radial = diff / dist
        offset = radial * weights[:, None]
    else:
        offset = normals * weights[:, None]
    pts = vertices + offset
    return pts.astype(np.float32), normals.astype(np.float32)


def _deform_taper(spec: DeformerSpec, vertices: np.ndarray,
                  normals: np.ndarray, weights: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Taper 变形：沿 axis 线性缩放横截面。

    params:
        amount: 锥化量（1=无变化，>1 上端放大，<1 上端缩小，默认 1.5）。
    """
    axis = _axis_index(spec.axis)
    amount = float(spec.params.get("amount", 1.5))
    center = spec.center
    h = vertices[:, axis] - center[axis]
    h_range = float(np.max(np.abs(h))) if h.shape[0] > 0 else 1.0
    if h_range < 1e-9:
        return vertices, normals
    t = h / h_range  # [-1,1]
    # 缩放因子：t=1（顶部）时 = amount，t=-1（底部）时 = 1
    scale = 1.0 + (amount - 1.0) * 0.5 * (t + 1.0) * weights
    pts = vertices.copy()
    other_axes = [i for i in range(3) if i != axis]
    for a in other_axes:
        rel = pts[:, a] - center[a]
        pts[:, a] = center[a] + rel * scale
    # AUD-8702: 非均匀（且逐顶点不同的）缩放改变曲面切平面，法线必须按
    # 缩放 Jacobian 的 inverse-transpose 变换后重新归一化——对角缩放的
    # inverse-transpose 即各轴 1/scale（axis 分量未缩放 → 1.0）。
    # |scale|→0 的退化行产生 inf/NaN，由 evaluate_deformer 的出口有限性
    # 防护回退原法线。
    with np.errstate(divide="ignore", invalid="ignore"):
        nrm = normals.astype(np.float64, copy=True)
        for a in other_axes:
            nrm[:, a] /= scale.astype(np.float64)
        lens = np.linalg.norm(nrm, axis=1, keepdims=True)
        lens[lens < 1e-12] = 1.0
        nrm /= lens
    return pts.astype(np.float32), nrm.astype(np.float32)


def _deform_stretch(spec: DeformerSpec, vertices: np.ndarray,
                    normals: np.ndarray, weights: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Stretch 变形：沿 axis 拉伸 + 垂直压缩（体积守恒近似）。

    params:
        amount: 拉伸量（1=无变化，>1 拉长，默认 1.3）。
    """
    axis = _axis_index(spec.axis)
    amount = float(spec.params.get("amount", 1.3))
    center = spec.center
    # AUD-8701: UI 允许 amount∈[-5,5]、strength∈[0,5]，二者组合可使
    # stretch_f ≤ 0（如 amount=0.5、strength=2.5 → -0.25）。负数开根得
    # NaN（仅 RuntimeWarning）、除 0 得 inf，都会写进顶点并上传 VBO。
    # 防护：权重先清洗非有限值，再对 stretch_f 施加正下界（epsilon）。
    clean_w = np.nan_to_num(
        weights.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    stretch_f = 1.0 + (amount - 1.0) * clean_w
    stretch_f = np.maximum(stretch_f, _STRETCH_FACTOR_EPS)
    squash_f = 1.0 / np.sqrt(stretch_f)  # 体积守恒：s_xy = 1/sqrt(s_axis)
    pts = vertices.copy()
    rel_axis = pts[:, axis] - center[axis]
    pts[:, axis] = center[axis] + rel_axis * stretch_f
    other_axes = [i for i in range(3) if i != axis]
    for a in other_axes:
        rel = pts[:, a] - center[a]
        pts[:, a] = center[a] + rel * squash_f
    # AUD-8702: 法线按缩放 Jacobian 的 inverse-transpose 变换：
    # axis 分量 × 1/stretch_f，其余轴 × 1/squash_f（= sqrt(stretch_f)），
    # 再归一化。stretch_f/squash_f 均已被下界钳为正，无除零。
    nrm = normals.astype(np.float64, copy=True)
    nrm[:, axis] /= stretch_f.astype(np.float64)
    for a in other_axes:
        nrm[:, a] *= np.sqrt(stretch_f.astype(np.float64))
    lens = np.linalg.norm(nrm, axis=1, keepdims=True)
    lens[lens < 1e-12] = 1.0
    nrm /= lens
    return pts.astype(np.float32), nrm.astype(np.float32)


def _deform_noise(spec: DeformerSpec, vertices: np.ndarray,
                  normals: np.ndarray, weights: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Noise 变形：沿法线的程序噪声位移。

    用 deterministic sin 组合噪声（输入 position），沿法线推出。
    动画用：params['time'] 控制噪声演变。

    params:
        frequency: 噪声频率（默认 2.0）。
        amplitude: 基础振幅（默认 0.1）。
        seed: 随机种子（默认 0）。
        time: 时间（动画用，默认 0）。
    """
    freq = float(spec.params.get("frequency", 2.0))
    amp = float(spec.params.get("amplitude", 0.1))
    seed = float(spec.params.get("seed", 0.0))
    time = float(spec.params.get("time", 0.0))
    # deterministic noise：sin 组合（无 numpy.random，保证可重复）
    p = vertices
    n1 = np.sin(p[:, 0] * freq + seed) * np.cos(p[:, 1] * freq + time)
    n2 = np.sin(p[:, 2] * freq + seed * 1.7) * np.cos(p[:, 0] * freq * 1.3 + time)
    n3 = np.sin(p[:, 1] * freq * 0.7 + seed * 2.1) * np.cos(p[:, 2] * freq + time * 1.3)
    noise = (n1 + n2 + n3) / 3.0  # [-1,1] 近似
    offset = normals * (noise * amp * weights)[:, None]
    return (vertices + offset).astype(np.float32), normals.astype(np.float32)


# ──────────────────────────────────────────────────────────────────────
# 分发
# ──────────────────────────────────────────────────────────────────────

_DEFORMERS = {
    DEFORMER_BEND: _deform_bend,
    DEFORMER_TWIST: _deform_twist,
    DEFORMER_BULGE: _deform_bulge,
    DEFORMER_TAPER: _deform_taper,
    DEFORMER_STRETCH: _deform_stretch,
    DEFORMER_NOISE: _deform_noise,
}


def evaluate_deformer(spec: DeformerSpec, vertices: np.ndarray,
                      normals: np.ndarray,
                      selection_mask: Optional[np.ndarray] = None
                      ) -> Tuple[np.ndarray, np.ndarray]:
    """执行单个变形器（master_plan §16.4 执行框架的核心分派）。

    Args:
        spec: 变形器规格（enabled=False 时原样返回）。
        vertices: (N,3) 输入顶点（会被复制，不修改原数组）。
        normals: (N,3) 输入法线。
        selection_mask: (N,) bool 选择集（None=全部顶点）。

    Returns:
        (变形后顶点, 变形后法线)，均为 float32 (N,3)。
    """
    if not spec.enabled or vertices.shape[0] == 0:
        return vertices.astype(np.float32), normals.astype(np.float32)
    verts_f = vertices.astype(np.float32)
    norms_f = normals.astype(np.float32)
    weights = _compute_weights(spec, verts_f, selection_mask)
    func = _DEFORMERS.get(spec.deformer_type)
    if func is None:
        # AUD-8703: 拼错的类型名与已导出但未实现的 DEFORMER_SHEAR /
        # DEFORMER_SPHERIFY 原先静默 no-op（变形器"不生效"且无诊断）。
        # 调用方（deformer3d_node）已有 except ValueError → 降级透传 +
        # diagnostics 路径，显式报错优于静默跳过。
        raise ValueError(
            f"未知或未实现的 deformer_type: {spec.deformer_type!r} "
            f"(已实现: {', '.join(sorted(_DEFORMERS))})")
    pts, nrm = func(spec, verts_f, norms_f, weights)
    # AUD-8701 出口防护：变形结果在上传 VBO 前的最后边界做有限性过滤。
    # 极端参数或退化输入产生的 NaN/inf 顶点回退原坐标（原坐标本身非有限
    # 时归零兜底），确保任何非有限值都不会离开变形器到达 GPU。
    if not np.isfinite(pts).all():
        bad = ~np.isfinite(pts).all(axis=1)
        pts = pts.copy()
        pts[bad] = np.nan_to_num(
            verts_f[bad], nan=0.0, posinf=0.0, neginf=0.0)
    if not np.isfinite(nrm).all():
        bad = ~np.isfinite(nrm).all(axis=1)
        nrm = nrm.copy()
        nrm[bad] = np.nan_to_num(
            norms_f[bad], nan=0.0, posinf=0.0, neginf=0.0)
    return pts, nrm
