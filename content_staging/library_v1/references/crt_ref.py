"""C08 crt —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流。算法参照 plugins/CRT_Scanlines.tglsl 逐式同构（两个亮度
中性乘性增益场；零采样、纯代数三角波 1 − |2x − 1|）：

- 扫描线场（Scanlines / Full 模式）：
    period = max(H / max(scan_count, 1), 1)
    tri = 1 − |2·fract(px_y / period) − 1|;  tri = tri² （平方收窄亮脊）
    scan_gain = 1 − scan_strength·0.6·(1 − tri)
  px_y = y + 0.5（像素中心坐标，与 .tglsl 的 px = uv·tex_size 同值）。
- 荫罩条纹场（RGB Grille / Full 模式）：3 px 周期，三通道相位互错 1/3：
    k = mask_strength·0.8;  s = px_x / 3
    gain_c = 1 + (tri(fract(s + φ_c)) − 0.5)·k，φ = 0 / 0.33333333 / 0.66666667
- 输出 out = rgb · scan_gain · gain（逐通道乘性；HDR 负值/超白按同一
  增益缩放，不 clamp 内容色；alpha 原样透传）。

恒等语义：scan_strength 与 mask_strength 同时为 0 → 精确恒等
（含 HDR；.tglsl 首行早退同值）。荫罩增益连续域均值中性（tri 均值
0.5）；像素中心采样的离散相位集 s ∈ {1/6, 1/2, 5/6} 给出离散均值
1 + k/18（测试钉住）。扫描线 tri² 均值 1/3 < 1/2，整体增益 < 1
（.tglsl 实际行为，oracle 镜像不修正）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

MODES = ("Scanlines", "RGB Grille", "Full")

DEFAULTS: dict[str, Any] = {
    "mode": "Full",
    "scan_count": 480.0,     # 120–1200 条
    "scan_strength": 0.35,   # 0–1
    "mask_strength": 0.5,    # 0–1
}


def _fract(x: np.ndarray) -> np.ndarray:
    return x - np.floor(x)


def _triangle(x: np.ndarray) -> np.ndarray:
    """[0,1] 周期三角波：0.5 处峰值 1.0，整数相位处 0.0。"""
    return 1.0 - np.abs(x * 2.0 - 1.0)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    mode = str(p["mode"])
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {list(MODES)}，收到 {mode!r}")
    scan_count = max(float(p["scan_count"]), 1.0)
    scan_strength = float(p["scan_strength"])
    mask_strength = float(p["mask_strength"])

    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]
    rgb = img[..., :3]

    if scan_strength < 0.001 and mask_strength < 0.001:
        return img.astype(np.float32)

    px_y = (np.arange(h, dtype=np.float64) + 0.5)[:, None] * np.ones((1, w))
    px_x = (np.arange(w, dtype=np.float64) + 0.5)[None, :] * np.ones((h, 1))

    scan_gain = np.ones((h, w), dtype=np.float64)
    if mode in ("Scanlines", "Full"):
        period = max(float(h) / scan_count, 1.0)
        tri = _triangle(_fract(px_y / period))
        tri = tri * tri
        scan_gain = 1.0 - scan_strength * 0.6 * (1.0 - tri)

    gain = np.ones((h, w, 3), dtype=np.float64)
    if mode in ("RGB Grille", "Full"):
        k = mask_strength * 0.8
        s = px_x / 3.0
        phases = (0.0, 0.33333333, 0.66666667)
        for c, phi in enumerate(phases):
            gain[..., c] = 1.0 + (_triangle(_fract(s + phi)) - 0.5) * k

    out_rgb = rgb * scan_gain[..., None] * gain
    if img.shape[-1] == 4:
        return np.concatenate([out_rgb, img[..., 3:4]], axis=-1).astype(np.float32)
    return out_rgb.astype(np.float32)
