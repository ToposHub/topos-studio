"""程序生成默认 HDR 环境贴图（阶段七）。

当用户未提供 HDR 素材时，用纯 numpy 生成一张柔和的天空渐变 equirectangular
HDR 作为 Scene3D 的默认 IBL 环境。零文件依赖、零版权、开箱即用。

输出格式与 ``hdri.load_equirect`` 完全一致：(H, W, 3) float32 线性 RGB，
水平 360°（W 方向）、垂直 180°（H 方向，第 0 行=天顶，最后一行=天底）。

天空模型：
  - 天顶（y=0）：深蓝偏紫，较暗
  - 中天（y≈0.3）：亮蓝，最亮区
  - 地平线（y≈0.5）：暖白辉光（模拟太阳散射）
  - 地面（y>0.5）：暗灰，向下衰减
  - 水平方向均匀（无特定光源），保证环境反射柔和、无硬高光

这种渐变不会产生照片级细节，但作为 fallback 环境光足够：
PBR 金属/粗糙材质能得到柔和的渐变反射，比纯黑环境自然得多。
用户往 hdri/ 放真实素材后，下拉里选真实 HDR 即可覆盖。
"""

from __future__ import annotations

import numpy as np


# 天空颜色关键帧（垂直角度 v ∈ [0,1]，0=天顶 1=天底）：(R, G, B) 线性
# 数值 >1.0 是 HDR 高光（地平线辉光）。
_SKY_KEYFRAMES = np.array([
    [0.00, 0.15, 0.25, 0.45],   # 天顶：深蓝偏紫，较暗
    [0.30, 0.40, 0.60, 0.95],   # 中天：亮蓝（最亮区）
    [0.48, 0.85, 0.80, 0.70],   # 地平线上方：暖白辉光开始
    [0.50, 1.20, 1.05, 0.85],   # 地平线：暖白高光（模拟太阳散射）
    [0.52, 0.55, 0.45, 0.35],   # 地平线下方：暖色衰减
    [0.70, 0.18, 0.15, 0.12],   # 地面：暗灰棕
    [1.00, 0.05, 0.04, 0.03],   # 天底：极暗
], dtype=np.float32)


def _interp_sky(v: np.ndarray) -> np.ndarray:
    """对天空关键帧做分段线性插值，返回 (N, 3) RGB。

    v 是垂直角度数组（值域 [0,1]，0=天顶 1=天底）。
    """
    vs = _SKY_KEYFRAMES[:, 0]
    rgb = _SKY_KEYFRAMES[:, 1:]
    # np.interp 对每个通道分别插值
    out = np.empty((v.shape[0], 3), dtype=np.float32)
    out[:, 0] = np.interp(v, vs, rgb[:, 0])
    out[:, 1] = np.interp(v, vs, rgb[:, 1])
    out[:, 2] = np.interp(v, vs, rgb[:, 2])
    return out


def generate_sky_equirect(size: int = 512) -> np.ndarray:
    """生成柔和天空渐变 equirectangular HDR。

    Args:
        size: 输出图的高度（像素）。宽度自动 = size*2（2:1 全景比例）。
              512 是质量/速度的平衡点，足够 IBL 卷积用。

    Returns:
        (H, W, 3) float32 线性 RGB 数组，H=size, W=size*2。
        水平方向（W）均匀，垂直方向（H）按天空渐变变化。
        第 0 行 = 天顶，最后一行 = 天底。
    """
    h = max(64, int(size))
    w = h * 2
    # 垂直角度 v ∈ [0,1]：第 0 行天顶(0) → 最后行天底(1)
    # 用 +0.5 居中采样，避免边缘锯齿
    v = (np.arange(h, dtype=np.float32) + 0.5) / h
    sky_row = _interp_sky(v)  # (h, 3)
    # 水平方向均匀：每行重复 sky_row 对应的颜色
    img = np.broadcast_to(sky_row[:, None, :], (h, w, 3)).copy()
    return np.ascontiguousarray(img, dtype=np.float32)


# 内置默认 HDR 的缓存键（供 HDRIBaker.bake_array 去重）
# 用固定字符串而非文件路径，区分于文件加载的 IBL。
DEFAULT_SKY_KEY = "__procedural_sky__"
