"""Fields — F13：Field / Falloff 权重控制（纯 numpy）。

设计目标（master_plan §16.5 / §21.16）：
- Field 控制变形器/效果器的作用范围与强度分布。
- 纯 numpy 计算（无 GL/PyQt），输入顶点位置数组，输出每顶点权重 [0,1]。
- 支持 linear / radial / box 三种基础 field（master_plan §16.6 最小可用版本）。

每种 Field 的 evaluate(vertices) → (N,) float32 权重：
- LinearField：按顶点在轴向的位置线性映射 0→1（from→to）。
- RadialField：按顶点到 center 的半径映射 1→0（中心强，边缘弱）。
- BoxField：盒内 =1，盒外按 smooth falloff 衰减。

falloff 曲线：none（硬切）/ linear / smooth（smoothstep）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# falloff 曲线类型
FALLOFF_NONE = "none"        # 硬切（盒内1/盒外0，圆内1/圆外0）
FALLOFF_LINEAR = "linear"    # 线性衰减
FALLOFF_SMOOTH = "smooth"    # smoothstep 衰减（C4D 默认）
FALLOFF_RADIAL = "radial"    # 径向（=smooth 的别名，兼容旧调用）

_VALID_FALLOFFS = {FALLOFF_NONE, FALLOFF_LINEAR, FALLOFF_SMOOTH, FALLOFF_RADIAL}


def _apply_falloff(t: np.ndarray, falloff: str, invert: bool = False) -> np.ndarray:
    """把 [0,1] 范围的距离参数 t 经 falloff 曲线映射为权重。

    Args:
        t: (N,) 已归一化到 [0,1] 的位置/距离参数（0=近/内，1=远/外）。
        falloff: FALLOFF_* 常量。
        invert: True 时反转权重（1-w）。

    Returns:
        (N,) float32 权重 [0,1]。t 已 clamp 到 [0,1]。
    """
    t = np.clip(t, 0.0, 1.0)
    if falloff == FALLOFF_NONE:
        w = np.where(t < 1.0, 1.0, 0.0)
    elif falloff == FALLOFF_LINEAR:
        w = 1.0 - t
    elif falloff in (FALLOFF_SMOOTH, FALLOFF_RADIAL):
        # smoothstep: 3t² - 2t³，但这里 t 是「外侧衰减距离」→ 权重 = 1 - smoothstep(t)
        s = t * t * (3.0 - 2.0 * t)
        w = 1.0 - s
    else:
        w = 1.0 - t  # 默认线性
    if invert:
        w = 1.0 - w
    return np.clip(w, 0.0, 1.0).astype(np.float32)


@dataclass
class FieldSpec:
    """Field 参数描述（线性/径向/盒体通用）。

    Attributes:
        field_type: "linear" / "radial" / "box"。
        center: (3,) 变形/场中心（世界或对象空间，由 deformer.space 决定）。
        axis: (3,) 归一化轴向（linear 用；radial/box 忽略）。
        radius: 径向场半径（radial 用）。
        size: (3,) 盒体半尺寸（box 用）。
        falloff: FALLOFF_* 曲线。
        invert: 反转权重。
    """
    field_type: str = "radial"
    center: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float32))
    axis: np.ndarray = field(default_factory=lambda: np.array([0, 1, 0], dtype=np.float32))
    radius: float = 1.0
    size: np.ndarray = field(default_factory=lambda: np.ones(3, dtype=np.float32))
    falloff: str = FALLOFF_SMOOTH
    invert: bool = False

    def __post_init__(self) -> None:
        self.center = np.asarray(self.center, dtype=np.float32).reshape(3)
        self.axis = np.asarray(self.axis, dtype=np.float32).reshape(3)
        n = float(np.linalg.norm(self.axis))
        if n > 1e-9:
            self.axis = (self.axis / n).astype(np.float32)
        self.size = np.asarray(self.size, dtype=np.float32).reshape(3)

    def evaluate(self, vertices: np.ndarray) -> np.ndarray:
        """计算每顶点权重 [0,1]。

        Args:
            vertices: (N,3) 顶点位置。

        Returns:
            (N,) float32 权重。
        """
        verts = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
        n = verts.shape[0]
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        if self.field_type == "linear":
            return self._eval_linear(verts)
        if self.field_type == "radial":
            return self._eval_radial(verts)
        if self.field_type == "box":
            return self._eval_box(verts)
        # 未知类型 → 全 1（不限制）
        return np.ones(n, dtype=np.float32)

    def _eval_linear(self, verts: np.ndarray) -> np.ndarray:
        """线性场：沿 axis 方向，center 处=1，center+axis*range 处=0。"""
        rel = verts - self.center  # (N,3)
        # 投影到 axis
        proj = rel @ self.axis  # (N,) 沿轴距离（可负）
        range_ = float(self.radius) if self.radius > 1e-9 else 1.0
        t = proj / range_  # 归一化
        # |t| 作为衰减距离（双向场）
        return _apply_falloff(np.abs(t), self.falloff, self.invert)

    def _eval_radial(self, verts: np.ndarray) -> np.ndarray:
        """径向场：到 center 距离 0→1，半径 radius 外→0。"""
        diff = verts - self.center
        dist = np.linalg.norm(diff, axis=1)  # (N,)
        radius = float(self.radius) if self.radius > 1e-9 else 1.0
        t = dist / radius
        return _apply_falloff(t, self.falloff, self.invert)

    def _eval_box(self, verts: np.ndarray) -> np.ndarray:
        """盒体场：盒内=1，盒外按到盒面的距离衰减。"""
        rel = np.abs(verts - self.center)  # (N,3)
        half = self.size  # (3,)
        # 每轴超出盒的量（盒内=0）
        over = np.maximum(rel - half, 0.0)  # (N,3)
        max_over = np.max(over, axis=1)  # (N,) 最远超出的轴
        # 衰减距离 = max_over / (avg half size)
        avg_half = float(np.mean(half[half > 1e-9])) if np.any(half > 1e-9) else 1.0
        t = max_over / max(avg_half, 1e-9)
        return _apply_falloff(t, self.falloff, self.invert)


# ──────────────────────────────────────────────────────────────────────
# 便捷工厂
# ──────────────────────────────────────────────────────────────────────

def linear_field(center=(0, 0, 0), axis=(0, 1, 0), range_: float = 1.0,
                 falloff: str = FALLOFF_SMOOTH, invert: bool = False) -> FieldSpec:
    """构造线性 field。"""
    return FieldSpec(field_type="linear", center=np.array(center, dtype=np.float32),
                     axis=np.array(axis, dtype=np.float32), radius=range_,
                     falloff=falloff, invert=invert)


def radial_field(center=(0, 0, 0), radius: float = 1.0,
                 falloff: str = FALLOFF_SMOOTH, invert: bool = False) -> FieldSpec:
    """构造径向 field。"""
    return FieldSpec(field_type="radial", center=np.array(center, dtype=np.float32),
                     radius=radius, falloff=falloff, invert=invert)


def box_field(center=(0, 0, 0), size=(1, 1, 1),
              falloff: str = FALLOFF_SMOOTH, invert: bool = False) -> FieldSpec:
    """构造盒体 field。size 为半尺寸。"""
    return FieldSpec(field_type="box", center=np.array(center, dtype=np.float32),
                     size=np.array(size, dtype=np.float32),
                     falloff=falloff, invert=invert)
