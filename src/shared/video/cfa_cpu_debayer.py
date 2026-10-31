"""TRAW CFA CPU debayer（W4）——cfa_gpu_debayer 的 CPU 镜像。

语义同源（RGGB 相位网格 + 双线性方向平均 + LOG0 逆曲线/16-bit linear +
WB/EI + gamma 2.2 OOTF），供 GPU 链不可用时的回退渲染（CPU 预览路径、
缩略图回退）。实现为相位平面 → 全分辨率 R/G/B 平面的向量化组装，
边界 fetch 语义与 shader 的 clamp 一致。
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np

# ---- PWL-log12 曲线参数（topos_traw_format_plan §3.2，冻结；与
# cfa_gpu_debayer 同源） ----
TRAW_CURVE_TOE_E = 8.0
TRAW_CURVE_FW_E = 16384.0
TRAW_CODE_MAX = 4095.0


def traw_log12_constants() -> Tuple[float, float, float]:
    """返回 (A, K, C0)——与 cfa_gpu_debayer/tools 同源公式。"""
    s = TRAW_CURVE_TOE_E
    fw = TRAW_CURVE_FW_E
    a = TRAW_CODE_MAX / math.log2(1.0 + (fw - s) / s)
    k = a / (math.log(2.0) * (s + s))
    c0 = a - k * s
    return a, k, c0


def _fetch(arr: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """shader fetch(tex, tc+delta, size) 的 clamp 边界语义（delta ∈ {−1,0,1}）。

    edge pad 一圈后按偏移切片——越界坐标复制边界值，输出恒等输入形状。
    """
    padded = np.pad(arr, ((1, 1), (1, 1)), mode="edge")
    h, w = arr.shape
    return padded[1 + dy: 1 + dy + h, 1 + dx: 1 + dx + w]


def _assemble_rgb(r: np.ndarray, gr: np.ndarray, gb: np.ndarray,
                  b: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """相位平面 → 全分辨率 R/G/B（RGGB 双线性方向平均，shader 四位点同式）。

    平面索引 (i,j) → 传感器 (2i+ex, 2j+ey)：
      R 位点 (ex0,ey0) / Gr 位点 (1,0) / Gb 位点 (0,1) / B 位点 (1,1)。
    """
    r00 = r
    r10 = _fetch(r, 0, 1)     # Gr 位点：R = 右邻
    r01 = _fetch(r, 1, 0)     # Gb 位点：R = 下邻
    r11 = 0.25 * (_fetch(r, 0, 0) + _fetch(r, 0, 1)
                  + _fetch(r, 1, 0) + _fetch(r, 1, 1))
    b11 = b
    b01 = _fetch(b, 0, -1)    # Gb 位点：B = 左邻
    b10 = _fetch(b, -1, 0)    # Gr 位点：B = 上邻
    b00 = 0.25 * (_fetch(b, 0, 0) + _fetch(b, 0, -1)
                  + _fetch(b, -1, 0) + _fetch(b, -1, -1))

    h, w = gr.shape
    r_full = np.empty((h * 2, w * 2), dtype=np.float32)
    r_full[0::2, 0::2] = r00
    r_full[0::2, 1::2] = 0.5 * (r00 + r10)
    r_full[1::2, 0::2] = 0.5 * (r00 + r01)
    r_full[1::2, 1::2] = r11

    b_full = np.empty((h * 2, w * 2), dtype=np.float32)
    b_full[1::2, 1::2] = b11
    b_full[1::2, 0::2] = 0.5 * (b11 + b01)
    b_full[0::2, 1::2] = 0.5 * (b11 + b10)
    b_full[0::2, 0::2] = b00

    g_full = np.empty((h * 2, w * 2), dtype=np.float32)
    g_full[0::2, 1::2] = gr
    g_full[1::2, 0::2] = gb
    g_full[0::2, 0::2] = 0.5 * (gr + gb)
    g_full[1::2, 1::2] = 0.5 * (gr + gb)
    return r_full, g_full, b_full


def debayer_cfa_planes(planes, width: int, height: int, *,
                       bit_depth: int = 12,
                       wb_gains: Tuple[float, float, float] = (1.0, 1.0, 1.0),
                       exposure_stops: float = 0.0,
                       display_ootf: bool = True,
                       black_level: float = 0.0,
                       gamut_matrix=None,
                       gamma_mode: int | None = None) -> np.ndarray:
    """TRAW CFA 相位平面 → RGB float32（显示域 [0,1]，全分辨率）。

    12-bit = LOG0 逆曲线（PW-log12 冻结对）；16-bit = linear 码值直接
    归一化（transfer=linear 契约）。返回 H×W×3（R,G,B 序）float32。

    RC3：gamut_matrix 为行主序 3x3（camera-native → BT.709 linear，
    camera_gamut_matrices 推导；None = identity 零漂移）；gamma_mode
    0=linear / 1=gamma2.2 / 2=sRGB，None 时按 display_ootf 折算——
    与 GPU shader（cfa_gpu_debayer 模板）同点同式。
    """
    if len(planes) != 4:
        raise ValueError(f"CFA debayer requires 4 phase planes, "
                         f"got {len(planes)}")
    r, gr, gb, b = (np.asarray(p, dtype=np.float32) for p in planes)
    r_full, g_full, b_full = _assemble_rgb(r, gr, gb, b)
    # 奇数帧尺寸：平面 = ceil(W/2)，组装得 2*ceil = W+1——按调用方
    # 声明的全帧宽高截取（GPU 链同语义：out_size = 元数据全尺寸）
    if height and width:
        r_full = r_full[:height, :width]
        g_full = g_full[:height, :width]
        b_full = b_full[:height, :width]

    if black_level:
        # RC8 / AUD-38702：码值域黑位减除。与 GPU shader 同点同式：
        # 先完成双线性方向平均（debayer 插值），再对插值结果减除黑位
        # 并 clamp 0（"先平均后减"）。此前 CPU 在逐相位平面上"先减后
        # 平均"，max 与平均不可交换，阴影区（部分采样低于黑位的位点）
        # 与 GPU 链可见漂移。
        r_full = np.maximum(r_full - black_level, 0.0)
        g_full = np.maximum(g_full - black_level, 0.0)
        b_full = np.maximum(b_full - black_level, 0.0)

    if bit_depth <= 12:
        a, k, c0 = traw_log12_constants()
        s_toe = TRAW_CURVE_TOE_E
        fw = TRAW_CURVE_FW_E
        # 逆曲线与 shader 同式：c<A 线性趾 x=(c−c0)/k，否则 x=S·(2^(c/A)−1)
        lin_r = np.where(r_full < a, (r_full - c0) / k,
                         s_toe * (np.exp2(r_full / a) - 1.0))
        lin_g = np.where(g_full < a, (g_full - c0) / k,
                         s_toe * (np.exp2(g_full / a) - 1.0))
        lin_b = np.where(b_full < a, (b_full - c0) / k,
                         s_toe * (np.exp2(b_full / a) - 1.0))
    else:
        fw = 65535.0
        lin_r, lin_g, lin_b = r_full, g_full, b_full

    wr, wg, wb = wb_gains
    gain = (2.0 ** exposure_stops) / fw
    lin = np.stack([lin_r * wr, lin_g * wg, lin_b * wb], axis=-1) * gain
    if gamut_matrix is not None:
        # RC3：IDT gamut 变换（行向量存储 → 右乘 M.T 等价 M@v；变换在
        # clamp 前）
        m = np.asarray(gamut_matrix, dtype=np.float64)
        if m.shape == (3, 3) and not np.array_equal(m, np.eye(3)):
            lin = lin @ m.T
    lin = np.clip(lin, 0.0, 1.0)
    mode = gamma_mode if gamma_mode is not None else (1 if display_ootf else 0)
    if mode == 1:
        lin = np.power(lin, 1.0 / 2.2)
    elif mode == 2:
        hi = 1.055 * np.power(np.maximum(lin, 0.0), 1.0 / 2.4) - 0.055
        lin = np.where(lin >= 0.0031308, hi, lin * 12.92)
    return lin.astype(np.float32)
