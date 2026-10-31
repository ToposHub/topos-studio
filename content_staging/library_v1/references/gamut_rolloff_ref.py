"""F05 gamut_rolloff —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/gamut_rolloff/gamut_rolloff.tglsl 逐式同构：
    l = dot(color, (0.2126, 0.7152, 0.0722))      # Rec.709 luma（保持）
    c = rgb - l；m = |c|                          # 欧氏色度幅值（近似声明）
    m ≤ thr → 恒等
    m > thr：ex = m - thr
      roll = smoothstep(clamp(ex/knee, 0, 1))，knee=0 → roll=1（硬膝）
      m' = thr + ex·(1 - strength·roll)
    out = l + c·(m'/m)                            # 不 clamp，负值/HDR 透传
GLSL 首行早退出 strength < 0.001 在此镜像（逐式同构，含早退阈值）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

DEFAULTS: dict[str, Any] = {
    "threshold": 1.0,
    "knee": 0.2,
    "strength": 0.8,
}

# 与 GLSL 早退阈值逐字一致（邻域/点处理对拍口径的前提）
_EARLY_EXIT = 0.001


def _rolloff(excess: np.ndarray, knee: float) -> np.ndarray:
    if knee <= 1e-6:
        return np.ones_like(excess)
    t = np.clip(excess / knee, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) RGB 数组应用色度软滚降，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)

    strength = float(p["strength"])
    if strength < _EARLY_EXIT:
        return np.asarray(image, dtype=np.float32)

    thr = max(float(p["threshold"]), 1e-4)
    knee = min(max(float(p["knee"]), 0.0), 1.0)

    luma = img64 @ LUMA_W
    chroma = img64 - luma[..., None]
    m = np.sqrt(np.sum(chroma * chroma, axis=-1))

    active = (m > thr) & (m >= 1e-6)
    out = img64.copy()
    if active.any():
        ex = (m - thr)[active]
        roll = _rolloff(ex, knee)
        m2 = thr + ex * (1.0 - strength * roll)
        out[active] = luma[active][..., None] + chroma[active] * (m2 / m[active])[..., None]
    return out.astype(np.float32)
