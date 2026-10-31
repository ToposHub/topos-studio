"""C09 glitch —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流（seed 进整数化 hash，静态确定性）。算法参照
plugins/Glitch_Blocks.tglsl 逐式同构 + seed 扩展：

- 条带划分：band = floor((y + 0.5) / max(band_height, 1))；
- Dave Hoskins 无三角函数 hash12(x, y)（与 .tglsl glitch_hash 逐式同构）：
    p3 = fract((x, y, x)·0.1031)
    p3 += dot(p3, p3.yzx + 33.33)
    h  = fract((p3.x + p3.y)·p3.z)
- **seed 进 hash**（L16 差量：节点化静态确定性）：n1/n2 的条带坐标
  平移 seed·97（seed ∈ [0,100]，整数语义；GPU 侧同式进 uniform）；
    n1 = hash12(band + seed·97, 7);  n2 = hash12(band + seed·97, 23)
- 条带激活：hot = 1 当 n1 ≥ 1 − corruption（出错条带比例）；
- 水平错位（UV 域）：x_shift = (n2 − 0.5)·shift_amount·0.25·hot
  （±12.5% 画幅宽以内，方向由 n2 定）；
- RGB 分离：split_px = rgb_split·12·(0.25 + 0.75·hot)，出错条带全量、
  正常条带 25% 底噪式微量；r/g/b 分别采 x_shift ± split_px 处；
- 采样：像素中心 px = x + 0.5，采样位钳到 [0.5, W − 0.5]（半纹素），
  oracle 钉 **NEAREST** 口径 texel = floor(采样位)（LINEAR 滤波差异
  由 L02 GPU 实测声明）；alpha 原样透传。

恒等语义：corruption=0 且 rgb_split=0 → 精确恒等（任意 band_height/
shift_amount/seed 与任意图像，含 HDR）：n1 < 1 恒成立 → hot=0 →
x_shift=0，且 split_px=0，三通道采样位重合。三滑杆全零亦早退恒等。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

DEFAULTS: dict[str, Any] = {
    "band_height": 24.0,    # 4–128 px
    "shift_amount": 0.35,   # 0–1
    "rgb_split": 0.3,       # 0–1
    "corruption": 0.35,     # 0–1
    "seed": 0.0,            # 0–100（进 hash 的静态确定性 seed）
}

_SEED_STRIDE = 97.0  # seed 进 hash 的条带坐标平移步长（与 README 声明一致）


def hash12(x: np.ndarray | float, y: np.ndarray | float) -> np.ndarray | float:
    """Dave Hoskins 无三角函数 hash，输出 [0, 1)。与 .tglsl 逐式同构。"""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    # vec3 p3 = fract(vec3(p.xyx) * 0.1031)
    a = np.mod(x * 0.1031, 1.0)
    b = np.mod(y * 0.1031, 1.0)
    c = a  # 第三分量复用 x（p.xyx）
    # p3 += dot(p3, p3.yzx + 33.33) = a·(b+33.33) + b·(c+33.33) + c·(a+33.33)
    d = a * (b + 33.33) + b * (c + 33.33) + c * (a + 33.33)
    a2, b2, c2 = a + d, b + d, c + d
    out = (a2 + b2) * c2
    return out - np.floor(out)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    band_height = max(float(p["band_height"]), 1.0)
    shift_amount = float(p["shift_amount"])
    rgb_split = float(p["rgb_split"])
    corruption = float(p["corruption"])
    seed = float(p["seed"])

    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    if shift_amount < 0.001 and rgb_split < 0.001 and corruption < 0.001:
        return img.astype(np.float32)

    yy = np.arange(h, dtype=np.float64) + 0.5
    band = np.floor(yy / band_height)
    shifted_band = band + seed * _SEED_STRIDE
    n1 = np.asarray(hash12(shifted_band, np.full_like(shifted_band, 7.0)))
    n2 = np.asarray(hash12(shifted_band, np.full_like(shifted_band, 23.0)))
    hot = (n1 >= 1.0 - corruption).astype(np.float64)

    x_shift_uv = (n2 - 0.5) * shift_amount * 0.25 * hot      # UV 域 → px 乘 W
    shift_px = x_shift_uv * float(w)                          # (h,)
    split_px = rgb_split * 12.0 * (0.25 + 0.75 * hot)         # (h,)

    xs = np.arange(w, dtype=np.float64) + 0.5                 # (w,)

    def sample(channel: int, offsets: np.ndarray) -> np.ndarray:
        """offsets: (h,) 逐行水平位移 → NEAREST 采样该通道。"""
        pos = xs[None, :] + offsets[:, None]                  # (h, w)
        pos = np.clip(pos, 0.5, float(w) - 0.5)
        idx = np.floor(pos).astype(np.int64)
        return img[np.arange(h)[:, None], idx, channel]

    out_r = sample(0, shift_px + split_px)
    out_g = sample(1, shift_px)
    out_b = sample(2, shift_px - split_px)
    out_rgb = np.stack([out_r, out_g, out_b], axis=-1)

    if img.shape[-1] == 4:
        return np.concatenate([out_rgb, img[..., 3:4]], axis=-1).astype(np.float32)
    return out_rgb.astype(np.float32)
