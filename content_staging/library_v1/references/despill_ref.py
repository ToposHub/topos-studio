"""C03 despill —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流、防御回退。语义从既有 Keyer despill 提取（L00 台账 C03 行：
keyer_node.py despill_strength/despill_method；shaders.py
``_KEYLIGHT_FRAGMENT::remove_spill``——subtract=mix(主导通道, 其他通道
最大值, strength)，scale=按幕色占比的通道增益）。

标准绿幕去溢色口径（本 oracle 钉住）：
    m    = max(r, b)              # 非绿通道上界
    Δ    = g − m                  # 绿剩余（正=溢色，负=绿不足）
    spill(Δ, lim) = clip(Δ, 0, lim)          # Subtract 模式削峰量
    ratio(Δ, lim) = clip(clip(Δ, −lim, lim) / max(g, 1e-6), −1, 1)
    Subtract: g' = g − strength·spill   （仅下拉，绝对量受 limit 封顶；
                                          任务式 g' = g − g_spill·strength）
    Scale:    g' = g·(1 − strength·ratio)（乘性双向拉向 max(r,b)——
                                          Keylight subtract mix 语义；
                                          g≈0 由 ratio∈[−1,1] 守护）

两模式在 0 ≤ Δ ≤ limit 区代数同值（乘性形式展开即减法形式）；差异区：
Δ < 0（绿不足，Subtract 不动 / Scale 抬升）与 Δ > limit / g≈0 的守护。
strength=0 精确恒等（含 HDR）；r/b 通道与 alpha 原样透传，无 clamp。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

METHODS = ("Subtract", "Scale")

DEFAULTS: dict[str, Any] = {
    "method": "Subtract",
    "limit": 1.0,       # 每像素处理的 |Δ| 上限（绝对量）
    "strength": 1.0,    # 0 恒等
}


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    method = str(p["method"])
    if method not in METHODS:
        raise ValueError(f"method 必须是 {list(METHODS)}，收到 {method!r}")
    limit = max(0.0, float(p["limit"]))
    strength = float(p["strength"])

    img = np.asarray(image, dtype=np.float64)
    rgb = img[..., :3]
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    m = np.maximum(r, b)
    delta = g - m

    if method == "Subtract":
        spill = np.clip(delta, 0.0, limit)
        g_out = g - strength * spill
    else:  # Scale
        capped = np.clip(delta, -limit, limit)
        ratio = np.clip(capped / np.maximum(g, 1e-6), -1.0, 1.0)
        g_out = g * (1.0 - strength * ratio)

    out_rgb = np.stack([r, g_out, b], axis=-1)
    if img.shape[-1] == 4:
        return np.concatenate([out_rgb, img[..., 3:4]], axis=-1).astype(np.float32)
    return out_rgb.astype(np.float32)
