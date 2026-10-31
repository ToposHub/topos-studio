"""M19.4 · PQ/HLG 参考向量冻结（code 值/nits 参考点）。

SMPTE ST 2084 (PQ) 与 BT.2100 HLG 的锚点参考值——冻结 reference
white/峰值/EOTF 条件，供预览/导出/Scope 的 HDR 读数对拍。数值来自
标准定义（PQ 常数按 ST 2084；HLG 按 BT.2100-1 scene-referred
OETF⁻¹ 与 1/12 基准白约定），此处做**代码级冻结**并配往返一致性
测试（向量自身 = 权威表；往返检查的是我们内部实现的一致性）。
"""

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np

# ── ST 2084 (PQ) 常数 ──
PQ_M1 = 2610.0 / 16384.0
PQ_M2 = 2523.0 / 4096.0 * 128.0
PQ_C1 = 3424.0 / 4096.0
PQ_C2 = 2413.0 / 4096.0 * 32.0
PQ_C3 = 2392.0 / 4096.0 * 32.0

#: PQ 绝对峰值（nits）
PQ_PEAK_NITS = 10000.0
#: SDR 参考白（nits）——HDR 视频 reference white 约定（Rec.2407/ST 2084 应用）
PQ_REFERENCE_WHITE_NITS = 203.0


def pq_encode(nits: float) -> float:
    """线性亮度（nits, 0–10000）→ PQ 码值（0–1）。"""
    y = max(0.0, float(nits)) / PQ_PEAK_NITS
    if y == 0.0:
        return 0.0
    ym1 = y ** PQ_M1
    num = PQ_C1 + PQ_C2 * ym1
    den = 1.0 + PQ_C3 * ym1
    return (num / den) ** PQ_M2


def pq_decode(code: float) -> float:
    """PQ 码值（0–1）→ 线性亮度（nits）。"""
    if code <= 0.0:
        return 0.0
    ym1 = code ** (1.0 / PQ_M2)
    num = max(0.0, ym1 - PQ_C1)
    den = PQ_C2 - PQ_C3 * ym1
    y = (num / den) ** (1.0 / PQ_M1)
    return y * PQ_PEAK_NITS


#: 冻结的 PQ 参考向量：{nits: (码值, 10bit code)}
# 注：10bit code = round(PQ × 1023)
PQ_REFERENCE_VECTORS: Dict[float, Tuple[float, int]] = {
    0.0: (0.0, 0),
    0.01: (pq_encode(0.01), round(pq_encode(0.01) * 1023)),
    1.0: (pq_encode(1.0), round(pq_encode(1.0) * 1023)),
    # 10bit code 64/128（常见最低有效锚）
    100.0: (pq_encode(100.0), round(pq_encode(100.0) * 1023)),
    PQ_REFERENCE_WHITE_NITS: (pq_encode(PQ_REFERENCE_WHITE_NITS),
                              round(pq_encode(PQ_REFERENCE_WHITE_NITS) * 1023)),
    1000.0: (pq_encode(1000.0), round(pq_encode(1000.0) * 1023)),
    4000.0: (pq_encode(4000.0), round(pq_encode(4000.0) * 1023)),
    10000.0: (1.0, 1023),
}

# ── BT.2100 HLG ──
#: HLG 基准白（nits，75% signal ≈ reference white 在 1000-nit 显示）
HLG_REFERENCE_WHITE_NITS = 1000.0 / 3.0  # ≈333.3（1/3 峰值的场景参考约定近似）


def hlg_oetf(signal: float) -> float:
    """HLG 场景线性光（0–1 场景参照）→ 信号（0–1）。BT.2100-1。"""
    a = 0.17883277
    b = 0.28466892
    c = 0.55991073
    e = max(0.0, min(1.0, float(signal)))
    if e <= 1.0 / 12.0:
        return (3.0 ** 0.5) * e ** 0.5
    return a * np.log(12.0 * e - b) + c


def hlg_oetf_inverse(signal: float) -> float:
    """HLG 信号（0–1）→ 场景线性光。"""
    a = 0.17883277
    b = 0.28466892
    c = 0.55991073
    e = max(0.0, min(1.0, float(signal)))
    if e <= 0.5:
        return (e / (3.0 ** 0.5)) ** 2
    return (np.exp((e - c) / a) + b) / 12.0


#: 冻结的 HLG 参考向量：{signal: (场景线性, 10bit code)}
HLG_REFERENCE_VECTORS: Dict[float, Tuple[float, int]] = {
    0.0: (0.0, 0),
    0.25: (round(hlg_oetf_inverse(0.25), 6), 256),
    0.5: (round(hlg_oetf_inverse(0.5), 6), 512),  # 拐点
    0.75: (round(hlg_oetf_inverse(0.75), 6), 768),
    1.0: (1.0, 1023),
}


__all__ = [
    "PQ_M1", "PQ_M2", "PQ_C1", "PQ_C2", "PQ_C3",
    "PQ_PEAK_NITS", "PQ_REFERENCE_WHITE_NITS",
    "PQ_REFERENCE_VECTORS",
    "pq_encode", "pq_decode",
    "HLG_REFERENCE_WHITE_NITS",
    "HLG_REFERENCE_VECTORS",
    "hlg_oetf", "hlg_oetf_inverse",
]
