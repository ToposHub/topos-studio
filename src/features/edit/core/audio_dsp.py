"""
Audio DSP utilities — 参数化 EQ / 动态 / 声学混响 / 立体声延迟 / 空间声像（CW-A1 重构）。

核心模块：
  - StripEQFilter: 6 频段全参量 EQ（HPF/LowShelf/Bell/HighShelf/Notch/LPF，
    RBJ cookbook biquad 级联 SOS，voicing Q 染色，频段 Solo）
  - CompressorEffect / LimiterEffect / ExpanderEffect: 动态三件套
    （hold、并行 mix、外部侧链、GR/检源电平暴露）
  - ReverbEffect: 声学房间混响（直达/早反射簇/晚期 FDN + 双级音色 + 微调制）
  - DelayEffect: 立体声双延迟线（负反馈、交叉反馈矩阵、输入滤波、高频阻尼）
  - SpatialPanner: 空间声像器（立体声域：spread/rotate/divergence/boom）
  - EffectChain: FX 链容器（instance_id 贯穿、侧链透传、GR 收集）

滤波器系数基于 RBJ Audio EQ Cookbook（Robert Bristow-Johnson）。
使用 scipy.signal.sosfilt/lfilter 做实时 IIR 滤波（C 实现性能足够）。
线程安全：实例不共享（每轨一个），AudioMixer 在 _mutex 下访问。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Optional, Tuple

import numpy as np

from src.features.edit.core.audio_capabilities import is_scipy_available_for_dsp

logger = logging.getLogger(__name__)

try:
    from scipy.signal import butter, lfilter, sosfilt
except ImportError:  # pragma: no cover
    butter = None  # type: ignore[assignment]
    lfilter = None  # type: ignore[assignment]
    sosfilt = None  # type: ignore[assignment]

# ST-03: 音频算法版本。语义/数学修正会改变旧工程声音——版本号用于
# 诊断与兼容决策（"保持旧声音" vs "显式升级到新语义"），不做静默改写。
# v2（2026-11，音频成熟化计划）：
#   - EQ-01: HPF/LPF 真 N 阶 Butterworth（旧实现斜率加倍）；Shelf Q 实际生效
#   - DY-02: Expander 标准 (ratio-1) 下扩斜率；Gate 独立迟滞状态机
#   - DY-02/03: Compressor 检测域固定为线性峰值跟随；Make Up 移到
#     dynamics 之后（post-dynamics）
#   - DY-04: SC Listen 输出原始检源（旧实现播放整流检测量）
#   - PAN-01: SpatialPanner 矩阵别名修复（R 计算不再读被污染的 L）
#   - RV-01: Brightness 就地更新 comb 阻尼（运行中生效，不清尾音）
#   - DL-01: Delay 输入矩阵 Stereo/Mono/Left/Right（旧 LCR/Quad/5.x 枚举
#     从未实现，迁移为 Stereo，声音不变）
#   - MX-04: 同轨多 clip 先求和、轨 DSP 一次处理（重叠 clip 区间声音改变
#     ——这是缺陷修复而非回归）
ALGORITHM_VERSION = 4

ALGORITHM_CHANGELOG = {
    1: "初始版本（CW-A1）",
    2: "EQ 斜率/Shelf Q、Expander 斜率/Gate 状态机、压缩检测域、Make Up "
       "位置、SC Listen、Panner 矩阵、Reverb 阻尼热更新、Delay 输入矩阵、"
       "同轨 clip 求和",
    3: "EQ-02 非 Clean Voicing 的带宽/增益耦合（boost→Q 窄/cut→Q 宽；"
       "Clean 与 0dB 不变）；DY-03 Limiter 过冲遥测；RV-02 image-source "
       "ER/负 Pre Delay 传播/Distance 只动 ER",
    4: "R07：Delay 延迟时间热改走 15ms 双读头线性交叉淡化（旧/新读头共享"
       "历史，反馈消费淡化后湿信号；淡化中改目标只更新目标、进度接续）。"
       "静态参数渲染与 v3 逐位一致，仅热改过渡行为变化。",
}


# AM-01: scipy 可用性收口到 audio_capabilities 单一真相源，
# DSP 层不再各自 import-guard，避免定义漂移与 UI 无法感知降级。
# 运行时查询（而非加载时缓存）：使测试可 monkeypatch capabilities 而无需
# reload 本模块（reload 会污染 sys.modules 导致类身份断裂）。
# O(1) 缓存查表，hot path 开销可忽略。
def _scipy_available() -> bool:
    return is_scipy_available_for_dsp()


from src.features.edit.core.timeline_v2 import StripEQParams


# ─────────────────────────────────────────────────────────────────────────────
# RBJ Audio EQ Cookbook 系数公式
# ─────────────────────────────────────────────────────────────────────────────

def _biquad_lowshelf(freq_hz: float, gain_db: float, sample_rate: float,
                     q: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
    """LowShelf 滤波器系数（b, a）。

    Args:
        freq_hz: 中心/转折频率（Hz）
        gain_db: 增益（dB），正值提升、负值衰减
        sample_rate: 采样率（Hz）
        q: EQ 频段 Q 值。None（Reverb 音色等内部调用）保持 legacy S=1 shelf；
            传入时使用 Q 缩放变体 alpha = sin(w0)/(2q)（与 EQ 曲线 UI 同源，
            EQ-01：Shelf Q 必须实际控制滤波器）。q=0.7071 与 legacy S=1 等价。

    Returns:
        (b, a) IIR 系数，长度均为 3，scipy.signal.lfilter 格式。
    """
    A = 10.0 ** (gain_db / 40.0)  # gain_db/2 转 linear，再开方（RBJ 公式）
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    if q is not None:
        # Q 缩放 shelf（EQ-01）：过渡带宽随 Q 收窄，Q 越大越陡。
        alpha = sin_w0 / (2.0 * max(0.05, float(q)))
    else:
        # shelf slope factor S=1（legacy，Reverb tone 语义不变）
        alpha = sin_w0 / 2.0 * np.sqrt(2.0)
    beta = 2.0 * np.sqrt(A) * alpha

    b0 = A * ((A + 1) - (A - 1) * cos_w0 + beta)
    b1 = 2.0 * A * ((A - 1) - (A + 1) * cos_w0)
    b2 = A * ((A + 1) - (A - 1) * cos_w0 - beta)
    a0 = (A + 1) + (A - 1) * cos_w0 + beta
    a1 = -2.0 * ((A - 1) + (A + 1) * cos_w0)
    a2 = (A + 1) + (A - 1) * cos_w0 - beta

    b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


def _biquad_peaking(freq_hz: float, gain_db: float, q: float, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """Peaking 滤波器系数（b, a）。

    Args:
        freq_hz: 中心频率（Hz）
        gain_db: 增益（dB）
        q: Q 值（带宽倒数，越大越窄）
        sample_rate: 采样率（Hz）
    """
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    b0 = 1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / A

    b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


def _biquad_highshelf(freq_hz: float, gain_db: float, sample_rate: float,
                      q: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
    """HighShelf 滤波器系数（b, a）。q 语义同 :func:`_biquad_lowshelf`。"""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    if q is not None:
        alpha = sin_w0 / (2.0 * max(0.05, float(q)))
    else:
        alpha = sin_w0 / 2.0 * np.sqrt(2.0)  # legacy S=1
    beta = 2.0 * np.sqrt(A) * alpha

    b0 = A * ((A + 1) + (A - 1) * cos_w0 + beta)
    b1 = -2.0 * A * ((A - 1) + (A + 1) * cos_w0)
    b2 = A * ((A + 1) + (A - 1) * cos_w0 - beta)
    a0 = (A + 1) - (A - 1) * cos_w0 + beta
    a1 = 2.0 * ((A - 1) - (A + 1) * cos_w0)
    a2 = (A + 1) - (A - 1) * cos_w0 - beta

    b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


def _biquad_highpass(freq_hz: float, q: float, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """Highpass 滤波器系数（b, a），用于 Low-Cut。

    Args:
        freq_hz: 截止频率（Hz）
        q: Q 值（0.7071 = Butterworth 平滑，无共振凸起）
        sample_rate: 采样率（Hz）
    """
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    b0 = (1.0 + cos_w0) / 2.0
    b1 = -(1.0 + cos_w0)
    b2 = (1.0 + cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha

    b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


def _biquad_lowpass(freq_hz: float, q: float, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """Lowpass 滤波器系数（b, a），用于 High-Cut。

    Args:
        freq_hz: 截止频率（Hz）
        q: Q 值（0.7071 = Butterworth 平滑）
        sample_rate: 采样率（Hz）
    """
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    b0 = (1.0 - cos_w0) / 2.0
    b1 = 1.0 - cos_w0
    b2 = (1.0 - cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha

    b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


def _biquad_notch(freq_hz: float, q: float, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """Notch 陷波器系数（b, a）。"""
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    b0, b1, b2 = 1.0, -2.0 * cos_w0, 1.0
    a0, a1, a2 = 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha

    b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


# ─────────────────────────────────────────────────────────────────────────────
# StripEQ 共享系数/响应服务（EQ-01：DSP 与 UI 同一来源）
# ─────────────────────────────────────────────────────────────────────────────

def _butter_cut_sos(kind: str, cutoff_hz: float, slope_db_oct: int,
                    sample_rate: float) -> Optional[np.ndarray]:
    """HPF/LPF 标称斜率 → 真 N 阶 Butterworth SOS（N = slope/6）。

    EQ-01：旧实现级联 `slope//6` 个相同 Q=0.7071 的二阶 biquad，实际斜率
    是标称的 2 倍（12/24/36/48 dB/oct）且截止处 -3/-6/-9/-12 dB。
    scipy.signal.butter 输出的 SOS 含正确的每段 Q 分布与一阶段
    （6/18 dB/oct 选项不丢失）。scipy 不可用时返回 None（调用方回退 legacy）。
    """
    if slope_db_oct not in (6, 12, 18, 24) or butter is None:
        return None
    order = slope_db_oct // 6
    if not 0.0 < cutoff_hz < sample_rate * 0.5:
        return None
    return butter(order, cutoff_hz, btype=kind, fs=sample_rate, output="sos")


def _sos_row_mag_db(row: np.ndarray, w: float) -> float:
    """单个 SOS 行 [b0,b1,b2,1,a1,a2] 在数字角频率 w 处的幅度（dB）。"""
    b0, b1, b2, _a0, a1, a2 = row
    cos_w = np.cos(w)
    sin_w = np.sin(w)
    cos_2w = np.cos(2.0 * w)
    sin_2w = np.sin(2.0 * w)
    num_re = b0 + b1 * cos_w + b2 * cos_2w
    num_im = -(b1 * sin_w + b2 * sin_2w)
    den_re = 1.0 + a1 * cos_w + a2 * cos_2w
    den_im = -(a1 * sin_w + a2 * sin_2w)
    num_sq = num_re * num_re + num_im * num_im
    den_sq = max(1e-24, den_re * den_re + den_im * den_im)
    return 10.0 * float(np.log10(max(num_sq / den_sq, 1e-12)))


def strip_eq_band_sections(band: dict, sample_rate: float,
                           voicing: str = "Clean",
                           algorithm_version: Optional[int] = None) -> list:
    """单个 EQ 频段 dict → SOS 行列表（DSP 滤波与 UI 响应的唯一来源）。

    HPF/LPF 走 :func:`_butter_cut_sos`（真 N 阶 Butterworth）；Shelf/Bell/
    Notch 走 RBJ 系数（Shelf 消费 Q）。scipy 不可用时 HPF/LPF 回退 legacy
    级联（降级可解释，不静默改变斜率语义——响应服务用同一函数求值）。

    H01 兼容处理器（逐版本精确分发，冻结基线见
    docs/audit/2026-11-15-audio-fx-post-integration/h01/baseline-historical.json）：
    - ``v ≤ 1``（含未标注 0）：v1 legacy 系数——HPF/LPF 级联 ``slope//6``
      个相同 Q=0.7071 biquad、Shelf 固定 S=1（不消费 Q）、无 EQ-02 耦合
      （df749759/CW-A1 时代语义）。
    - ``v == 2``：302b88b6 语义——EQ-01 真 N 阶 Butterworth + Shelf 消费
      Q，但**无 EQ-02 增益-Q 耦合**（EQ-02 属 v3 变更；v2 工程 HPF/ Shelf
      响应与历史 PCM 对齐）。
    - ``v ≥ 3`` 或 ``None``（无版本上下文的直用，测试/UI 新建实例）：
      现行（Butterworth + EQ-02 耦合）。
    legacy 判定只认模型层显式传入的整数版本。

    系数按频段内容 LRU 缓存（UI 曲线每次重绘 ~2000 次求值，缓存后只做
    三角函数求值；DSP 在 set_params 时调用，缓存命中无开销）。

    Returns:
        list[[b0, b1, b2, 1.0, a1, a2], ...]（可能为空 = 该段中性）。
    """
    ftype = band.get("filter_type", "Bell")
    freq = min(max(10.0, float(band.get("freq", 1000.0))), sample_rate * 0.45)
    gain = float(band.get("gain", 0.0))
    q = max(0.1, float(band.get("q", 1.1))) * VOICING_Q_SCALE.get(voicing, 1.0)
    # H01 三分桶：legacy（v≤1 级联）/ v2（Butterworth 无 EQ-02）/ current。
    _v = None if algorithm_version is None else int(algorithm_version)
    legacy = _v is not None and _v <= 1
    v2_era = _v is not None and _v == 2
    if not legacy and not v2_era:
        # EQ-02: 带宽/增益耦合（非 Clean voicing；带增益形状；0dB/无耦合直通）
        q = _effective_band_q(q, gain, voicing, ftype)
    slope = int(band.get("slope", 12))

    cache_key = (ftype, round(freq, 6), round(gain, 6), round(q, 6),
                 slope, round(sample_rate, 3), voicing, _v)
    cached = _EQ_SECTION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    def _row(b: np.ndarray, a: np.ndarray) -> list:
        return [b[0], b[1], b[2], 1.0, a[1], a[2]]

    if ftype == "HPF":
        sos = None if legacy else _butter_cut_sos("highpass", freq, slope, sample_rate)
        if sos is not None:
            rows = [list(r) for r in sos]
        else:
            order = max(1, min(4, slope // 6))
            rows = [_row(*_biquad_highpass(freq, _BUTTERWORTH_Q, sample_rate))
                    for _ in range(order)]
    elif ftype == "LPF":
        sos = None if legacy else _butter_cut_sos("lowpass", freq, slope, sample_rate)
        if sos is not None:
            rows = [list(r) for r in sos]
        else:
            order = max(1, min(4, slope // 6))
            rows = [_row(*_biquad_lowpass(freq, _BUTTERWORTH_Q, sample_rate))
                    for _ in range(order)]
    elif ftype == "Low Shelf":
        if abs(gain) < 0.05:
            rows = []
        elif legacy:
            rows = [_row(*_biquad_lowshelf(freq, gain, sample_rate))]
        else:
            rows = [_row(*_biquad_lowshelf(freq, gain, sample_rate, q=q))]
    elif ftype == "High Shelf":
        if abs(gain) < 0.05:
            rows = []
        elif legacy:
            rows = [_row(*_biquad_highshelf(freq, gain, sample_rate))]
        else:
            rows = [_row(*_biquad_highshelf(freq, gain, sample_rate, q=q))]
    elif ftype == "Notch":
        rows = [_row(*_biquad_notch(freq, q, sample_rate))]
    else:  # Bell
        rows = [] if abs(gain) < 0.05 else [
            _row(*_biquad_peaking(freq, gain, q, sample_rate))]

    if len(_EQ_SECTION_CACHE) >= _EQ_SECTION_CACHE_MAX:
        _EQ_SECTION_CACHE.popitem(last=False)
    _EQ_SECTION_CACHE[cache_key] = rows
    return rows


# EQ 段系数 LRU 缓存（butter/RBJ 求值较贵；频段内容不变时零重算）
_EQ_SECTION_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_EQ_SECTION_CACHE_MAX = 512


def strip_eq_band_response_db(band: dict, freq_hz: float, sample_rate: float,
                              voicing: str = "Clean",
                              algorithm_version: Optional[int] = None) -> float:
    """单个频段在 freq_hz 处的响应（dB），与其 SOS 级联传输严格一致。

    ``algorithm_version`` 语义同 :func:`strip_eq_band_sections`（R01：旧
    实例的 UI 曲线与 legacy DSP 路径同源）。
    """
    if not band.get("enabled", True):
        return 0.0
    rows = strip_eq_band_sections(band, sample_rate, voicing,
                                  algorithm_version=algorithm_version)
    if not rows:
        return 0.0
    f = min(max(freq_hz, 1e-3), sample_rate * 0.495)
    w = 2.0 * np.pi * f / sample_rate
    total = 0.0
    for row in rows:
        total += _sos_row_mag_db(np.asarray(row, dtype=np.float64), w)
    return total


def strip_eq_total_response_db(bands, voicing: str, sample_rate: float,
                               freqs, master_gain_db: float = 0.0) -> list:
    """整条 EQ 链在给定频率点上的响应（dB 列表，UI 曲线唯一来源）。

    Solo 语义与 DSP 一致：任一 enabled 频段 Solo 时只应用 Solo 频段。
    """
    enabled = [b for b in bands if b.get("enabled", True)]
    if any(b.get("solo", False) for b in enabled):
        enabled = [b for b in enabled if b.get("solo", False)]
    out = []
    for f in freqs:
        total = float(master_gain_db)
        for b in enabled:
            total += strip_eq_band_response_db(b, float(f), sample_rate, voicing)
        out.append(total)
    return out


def strip_eq_band_response_curve_db(band: dict, freqs, sample_rate: float,
                                    voicing: str = "Clean") -> list:
    """单个频段在多个频率点上的响应曲线（系数只算一次，UI 绘图用）。"""
    if not band.get("enabled", True):
        return [0.0 for _ in freqs]
    rows = strip_eq_band_sections(band, sample_rate, voicing)
    if not rows:
        return [0.0 for _ in freqs]
    rows_arr = [np.asarray(r, dtype=np.float64) for r in rows]
    out = []
    for f in freqs:
        ff = min(max(float(f), 1e-3), sample_rate * 0.495)
        w = 2.0 * np.pi * ff / sample_rate
        out.append(sum(_sos_row_mag_db(r, w) for r in rows_arr))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 参数化 EQ：Voicing 染色（Q 缩放系数）与频段布局常量（CW-A1）
# ─────────────────────────────────────────────────────────────────────────────

VOICING_Q_SCALE: dict = {
    "Clean":   1.00,  # 线性精准数字
    "British": 1.18,  # 经典英式调音台比例 Q
    "Warm":    0.92,  # 宽厚模拟润色
    "Punch":   0.85,  # 现代紧凑聚焦
}

# EQ-02: 带宽/增益耦合（自有可测模型，非任何专有模拟音色复刻）。
# 模拟调音台带宽行为：boost 增益越高 Q 越窄（能量聚焦），cut 越深 Q 越
# 宽（宽切更音乐性）。耦合因子 = 10^(k·gain_db/60)，k 每 voicing 不同；
# |gain|≤24dB 时因子有界于 [0.67, 1.50]。Clean k=0（纯数字、无耦合，
# 默认工程声音不变）。只作用于带增益的形状（Bell/Low Shelf/High Shelf）。
VOICING_GAIN_Q_COUPLING: dict = {
    "Clean":   0.00,
    "British": 0.30,
    "Warm":    0.22,
    "Punch":   0.36,
}


def _effective_band_q(base_q: float, gain_db: float, voicing: str,
                      shape: str) -> float:
    """EQ-02: 有效 Q = 基础 Q × voicing 比例 × 增益耦合因子。

    仅对带增益的形状（Bell/Low Shelf/High Shelf）生效；gain=0 或 Clean
    恒为直通（因子 1）。单调：gain ↑ → Q ↑（带宽窄）；有界；可测。
    """
    if shape not in ("Bell", "Low Shelf", "High Shelf") or gain_db == 0.0:
        return base_q
    k = VOICING_GAIN_Q_COUPLING.get(voicing, 0.0)
    if k == 0.0:
        return base_q
    return base_q * 10.0 ** (k * float(gain_db) / 60.0)

# Butterworth 平滑 Q 值（HPF/LPF 级联用）
_BUTTERWORTH_Q = 0.7071


# ─────────────────────────────────────────────────────────────────────────────
# 声像（pan）增益（AM-08 阶段 2）
# ─────────────────────────────────────────────────────────────────────────────

# AM-08: pan 算法选择。False=线性 pan（旧行为，中心 +3dB）；True=等功率 pan
# （中心 -3dB 恒功率，专业标准）。equal-power 是专业混音器默认，避免声像居中时
# 响度隆起。验收：单声道粉噪扫描 pan，合成功率变化 ≤±0.2 dB。
USE_EQUAL_POWER_PAN: bool = True


def pan_gains(pan: float) -> tuple:
    """计算声像增益 (left_gain, right_gain)。

    根据 USE_EQUAL_POWER_PAN 选择算法：
    - 线性 pan：left = 1-max(0,pan), right = 1+min(0,pan)（中心 1.0/1.0，+3dB）
    - 等功率 pan：left = cos(θ), right = sin(θ)，θ=(pan+1)/2*π/2（中心 0.707/0.707，-3dB 恒功率）

    Args:
        pan: 声像位置 [-1.0, 1.0]，-1=全左，0=中心，+1=全右

    Returns:
        (left_gain, right_gain) 线性增益。
    """
    pan = max(-1.0, min(1.0, float(pan)))
    if USE_EQUAL_POWER_PAN:
        # 等功率：θ 从 0（全左）到 π/2（全右）
        theta = (pan + 1.0) * 0.5 * (np.pi / 2.0)
        left_gain = float(np.cos(theta))
        right_gain = float(np.sin(theta))
        return (left_gain, right_gain)
    # 线性（旧行为）
    return (1.0 - max(0.0, pan), 1.0 + min(0.0, pan))


def linear_pan_gains(pan: float) -> tuple:
    """线性 pan 墰益（向后兼容/测试用）。

    与旧实现一致：left = 1-max(0,pan), right = 1+min(0,pan)。
    """
    pan = max(-1.0, min(1.0, float(pan)))
    return (1.0 - max(0.0, pan), 1.0 + min(0.0, pan))


def balance_gains(pan: float) -> tuple:
    """立体声 balance 增益（unity-center，RMA-08）。

    与 equal-power pan 的区别：
    - equal-power（mono pan）：center 每侧 0.7071（-3dB），合成功率恒定——
      适合把 mono 源放到立体声场，但用于原生立体声会整体降低且改变声像。
    - balance（stereo balance）：center 每侧 1.0（0dB，unity），pan 偏离时只衰减
      远侧声道——原生立体声在 center 保持原响度与声像，符合用户预期。

    系数：left = 1-max(0,pan)，right = 1+min(0,pan)（与 linear_pan_gains 一致，
    但语义是"balance"而非"mono pan"）。center=0 时 (1.0, 1.0)。

    Args:
        pan: 声像位置 [-1.0, 1.0]

    Returns:
        (left_gain, right_gain) 线性增益，center 为 (1.0, 1.0)。
    """
    pan = max(-1.0, min(1.0, float(pan)))
    return (1.0 - max(0.0, pan), 1.0 + min(0.0, pan))


def pan_gains_for_channels(pan: float, channels: int) -> tuple:
    """按输入声道数选择正确的 pan 算法（RMA-08）。

    - 1 声道（mono）：equal-power pan（center 恒功率 -3dB/侧），把 mono 源定位。
    - 2 声道（stereo）：balance（unity-center），原生立体声保持响度与声像。
    - >2 声道（surround）：暂用 balance（trim 语义），完整 surround panner 待 C1 layout。
    """
    if channels <= 1:
        return pan_gains(pan)
    return balance_gains(pan)


# ─────────────────────────────────────────────────────────────────────────────
# Clip 淡化包络（AM-06 阶段 2.3）
# ─────────────────────────────────────────────────────────────────────────────

def linear_fade_envelope(num_samples: int, fade_in_samples: int = 0,
                         fade_out_samples: int = 0) -> np.ndarray:
    """生成 clip 淡入/淡出线性包络（AM-06）。

    淡入：前 fade_in_samples 个样本从 0 线性升到 1。
    淡出：最后 fade_out_samples 个样本从 1 线性降到 0。
    中间保持 1.0。fade_in/out 区域不重叠（若重叠，淡出优先于淡入的中段）。

    Args:
        num_samples: 该块样本数
        fade_in_samples: 淡入样本数（0=无淡入）
        fade_out_samples: 淡出样本数（0=无淡出）

    Returns:
        shape (num_samples,) 的 float64 增益包络，逐样本相乘用。
    """
    env = np.ones(num_samples, dtype=np.float64)
    fi = max(0, min(int(fade_in_samples), num_samples))
    fo = max(0, min(int(fade_out_samples), num_samples))
    if fi > 0:
        env[:fi] = np.linspace(0.0, 1.0, fi, dtype=np.float64)
    if fo > 0:
        env[-fo:] = np.linspace(1.0, 0.0, fo, dtype=np.float64)
    return env


def equal_power_fade_envelope(num_samples: int, fade_in_samples: int = 0,
                              fade_out_samples: int = 0) -> np.ndarray:
    """生成 clip 淡入/淡出等功率包络（AM-06）。

    等功率淡化使用余弦/正弦曲线，使淡入淡出交叉时总功率近似恒定
   （避免线性淡化的中心 -6dB 衰减）。

    淡入：gain = sin(π/2 * t/T)，t∈[0,T]，从 0 升到 1。
    淡出：gain = cos(π/2 * t/T)，t∈[0,T]，从 1 降到 0。
    两个等功率淡化交叉时合成功率 ≈ 1.0（中心 -3dB，恒功率）。

    Args:
        num_samples: 该块样本数
        fade_in_samples: 淡入样本数
        fade_out_samples: 淡出样本数

    Returns:
        shape (num_samples,) 的 float64 增益包络。
    """
    env = np.ones(num_samples, dtype=np.float64)
    fi = max(0, min(int(fade_in_samples), num_samples))
    fo = max(0, min(int(fade_out_samples), num_samples))
    if fi > 0:
        t = np.linspace(0.0, 1.0, fi, dtype=np.float64)
        env[:fi] = np.sin(np.pi / 2.0 * t)
    if fo > 0:
        t = np.linspace(0.0, 1.0, fo, dtype=np.float64)
        env[-fo:] = np.cos(np.pi / 2.0 * t)
    return env


def smooth_fade_envelope(num_samples: int, fade_in_samples: int = 0,
                         fade_out_samples: int = 0) -> np.ndarray:
    """B16：smoothstep 淡化包络（两端切线水平，过渡更柔和）。

    淡入 gain = 3t²-2t³（t∈[0,1]）；淡出为其镜像 1-(3t²-2t³)。
    与 linear/equal_power 同一签名，可互换注入 ``apply_clip_fade``。
    """
    env = np.ones(num_samples, dtype=np.float64)
    fi = max(0, min(int(fade_in_samples), num_samples))
    fo = max(0, min(int(fade_out_samples), num_samples))
    if fi > 0:
        t = np.linspace(0.0, 1.0, fi, dtype=np.float64)
        env[:fi] = 3.0 * t * t - 2.0 * t * t * t
    if fo > 0:
        t = np.linspace(0.0, 1.0, fo, dtype=np.float64)
        env[-fo:] = 1.0 - (3.0 * t * t - 2.0 * t * t * t)
    return env


# B16：FadeCurve 形状 → 包络函数。None/未知 = 既有 equal_power（音频
# legacy 默认，保证旧工程逐样本不变）；"linear" 视听统一线性。
FADE_CURVE_ENVELOPES = {
    "linear": linear_fade_envelope,
    "equal_power": equal_power_fade_envelope,
    "smooth": smooth_fade_envelope,
}


def fade_curve_factor(shape: str | None, progress: float) -> float:
    """B16：归一化进度（0..1）→ 淡化增益（0..1），视频/音频共用定义。

    曲线沿各自 duration 的归一化进度定义（计划 B16）；``None`` 由
    调用方决定 legacy 默认（视频 linear / 音频 equal_power）——默认
    曲线保留旧结果，不引入隐式第二语义。增益值域 [0,1]，对 HDR
    像素只做透明度系数、无理由不 clamp 颜色数据。
    """
    p = 0.0 if progress < 0.0 else (1.0 if progress > 1.0 else progress)
    if shape == "equal_power":
        import math
        return math.sin(math.pi / 2.0 * p)
    if shape == "smooth":
        return 3.0 * p * p - 2.0 * p * p * p
    # "linear" 与 None 的视频默认
    return p


def apply_clip_fade(samples: np.ndarray, clip_timeline_start: int,
                    clip_timeline_duration: int, current_frame: int,
                    fade_in_frames: int, fade_out_frames: int,
                    fps: float, sample_rate: float,
                    envelope_fn=equal_power_fade_envelope,
                    fade_in_samples_override: int | None = None,
                    fade_out_samples_override: int | None = None,
                    audio_sample_offset: int = 0,
                    fade_curve: str | None = None,
                    fade_curve_in: str | None = None,
                    fade_curve_out: str | None = None) -> np.ndarray:
    """对一块样本应用 clip 淡入/淡出包络（AM-06）。

    根据当前块在 clip 时间线范围内的绝对采样位置生成包络并逐样本相乘。

    ``current_frame`` 是按块推进的时间线帧，而不是每个块内从 0 开始的
    局部淡化位置。旧实现只计算当前块覆盖了多少淡化样本，然后把包络
    每次都从 0 重新开始，连续播放时会听到每个音频块反复下坠再上升。
    这里直接用片段起点/终点推导每个样本的相对位置，保证跨块连续。

    F3.3 采样域语义：
    - ``fade_in/out_samples_override``：精确整数样本淡化长度（设置时
      覆盖帧域换算——帧→样本的浮点乘积不再参与边界，淡化第 N 样本的
      包络值精确可预测）。
    - ``audio_sample_offset``：clip 子帧位置偏移（clip 帧起点后的延迟
      样本数）——淡化区随内容整体右移，与 SampleSkipGate 的内容位移
      严格一致（预听/成片同刻）。

    Args:
        samples: (channels, n) 样本块
        clip_timeline_start: clip 在时间线的起始帧
        clip_timeline_duration: clip 时长（帧）
        current_frame: 当前块的起始时间线帧
        fade_in_frames: 淡入时长（帧，0=无）
        fade_out_frames: 淡出时长（帧，0=无）
        fps: 时间线帧率
        sample_rate: 采样率
        envelope_fn: 包络函数（linear_fade_envelope / equal_power_fade_envelope）
        fade_in_samples_override: 采样域淡入（None=用帧域换算）
        fade_out_samples_override: 采样域淡出（None=用帧域换算）
        audio_sample_offset: 子帧位置偏移（样本，内容延迟量）

    Returns:
        应用包络后的样本（同 shape）。
    """
    n = samples.shape[-1]
    if n == 0:
        return samples
    samples_per_frame = sample_rate / fps if fps > 0 else 0.0
    if samples_per_frame <= 0.0:
        return samples

    has_in = fade_in_frames > 0 or (
        fade_in_samples_override is not None and fade_in_samples_override > 0)
    has_out = fade_out_frames > 0 or (
        fade_out_samples_override is not None and fade_out_samples_override > 0)
    if not has_in and not has_out:
        return samples

    # Use one absolute sample coordinate for both edges.  The decoder normally
    # returns a block fully inside the clip, but clipping the coordinates keeps
    # this helper safe for a short final block or a seek boundary.
    # F3.3：子帧偏移让内容起点右移 offset 样本——淡化坐标同样右移。
    clip_sample_start = (
        (float(current_frame) - float(clip_timeline_start)) * samples_per_frame
        - float(audio_sample_offset))

    # 中间段（sustain）O(1) 早退：块完全不在任何淡化区 → 包络恒 1，无需
    # 逐样本计算。区间判定与下方完整路径的 mask 语义一致：
    #   淡入区 = 位置 < fade_in_samples；淡出区 = 位置 >= fade_out_start。
    if fade_in_samples_override is not None and fade_in_samples_override > 0:
        fade_in_samples = float(fade_in_samples_override)
    else:
        fade_in_samples = max(1.0, float(fade_in_frames) * samples_per_frame) \
            if fade_in_frames > 0 else 0.0
    clip_duration_samples = max(0.0, float(clip_timeline_duration) * samples_per_frame)
    if fade_out_samples_override is not None and fade_out_samples_override > 0:
        fade_out_samples = float(fade_out_samples_override)
    else:
        fade_out_samples = max(1.0, float(fade_out_frames) * samples_per_frame) \
            if fade_out_frames > 0 else 0.0
    fade_out_start = clip_duration_samples - fade_out_samples
    fade_in_active = has_in and clip_sample_start < fade_in_samples
    fade_out_active = has_out and \
        (clip_sample_start + float(n - 1)) >= fade_out_start
    if not fade_in_active and not fade_out_active:
        return samples

    sample_positions = clip_sample_start + np.arange(n, dtype=np.float64)
    env = np.ones(n, dtype=np.float64)

    def curve_values(t: np.ndarray, fade_in: bool) -> np.ndarray:
        """Evaluate the selected curve without restarting it per audio block."""
        t = np.clip(t, 0.0, 1.0)
        # B16：显式 fade_curve 优先（与视频同一形状定义）；缺省回落
        # envelope_fn（实时路径 legacy equal_power / 显式注入的 linear）。
        # R09：两端独立曲线优先（in/out 各自），再回落共享 fade_curve。
        per_end = fade_curve_in if fade_in else fade_curve_out
        chosen = per_end if per_end is not None else fade_curve
        selected = (FADE_CURVE_ENVELOPES.get(chosen)
                    if chosen is not None else None)
        if selected is smooth_fade_envelope:
            s = 3.0 * t * t - 2.0 * t * t * t
            return s if fade_in else 1.0 - s
        if (selected is equal_power_fade_envelope
                or (selected is None
                    and envelope_fn is equal_power_fade_envelope)):
            if fade_in:
                return np.sin(np.pi / 2.0 * t)
            return np.cos(np.pi / 2.0 * t)
        # linear（显式或缺省注入）与未知自定义曲线：可预测的线性回退
        return t if fade_in else 1.0 - t

    if has_in:
        fade_in_t = sample_positions / max(1.0, fade_in_samples - 1.0)
        fade_in_gain = curve_values(fade_in_t, fade_in=True)
        fade_in_mask = sample_positions < fade_in_samples
        env = np.where(fade_in_mask, fade_in_gain, env)

    if has_out:
        fade_out_t = (sample_positions - fade_out_start) / max(1.0, fade_out_samples - 1.0)
        fade_out_gain = curve_values(fade_out_t, fade_in=False)
        fade_out_mask = sample_positions >= fade_out_start
        # If the two fades overlap, the quieter envelope wins.  This avoids a
        # sudden gain bump in very short clips where both edges meet.
        env = np.where(fade_out_mask, np.minimum(env, fade_out_gain), env)

    return samples * env


# ─────────────────────────────────────────────────────────────────────────────
# 音频转场（Audio Transition / Crossfade）
# ─────────────────────────────────────────────────────────────────────────────
#
# 与 clip fade 的区别：clip fade 是单片段头尾淡化（不知道邻居）；
# crossfade 是相邻两片段在切换点附近的成对淡化，左右包络互补，
# 在 cut-point 模型下两片段时间上不重叠——等功率曲线保证交叉点功率恒定。
# 这里只负责"曲线形状"，区间/对齐/邻居关系由调用方
#（audio_playback.mix_samples / audio_graph_renderer.render_track_block）计算。

# cut-point 模型支持的曲线。保持与 audio_transition_catalog 的 curve 字段同步。
AUDIO_CROSSFADE_CURVES: frozenset = frozenset(
    {"constant_power", "linear", "equal_power", "fade"}
)


def crossfade_envelopes(curve: str, n: int) -> tuple[np.ndarray, np.ndarray]:
    """生成一段交叉淡化的左/右增益包络。

    左包络从 1 降到 0（左片段淡出），右包络从 0 升到 1（右片段淡入），
    两者定义域均为归一化进度 ``t∈[0,1]``（0=切换点左侧开头，1=切换点右侧结尾）。

    曲线（与 :mod:`audio_transition_catalog` 的 ``curve`` 字段对应）：
      - ``constant_power``: cos²/sin²，交叉点总功率≈1（-3dB），听感最自然。
      - ``linear``: 线性，交叉点功率≈0.5（-6dB），最轻量但有明显下坠。
      - ``equal_power``: √cos²/√sin²，标准 3dB 等功率变体。
      - ``fade``: 非交叉——左前半段淡出到 0，右后半段从 0 淡入，中间静音。

    Args:
        curve: 曲线名（见 :data:`AUDIO_CROSSFADE_CURVES`）。
        n: 跨度样本数（必须 >0）。

    Returns:
        ``(left_out_gain, right_in_gain)``，两个 shape ``(n,)`` 的 float64 数组。
        未知 curve 回退到 ``constant_power``（最安全的听感）。

    Raises:
        ValueError: ``n <= 0``。
    """
    if n <= 0:
        raise ValueError(f"crossfade length must be positive: {n}")
    t = np.linspace(0.0, 1.0, n, dtype=np.float64)
    if curve == "linear":
        left = 1.0 - t
        right = t
    elif curve == "equal_power":
        # 半角 sqrt 形式：left=√(1-t)，right=√t，交叉点功率≈1
        left = np.sqrt(1.0 - t)
        right = np.sqrt(t)
    elif curve == "fade":
        # 前半：左片段线性淡出到 0；后半：右片段从 0 线性淡入。
        # 中点附近两片段均为 0（静音间隙），因此命名为 fade 而非 crossfade。
        # 用 floor(n/2) 作为分界，保证两段各自在端点达到 0/1（避免 n 为奇数
        # 时半段斜率不对称导致端点不为整值）。
        half = n // 2
        idx = np.arange(n, dtype=np.float64)
        if half <= 0:
            # 极短跨度：退化为 constant_power（已有默认分支），避免除零。
            theta = np.pi / 2.0 * t
            left = np.cos(theta) ** 2
            right = np.sin(theta) ** 2
        else:
            left = np.where(idx < half, 1.0 - idx / half, 0.0)
            right = np.where(idx >= (n - half), (idx - (n - half - 1)) / half, 0.0)
    else:  # constant_power (default)
        # cos²/sin²：交叉点 left²+right² = 0.5+0.5 = 1.0（恒功率）。
        theta = np.pi / 2.0 * t
        left = np.cos(theta) ** 2
        right = np.sin(theta) ** 2
    return left, right


# ─────────────────────────────────────────────────────────────────────────────
# Crossfade 包络缓存
# ─────────────────────────────────────────────────────────────────────────────
# 同一 (curve, 跨度样本数) 的完整包络是确定性曲线：转场进行中的每个音频块
# 需要的都是同一条包络的不同切片。逐块重新生成意味着成本随转场时长线性
# 增长（5s 转场 = 24 万点三角函数，每块只用 ~2000 点，120 倍浪费）。缓存后
# 每块只剩切片索引，成本与转场时长无关。实时（playback/scrub）与离线
# （export/renderer）两条线程共享，加锁保护；返回数组只读共享，调用方
# （apply_crossfade_block）仅做花式索引不修改。
_CROSSFADE_ENV_CACHE: "OrderedDict[Tuple[str, int], Tuple[np.ndarray, np.ndarray]]" = OrderedDict()
_CROSSFADE_ENV_CACHE_LOCK = threading.Lock()
_CROSSFADE_ENV_CACHE_MAX_SAMPLES = 1_000_000  # 总样本上限（×2 数组 ×8B ≈ 16MB）


def _crossfade_envelopes_cached(curve: str, n: int) -> Tuple[np.ndarray, np.ndarray]:
    """带 LRU 缓存的 crossfade 包络（数值与 :func:`crossfade_envelopes` 一致）。"""
    key = (str(curve), int(n))
    with _CROSSFADE_ENV_CACHE_LOCK:
        cached = _CROSSFADE_ENV_CACHE.get(key)
        if cached is not None:
            _CROSSFADE_ENV_CACHE.move_to_end(key)
            return cached
    # 生成在锁外（三角函数较慢，避免长时间持锁）
    env = crossfade_envelopes(curve, n)
    if 2 * n <= _CROSSFADE_ENV_CACHE_MAX_SAMPLES:
        with _CROSSFADE_ENV_CACHE_LOCK:
            _CROSSFADE_ENV_CACHE[key] = env
            total = sum(2 * k[1] for k in _CROSSFADE_ENV_CACHE)
            while total > _CROSSFADE_ENV_CACHE_MAX_SAMPLES and len(_CROSSFADE_ENV_CACHE) > 1:
                evicted = _CROSSFADE_ENV_CACHE.popitem(last=False)
                total -= 2 * evicted[0][1]
    return env


def apply_crossfade_block(
    samples: np.ndarray,
    transition_start_frame: int,
    transition_duration_frames: int,
    current_frame: int,
    role: str,
    curve: str,
    fps: float,
    sample_rate: float,
) -> np.ndarray:
    """对一个样本块应用音频转场增益包络。

    与 :func:`apply_clip_fade` 同一套"绝对采样位置"思路，保证跨块连续：
    根据当前块在转场跨度内的绝对采样位置，从 :func:`crossfade_envelopes`
    生成完整包络后切片，再逐样本相乘。

    Args:
        samples: ``(channels, n)`` 样本块。
        transition_start_frame: 转场跨度起始时间线帧
            （= ``TransitionInstance.start_frame``）。
        transition_duration_frames: 转场跨度（帧 = ``duration``）。
        current_frame: 当前块的起始时间线帧。
        role: ``"left"``（淡出）或 ``"right"``（淡入）。
        curve: 曲线名（见 :data:`AUDIO_CROSSFADE_CURVES`）。
        fps: 时间线帧率。
        sample_rate: 采样率。

    Returns:
        应用包络后的样本（同 shape）。块完全在跨度外时原样返回。
    """
    n = samples.shape[-1]
    if n == 0:
        return samples
    samples_per_frame = sample_rate / fps if fps > 0 else 0.0
    if samples_per_frame <= 0.0 or transition_duration_frames <= 0:
        return samples

    span_samples = max(1, int(round(transition_duration_frames * samples_per_frame)))
    # 块在跨度内的绝对样本坐标（相对跨度起点）。
    block_start = (float(current_frame) - float(transition_start_frame)) * samples_per_frame
    block_positions = block_start + np.arange(n, dtype=np.float64)

    # 整块都在跨度外 → 无包络。
    if block_positions[-1] < 0.0 or block_positions[0] >= float(span_samples):
        return samples

    left_full, right_full = _crossfade_envelopes_cached(curve, span_samples)
    full = left_full if role == "left" else right_full

    # 切片：把块位置 clamp 到 [0, span-1] 取包络值，块外的样本增益为 1。
    env = np.ones(n, dtype=np.float64)
    in_span = (block_positions >= 0.0) & (block_positions < float(span_samples))
    if not np.any(in_span):
        return samples
    clamped = np.clip(block_positions[in_span], 0.0, float(span_samples - 1)).astype(np.int64)
    env[in_span] = full[clamped]
    return samples * env


# ─────────────────────────────────────────────────────────────────────────────
# AudioEffect 基类（FX 机架效果器统一接口）
# ─────────────────────────────────────────────────────────────────────────────

class AudioEffect:
    """音频效果器基类。

    约定所有 FX 机架效果器的统一接口，使 EffectChain 能透明地串联任意效果器。

    核心契约（与 StripEQFilter 一致，确保 AudioMixer.mix_samples 的样本流兼容）：
      - process(samples): 输入/输出 shape 均为 (channels, num_samples) 2D float；
        必须返回新数组，禁止原地修改输入（与 decoder buffer 共享安全）。
      - 旁路 / scipy 不可用 / 维度异常时原样返回输入。
      - set_params: 参数变化时触发系数/状态重算；sample_rate 变化时重置内部状态。
      - is_active: 是否启用并生效（用于短路跳过无谓拷贝）。

    线程安全：实例不共享（每轨一个），AudioMixer 在 _mutex 下访问。
    """

    # 子类覆盖：效果器类型标识（对应 TrackEffectInstance.effect_type / 注册中心）
    effect_type: str = ""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)

    def set_params(self, params: object, sample_rate: Optional[float] = None) -> None:
        """更新效果器参数。子类必须实现。"""
        raise NotImplementedError

    def process(self, samples: np.ndarray) -> np.ndarray:
        """处理一块样本。子类必须实现。"""
        raise NotImplementedError

    def reset_state(self) -> None:
        """重置内部状态（seek/暂停后调用，避免残留瞬态）。子类必须实现。"""
        raise NotImplementedError

    def tail_estimate_samples(self) -> int:
        """可闻尾音估计（样本数；MX-07 feed-zero drain 契约）。

        有状态效果器（Delay/Reverb）在输入停止后仍会继续输出（回声/混响
        尾巴）。引擎据此在"无 clip 但尾音未结束"时喂零块推进状态。
        0 = 无尾音（静态/瞬时效果，输入停即输出停）。
        上限由各效果器自行钳制（强反馈不得无限挂住调度）。
        """
        return 0

    @property
    def is_active(self) -> bool:
        """是否启用并生效（用于短路跳过无谓拷贝）。子类必须实现。"""
        raise NotImplementedError


#: R01：缓存"某效果类的 set_params 是否接受 algorithm_version kwarg"
_ACCEPTS_ALGORITHM_VERSION: dict[type, bool] = {}


def _set_params_accepts_version(effect: AudioEffect) -> bool:
    import inspect
    cls = type(effect)
    cached = _ACCEPTS_ALGORITHM_VERSION.get(cls)
    if cached is None:
        try:
            sig = inspect.signature(effect.set_params)
            cached = "algorithm_version" in sig.parameters
        except (TypeError, ValueError):  # pragma: no cover - 极端替身
            cached = False
        _ACCEPTS_ALGORITHM_VERSION[cls] = cached
    return cached


def _apply_params_with_version(effect: AudioEffect, params: object,
                               sample_rate: float,
                               algorithm_version: int) -> None:
    """EffectChain.set_params 的统一调用点（R01）。

    声明了 ``algorithm_version`` 形参的效果（StripEQFilter 等）收到版本，
    用于选择 legacy/current 兼容路径；未声明的旧效果器照旧调用（版本仅
    记录在模型层，不影响其行为——v3 对它们的声音语义未变的部分由各
    自的兼容实现负责）。
    """
    if _set_params_accepts_version(effect):
        effect.set_params(params, sample_rate=sample_rate,
                           algorithm_version=int(algorithm_version or 0))
    else:
        effect.set_params(params, sample_rate=sample_rate)


def _clamp_finite(value: Any, lo: float, hi: float, fallback: float) -> float:
    """FX dict 参数范围钳位 + NaN 防护（AUD-15304）。

    上游 ``TrackEffectInstance.params`` 是未校验的裸 dict（``from_dict`` 对
    params 只做 ``dict(...)``），工程文件反序列化内容可原样进入 DSP：
    ``ratio=0`` 会在 ``process()`` 热路径抛 ZeroDivisionError，负值/NaN 产生
    无声或异常结果，非数值直接让 set_params 崩溃。本助手把值钳到各效果器
    dataclass（timeline_v2）文档声明的范围；不可转换/非有限值回退 fallback
    （各效果器默认值）。

    Args:
        value: 裸 dict 中的原始值（Any）。
        lo/hi: 声明范围下/上界。
        fallback: value 不可用（类型错误/NaN/inf）时的回退默认值。

    Returns:
        钳位后的有限 float。
    """
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(fallback)
    if not np.isfinite(v):
        return float(fallback)
    return max(lo, min(hi, v))


# ─────────────────────────────────────────────────────────────────────────────
# 攻击/释放包络平滑器（Limiter / Compressor / MasterLimiter 共用）
# ─────────────────────────────────────────────────────────────────────────────

def _smooth_attack_release(
    x: np.ndarray,
    env_start: float,
    rise_c: float,
    fall_c: float,
) -> Tuple[np.ndarray, float]:
    """逐样本分支平滑的精确向量化实现。

    递推：``env[i] = x[i] + (env[i-1] - x[i]) * c``，其中 ``x[i] > env[i-1]``
    （信号上升）取 ``c = rise_c``，否则取 ``c = fall_c``。

    快路径（常系数分支，可被 lfilter 一步精确求解）：
      - 上升支：x 非降且 x[0] > env_start（包络全程低于 x，分支恒为 rise）
      - 下降支：x 非增且 x[0] <= env_start（包络全程不低于 x，分支恒为 fall）

    静音/衰减/平稳电平等常见块走快路径（一次 lfilter）；混合分支的块回退
    到逐样本循环（与旧实现完全相同的算式，结果逐位一致）。快路径与逐样本
    结果仅在浮点结合顺序上有 ~1 ulp 差异。

    Args:
        x: (n,) float64 驱动信号
        env_start: 起始包络状态（上一块末尾值）
        rise_c: 上升沿平滑系数（0=瞬时跟随）
        fall_c: 下降沿平滑系数

    Returns:
        (env, env_end)：平滑后的包络数组与块末状态。
    """
    n = x.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.float64), float(env_start)

    c: "float | None" = None
    if n == 1:
        c = rise_c if x[0] > env_start else fall_c
    else:
        diffs = x[1:] - x[:-1]
        if x[0] > env_start and not bool((diffs < 0.0).any()):
            c = rise_c
        elif x[0] <= env_start and not bool((diffs > 0.0).any()):
            c = fall_c

    if c is not None:
        if c <= 0.0:
            env = x.astype(np.float64, copy=True)
            return env, float(env[-1])
        if lfilter is not None:
            env, zf = lfilter(
                np.array([1.0 - c]), np.array([1.0, -c]), x,
                zi=np.array([c * env_start]),
            )
            return env, float(zf[0]) / c
        # lfilter 不可用：落入下方逐样本路径

    env = float(env_start)
    res: list = []
    append = res.append
    for v in x.tolist():
        if v > env:
            env = v + (env - v) * rise_c
        else:
            env = v + (env - v) * fall_c
        append(env)
    return np.fromiter(res, dtype=np.float64, count=n), env


# ─────────────────────────────────────────────────────────────────────────────
# 参数化 EQ 滤波器（6 频段全参量，CW-A1 重构）
# ─────────────────────────────────────────────────────────────────────────────

class StripEQFilter(AudioEffect):
    """6 频段全参量 EQ 滤波器。

    每个频段独立形状（HPF / Low Shelf / Bell / High Shelf / Notch / LPF）：
      - HPF/LPF：slope dB/oct → slope//6 阶 Butterworth-Q biquad 级联
      - Shelf/Bell：RBJ cookbook 系数（gain≈0 时该段跳过）
      - Notch：RBJ 陷波
    voicing 按调音台染色比例缩放 Q；任一频段 Solo 时仅应用 Solo 频段；
    末端统一 master_gain。

    系数按 StripEQParams 预算并缓存（参数 + 采样率变化时重算）。
    全部 biquad 段堆叠为 SOS 矩阵，每块一次 sosfilt 完成级联滤波；
    每段每声道维护 IIR 状态 zi（跨样本块连续，无边界咔哒声），
    状态按段身份（'b0'/'b0h1'...）在参数变化时对位保留。

    线程安全：实例不共享。AudioMixer 在 _mutex 保护下每轨一个实例。
    """

    effect_type = "strip_eq"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)
        self._params: Optional[StripEQParams] = None
        # R01：实例算法版本（None=直用无版本上下文→current 桶；整数按桶
        # 判定，legacy ≤2 走兼容系数；未来版本按 current 渲染，数据层不覆写）
        self._algorithm_version: Optional[int] = None
        # SOS 级联矩阵：(sections, 6)，行序 = 频段链
        self._sos: Optional[np.ndarray] = None
        # 每行的段身份（参数变化时按身份对位保留 zi）
        self._section_keys: tuple = ()
        # 每段每声道的 IIR 状态：{key: (channels, 2)}
        self._zi_by_key: dict = {}
        # 整体增益（linear）
        self._gain_linear: float = 1.0

    def set_params(self, params: object, sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        """更新 EQ 参数（触发系数重算）。

        Args:
            params: StripEQParams 或其 to_dict() 形式的 dict，None 表示旁路（直通）。
            sample_rate: 可选新采样率
            algorithm_version: R01 兼容处理器选择——整数 v ≤ 2 走 legacy
                系数设计（级联 Q=0.7071 HPF/LPF、S=1 Shelf、无增益-Q 耦合），
                使旧工程 HPF 响应不随本地 ALGORITHM_VERSION 升级漂移；
                None 保持现状（直用默认 current 桶）。
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._zi_by_key = {}
        if algorithm_version is not None:
            try:
                self._algorithm_version = max(0, int(algorithm_version))
            except (TypeError, ValueError):
                self._algorithm_version = None
        # dict 形式（来自 EffectChain / 序列化）转换为 StripEQParams
        if isinstance(params, dict):
            params = StripEQParams.from_dict(params)
        self._params = params
        self._recompute_coefficients()

    def _band_sections(self, band: dict, sr: float) -> list:
        """把单个频段 dict 展开为 SOS 行列表（EQ-01：与 UI 共享同一服务）。"""
        voicing = self._params.voicing if self._params else "Clean"
        return strip_eq_band_sections(
            band, sr, voicing=voicing,
            algorithm_version=self._algorithm_version)

    def _recompute_coefficients(self) -> None:
        """根据当前 params 和 sample_rate 重算所有系数（SOS 矩阵）。"""
        p = self._params
        if p is None or not p.enabled:
            self._sos = None
            self._section_keys = ()
            self._gain_linear = 1.0
            return

        sr = self._sample_rate
        # Solo：任一频段独奏时仅应用该频段（扫频监听语义）
        bands = [b for b in p.bands if b.get("enabled", True)]
        if any(b.get("solo", False) for b in bands):
            bands = [b for b in bands if b.get("solo", False)]

        rows: list = []
        keys: list = []
        for band in bands:
            band_rows = self._band_sections(band, sr)
            for k, row in enumerate(band_rows):
                key = f"b{band.get('index', 0)}" if len(band_rows) == 1 else f"b{band.get('index', 0)}h{k}"
                rows.append(row)
                keys.append(key)

        self._sos = np.array(rows, dtype=np.float64) if rows else None
        self._section_keys = tuple(keys)
        self._gain_linear = float(10.0 ** (p.master_gain / 20.0))

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块音频样本应用 EQ 链（一次 sosfilt 级联）。

        处理顺序：频段链 → master_gain。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            滤波后的 samples，同 shape（旁路时原样返回）。
        """
        p = self._params
        if p is None or not p.enabled or not _scipy_available():
            return samples

        if samples.ndim != 2 or samples.shape[0] < 1:
            return samples

        sos = self._sos
        if sos is None or sos.shape[0] == 0:
            # 仅整体增益，无滤波段
            if abs(self._gain_linear - 1.0) > 1e-9:
                out = samples.astype(np.float64, copy=True)
                out *= self._gain_linear
                return out.astype(samples.dtype, copy=False)
            return samples

        channels = samples.shape[0]
        out = samples.astype(np.float64, copy=True)

        # 堆叠各段状态（缺失/维度不符的段置零）
        zi = np.zeros((sos.shape[0], channels, 2), dtype=np.float64)
        for j, key in enumerate(self._section_keys):
            z = self._zi_by_key.get(key)
            if z is not None and z.shape == (channels, 2):
                zi[j] = z
        out, zf = sosfilt(sos, out, axis=-1, zi=zi)
        for j, key in enumerate(self._section_keys):
            self._zi_by_key[key] = zf[j]

        # 整体增益
        if abs(self._gain_linear - 1.0) > 1e-9:
            out *= self._gain_linear

        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置 IIR 状态（seek/暂停后调用，避免残留状态产生咔哒声）。"""
        self._zi_by_key = {}

    @property
    def is_active(self) -> bool:
        """是否启用并生效（有参数 + enabled + scipy 可用）。"""
        p = self._params
        return p is not None and p.enabled and _scipy_available()

    @property
    def is_backend_available(self) -> bool:
        """DSP 后端（scipy）是否可用。

        AM-01: 供 UI 区分"用户未启用 EQ"与"后端不可用导致静默旁路"。
        `is_active` 为 False 时，若本属性也为 False，则 EQ 实际上无法工作，
        UI 应显示降级提示而非"已激活"badge。
        """
        return _scipy_available()


# ─────────────────────────────────────────────────────────────────────────────
# Limiter 限幅器（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Limiter 默认参数（与 LimiterParams dataclass 保持一致，避免循环 import）
_LIMITER_DEFAULT_THRESHOLD_DB = -6.0
_LIMITER_DEFAULT_ATTACK_MS = 0.71
_LIMITER_DEFAULT_HOLD_MS = 0.0
_LIMITER_DEFAULT_RELEASE_MS = 93.0


class LimiterEffect(AudioEffect):
    """峰值限幅器（attack/hold/release brick-wall limiter）。

    超过阈值的信号被压到阈值附近，attack 时间内增益下降、release 时间
    指数恢复；hold 时间内增益冻结（不开始恢复）。所有声道联动（link
    channels），取最大绝对值作为包络，避免立体声声像漂移。

    GR 遥测：``last_gr_db`` 暴露块内最大增益衰减（正 dB 值），
    供 Dynamics 面板 GR 表经 EffectChain 收集。

    参数（LimiterParams，接收 dict）：
      - threshold_db: 阈值（dB），[-30, 0]
      - attack_ms:    增益下降时间（ms），[0.7, 30]
      - hold_ms:      增益冻结时间（ms），[0, 4000]
      - release_ms:   增益恢复时间（ms），[50, 4000]
      - enabled:      是否启用（False=旁路直通）

    线程安全：实例不共享。
    """

    effect_type = "limiter"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._threshold_db: float = _LIMITER_DEFAULT_THRESHOLD_DB
        self._attack_ms: float = _LIMITER_DEFAULT_ATTACK_MS
        self._hold_ms: float = _LIMITER_DEFAULT_HOLD_MS
        self._release_ms: float = _LIMITER_DEFAULT_RELEASE_MS
        self._enabled: bool = False
        # 峰值包络（标量，跨块连续）。声道联动，故只需一个值。
        self._envelope: float = 0.0
        # 增益平滑状态（1.0=不衰减；attack 作用于增益下降沿）
        self._gain_state: float = 1.0
        # hold 剩余样本计数（跨块）
        self._hold_remaining: int = 0
        self._release_coeff: float = self._compute_release_coeff(self._release_ms)
        self._attack_coeff: float = self._compute_release_coeff(self._attack_ms)
        # GR 遥测（块内最大衰减，正 dB）
        self.last_gr_db: float = 0.0
        # DY-03: 过冲报告（正 dB）——attack 平滑期间瞬态超出阈值的量，
        # 硬保护钳制介入时记录（0=未触发）。供面板/诊断显示失真风险。
        self.last_overshoot_db: float = 0.0

    @staticmethod
    def _compute_release_coeff(release_ms: float, sample_rate: float = 48000.0) -> float:
        """由时间常数（ms）计算每采样指数平滑系数（0..1，越小越快）。"""
        if release_ms <= 0.0 or sample_rate <= 0.0:
            return 0.0
        return float(np.exp(-1.0 / (release_ms * 0.001 * sample_rate)))

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新限幅器参数。

        Args:
            params: dict 形式的 LimiterParams，None 表示旁路。
            sample_rate: 可选新采样率
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._envelope = 0.0  # 采样率变化重置状态

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 LimiterParams 声明范围钳位（裸 dict 外部数据防护）。
        self._threshold_db = _clamp_finite(params.get("threshold_db", _LIMITER_DEFAULT_THRESHOLD_DB),
                                           -30.0, 0.0, _LIMITER_DEFAULT_THRESHOLD_DB)
        self._attack_ms = _clamp_finite(params.get("attack_ms", _LIMITER_DEFAULT_ATTACK_MS),
                                        0.7, 30.0, _LIMITER_DEFAULT_ATTACK_MS)
        self._hold_ms = _clamp_finite(params.get("hold_ms", _LIMITER_DEFAULT_HOLD_MS),
                                      0.0, 4000.0, _LIMITER_DEFAULT_HOLD_MS)
        self._release_ms = _clamp_finite(params.get("release_ms", _LIMITER_DEFAULT_RELEASE_MS),
                                         50.0, 4000.0, _LIMITER_DEFAULT_RELEASE_MS)
        self._enabled = bool(params.get("enabled", True))
        self._release_coeff = self._compute_release_coeff(self._release_ms, self._sample_rate)
        self._attack_coeff = self._compute_release_coeff(self._attack_ms, self._sample_rate)

    def _process_with_hold(self, peak_input: np.ndarray, threshold_linear: float) -> np.ndarray:
        """hold>0 时的逐样本路径：瞬时检测包络 → 目标增益 → hold 冻结增益恢复。

        信号超阈值时刷新 hold 计数；hold 期内增益冻结在限幅水平，hold
        结束后按 release 系数平滑恢复（无跳变）。attack 系数作用于增益
        下降沿（与无 hold 路径同构）。
        """
        n = peak_input.shape[0]
        gains = np.empty(n, dtype=np.float64)
        env = self._envelope
        gain = self._gain_state
        hold_left = self._hold_remaining
        att_c = self._attack_coeff
        rel_c = self._release_coeff
        hold_samples = int(round(self._hold_ms * 0.001 * self._sample_rate))
        thr = threshold_linear
        for i in range(n):
            x = peak_input[i]
            env = x if x > env else x + (env - x) * rel_c
            if x > thr:
                hold_left = hold_samples
            target = thr / env if env > thr else 1.0
            # 增益平滑：下降（target < gain）用 attack，恢复用 release；
            # hold 期内禁止恢复（只允许继续下降）
            if target < gain:
                gain = target + (gain - target) * att_c
            elif hold_left > 0:
                hold_left -= 1  # 增益冻结
            else:
                gain = target + (gain - target) * rel_c
            gains[i] = gain
        self._envelope = env
        self._gain_state = gain
        self._hold_remaining = hold_left
        return gains

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用限幅。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            限幅后的 samples，同 shape（旁路时原样返回）。
        """
        self.last_gr_db = 0.0
        self.last_overshoot_db = 0.0
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        threshold_linear = float(10.0 ** (self._threshold_db / 20.0))
        if threshold_linear <= 0.0:
            return samples

        out = samples.astype(np.float64, copy=True)
        peak_input = np.max(np.abs(out), axis=0)  # shape (n,)

        if self._hold_ms > 0.0:
            gain = self._process_with_hold(peak_input, threshold_linear)
        else:
            # 检测包络：瞬时 attack（brick-wall 语义——限幅器必须即时捕获
            # 瞬态），release 平滑恢复。
            envelope_arr, self._envelope = _smooth_attack_release(
                peak_input, self._envelope, 0.0, self._release_coeff)

            # 快路径：包络全程低于阈值且增益已完全恢复 → 直通。
            # 增益仍在恢复期（gain_state < 1）时必须继续走增益路径，
            # 否则块边界处增益会从恢复值跳回 1（可闻抽吸）。
            if not bool((envelope_arr > threshold_linear).any()) \
                    and self._gain_state >= 1.0 - 1e-9:
                return samples

            # 目标增益（检测域，瞬时）→ 增益平滑：下降（限幅介入）用
            # attack 系数、恢复用 release 系数（与 MasterLimiter 同构）。
            target_gain = np.where(
                envelope_arr > threshold_linear,
                threshold_linear / np.maximum(envelope_arr, 1e-30), 1.0)
            gain, self._gain_state = _smooth_attack_release(
                target_gain, self._gain_state,
                self._release_coeff, self._attack_coeff)

        # GR 遥测（正 dB）：块内最小增益 → 最大衰减
        min_gain = float(np.min(gain))
        if min_gain < 0.999999:
            self.last_gr_db = -20.0 * float(np.log10(max(min_gain, 1e-6)))

        # 同一 gain 应用到所有声道（link channels，保持声像）
        out *= gain[np.newaxis, :]
        # 硬保护：attack 期间瞬态可能短时超阈值（attack limiter 固有特性，
        # 完全消除需 look-ahead）。逐样本钳制（块无关——整块统一缩放会让
        # 保护量依赖块边界，破坏分块等价性）；增益已平滑，钳制极少触发。
        clip_ceiling = threshold_linear * 1.0001
        pre_clip_peak = float(np.max(np.abs(out))) if out.size else 0.0
        np.clip(out, -clip_ceiling, clip_ceiling, out=out)
        # DY-03: 过冲报告（钳制介入 = 增益未及收敛的超阈瞬态）
        if pre_clip_peak > threshold_linear:
            self.last_overshoot_db = 20.0 * float(np.log10(
                pre_clip_peak / threshold_linear))
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置包络状态（seek/暂停后调用）。"""
        self._envelope = 0.0
        self._gain_state = 1.0
        self._hold_remaining = 0
        self.last_gr_db = 0.0
        self.last_overshoot_db = 0.0

    @property
    def is_active(self) -> bool:
        return self._enabled


# ─────────────────────────────────────────────────────────────────────────────
# Compressor 压缩器（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Compressor 默认参数（与 CompressorParams dataclass 一致，避免循环 import）
_COMP_DEFAULT_THRESHOLD_DB = -15.0
_COMP_DEFAULT_RATIO = 2.5
_COMP_DEFAULT_ATTACK_MS = 1.4
_COMP_DEFAULT_HOLD_MS = 0.0
_COMP_DEFAULT_RELEASE_MS = 93.0
_COMP_DEFAULT_KNEE = 15.0
_COMP_DEFAULT_MIX = 100.0
_COMP_DEFAULT_MAKEUP_DB = 0.0

# 软拐点宽度（dB），用于 knee 区域的平滑过渡计算
_KNEE_HALF_WIDTH_FACTOR = 0.5
# knee 百分比（0..100）→ 拐点宽度（dB）的映射上限
_KNEE_MAX_DB = 24.0


class CompressorEffect(AudioEffect):
    """动态范围压缩器（feed-forward 峰值包络 + 软拐点 + 并行混合）。

    信号超过 threshold 时按 ratio 压缩，knee（百分比→0..24 dB 宽度）平滑
    过渡。attack/release 控制包络跟随速度，hold 冻结包络恢复。mix 并行
    混合干/湿信号（upward 多段压缩风格）。makeup_gain 补偿输出电平。
    所有声道联动（link channels），避免声像漂移。

    侧链（CW-A1）：``process(samples, sidechain=...)`` 提供检源缓冲
    （(channels, n)）时以检源包络驱动增益（sc_on=True 前提由引擎保证）；
    ``sc_listen=True`` 时直接广播检源信号作为输出（audition）。

    遥测：``last_gr_db``（块内最大衰减，正 dB）与
    ``last_detector_db``（检源/本链探测电平 dBFS）供 Dynamics 面板表头。

    线程安全：实例不共享。
    """

    effect_type = "compressor"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._threshold_db: float = _COMP_DEFAULT_THRESHOLD_DB
        self._ratio: float = _COMP_DEFAULT_RATIO
        self._attack_ms: float = _COMP_DEFAULT_ATTACK_MS
        self._hold_ms: float = _COMP_DEFAULT_HOLD_MS
        self._release_ms: float = _COMP_DEFAULT_RELEASE_MS
        self._knee: float = _COMP_DEFAULT_KNEE
        self._mix: float = _COMP_DEFAULT_MIX
        self._makeup_db: float = _COMP_DEFAULT_MAKEUP_DB
        self._sc_on: bool = False
        self._sc_listen: bool = False
        self._sc_source: str = "internal"
        self._enabled: bool = False
        # 包络（线性域峰值跟随，跨块连续）。声道联动，故只需一个值。
        # 初值 0（静默起点）；增益计算前转 dB（避免 -inf 在 dB 平滑中传播）。
        self._env_lin: float = 0.0
        # hold 剩余样本计数（跨块）
        self._hold_remaining: int = 0
        self._attack_coeff: float = self._time_to_coeff(self._attack_ms)
        self._release_coeff: float = self._time_to_coeff(self._release_ms)
        self._makeup_linear: float = 1.0
        self._wet_mix: float = 1.0
        # 遥测
        self.last_gr_db: float = 0.0
        self.last_detector_db: float = -100.0

    @staticmethod
    def _time_to_coeff(time_ms: float, sample_rate: float = 48000.0) -> float:
        """由时间常数（ms）计算每采样指数平滑系数（0..1，越小越快）。

        标准 RC 低通式： coeff = exp(-1 / (time_ms * 0.001 * sr))
        time_ms=0 → coeff=0（瞬时跟随）。
        """
        if time_ms <= 0.0 or sample_rate <= 0.0:
            return 0.0
        return float(np.exp(-1.0 / (time_ms * 0.001 * sample_rate)))

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新压缩器参数。

        Args:
            params: dict 形式的 CompressorParams，None 表示旁路。
            sample_rate: 可选新采样率
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._env_lin = 0.0  # 采样率变化重置状态

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 CompressorParams 声明范围钳位。未钳位时 ratio=0 会在
        # _compute_gain_db 的 1.0/ratio 抛 ZeroDivisionError（渲染线程），
        # 负 ratio/NaN 产生无声或异常增益；损坏/手改工程文件可直接触发。
        self._threshold_db = _clamp_finite(params.get("threshold_db", _COMP_DEFAULT_THRESHOLD_DB),
                                           -60.0, 0.0, _COMP_DEFAULT_THRESHOLD_DB)
        self._ratio = _clamp_finite(params.get("ratio", _COMP_DEFAULT_RATIO),
                                    1.2, 20.0, _COMP_DEFAULT_RATIO)
        self._attack_ms = _clamp_finite(params.get("attack_ms", _COMP_DEFAULT_ATTACK_MS),
                                        0.7, 1000.0, _COMP_DEFAULT_ATTACK_MS)
        self._hold_ms = _clamp_finite(params.get("hold_ms", _COMP_DEFAULT_HOLD_MS),
                                      0.0, 4000.0, _COMP_DEFAULT_HOLD_MS)
        self._release_ms = _clamp_finite(params.get("release_ms", _COMP_DEFAULT_RELEASE_MS),
                                         50.0, 5000.0, _COMP_DEFAULT_RELEASE_MS)
        # 旧 knee_db 键（dB 值）映射到百分比宽度
        knee_val = params.get("knee", params.get("knee_db", _COMP_DEFAULT_KNEE))
        if "knee_db" in params and "knee" not in params:
            knee_val = _clamp_finite(knee_val, 0.0, 40.0, _COMP_DEFAULT_KNEE) / 40.0 * 100.0
        self._knee = _clamp_finite(knee_val, 0.0, 100.0, _COMP_DEFAULT_KNEE)
        self._mix = _clamp_finite(params.get("mix", _COMP_DEFAULT_MIX),
                                  0.0, 100.0, _COMP_DEFAULT_MIX)
        self._makeup_db = _clamp_finite(params.get("makeup_gain", _COMP_DEFAULT_MAKEUP_DB),
                                        0.0, 24.0, _COMP_DEFAULT_MAKEUP_DB)
        self._sc_on = bool(params.get("sc_on", False))
        self._sc_listen = bool(params.get("sc_listen", False))
        self._sc_source = str(params.get("sc_source", "internal") or "internal")
        self._enabled = bool(params.get("enabled", True))
        self._attack_coeff = self._time_to_coeff(self._attack_ms, self._sample_rate)
        self._release_coeff = self._time_to_coeff(self._release_ms, self._sample_rate)
        self._makeup_linear = float(10.0 ** (self._makeup_db / 20.0))
        self._wet_mix = self._mix / 100.0

    @property
    def knee_db(self) -> float:
        """knee 百分比 → 拐点宽度（dB，0..24）。"""
        return self._knee / 100.0 * _KNEE_MAX_DB

    @property
    def sc_source(self) -> str:
        """侧链源标识（"internal" 或 track_id），供引擎解析检源缓冲。"""
        return self._sc_source if self._sc_on else "internal"

    def _compute_gain_db(self, input_db: np.ndarray) -> np.ndarray:
        """根据输入电平（dB）计算压缩增益（dB）。

        soft-knee 压缩曲线：
          - 低于 (threshold - knee/2)：不压缩（gain=0）
          - 在 knee 区域 [threshold - knee/2, threshold + knee/2]：平滑过渡
          - 高于 (threshold + knee/2)：按 ratio 线性压缩
        """
        threshold = self._threshold_db
        ratio = self._ratio
        knee_w = self.knee_db
        if knee_w < 1e-6:
            gain_db = np.where(input_db > threshold,
                               (input_db - threshold) * (1.0 / ratio - 1.0),
                               0.0)
            return gain_db
        knee_half = knee_w * _KNEE_HALF_WIDTH_FACTOR
        knee_lower = threshold - knee_half
        knee_upper = threshold + knee_half

        gain_db = np.zeros_like(input_db)

        # 高于 knee 上界：完整压缩
        above = input_db > knee_upper
        gain_db[above] = (input_db[above] - threshold) * (1.0 / ratio - 1.0)

        # 在 knee 区域：二次平滑过渡
        in_knee = (input_db > knee_lower) & (input_db <= knee_upper)
        if np.any(in_knee):
            # soft-knee 公式（标准实现）：
            # gain = -(x - threshold + knee/2)^2 / (2 * knee) * (1 - 1/ratio)
            x = input_db[in_knee]
            gain_db[in_knee] = -((x - knee_lower) ** 2) / (2.0 * knee_w) * (1.0 - 1.0 / ratio)

        return gain_db

    def _smooth_env(self, detector_linear: np.ndarray) -> np.ndarray:
        """包络平滑（DY-02/DY-03：线性域峰值跟随 → dB，检测域固定唯一）。

        attack/release 分支平滑作用于线性幅度；返回 dB 域包络数组。
        hold>0 时用线性域冻结逐样本路径（超 knee 下界刷新 hold 计数）。
        """
        if self._hold_ms <= 0.0:
            env_lin, self._env_lin = _smooth_attack_release(
                detector_linear, self._env_lin,
                self._attack_coeff, self._release_coeff)
            return 20.0 * np.log10(np.maximum(env_lin, 1e-9))
        # hold 路径：包络上升 attack 平滑；进入开放态刷新 hold 计数，
        # hold 期内冻结；随后 release 平滑下降（无恢复跳变）。
        n = detector_linear.shape[0]
        env_arr = np.empty(n, dtype=np.float64)
        env = self._env_lin
        hold_left = self._hold_remaining
        att_c = self._attack_coeff
        rel_c = self._release_coeff
        hold_samples = int(round(self._hold_ms * 0.001 * self._sample_rate))
        knee_lower_lin = 10.0 ** (
            (self._threshold_db - self.knee_db * _KNEE_HALF_WIDTH_FACTOR) / 20.0)
        for i in range(n):
            x = detector_linear[i]
            if x > env:
                env = x + (env - x) * att_c
                if x > knee_lower_lin:
                    hold_left = hold_samples
            elif hold_left > 0:
                hold_left -= 1  # 冻结
            else:
                env = x + (env - x) * rel_c
            env_arr[i] = env
        self._env_lin = env
        self._hold_remaining = hold_left
        return 20.0 * np.log10(np.maximum(env_arr, 1e-9))

    def process(self, samples: np.ndarray, sidechain: Optional[np.ndarray] = None) -> np.ndarray:
        """对一块样本应用压缩。

        Args:
            samples: shape (channels, num_samples)，float32/float64
            sidechain: 侧链检源缓冲（(channels, n)），None/不可用时用本链探测

        Returns:
            压缩后的 samples，同 shape（旁路时原样返回）。
        """
        self.last_gr_db = 0.0
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 探测信号：外部侧链（sc_on + 引擎提供检源）或本链（声道联动峰值）
        sc_buf = None
        if sidechain is not None and isinstance(sidechain, np.ndarray) \
                and sidechain.ndim == 2 and sidechain.shape[1] == n:
            sc_buf = sidechain
        detector = np.max(np.abs((sc_buf if sc_buf is not None else out)
                                 .astype(np.float64, copy=False)), axis=0)
        # 探测电平遥测（dBFS）
        det_peak = float(np.max(detector)) if detector.size else 0.0
        self.last_detector_db = max(-100.0, 20.0 * float(np.log10(max(det_peak, 1e-10))))

        # 侧链监听（DY-04）：输出 = 检源**原始音频**（保留波形极性），
        # 不播放整流后的检测量（旧实现把 abs 检测量广播出去，±0.5 信号
        # 变成全正 DC）。内部检源监听 = 本链输入原样直通。
        if self._sc_listen:
            if sc_buf is not None:
                for ch in range(channels):
                    out[ch] = sc_buf[ch % sc_buf.shape[0]]
                return out.astype(samples.dtype, copy=False)
            return samples

        # 包络平滑（DY-02：线性域峰值跟随，检测域唯一），返回 dB 域
        env_smoothed = self._smooth_env(detector)

        # 快路径：包络全程低于 knee 下界 → 增益恰为 0dB（无压缩）。
        # 状态已照常推进，数学上与完整计算逐点等价。
        knee_lower = self._threshold_db - self.knee_db * _KNEE_HALF_WIDTH_FACTOR
        if not bool((env_smoothed > knee_lower).any()):
            if abs(self._makeup_linear - 1.0) > 1e-9:
                out *= self._makeup_linear
                return out.astype(samples.dtype, copy=False)
            return samples

        # 向量化计算压缩增益 + 并行混合（mix<100 时保留干信号比例）
        gain_db = self._compute_gain_db(env_smoothed)
        gain_linear = 10.0 ** (gain_db / 20.0)
        wet = self._wet_mix
        if wet < 0.999:
            eff_gain = (1.0 - wet) + wet * gain_linear
        else:
            eff_gain = gain_linear

        # GR 遥测（正 dB）：块内最大衰减（按湿链路计）
        min_gain = float(np.min(gain_linear))
        if min_gain < 0.999999:
            self.last_gr_db = -20.0 * float(np.log10(max(min_gain, 1e-6)))

        # 应用增益 + makeup
        out *= eff_gain[np.newaxis, :]
        if abs(self._makeup_linear - 1.0) > 1e-9:
            out *= self._makeup_linear

        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置包络状态（seek/暂停后调用）。"""
        self._env_lin = 0.0
        self._hold_remaining = 0
        self.last_gr_db = 0.0
        self.last_detector_db = -100.0

    @property
    def is_active(self) -> bool:
        return self._enabled


# ─────────────────────────────────────────────────────────────────────────────
# Reverb 声学房间混响器（CW-A1 重构）
# ─────────────────────────────────────────────────────────────────────────────

# 声速（m/s）：距离 → 延迟换算
_SPEED_OF_SOUND = 343.0

# Schroeder 经典结构参数（Freeverb 风格）：L 组梳状延迟 / 串联全通延迟与反馈
_COMB_DELAY_MS = (29.7, 37.1, 41.1, 43.7)
_ALLPASS_DELAY_MS = (5.0, 1.7)
_ALLPASS_FEEDBACK = 0.7

# 早反射抽头权重（与 IR 图谱一致的确定性权重簇）
_ER_TAP_WEIGHTS = (0.85, 0.68, 0.76, 0.55, 0.64, 0.48, 0.58, 0.42, 0.50, 0.38)

# L/R 去相关的第二组梳状延迟（相对经典组按素数比例偏移）
_COMB_DELAY_MS_R = (31.9, 39.7, 43.9, 46.7)

# 微调制 LFO（Hz，L/R 相位差）：梳状延迟时间微摆（chorus 柔化）
_MOD_LFO_HZ = (0.9, 1.1)
_MOD_MAX_DEPTH_SAMPLES = 24.0


def _db_to_lin_gain(db: float) -> float:
    """dB → 线性增益（≤ -59.5 dB 视为 -inf → 0）。"""
    if db <= -59.5:
        return 0.0
    return float(10.0 ** (db / 20.0))


class ReverbEffect(AudioEffect):
    """声学房间混响器（直达 / 早反射簇 / 晚期 FDN + 双级音色 + 微调制）。

    拓扑（与 IR 衰减图谱一致；RV-03 后晚期为 8 线正交 FDN）：
      input → [直达 direct_db ×(1-w)]
            → [湿输入环形缓冲（mono 求和）]
              ├─ ER：有效传播+镜像附加行程的 image-source 抽头簇（RV-02）
              │   → ER lo/hi shelf 染色 → er_db ×w
              └─ Late：pre_delay 起始（时间微调制 chorus，mod=0 无调制）
                  → 8 线正交 FDN（Householder；每线 RT60 校准增益+阻尼）
                  → L/R 互补线组去相关读出 → Rev lo/hi shelf 染色 → reverb_db ×w
    dry_wet 是直达声 vs (ER+Late) 的交叉淡化。负 pre_delay 有效值钳为 0
    （物理上早反射不可能先于直达声；UI 双极旋钮保留自由度）。

    性能：梳/全通按块向量化（复用 _CombFilter/_AllpassFilter），环形缓冲
    读取用 np.take 整数索引一次完成，音色 shelf 每声道一次 lfilter。

    线程安全：实例不共享。
    """

    effect_type = "reverb"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: Optional[dict] = None
        self._enabled: bool = False
        # 湿输入环形缓冲（mono）
        self._ring: Optional[np.ndarray] = None
        self._ring_size: int = 0
        self._ring_write: int = 0
        self._ring_history_samples: int = 0
        # L/R FDN（每声道独立 comb + allpass——共享 allpass 会让 R 声道
        # 继承 L 的扩散状态，产生块首泄漏与跨块交错差异）
        self._combs_l: list = []
        self._combs_r: list = []
        self._allpasses_l: list = []
        self._allpasses_r: list = []
        # RV-03: 8 线正交 FDN（替代上面的双 Schroeder 组；字段保留仅为
        # _rebuild 变更检测哨兵——见 _rebuild 注释）
        self._fdn8: Optional[_FDNNetwork8] = None
        # 音色 shelf（每声道 [lo, hi] 两段）
        self._er_shelf_zi: Optional[np.ndarray] = None
        self._rev_shelf_zi: Optional[np.ndarray] = None
        self._er_sos: Optional[np.ndarray] = None
        self._rev_sos: Optional[np.ndarray] = None
        # 当前生效的派生量（变更检测）
        self._er_taps_l: np.ndarray = np.zeros(0, dtype=np.int64)
        self._er_taps_r: np.ndarray = np.zeros(0, dtype=np.int64)
        # RV-02: L/R 独立权重（镜像源横向位置决定方向分配）
        self._er_weights_l: np.ndarray = np.array(_ER_TAP_WEIGHTS)
        self._er_weights_r: np.ndarray = np.array(_ER_TAP_WEIGHTS)
        self._late_offset_l: float = 0.0
        self._late_offset_r: float = 0.0
        self._mod_phase: float = 0.0
        # 先落默认参数再重建：保证首次 set_params 的变更检测有正确基准
        self._params = self._default_params_dict()
        self._enabled = False
        self._rebuild(self._params)

    # ── 参数 ─────────────────────────────────────────────────────

    @staticmethod
    def _default_params_dict() -> dict:
        return {
            "height": 10.0, "length": 30.0, "width": 40.0,
            "pre_delay": 42.0, "reverb_time": 2730.0, "distance": 19.87,
            "brightness": 50.0, "modulation": 2.0,
            "er_lo_freq": 300.0, "er_hi_freq": 10000.0,
            "er_lo_gain": 0.0, "er_hi_gain": -6.1,
            "rev_lo_freq": 291.0, "rev_hi_freq": 11100.0,
            "rev_lo_gain": 1.1, "rev_hi_gain": -2.2,
            "dry_wet": 100.0, "direct_db": 0.0, "er_db": 0.0,
            "reverb_db": 0.0, "enabled": True,
        }

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新混响器参数（房间几何/延迟变更重建抽头，其余就地更新）。"""
        sr_changed = sample_rate is not None and sample_rate != self._sample_rate
        if sr_changed:
            self._sample_rate = float(sample_rate)

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 ReverbParams 声明范围钳位（裸 dict 外部数据防护）
        p = self._default_params_dict()
        ranges = {
            "height": (2.0, 30.0), "length": (3.0, 80.0), "width": (3.0, 80.0),
            "pre_delay": (-200.0, 200.0), "reverb_time": (100.0, 4000.0),
            "distance": (0.0, 30.0), "brightness": (6.0, 100.0),
            "modulation": (0.0, 100.0),
            "er_lo_freq": (150.0, 500.0), "er_hi_freq": (1000.0, 16000.0),
            "er_lo_gain": (-18.0, 6.0), "er_hi_gain": (-18.0, 6.0),
            "rev_lo_freq": (150.0, 500.0), "rev_hi_freq": (1000.0, 16000.0),
            "rev_lo_gain": (-18.0, 6.0), "rev_hi_gain": (-18.0, 6.0),
            "dry_wet": (0.0, 100.0), "direct_db": (-60.0, 6.0),
            "er_db": (-60.0, 6.0), "reverb_db": (-60.0, 6.0),
        }
        for key, (lo, hi) in ranges.items():
            p[key] = _clamp_finite(params.get(key, p[key]), lo, hi, p[key])
        p["enabled"] = bool(params.get("enabled", True))

        old = self._params or {}
        geometry_changed = sr_changed or any(
            abs(p[k] - old.get(k, p[k])) > 1e-6
            for k in ("height", "length", "width", "pre_delay", "distance", "reverb_time"))
        # RV-01：brightness 消费于 comb 反馈阻尼（_apply_fdn_params），
        # 必须与 shelf 音色分离——运行中改变时**就地更新阻尼**，不清空网络
        # 状态（旧实现把 brightness 归入 tone_changed 只重建 shelf，
        # 阻尼纹丝不动：50→100 输出差 0）。
        brightness_changed = abs(p["brightness"] - old.get("brightness", p["brightness"])) > 1e-6
        tone_changed = any(
            abs(p[k] - old.get(k, p[k])) > 1e-6
            for k in ("er_lo_freq", "er_hi_freq", "er_lo_gain", "er_hi_gain",
                      "rev_lo_freq", "rev_hi_freq", "rev_lo_gain", "rev_hi_gain"))
        self._params = p
        self._enabled = p["enabled"]
        if geometry_changed:
            # RV-04: 几何变化不再整体重建网络——comb/allpass 拓扑由固定
            # 常量决定（房间尺寸只影响 ER 抽头/晚期偏移），缓冲与尾音
            # 原样保留；仅在采样率变化（系数失配）时才重置网络。
            self._rebuild(p, reset_network=sr_changed)
        elif brightness_changed:
            # 就地更新 RT60/阻尼（comb 缓冲与 allpass 状态全部保留）
            self._apply_fdn_params(p)
            if tone_changed:
                self._rebuild_tone(p)
        elif tone_changed:
            self._rebuild_tone(p)

    # ── 结构重建 ─────────────────────────────────────────────────

    def _rebuild(self, p: dict, reset_network: bool = False) -> None:
        """按当前参数重建延迟结构（RV-04：网络缓冲跨几何变化保留）。

        comb/allpass 拓扑由固定常量决定，几何参数只影响 ER 抽头与晚期
        偏移——重置网络仅在采样率变化（系数失配）或首次构建时发生。
        环形缓冲只增不缩（容量不足才重新分配）。
        """
        sr = self._sample_rate
        # RV-03: 晚期网络 = 8 线正交 FDN（重建仅发生在采样率变化/首次构建，
        # RV-04 热更新语义保持：几何/RT60/brightness 拖动不清网络缓冲）
        if reset_network or self._fdn8 is None or \
                getattr(self._fdn8, "_min_delay", 0) <= 0:
            self._fdn8 = _FDNNetwork8(sr)
            self._er_shelf_zi = None
            self._rev_shelf_zi = None
        (self._er_taps_l, self._er_taps_r,
         self._er_weights_l, self._er_weights_r,
         self._late_offset_l, self._late_offset_r) = self._compute_taps(p)
        # Image-source paths can exceed the old room-mean estimate. Size the
        # history from the actual taps, including fractional-read interpolation
        # and the full modulation range (modulation may change without rebuild).
        er_max = max(int(self._er_taps_l.max()), int(self._er_taps_r.max()))
        late_max = int(np.ceil(max(self._late_offset_l, self._late_offset_r)
                               + _MOD_MAX_DEPTH_SAMPLES)) + 2
        self._ring_history_samples = max(er_max, late_max)
        self._ensure_ring_capacity(self._ring_history_samples + 8,
                                   reset_history=reset_network)
        self._apply_fdn_params(p)
        self._rebuild_tone(p)

    def _ensure_ring_capacity(self, required: int,
                              reset_history: bool = False) -> None:
        """Grow without discarding chronological history; reset on rate changes.

        The whole input block is written before taps are read, so capacity must
        cover both the oldest requested sample and the entire incoming block.
        On growth, place old history at the end of the new ring and resume at
        zero; older, previously unavailable history remains zero-filled.
        """
        if (not reset_history and self._ring is not None
                and self._ring_size >= required):
            return
        size = max(int(required), 4096)
        ring = np.zeros(size, dtype=np.float64)
        if self._ring is not None and not reset_history:
            old_size = self._ring_size
            write = self._ring_write
            first = old_size - write
            start = size - old_size
            ring[start:start + first] = self._ring[write:]
            ring[start + first:] = self._ring[:write]
        self._ring = ring
        self._ring_size = size
        self._ring_write = 0
        if reset_history:
            self._mod_phase = 0.0

    def _compute_taps(self, p: dict) -> tuple:
        """RV-02：矩形房间 image-source ER + 正负 Pre Delay 传播模型。

        - 一阶镜像（每维近/远墙，6 个）+ 有限阶角镜像（xy/xz/yz/xyz，4 个）
          共 10 个抽头，按附加路径排序；各维独立决定反射路径与延迟
          （同均值不同形状产生不同 ER 分布）；
        - 抽头延迟 = 有效传播 + (镜像路径 − 直达几何路径)/c（反射相对直达
          的附加行程；不与 distance 重复计程）；
        - 有效传播时间 effective = max(0, pre_delay_s + distance/c)：负
          pre-delay 抵消距离传播时延（旧实现把整个负区间钳 0）；
        - 权重 ∝ 1/镜像路径（几何扩散），整体能量归一到 _ER_TAP_WEIGHTS
          尺度；L/R 对称权重 + ±2% 确定性延迟去相关（对称输入对称输出，
          结构对称契约保持）；
        - 晚期起始只随 pre_delay（Distance 只改 ER，不移动 Late——旧实现
          无意中把整个晚期随距离平移）。

        Returns:
            (taps_l, taps_r, weights_l, weights_r, late_offset_l, late_offset_r)
        """
        sr = self._sample_rate
        h, l, w = float(p["height"]), float(p["length"]), float(p["width"])
        room = np.array([l, w, h])
        src = np.array([l * 0.4, w * 0.5, h * 0.5])
        lis = np.array([l * 0.6, w * 0.5, h * 0.5])
        # 一阶镜像（每维近/远墙）
        images = []
        for axis in range(3):
            for far_wall in (False, True):
                img = src.copy()
                img[axis] = (2.0 * room[axis] - src[axis] if far_wall
                             else -src[axis])
                images.append(img)
        # 有限阶补充：角镜像（xy/xz/yz 面 + xyz 体对角，取远墙角）
        for axes in ((0, 1), (0, 2), (1, 2), (0, 1, 2)):
            img = src.copy()
            for axis in axes:
                img[axis] = 2.0 * room[axis] - src[axis]
            images.append(img)
        imgs = np.array(images)
        paths = np.linalg.norm(imgs - lis, axis=1)
        direct = float(np.linalg.norm(lis - src))
        extra = paths - direct  # 反射相对直达的附加行程
        order = np.argsort(extra)
        extra_sorted = extra[order]
        paths_sorted = paths[order]

        # 权重：几何扩散 1/path × 抽头序曲线，整体能量归一；L/R 对称
        base_w = np.array(_ER_TAP_WEIGHTS, dtype=np.float64)
        weights = base_w * (paths_sorted[0] / np.maximum(paths_sorted, 1e-6))
        wsum = float(weights.sum())
        if wsum > 0.0:
            weights = weights * (float(base_w.sum()) / wsum)
        weights_l = weights
        weights_r = weights.copy()

        # 有效传播：负 pre-delay 抵消距离时延（RV-02 候选公式）
        pre_s = float(p["pre_delay"]) * 0.001
        dist_s = float(p["distance"]) / _SPEED_OF_SOUND
        effective = max(0.0, pre_s + dist_s)
        delays_s = effective + extra_sorted / _SPEED_OF_SOUND
        # L/R ±2% 确定性延迟去相关（对称权重 + 延迟抖动；与旧实现同型）
        k = np.arange(1, len(delays_s) + 1)
        taps_l = np.maximum(
            1, np.round(delays_s * (1.0 + 0.02 * np.sin(k * 2.1))
                        * sr)).astype(np.int64)
        taps_r = np.maximum(
            1, np.round(delays_s * (1.0 - 0.02 * np.cos(k * 1.7))
                        * sr)).astype(np.int64)

        # 晚期起始：只随 pre_delay（distance 不移动晚期）+ 房间自然建声
        room_mean = (h + l + w) / 3.0
        span = max(0.045, room_mean * 3.8 * 0.001)
        late_base = max(0.0, pre_s) + span * 0.88
        late_l = late_base * sr
        late_r = late_l * 1.007
        return taps_l, taps_r, weights_l, weights_r, float(late_l), float(late_r)

    def _apply_fdn_params(self, p: dict) -> None:
        """reverb_time(RT60) / brightness(阻尼) 就地应用到 8 线 FDN（RV-03）。"""
        rt60_s = p["reverb_time"] * 0.001
        damping_hz = 800.0 + (p["brightness"] / 100.0) * 15200.0
        damping_linear = _damping_hz_to_linear(damping_hz, self._sample_rate)
        self._fdn8.set_params(rt60_s, damping_linear, self._sample_rate)

    def _rebuild_tone(self, p: dict) -> None:
        """重建 ER/Rev 音色 shelf（lo+hi 各两段，SOS）。"""
        def _sos(lo_f: float, hi_f: float, lo_g: float, hi_g: float) -> np.ndarray:
            rows = []
            for f, g in ((lo_f, lo_g), (hi_f, hi_g)):
                if abs(g) < 0.05:
                    continue
                f_clamped = min(max(10.0, f), self._sample_rate * 0.45)
                if g >= 0 or lo_f == f:
                    b, a = _biquad_lowshelf(f_clamped, g, self._sample_rate)
                else:
                    b, a = _biquad_lowshelf(f_clamped, g, self._sample_rate)
                if f == hi_f:
                    b, a = _biquad_highshelf(f_clamped, g, self._sample_rate)
                rows.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
            return np.array(rows, dtype=np.float64) if rows else None

        self._er_sos = _sos(p["er_lo_freq"], p["er_hi_freq"], p["er_lo_gain"], p["er_hi_gain"])
        self._rev_sos = _sos(p["rev_lo_freq"], p["rev_hi_freq"], p["rev_lo_gain"], p["rev_hi_gain"])

    # ── 块处理 ───────────────────────────────────────────────────

    def _ring_read_fixed(self, offsets: np.ndarray, n: int) -> np.ndarray:
        """按每样本递进的固定整数偏移读环形缓冲。

        offsets: 抽头延迟样本数（int64）。返回 (taps, n)。
        读位 = (块起始写位 + i - offset) mod size；块起始写位 =
        块末写位 - n（self._ring_write 已在读取前更新为块末值）。
        """
        block_start = (self._ring_write - n) % self._ring_size
        idx = (block_start + np.arange(n, dtype=np.int64)[np.newaxis, :]
               - offsets[:, np.newaxis]) % self._ring_size
        return self._ring[idx]

    def _ring_read_frac_single(self, offset_samples: float, frac: np.ndarray, n: int) -> np.ndarray:
        """按随样本变化的小数偏移读环形缓冲（chorus 微调制）。

        offset_samples: 基础延迟（样本）；frac: 每样本附加小数偏移（样本）。
        线性插值读取。读位基准同 _ring_read_fixed（块起始写位）。
        """
        block_start = (self._ring_write - n) % self._ring_size
        base = block_start + np.arange(n, dtype=np.float64) - offset_samples - frac
        i0 = np.floor(base).astype(np.int64) % self._ring_size
        i1 = (i0 + 1) % self._ring_size
        w = base - np.floor(base)
        return self._ring[i0] * (1.0 - w) + self._ring[i1] * w

    def _apply_tone(self, sos: Optional[np.ndarray], zi_store: str, x: np.ndarray) -> np.ndarray:
        """对 (2, n) 总线应用 shelf SOS（跨块 zi 连续）。返回处理后的副本。"""
        if sos is None or sos.shape[0] == 0 or sosfilt is None:
            return x
        channels = x.shape[0]
        zi = getattr(self, zi_store)
        if zi is None or zi.shape != (sos.shape[0], channels, 2):
            zi = np.zeros((sos.shape[0], channels, 2), dtype=np.float64)
        out, zf = sosfilt(sos, x, axis=-1, zi=zi)
        setattr(self, zi_store, zf)
        return out

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用声学房间混响。

        多声道输入求和为单声道驱动湿声网络；湿声 L/R 分配回各声道
        （≥2 声道交替 L/R，单声道取均值）。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            混响后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or self._params is None \
                or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        p = self._params
        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # Preserve all requested past samples while writing this complete block.
        # Growing also preserves history when playback/export block sizes differ.
        self._ensure_ring_capacity(self._ring_history_samples + n + 8)

        wet_w = p["dry_wet"] / 100.0
        dry_w = 1.0 - wet_w
        direct_gain = _db_to_lin_gain(p["direct_db"])
        er_gain = _db_to_lin_gain(p["er_db"])
        rev_gain = _db_to_lin_gain(p["reverb_db"])

        # 快路径：全干且直达 0dB → 直通
        if wet_w <= 1e-6 and abs(direct_gain - 1.0) < 1e-9:
            return samples

        # 1) mono 求和写入环形缓冲（分段写，含块内回绕）
        mono = np.mean(out, axis=0) if channels > 1 else out[0]
        ring = self._ring
        size = self._ring_size
        pos = 0
        write = self._ring_write
        while pos < n:
            m = min(n - pos, size)
            end = write + m
            if end <= size:
                ring[write:end] = mono[pos:pos + m]
            else:
                k = size - write
                ring[write:] = mono[pos:pos + k]
                ring[:m - k] = mono[pos + k:pos + m]
            pos += m
            write = end % size

        # 2) ER 总线：抽头读取（块末写位先行更新，读取以块起始为基准回溯）
        self._ring_write = write
        er_l = (self._ring_read_fixed(self._er_taps_l, n) * self._er_weights_l[:, np.newaxis]).sum(axis=0)
        er_r = (self._ring_read_fixed(self._er_taps_r, n) * self._er_weights_r[:, np.newaxis]).sum(axis=0)
        er_bus = np.stack((er_l, er_r)) * (1.0 / 6.0)
        er_bus = self._apply_tone(self._er_sos, "_er_shelf_zi", er_bus)

        # 3) 晚期总线：微调制小数偏移读取 → 双 FDN
        mod_depth = p["modulation"] / 100.0 * _MOD_MAX_DEPTH_SAMPLES
        # LFO 相位 = 块首绝对时间（先取 t 再推进相位——先推进会让整块 t
        # 偏移一个块长，跨块分割时 LFO 相位与整块不一致产生可闻跳变）
        t = np.arange(n, dtype=np.float64) / self._sample_rate + self._mod_phase
        self._mod_phase += n / self._sample_rate
        frac_l = mod_depth * (0.5 + 0.5 * np.sin(2.0 * np.pi * _MOD_LFO_HZ[0] * t))
        frac_r = mod_depth * (0.5 + 0.5 * np.sin(2.0 * np.pi * _MOD_LFO_HZ[1] * t + 1.3))
        late_in_l = self._ring_read_frac_single(self._late_offset_l, frac_l, n)
        late_in_r = self._ring_read_frac_single(self._late_offset_r, frac_r, n)
        # RV-03: 单一 8 线正交 FDN；L/R 从互补线组去相关读出
        # （非交替复制）。调制读头两条仍提供（左右相位差 → 注入相关）。
        late_mono = 0.5 * (late_in_l + late_in_r)
        fdn_l, fdn_r = self._fdn8.process(late_mono)
        late_bus = np.stack((fdn_l, fdn_r)) * 0.8
        late_bus = self._apply_tone(self._rev_sos, "_rev_shelf_zi", late_bus)

        # 4) 输出：直达 ×dry + (ER+Late) ×wet
        wet_bus = (er_bus * er_gain + late_bus * rev_gain) * wet_w
        for ch in range(channels):
            if channels == 1:
                wet_ch = 0.5 * (wet_bus[0] + wet_bus[1])
            else:
                wet_ch = wet_bus[ch % 2]
            out[ch] = out[ch] * direct_gain * dry_w + wet_ch

        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置所有延迟线状态（seek/暂停后调用）。"""
        if self._fdn8 is not None:
            self._fdn8.reset()
        if self._ring is not None:
            self._ring.fill(0.0)
        self._ring_write = 0
        self._mod_phase = 0.0
        self._er_shelf_zi = None
        self._rev_shelf_zi = None

    def tail_estimate_samples(self) -> int:
        """MX-07：混响尾音估计 = 2×RT60（-60dB 后自然不可闻），上限 8s。"""
        sr = self._sample_rate
        p = self._params or {}
        rt60_s = float(p.get("reverb_time", 2000.0)) * 0.001
        if sr <= 0 or rt60_s <= 0:
            return 0
        return int(min(rt60_s * 2.0, 8.0) * sr) + 1

    @property
    def is_active(self) -> bool:
        return self._enabled


class _CombFilter:
    """带阻尼的 Schroeder comb filter（反馈延迟线 + 单极点低通）。

    阻尼（damping）通过在反馈回路放一个单极点低通实现，
    使高频衰减更快（模拟真实房间空气吸收 + 墙面吸收）。
    每实例独立维护环形缓冲 + 低通状态。
    """

    def __init__(self, delay_samples: int) -> None:
        self._size = max(int(delay_samples), 1)
        self._buf = np.zeros(self._size, dtype=np.float64)
        self._idx = 0
        # 单极点低通状态（damping）
        self._filt = 0.0
        # 阻尼系数（0..1，1=无阻尼直通，0=全低通）
        self._damp1: float = 1.0  # 直通权重
        self._damp2: float = 0.0  # 低通权重
        # 反馈系数
        self._feedback: float = 0.5
        # lfilter 系数缓存（set_params 时更新）
        self._b = np.array([self._damp2], dtype=np.float64)
        self._a = np.array([1.0, -self._damp1], dtype=np.float64)

    def set_params(self, feedback: float, damping_linear: float) -> None:
        """feedback: 反馈增益(0..1); damping_linear: 单极点低通系数(0..1)。

        damping_linear 由 damping_hz 经一阶低通公式计算（越低，高频衰减越快）。
        """
        self._feedback = float(feedback)
        self._damp1 = float(damping_linear)
        self._damp2 = 1.0 - float(damping_linear)
        # 预构建 lfilter 系数（阻尼低通：y[i] = damp2*x[i] + damp1*y[i-1]）
        self._b = np.array([self._damp2], dtype=np.float64)
        self._a = np.array([1.0, -self._damp1], dtype=np.float64)

    def process_block(self, inp: np.ndarray) -> np.ndarray:
        """处理一块样本（单声道）。输出 = 延迟反馈后的信号。

        向量化实现：块内按 min(n, size) 分段，段内读区与写区不相交——
        先整段读出延迟值，阻尼低通对该段是一次常系数 IIR（lfilter 精确求解，
        状态经 zi=damp1*prevfilt 交接），再整段写回。与逐样本循环结果一致
        （浮点结合顺序不同约有 ~1 ulp 差异）。scipy 不可用时回退逐样本。
        """
        if lfilter is None:
            return self._process_block_scalar(inp)
        n = inp.shape[0]
        out = np.empty_like(inp)
        buf = self._buf
        size = self._size
        idx = self._idx
        pos = 0
        while pos < n:
            m = min(n - pos, size)
            end = idx + m
            if end <= size:
                cur = buf[idx:end]  # view：先读后写
                filt, zf = lfilter(self._b, self._a, cur,
                                   zi=np.array([self._filt * self._damp1]))
                out[pos:pos + m] = cur
                buf[idx:end] = inp[pos:pos + m] + filt * self._feedback
            else:
                k = size - idx
                cur = np.concatenate((buf[idx:], buf[:m - k]))
                filt, zf = lfilter(self._b, self._a, cur,
                                   zi=np.array([self._filt * self._damp1]))
                out[pos:pos + k] = buf[idx:]
                out[pos + k:pos + m] = buf[:m - k]
                newv = inp[pos:pos + m] + filt * self._feedback
                buf[idx:] = newv[:k]
                buf[:m - k] = newv[k:]
            self._filt = float(zf[0]) / self._damp1 if self._damp1 > 0.0 else 0.0
            pos += m
            idx = end % size
        self._idx = idx
        return out

    def _process_block_scalar(self, inp: np.ndarray) -> np.ndarray:
        """逐样本回退实现（无 scipy 时使用，语义与向量化版本一致）。"""
        out = np.empty_like(inp)
        buf = self._buf
        size = self._size
        idx = self._idx
        filt = self._filt
        fb = self._feedback
        d1 = self._damp1
        d2 = self._damp2
        for i in range(len(inp)):
            current = buf[idx]
            # 阻尼低通（反馈信号经低通后再回写）
            filt = current * d2 + filt * d1
            out[i] = current
            buf[idx] = inp[i] + filt * fb
            idx += 1
            if idx >= size:
                idx = 0
        self._idx = idx
        self._filt = filt
        return out

    def reset(self) -> None:
        self._buf.fill(0.0)
        self._filt = 0.0
        self._idx = 0


class _AllpassFilter:
    """Schroeder allpass filter（用于扩散，增加回声密度）。"""

    def __init__(self, delay_samples: int) -> None:
        self._size = max(int(delay_samples), 1)
        self._buf = np.zeros(self._size, dtype=np.float64)
        self._idx = 0
        self._feedback: float = _ALLPASS_FEEDBACK

    def process_block(self, inp: np.ndarray) -> np.ndarray:
        """处理一块样本（向量化：段内先读后写，无状态依赖）。

        块内按 min(n, size) 分段；段内读区与写区为同一批槽位但读取全部
        先于写回完成，与逐样本顺序语义一致（读到的恒是写入前的旧值）。
        """
        n = inp.shape[0]
        out = np.empty_like(inp)
        buf = self._buf
        size = self._size
        idx = self._idx
        fb = self._feedback
        pos = 0
        while pos < n:
            m = min(n - pos, size)
            end = idx + m
            if end <= size:
                seg_out = buf[idx:end] - inp[pos:pos + m] * fb
                buf[idx:end] = inp[pos:pos + m] + buf[idx:end] * fb
                out[pos:pos + m] = seg_out
            else:
                k = size - idx
                out0 = buf[idx:] - inp[pos:pos + k] * fb
                out1 = buf[:m - k] - inp[pos + k:pos + m] * fb
                buf[idx:] = inp[pos:pos + k] + buf[idx:] * fb
                buf[:m - k] = inp[pos + k:pos + m] + buf[:m - k] * fb
                out[pos:pos + k] = out0
                out[pos + k:pos + m] = out1
            pos += m
            idx = end % size
        self._idx = idx
        return out

    def reset(self) -> None:
        self._buf.fill(0.0)
        self._idx = 0


# RV-03: 8 线 FDN 延迟（互质比例；并集自原 L/R 梳延迟组）
_FDN8_DELAY_MS = (29.7, 31.9, 37.1, 39.7, 41.1, 43.9, 43.7, 46.7)


class _FDNNetwork8:
    """8 线正交反馈 FDN（RV-03，Householder 反馈矩阵）。

    每线：延迟 D_i、按 RT60 逐线校准的反馈增益 g_i = 10^(-3·D_i/(RT60·sr))
    （延迟越长增益越小——同一 RT60 目标下所有线同步衰减到 -60dB）、
    反馈回路单极点阻尼（brightness → damping_hz）。

    反馈混合：Householder Q = I − (2/8)·J（正交，谱范数 1）→ 反馈环路
    增益 ≤ max|g_i| < 1：长尾无能量增长；正交打散消除孤立梳的周期啸叫。

    输入注入 b = (1,…,1)/√8（能量守恒）；输出去相关：L 取前 4 线均值、
    R 取后 4 线均值（非交替复制）。

    块处理：按 ≤ min(D_i) 子块推进（子块内读区与写区不重叠 → 无环内
    反馈，精确等价逐样本）；阻尼低通每线一次 lfilter（zi 交接）。
    确定性（无随机源）——预览/导出种子与时间轴天然一致。
    """

    __slots__ = ("_delays", "_bufs", "_idx", "_g", "_filt", "_damp1",
                 "_damp2", "_b", "_a", "_min_delay")

    def __init__(self, sample_rate: float) -> None:
        self._delays = [max(2, int(round(d * 0.001 * sample_rate)))
                        for d in _FDN8_DELAY_MS]
        self._bufs = [np.zeros(D, dtype=np.float64) for D in self._delays]
        self._idx = [0] * 8
        self._g = [0.5] * 8
        self._filt = [0.0] * 8
        self._damp1 = 1.0
        self._damp2 = 0.0
        self._b = np.array([0.0], dtype=np.float64)
        self._a = np.array([1.0, -1.0], dtype=np.float64)
        self._min_delay = min(self._delays)

    def set_params(self, rt60_s: float, damping_linear: float,
                   sample_rate: float) -> None:
        """RT60（秒）→ 每线反馈增益；damping_linear → 回路单极点阻尼。

        RT60 语义按 RV-03 验收在"所定义中低频段"成立（阻尼拐角以下的
        频段 |H|≈1，每圈仅 ×g_i → 包络严格按 RT60 衰减）；高频随
        brightness 阻尼更快衰减（频谱倾斜，非校准误差）。g_i clamp
        <0.99995 保稳定（正交 Householder → 谱半径 ≤ max|g_i|）。
        """
        self._damp1 = float(damping_linear)
        self._damp2 = 1.0 - self._damp1
        self._b = np.array([self._damp2], dtype=np.float64)
        self._a = np.array([1.0, -self._damp1], dtype=np.float64)
        if rt60_s <= 0.0 or sample_rate <= 0.0:
            self._g = [0.0] * 8
            return
        self._g = [
            min(0.99995,
                float(10.0 ** (-3.0 * D / (rt60_s * sample_rate))))
            for D in self._delays]

    def process(self, x: np.ndarray) -> tuple:
        """处理一块 mono 输入，返回 (out_l, out_r) 去相关立体声。"""
        n = int(x.shape[-1])
        out_l = np.empty(n, dtype=np.float64)
        out_r = np.empty(n, dtype=np.float64)
        g = self._g
        binj = 1.0 / np.sqrt(8.0)
        pos = 0
        while pos < n:
            m = min(n - pos, self._min_delay)
            xs = x[pos:pos + m].astype(np.float64, copy=False)
            # 1) 读各线本子块的延迟值（读区=即将被写的旧内容）
            reads = []
            for i in range(8):
                buf = self._bufs[i]
                D = self._delays[i]
                idx = self._idx[i]
                end = idx + m
                if end <= D:
                    reads.append(buf[idx:end].copy())
                else:
                    k = D - idx
                    reads.append(np.concatenate((buf[idx:], buf[:m - k])))
            # 2) 反馈回路阻尼（每线 lfilter，zi 交接）
            damped = []
            for i in range(8):
                if self._damp2 > 1e-12 and lfilter is not None:
                    y, zf = lfilter(self._b, self._a, reads[i],
                                    zi=np.array([self._filt[i] * self._damp1]))
                    self._filt[i] = float(zf[0]) / self._damp1 \
                        if self._damp1 > 0.0 else 0.0
                    damped.append(y)
                else:
                    damped.append(reads[i])
                    self._filt[i] = 0.0
            R = np.stack(damped)  # (8, m)
            # 3) 输出 tap（去相关：前 4 线 → L，后 4 线 → R）
            out_l[pos:pos + m] = 0.5 * R[:4].sum(axis=0)
            out_r[pos:pos + m] = 0.5 * R[4:].sum(axis=0)
            # 4) Householder 正交混合 + 每线增益 + 输入注入 → 写回
            mixed = R - 0.25 * R.sum(axis=0, keepdims=True)  # Q @ R
            for i in range(8):
                newv = g[i] * mixed[i] + binj * xs
                buf = self._bufs[i]
                D = self._delays[i]
                idx = self._idx[i]
                end = idx + m
                if end <= D:
                    buf[idx:end] = newv
                else:
                    k = D - idx
                    buf[idx:] = newv[:k]
                    buf[:m - k] = newv[k:]
                self._idx[i] = end % D
            pos += m
        return out_l, out_r

    def reset(self) -> None:
        for buf in self._bufs:
            buf.fill(0.0)
        self._idx = [0] * 8
        self._filt = [0.0] * 8


def _decay_to_feedback(decay_s: float, delay_ms: float, sample_rate: float) -> float:
    """由目标混响时间（RT60）和 comb 延迟计算反馈系数。

    RT60 = -3 * delay / ln(feedback)  →  feedback = exp(-3 * delay / RT60)
    其中 delay = delay_ms * 0.001 * sample_rate（样本数）。
    feedback clamp 到 [0, 0.999] 避免发散。
    """
    if decay_s <= 0.0 or delay_ms <= 0.0 or sample_rate <= 0.0:
        return 0.0
    delay_samples = delay_ms * 0.001 * sample_rate
    # RT60 要求衰减 60dB，每圈衰减 delay_samples 个样本
    # n_circles = decay_s * sample_rate / delay_samples 圈达到 60dB
    # feedback^n_circles = 10^(-3) → feedback = 10^(-3/n_circles)
    n_circles = decay_s * sample_rate / delay_samples
    if n_circles <= 0.0:
        return 0.0
    fb = float(10.0 ** (-3.0 / n_circles))
    return max(0.0, min(0.999, fb))


def _damping_hz_to_linear(damping_hz: float, sample_rate: float) -> float:
    """由阻尼频率（Hz）计算单极点低通系数（0..1）。

    damping_hz 是低通的 -3dB 频率。系数 alpha = exp(-2*pi*fc/sr)。
    返回值：1=无阻尼（高频不衰减），接近 0=强低通。
    """
    if damping_hz <= 0.0 or sample_rate <= 0.0:
        return 0.0
    fc = min(damping_hz, sample_rate * 0.45)
    return float(np.exp(-2.0 * np.pi * fc / sample_rate))


# ─────────────────────────────────────────────────────────────────────────────
# Delay 立体声双线延迟器（CW-A1 重构）
# ─────────────────────────────────────────────────────────────────────────────

# Delay 默认参数（与 DelayParams dataclass 一致，避免循环 import）
_DELAY_DEFAULT_LOW_CUT = 108.0
_DELAY_DEFAULT_HIGH_CUT = 12500.0
_DELAY_DEFAULT_LEFT_DELAY = 121.0
_DELAY_DEFAULT_LEFT_FB = 16.0
_DELAY_DEFAULT_RIGHT_DELAY = 135.0
_DELAY_DEFAULT_RIGHT_FB = -7.0
_DELAY_DEFAULT_HIGH_RATIO = 60.0
_DELAY_DEFAULT_BLEND = 15.0
_DELAY_DEFAULT_DRY_WET = 30.0
_DELAY_DEFAULT_LEVEL_DB = 0.0

# 反馈回路高频阻尼映射：high_ratio(0..100%) → 单极点低通截止（Hz）
_DELAY_DAMP_MIN_HZ = 800.0
_DELAY_DAMP_MAX_HZ = 20000.0


def _high_ratio_to_damp_hz(high_ratio: float) -> float:
    """high_ratio 0% → 近直通（20kHz），100% → 强阻尼（800Hz）。"""
    t = max(0.0, min(1.0, high_ratio / 100.0))
    return _DELAY_DAMP_MIN_HZ + (1.0 - t) ** 2 * (_DELAY_DAMP_MAX_HZ - _DELAY_DAMP_MIN_HZ)


class DelayEffect(AudioEffect):
    """立体声双线延迟器（L/R 独立延迟 + 负反馈 + 交叉反馈矩阵）。

    拓扑：
      input → [low_cut/high_cut 输入滤波] → dry 总线
            → [L/R 独立环形延迟线]
            → 反馈矩阵：write_c = in_c + LP_damp(fb_c×(1-blend)·fbc
                                                 + fb_other×blend·fb_other_c)
      （反馈量支持负号=相位反转；high_ratio 控制反馈阻尼单极点低通）
      output = dry×(1-w) + delayed×w，再乘 level_db 输出电平。

    Mono 输入格式：求和为单声道驱动 L 线（R 线旁路），湿声复制到各声道。

    参数（DelayParams，接收 dict）。线程安全：实例不共享。
    """

    effect_type = "delay"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._low_cut: float = _DELAY_DEFAULT_LOW_CUT
        self._high_cut: float = _DELAY_DEFAULT_HIGH_CUT
        self._left_delay: float = _DELAY_DEFAULT_LEFT_DELAY
        self._left_fb: float = _DELAY_DEFAULT_LEFT_FB
        self._right_delay: float = _DELAY_DEFAULT_RIGHT_DELAY
        self._right_fb: float = _DELAY_DEFAULT_RIGHT_FB
        self._high_ratio: float = _DELAY_DEFAULT_HIGH_RATIO
        self._blend: float = _DELAY_DEFAULT_BLEND
        self._dry_wet: float = _DELAY_DEFAULT_DRY_WET
        self._level_db: float = _DELAY_DEFAULT_LEVEL_DB
        self._mono_input: bool = False
        self._enabled: bool = False
        # 延迟线（L/R）
        self._buf_l: Optional[np.ndarray] = None
        self._buf_r: Optional[np.ndarray] = None
        self._delay_idx: int = 0
        self._delay_size: int = 0
        # 输入滤波 SOS 状态（low_cut + high_cut 两段 × 声道）
        self._in_sos: Optional[np.ndarray] = None
        self._in_zi: Optional[np.ndarray] = None
        # 反馈阻尼单极点低通状态（每声道）
        self._damp_prev: np.ndarray = np.zeros(2, dtype=np.float64)
        self._damp_b: np.ndarray = np.array([1.0])
        self._damp_a: np.ndarray = np.array([1.0, 0.0])
        # R07: 双读头交叉淡化状态（延迟时间热改平滑）。_cur_delay_* 为
        # 当前目标读位（样本）；淡入中改目标只更新目标、进度接续，不重置。
        self._cur_delay_l: Optional[float] = None
        self._cur_delay_r: Optional[float] = None
        self._old_delay_l: float = 0.0   # 淡化起点读位（fade 开始时快照）
        self._old_delay_r: float = 0.0
        self._fade_l_remaining: int = 0
        self._fade_r_remaining: int = 0
        self._fade_l_total: int = 1
        self._fade_r_total: int = 1
        # H01: v ≤ 3 实例保留直接切换语义（crossfade 属 v4 算法契约变更）。
        self._algorithm_version: Optional[int] = None
        self._rebuild_delay_line()
        self._rebuild_input_filters()
        self._recompute_damp_coeff()

    # DL-02: 环形缓冲按最大延迟（1200ms）一次性预分配——参数时间变化
    # 不再重建（拖动不清空整段历史/尾音）；容量只在采样率变化时重置。
    _MAX_DELAY_MS = 1200.0
    # R07: 延迟时间热改的读头交叉淡化窗（ms）。固定值写入算法契约
    # （ALGORITHM_VERSION 4）：旧/新读头共享同一历史，线性等和交叉
    # 淡化（两端 unity），反馈路径消费淡化后的湿信号。
    _XFADE_MS = 15.0

    def _rebuild_delay_line(self) -> None:
        """按最大延迟容量预分配环形缓冲（仅采样率变化/首建时调用）。"""
        sr = self._sample_rate
        capacity = int(round(self._MAX_DELAY_MS * 0.001 * sr)) + 8
        self._delay_size = max(capacity, 2)
        self._buf_l = np.zeros(self._delay_size, dtype=np.float64)
        self._buf_r = np.zeros(self._delay_size, dtype=np.float64)
        self._delay_idx = 0
        self._damp_prev = np.zeros(2, dtype=np.float64)

    def _rebuild_input_filters(self) -> None:
        """重建输入 low_cut/high_cut 滤波 SOS。"""
        sr = self._sample_rate
        rows = []
        lc = min(self._low_cut, sr * 0.45)
        b, a = _biquad_highpass(lc, _BUTTERWORTH_Q, sr)
        rows.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
        hc = min(self._high_cut, sr * 0.45)
        b, a = _biquad_lowpass(hc, _BUTTERWORTH_Q, sr)
        rows.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
        self._in_sos = np.array(rows, dtype=np.float64)
        self._in_zi = None  # 声道数未知，首块初始化

    def _recompute_damp_coeff(self) -> None:
        """反馈阻尼：单极点低通 y = a·y' + (1-a)·x。"""
        fc = min(_high_ratio_to_damp_hz(self._high_ratio), self._sample_rate * 0.45)
        a = float(np.exp(-2.0 * np.pi * fc / self._sample_rate))
        self._damp_a = np.array([1.0, -a], dtype=np.float64)
        self._damp_b = np.array([1.0 - a], dtype=np.float64)

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        """更新延迟器参数。

        Args:
            params: dict 形式的 DelayParams，None 表示旁路。
            sample_rate: 可选新采样率
            algorithm_version: H01 兼容分发——v ≤ 3 走 v4 之前的直接读头
                切换（延迟时间热改无 15ms 交叉淡化），v ≥ 4 / None 现行。
        """
        sr_changed = sample_rate is not None and sample_rate != self._sample_rate
        if sr_changed:
            self._sample_rate = float(sample_rate)
        if algorithm_version is None:
            self._algorithm_version = None
        else:
            try:
                self._algorithm_version = max(0, int(algorithm_version))
            except (TypeError, ValueError):
                self._algorithm_version = None

        if params is None:
            self._enabled = False
            return

        # AUD-15304: dict 参数按 DelayParams 声明范围钳位
        new_left = _clamp_finite(params.get("left_delay", _DELAY_DEFAULT_LEFT_DELAY),
                                 0.0, 1200.0, _DELAY_DEFAULT_LEFT_DELAY)
        new_right = _clamp_finite(params.get("right_delay", _DELAY_DEFAULT_RIGHT_DELAY),
                                  0.0, 1200.0, _DELAY_DEFAULT_RIGHT_DELAY)
        new_lc = _clamp_finite(params.get("low_cut", _DELAY_DEFAULT_LOW_CUT),
                               20.0, 5000.0, _DELAY_DEFAULT_LOW_CUT)
        new_hc = _clamp_finite(params.get("high_cut", _DELAY_DEFAULT_HIGH_CUT),
                               100.0, 20000.0, _DELAY_DEFAULT_HIGH_CUT)

        filter_changed = (abs(new_lc - self._low_cut) > 1e-6
                          or abs(new_hc - self._high_cut) > 1e-6)

        self._low_cut = new_lc
        self._high_cut = new_hc
        # DL-02: 时间参数就地生效（分数读头平滑过渡），不清环形缓冲——
        # 旧实现 time_changed → _rebuild_delay_line 会把整段历史/尾音清零
        self._left_delay = new_left
        self._right_delay = new_right
        self._left_fb = _clamp_finite(params.get("left_feedback", _DELAY_DEFAULT_LEFT_FB),
                                      -99.0, 99.0, _DELAY_DEFAULT_LEFT_FB)
        self._right_fb = _clamp_finite(params.get("right_feedback", _DELAY_DEFAULT_RIGHT_FB),
                                       -99.0, 99.0, _DELAY_DEFAULT_RIGHT_FB)
        new_hr = _clamp_finite(params.get("high_ratio", _DELAY_DEFAULT_HIGH_RATIO),
                               0.0, 100.0, _DELAY_DEFAULT_HIGH_RATIO)
        hr_changed = abs(new_hr - self._high_ratio) > 1e-6
        self._high_ratio = new_hr

        if sr_changed:
            self._rebuild_delay_line()
        if sr_changed or filter_changed:
            self._rebuild_input_filters()
        if hr_changed or sr_changed:
            self._recompute_damp_coeff()

        self._blend = _clamp_finite(params.get("stereo_blend", _DELAY_DEFAULT_BLEND),
                                    0.0, 100.0, _DELAY_DEFAULT_BLEND)
        self._dry_wet = _clamp_finite(params.get("dry_wet", _DELAY_DEFAULT_DRY_WET),
                                      0.0, 100.0, _DELAY_DEFAULT_DRY_WET)
        self._level_db = _clamp_finite(params.get("level_db", _DELAY_DEFAULT_LEVEL_DB),
                                       -24.0, 6.0, _DELAY_DEFAULT_LEVEL_DB)
        # DL-01：输入选择矩阵。Mono=(L+R)/2 平均（相关内容电平恒定）；
        # Left/Right=单声道内容驱动双线（另一声道内容不进延迟网络）。
        fmt = str(params.get("input_format", "Stereo") or "Stereo")
        self._mono_input = fmt == "Mono"
        self._input_mode: str = fmt if fmt in ("Stereo", "Mono", "Left", "Right") else "Stereo"
        self._enabled = bool(params.get("enabled", True))

    def _legacy_direct_switch(self) -> bool:
        """H01：v ≤ 3 实例的延迟时间热改直接切读头（无 v4 交叉淡化）。"""
        v = self._algorithm_version
        return v is not None and v <= 3

    def _apply_input_filters(self, out: np.ndarray) -> np.ndarray:
        """输入 low_cut/high_cut SOS 滤波（跨块 zi 连续）。"""
        if sosfilt is None or self._in_sos is None:
            return out
        channels = out.shape[0]
        if self._in_zi is None or self._in_zi.shape != (self._in_sos.shape[0], channels, 2):
            self._in_zi = np.zeros((self._in_sos.shape[0], channels, 2), dtype=np.float64)
        out, zf = sosfilt(self._in_sos, out, axis=-1, zi=self._in_zi)
        self._in_zi = zf
        return out

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用立体声延迟（分段向量化）。

        块内按 min(n, delay_size) 分段：段内先读后写（与逐样本语义一致，
        含块内绕回时读到本块先前写入的值）。反馈阻尼对每段的混合反馈源
        做一次 lfilter（状态精确交接）。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            延迟后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 声道规整：取 L/R 两路（Mono 求平均、Left/Right 单声道驱动双线），
        # 其余声道用 L 湿声
        if channels == 1:
            in_l = out[0]
            in_r = out[0]
        else:
            in_l = out[0]
            in_r = out[1]
        if self._mono_input:
            mono = 0.5 * (in_l + in_r)
            in_l = mono
            in_r = mono
        elif self._input_mode == "Left":
            in_r = in_l
        elif self._input_mode == "Right":
            in_l = in_r

        # 输入滤波
        st = np.stack((in_l, in_r))
        st = self._apply_input_filters(st)
        in_l, in_r = st[0], st[1]

        sr = self._sample_rate
        # DL-02: 分数延迟（样本域浮点，线性插值读头）——121ms 时间误差
        # ≤1 采样由连续读头保证；最小 1.0 样本（0ms → 直通语义由 wet 路径
        # 的读位=写位决定，此处钳到 1.0 保持段切分正确性）。
        delay_l = max(1.0, self._left_delay * 0.001 * sr)
        delay_r = max(1.0, self._right_delay * 0.001 * sr)
        # R07: 双读头交叉淡化——延迟时间热改时旧/新读头短窗交叉淡化，
        # 消除读位跳变（10→10.25ms 旧实现边界跳变 ~0.44 @ 振幅 0.5）。
        # 淡入中再次改目标：只更新目标读位，进度接续，不重置回旧头。
        if self._cur_delay_l is None:
            self._cur_delay_l = delay_l
        elif delay_l != self._cur_delay_l:
            if self._legacy_direct_switch():
                self._fade_l_remaining = 0  # H01: v≤3 直接切读头
            elif self._fade_l_remaining <= 0:
                self._old_delay_l = self._cur_delay_l
                self._fade_l_total = max(
                    1, int(self._XFADE_MS * 0.001 * sr))
                self._fade_l_remaining = self._fade_l_total
            self._cur_delay_l = delay_l
        if self._cur_delay_r is None:
            self._cur_delay_r = delay_r
        elif delay_r != self._cur_delay_r:
            if self._legacy_direct_switch():
                self._fade_r_remaining = 0  # H01: v≤3 直接切读头
            elif self._fade_r_remaining <= 0:
                self._old_delay_r = self._cur_delay_r
                self._fade_r_total = max(
                    1, int(self._XFADE_MS * 0.001 * sr))
                self._fade_r_remaining = self._fade_r_total
            self._cur_delay_r = delay_r
        fb_l = self._left_fb / 100.0
        fb_r = self._right_fb / 100.0
        blend = self._blend / 100.0
        damp_passthrough = abs(self._damp_a[1]) < 1e-9  # a≈0 → 直通

        wet_l = np.empty(n, dtype=np.float64)
        wet_r = np.empty(n, dtype=np.float64)

        def _read_head(buf: np.ndarray, base_idx: np.ndarray,
                       delay: float, m: int) -> np.ndarray:
            """分数读头：读位 = (段起始写位 + j - delay) mod size，
            i0/i0+1 线性插值（i0+1 ≤ 段起始，读先于写）。"""
            rpos = (base_idx - delay).astype(np.float64)
            i0 = np.floor(rpos).astype(np.int64) % size
            i1 = (i0 + 1) % size
            w = rpos - np.floor(rpos)
            return buf[i0] * (1.0 - w) + buf[i1] * w

        def _read_head_nofb(in_seg: np.ndarray, buf: np.ndarray,
                            idx: int, delay: float, m: int) -> np.ndarray:
            """R08: 无反馈整段读头——块内历史直接取本块输入（无反馈时
            slot j-delay 的值 = 滤波输入 j-delay，与旧按 int(delay) 分段
            的"读上一段写入"逐位等价），块外取 ring 历史。近零延迟不再
            每样本一次 Python 循环。"""
            rpos = np.arange(m, dtype=np.float64) - delay
            i0 = np.floor(rpos).astype(np.int64)
            w = rpos - i0

            def _src(k: np.ndarray) -> np.ndarray:
                out = np.empty(m, dtype=np.float64)
                neg = k < 0
                if neg.any():
                    out[neg] = buf[(idx + k[neg]) % size]
                pos_k = ~neg
                if pos_k.any():
                    out[pos_k] = in_seg[k[pos_k]]
                return out

            return (1.0 - w) * _src(i0) + w * _src(i0 + 1)

        pos = 0
        idx = self._delay_idx
        size = self._delay_size
        # R08: 无反馈快速路径——写值不依赖读值，整块单段向量化；
        # 反馈非零时保持原按延迟切段语义（块内读自身先前写入）。
        _no_feedback = (fb_l == 0.0 and fb_r == 0.0)
        while pos < n:
            if _no_feedback:
                m = min(n - pos, size)
            else:
                # 段长不得超过"两声道新旧读位"的最小整数延迟：保证段内
                # 所有读位（含插值 pos+1 样本）落在段起始之前（先读后写
                # 语义完整）。
                _min_l = min(int(self._cur_delay_l),
                             int(self._old_delay_l) if self._fade_l_remaining > 0
                             else int(self._cur_delay_l))
                _min_r = min(int(self._cur_delay_r),
                             int(self._old_delay_r) if self._fade_r_remaining > 0
                             else int(self._cur_delay_r))
                m = min(n - pos, _min_l, _min_r)
                m = max(1, m)
            end = idx + m
            # 1) 分数读头（R07 淡化 + R08 无反馈快速路径）
            _read = _read_head_nofb if _no_feedback else _read_head
            _read_args_nofb = _no_feedback
            if self._fade_l_remaining > 0:
                _g = np.clip(
                    1.0 - (self._fade_l_remaining - np.arange(m))
                    / float(self._fade_l_total), 0.0, 1.0)
                if _read_args_nofb:
                    _seg = in_l[pos:pos + m]
                    _b = self._buf_l
                    _ro = _read(_seg, _b, idx, self._old_delay_l, m)
                    _rn = _read(_seg, _b, idx, self._cur_delay_l, m)
                else:
                    base_idx = (idx + np.arange(m)) % size
                    _ro = _read(self._buf_l, base_idx, self._old_delay_l, m)
                    _rn = _read(self._buf_l, base_idx, self._cur_delay_l, m)
                wet_l[pos:pos + m] = (1.0 - _g) * _ro + _g * _rn
                self._fade_l_remaining = max(0, self._fade_l_remaining - m)
            else:
                if _read_args_nofb:
                    wet_l[pos:pos + m] = _read(
                        in_l[pos:pos + m], self._buf_l, idx,
                        self._cur_delay_l, m)
                else:
                    base_idx = (idx + np.arange(m)) % size
                    wet_l[pos:pos + m] = _read(
                        self._buf_l, base_idx, self._cur_delay_l, m)
            if self._fade_r_remaining > 0:
                _g = np.clip(
                    1.0 - (self._fade_r_remaining - np.arange(m))
                    / float(self._fade_r_total), 0.0, 1.0)
                if _read_args_nofb:
                    _seg = in_r[pos:pos + m]
                    _b = self._buf_r
                    _ro = _read(_seg, _b, idx, self._old_delay_r, m)
                    _rn = _read(_seg, _b, idx, self._cur_delay_r, m)
                else:
                    base_idx = (idx + np.arange(m)) % size
                    _ro = _read(self._buf_r, base_idx, self._old_delay_r, m)
                    _rn = _read(self._buf_r, base_idx, self._cur_delay_r, m)
                wet_r[pos:pos + m] = (1.0 - _g) * _ro + _g * _rn
                self._fade_r_remaining = max(0, self._fade_r_remaining - m)
            else:
                if _read_args_nofb:
                    wet_r[pos:pos + m] = _read(
                        in_r[pos:pos + m], self._buf_r, idx,
                        self._cur_delay_r, m)
                else:
                    base_idx = (idx + np.arange(m)) % size
                    wet_r[pos:pos + m] = _read(
                        self._buf_r, base_idx, self._cur_delay_r, m)
            seg_l = wet_l[pos:pos + m]
            seg_r = wet_r[pos:pos + m]

            # 2) 反馈矩阵：own×(1-blend) + cross×blend（负号=相位反转）
            fb_src_l = seg_l * (1.0 - blend) * fb_l + seg_r * blend * fb_r
            fb_src_r = seg_r * (1.0 - blend) * fb_r + seg_l * blend * fb_l
            # 3) 反馈阻尼（单极点低通，lfilter 精确；无 scipy 时直通）
            if not damp_passthrough and lfilter is not None:
                src = np.stack((fb_src_l, fb_src_r))
                # scipy lfilter 的 zf 即下一块的精确 zi（一阶 IIR 状态直接
                # 透传，无需除以/乘回系数——`a[1]` 为负值，经 max() 钳位
                # 会把状态放大 1e12 倍导致反馈爆炸）
                fb_sig, zf = lfilter(self._damp_b, self._damp_a, src, axis=1,
                                     zi=self._damp_prev[:, np.newaxis])
                fb_src_l, fb_src_r = fb_sig[0], fb_sig[1]
                self._damp_prev = np.array(zf[:, 0], dtype=np.float64)
            # 4) 写回：滤波输入 + 阻尼反馈
            newv_l = in_l[pos:pos + m] + fb_src_l
            newv_r = in_r[pos:pos + m] + fb_src_r
            if end <= size:
                self._buf_l[idx:end] = newv_l
                self._buf_r[idx:end] = newv_r
            else:
                k = size - idx
                self._buf_l[idx:] = newv_l[:k]
                self._buf_l[:m - k] = newv_l[k:]
                self._buf_r[idx:] = newv_r[:k]
                self._buf_r[:m - k] = newv_r[k:]
            pos += m
            idx = end % size
        self._delay_idx = idx

        # 5) 输出：dry/wet + level
        wet_gain = self._dry_wet / 100.0
        dry_gain = 1.0 - wet_gain
        level = float(10.0 ** (self._level_db / 20.0))
        out *= dry_gain * level
        for ch in range(channels):
            wet_ch = wet_l if (self._mono_input or ch % 2 == 0) else wet_r
            out[ch] += wet_ch * wet_gain * level
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置延迟线/滤波器/阻尼状态（seek/暂停后调用）。

        R07：seek 是显式重建（清旧历史），交叉淡化状态一并清除。
        """
        if self._buf_l is not None:
            self._buf_l.fill(0.0)
        if self._buf_r is not None:
            self._buf_r.fill(0.0)
        self._delay_idx = 0
        self._damp_prev = np.zeros(2, dtype=np.float64)
        self._in_zi = None
        self._cur_delay_l = None
        self._cur_delay_r = None
        self._fade_l_remaining = 0
        self._fade_r_remaining = 0

    def tail_estimate_samples(self) -> int:
        """MX-07：延迟尾音估计——最大延迟 × 反馈衰减到 -60dB 的圈数。

        fb^n = 1e-3 → n = 3/|log10(fb)|；上限 4 秒（强反馈不得无限挂住
        调度；更长尾音属可见交付策略，超出部分按上限截断）。
        """
        sr = self._sample_rate
        if sr <= 0:
            return 0
        delay = max(self._left_delay, self._right_delay) * 0.001 * sr
        if delay <= 0.0:
            return 0
        fb = max(abs(self._left_fb), abs(self._right_fb)) / 100.0
        if fb < 0.01:
            return int(delay) + 1
        n_circles = 3.0 / max(0.0044, abs(float(np.log10(max(fb, 1e-4)))))
        return int(min(delay * n_circles, 4.0 * sr)) + 1

    @property
    def is_active(self) -> bool:
        return self._enabled


# ─────────────────────────────────────────────────────────────────────────────
# Saturation 谐波失真（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Saturation 默认参数（与 SaturationParams dataclass 一致）
_SAT_DEFAULT_DRIVE = 0.0  # 0..100（0=无失真，100=强过载）


class SaturationEffect(AudioEffect):
    """谐波失真饱和器（sigmoid WaveShaper）。

    使用 audio_pro 的 sigmoid 曲线：
      y = (1 + k) * x / (1 + k * |x|)
    其中 k = drive / 100 * 50（drive 0..100 映射到 k 0..50）。
    drive=0 时 k=0 → y=x（直通）；drive 越大谐波越丰富，软削波特性自然。

    无状态效果（逐样本独立），完全向量化处理，性能极佳。

    参数（SaturationParams，接收 dict）：
      - drive:   驱动量（0..100，0=无失真）
      - enabled: 是否启用（False=旁路直通）

    线程安全：实例不共享（虽然无状态，但保持一致性）。
    """

    effect_type = "saturation"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._drive: float = _SAT_DEFAULT_DRIVE
        self._k: float = 0.0  # 实际波形整形系数
        self._enabled: bool = False
        self._recompute_k()

    def _recompute_k(self) -> None:
        """由 drive（0..100）计算实际 k 系数。"""
        self._k = float(self._drive) / 100.0 * 50.0

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新饱和器参数。

        Args:
            params: dict 形式的 SaturationParams，None 表示旁路。
            sample_rate: 可选新采样率（对本效果无影响）
        """
        if sample_rate is not None:
            self._sample_rate = float(sample_rate)

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 SaturationParams 声明范围钳位（负 drive 会让 k<0，
        # sigmoid 分母 1 + k|x| 可过零 → 除零/翻相）。
        self._drive = _clamp_finite(params.get("drive", _SAT_DEFAULT_DRIVE),
                                    0.0, 100.0, _SAT_DEFAULT_DRIVE)
        self._enabled = bool(params.get("enabled", True))
        self._recompute_k()

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用 sigmoid 谐波失真。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            失真后的 samples，同 shape（旁路或 drive=0 时原样返回）。
        """
        if not self._enabled or self._k <= 1e-9 or \
                samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        k = self._k
        # sigmoid: y = (1+k)*x / (1 + k*|x|)，向量化
        np.divide(
            (1.0 + k) * out,
            (1.0 + k * np.abs(out)),
            out=out,
        )
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """无状态效果，无需重置。"""
        pass

    @property
    def is_active(self) -> bool:
        return self._enabled and self._k > 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# Imager 立体声成像（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Imager 默认参数（与 ImagerParams dataclass 一致）
_IMAGER_DEFAULT_PAN = 0.0     # -100..100（-100=全左, 0=中, 100=全右）
_IMAGER_DEFAULT_WIDTH = 100.0  # 0..200（0=mono, 100=normal, 200=超宽）


class ImagerEffect(AudioEffect):
    """立体声成像器（Mid/Side 处理 + Pan）。

    Mid/Side 矩阵：
      M = (L + R) * 0.5  （Mid 信号，两声道共有部分）
      S = (L - R) * 0.5  （Side 信号，立体声差异部分）
    width 控制 Side 增益：
      - 0%   → S gain = 0 → 输出 mono（L=M, R=M）
      - 100% → S gain = 1 → 输出 = 原始（L=M+S, R=M-S）
      - 200% → S gain = 2 → 超宽立体声（Side 加强）
    再经 Mid/Side 逆矩阵重构 L/R：
      L = M + S * width_gain
      R = M - S * width_gain

    Pan 控制（在 M/S 处理之后）：linear pan，左右声道增益对称。
      pan = -100 → L = 1.0, R = 0.0
      pan = 0    → L = 1.0, R = 1.0
      pan = +100 → L = 0.0, R = 1.0

    无状态效果，完全向量化。单声道输入时 width 无效（无 Side），仅 pan 生效。

    参数（ImagerParams，接收 dict）：
      - pan:    声像（-100..100）
      - width:  立体声宽度（0..200，%）
      - enabled: 是否启用（False=旁路直通）

    线程安全：实例不共享。
    """

    effect_type = "imager"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._pan: float = _IMAGER_DEFAULT_PAN
        self._width: float = _IMAGER_DEFAULT_WIDTH
        self._enabled: bool = False
        self._recompute_gains()

    def _recompute_gains(self) -> None:
        """由 pan/width 计算 side_gain 和 pan 增益。"""
        # width: 0..200 → side_gain 0..2
        self._side_gain: float = self._width / 100.0
        # pan: -100..100 → linear pan
        # pan_normalized: -1..1
        pan_norm = max(-1.0, min(1.0, self._pan / 100.0))
        if pan_norm <= 0.0:
            # 偏左：L=1.0, R=1+pan_norm（pan=-100→R=0, pan=0→R=1）
            self._pan_l: float = 1.0
            self._pan_r: float = 1.0 + pan_norm
        else:
            # 偏右：L=1-pan_norm, R=1.0
            self._pan_l: float = 1.0 - pan_norm
            self._pan_r: float = 1.0

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新成像器参数。

        Args:
            params: dict 形式的 ImagerParams，None 表示旁路。
            sample_rate: 可选新采样率（对本效果无影响）
        """
        if sample_rate is not None:
            self._sample_rate = float(sample_rate)

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 ImagerParams 声明范围钳位（|pan|>100/width<0 会产生
        # 负增益/反相声像）。
        self._pan = _clamp_finite(params.get("pan", _IMAGER_DEFAULT_PAN),
                                  -100.0, 100.0, _IMAGER_DEFAULT_PAN)
        self._width = _clamp_finite(params.get("width", _IMAGER_DEFAULT_WIDTH),
                                    0.0, 200.0, _IMAGER_DEFAULT_WIDTH)
        self._enabled = bool(params.get("enabled", True))
        self._recompute_gains()

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用 M/S width + pan。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            处理后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 单声道：width 无效（无 Side 信号），仅 pan
        if channels == 1:
            out[0] *= (self._pan_l + self._pan_r) * 0.5  # 单声道 pan = 平均
            return out.astype(samples.dtype, copy=False)

        # 多声道：默认处理前两声道（L/R），其余声道原样
        L = out[0]
        R = out[1]
        # Mid/Side 分解
        M = (L + R) * 0.5
        S = (L - R) * 0.5
        # 应用 width（Side 增益）
        S_scaled = S * self._side_gain
        # Mid/Side 重构
        new_L = M + S_scaled
        new_R = M - S_scaled
        # 应用 pan
        out[0] = new_L * self._pan_l
        out[1] = new_R * self._pan_r

        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """无状态效果，无需重置。"""
        pass

    @property
    def is_active(self) -> bool:
        # 仅当有实际效果时才活跃（pan≠0 或 width≠100）
        return self._enabled and (
            abs(self._pan) > 0.1 or abs(self._width - 100.0) > 0.1
        )


# ─────────────────────────────────────────────────────────────────────────────
# Expander / Gate 扩展器（FX 机架效果器，CW-A1 重构）
# ─────────────────────────────────────────────────────────────────────────────

# Expander 默认参数（与 ExpanderParams dataclass 一致，避免循环 import）
_EXP_DEFAULT_THRESHOLD_DB = -35.0
_EXP_DEFAULT_RANGE_DB = 18.0
_EXP_DEFAULT_RATIO = 1.8
_EXP_DEFAULT_ATTACK_MS = 1.4
_EXP_DEFAULT_HOLD_MS = 0.0
_EXP_DEFAULT_RELEASE_MS = 93.0

# Gate 迟滞：关门阈值低于开门阈值的固定偏移（dB），防止门限附近反复抖动
_GATE_HYSTERESIS_DB = 3.0


class ExpanderEffect(AudioEffect):
    """向下扩展器 / 噪声门（Expander / Gate 双模式）。

    Expander：低于阈值的信号按标准下扩斜率衰减——每低于阈值 1 dB 额外
    衰减 (ratio-1) dB（DY-02：1:2 → 20dB/20dB），range_db 封顶最大衰减。
    mode="Gate" 使用独立状态机（DY-02）：env ≥ threshold 开门，
    env < threshold-3dB（固定迟滞）关门，关门目标 = -range_db；
    attack 开门 / release 关门 / hold 冻结。包络线性域跟随峰值。

    遥测：``last_gr_db``（块内最大衰减，正 dB）供 GR 表。

    参数（ExpanderParams，接收 dict；旧 noise_gate 键由
    ExpanderParams.from_dict 归一化后进入）。线程安全：实例不共享。
    """

    effect_type = "expander"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._mode: str = "Expander"
        self._threshold_db: float = _EXP_DEFAULT_THRESHOLD_DB
        self._range_db: float = _EXP_DEFAULT_RANGE_DB
        self._ratio: float = _EXP_DEFAULT_RATIO
        self._attack_ms: float = _EXP_DEFAULT_ATTACK_MS
        self._hold_ms: float = _EXP_DEFAULT_HOLD_MS
        self._release_ms: float = _EXP_DEFAULT_RELEASE_MS
        self._enabled: bool = False
        # 包络（线性域，跨块连续）。初值 1.0（默认开门，与旧 NoiseGate
        # 的 _gate_gain=1.0 语义一致——避免块首 attack 期误衰减）。
        self._env_lin: float = 1.0
        self._hold_remaining: int = 0
        # DY-02: Gate 状态机（True=开门；hold 由状态机独占管理）
        self._gate_open: bool = True
        # Gate 增益平滑状态（dB，0=开门；attack 开门 / release 关门）
        self._gate_gain_db: float = 0.0
        self._attack_coeff: float = self._time_to_coeff(self._attack_ms)
        self._release_coeff: float = self._time_to_coeff(self._release_ms)
        # GR 遥测
        self.last_gr_db: float = 0.0

    @staticmethod
    def _time_to_coeff(time_ms: float, sample_rate: float = 48000.0) -> float:
        """由时间常数（ms）计算每采样指数平滑系数（0..1，越小越快）。"""
        if time_ms <= 0.0 or sample_rate <= 0.0:
            return 0.0
        return float(np.exp(-1.0 / (time_ms * 0.001 * sample_rate)))

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新扩展器参数。

        Args:
            params: dict 形式的 ExpanderParams，None 表示旁路。
            sample_rate: 可选新采样率
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._env_lin = 1.0

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 ExpanderParams 声明范围钳位（裸 dict 外部数据防护）。
        # 旧 noise_gate 键无损承接：无 mode/range 键但带 release_ms 键 → Gate
        legacy_gate = ("mode" not in params and "range_db" not in params
                       and "range" not in params and "release_ms" in params)
        mode = str(params.get("mode", "Gate" if legacy_gate else "Expander"))
        self._mode = mode if mode in ("Expander", "Gate") else ("Gate" if legacy_gate else "Expander")
        self._threshold_db = _clamp_finite(
            params.get("threshold_db", _EXP_DEFAULT_THRESHOLD_DB),
            -50.0, 0.0, _EXP_DEFAULT_THRESHOLD_DB)
        self._range_db = _clamp_finite(
            params.get("range_db", params.get("range", _EXP_DEFAULT_RANGE_DB)),
            0.0, 60.0, _EXP_DEFAULT_RANGE_DB)
        self._ratio = _clamp_finite(params.get("ratio", _EXP_DEFAULT_RATIO),
                                    1.1, 3.0, _EXP_DEFAULT_RATIO)
        self._attack_ms = _clamp_finite(params.get("attack_ms", _EXP_DEFAULT_ATTACK_MS),
                                        0.5, 100.0, _EXP_DEFAULT_ATTACK_MS)
        self._hold_ms = _clamp_finite(params.get("hold_ms", _EXP_DEFAULT_HOLD_MS),
                                      0.0, 4000.0, _EXP_DEFAULT_HOLD_MS)
        self._release_ms = _clamp_finite(params.get("release_ms", _EXP_DEFAULT_RELEASE_MS),
                                         50.0, 4000.0, _EXP_DEFAULT_RELEASE_MS)
        self._enabled = bool(params.get("enabled", True))
        self._attack_coeff = self._time_to_coeff(self._attack_ms, self._sample_rate)
        self._release_coeff = self._time_to_coeff(self._release_ms, self._sample_rate)

    def _gate_target_gain_db(self, env_db: np.ndarray) -> np.ndarray:
        """Gate 状态机目标增益（DY-02：迟滞 + hold，关门到达 range）。

        开门条件 env ≥ threshold；关门条件 env < threshold-hysteresis；
        迟滞区间保持原状态；hold 期内不关门。attack/release 平滑在
        process() 用 _smooth_attack_release 对目标序列统一施加。
        """
        threshold = self._threshold_db
        close_thr = threshold - _GATE_HYSTERESIS_DB
        hold_samples = int(round(self._hold_ms * 0.001 * self._sample_rate))
        gate_open = bool(getattr(self, "_gate_open", True))
        hold_left = int(self._hold_remaining)
        n = env_db.shape[0]
        target = np.empty(n, dtype=np.float64)
        env_list = env_db.tolist()
        for i in range(n):
            x = env_list[i]
            if x >= threshold:
                gate_open = True
                hold_left = hold_samples
            elif x < close_thr and hold_left <= 0:
                gate_open = False
            elif hold_left > 0:
                hold_left -= 1
            target[i] = 0.0 if gate_open else -self._range_db
        self._gate_open = gate_open
        self._hold_remaining = hold_left
        return target

    def _compute_gain_db(self, env_db: np.ndarray) -> np.ndarray:
        """扩展增益曲线（负增益 dB，向下扩展）。

        Expander：env < threshold 时 atten = (threshold - env) × (ratio - 1)
        （DY-02 标准下扩斜率：1:2 → 每低于 1dB 额外衰减 1dB），
        封顶 range_db；高于阈值不衰减。Gate 走独立状态机。
        """
        if self._mode == "Gate":
            return self._gate_target_gain_db(env_db)
        threshold = self._threshold_db
        ratio = self._ratio
        below = env_db < threshold
        atten = np.zeros_like(env_db)
        atten[below] = -np.minimum(
            (threshold - env_db[below]) * (ratio - 1.0), self._range_db)
        return atten

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用向下扩展（线性域包络 → dB 曲线，hold 冻结路径）。

        包络在线性域跟随峰值（attack 开门 / release 关门），再转 dB 计算
        扩展增益——dB 域逐样本平滑会骑在噪声峰值上导致低电平判定失真，
        线性域包络 + 长释放系数等效 RMS-ish 峰值探测（经典门限设计）。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            扩展后的 samples，同 shape（旁路时原样返回）。
        """
        self.last_gr_db = 0.0
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 声道联动：线性域峰值包络
        peak_input = np.max(np.abs(out), axis=0)

        # Gate 模式 hold 由状态机独占（_gate_target_gain_db），包络路径不消费
        # hold_remaining，避免双重冻结/刷新。
        use_env_hold = self._hold_ms > 0.0 and self._mode != "Gate"

        if not use_env_hold:
            env_lin, self._env_lin = _smooth_attack_release(
                peak_input, self._env_lin, self._attack_coeff, self._release_coeff)
            env = 20.0 * np.log10(np.maximum(env_lin, 1e-9))
        else:
            # hold：包络上升（attack）过阈值后冻结 hold 期再释放（release）
            env_lin = np.empty(n, dtype=np.float64)
            env_s = self._env_lin
            hold_left = self._hold_remaining
            att_c = self._attack_coeff
            rel_c = self._release_coeff
            thr_lin = 10.0 ** (self._threshold_db / 20.0)
            hold_samples = int(round(self._hold_ms * 0.001 * self._sample_rate))
            for i in range(n):
                x = peak_input[i]
                if x > env_s:
                    env_s = x + (env_s - x) * att_c
                    if x > thr_lin:
                        hold_left = hold_samples
                elif hold_left > 0:
                    hold_left -= 1
                else:
                    env_s = x + (env_s - x) * rel_c
                env_lin[i] = env_s
            self._env_lin = env_s
            self._hold_remaining = hold_left
            env = 20.0 * np.log10(np.maximum(env_lin, 1e-9))

        # 快路径：Expander 包络全程高于阈值 → 无衰减（Gate 不能走此快路径：
        # 状态机可能处于关门态，迟滞/hold 需要逐样本推进）
        if self._mode != "Gate" and not bool((env < self._threshold_db).any()):
            return samples

        gain_db = self._compute_gain_db(env)
        if self._mode == "Gate":
            # DY-02: 门开关增益按 attack/release 平滑（目标 0 ↔ -range 跳变
            # 不能直连，否则产生 click）。开门 rise=attack，关门 fall=release。
            gain_db, self._gate_gain_db = _smooth_attack_release(
                gain_db, self._gate_gain_db, self._attack_coeff,
                self._release_coeff)
        gain_linear = 10.0 ** (gain_db / 20.0)

        # GR 遥测（正 dB）
        min_gain = float(np.min(gain_linear))
        if min_gain < 0.999999:
            self.last_gr_db = -20.0 * float(np.log10(max(min_gain, 1e-6)))

        out *= gain_linear[np.newaxis, :]
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置包络状态（seek/暂停后调用）。"""
        self._env_lin = 1.0
        self._hold_remaining = 0
        self._gate_open = True
        self._gate_gain_db = 0.0
        self.last_gr_db = 0.0

    @property
    def is_active(self) -> bool:
        return self._enabled


class DynamicsEffect(AudioEffect):
    """Dynamics 完整单元（Da Vinci Fairlight 语义，CW-A1）。

    单一 DSP 实例内串联 Expander → Compressor → Limiter → Make Up，
    与 DynamicsDialog 的传输曲线链序一致。Make Up 是 **post-dynamics**
    节点（DY-01）：挂在 Limiter 之后而非内部附着 Comp——Comp 被旁路时
    Make Up 仍按定义生效。clip 级 Dynamics 是一条
    EffectInstance（effect_type="dynamics"，不在属性面板拆分为三张卡片）；
    轨级 Dynamics 仍以 panel="dynamics" 三实例存储（按实例的 GR/检源遥测；
    该形态下 makeup 随 comp 实例存储，位置约束在 MX-05 统一图阶段处理），
    两种形态共享同一子模块实现与同一面板 UI。

    参数（DynamicsParams dict）：
      - enabled: 总电源（False=整体旁路直通）
      - makeup:  全局 Make Up 增益（dB，0..24）；存储沿用 comp.makeup_gain
      - eg/comp/lim: Expander/Compressor/LimiterParams.to_dict() 子状态

    线程安全：实例不共享（每链一个），EffectChain 在 _mutex 下访问。
    """

    effect_type = "dynamics"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._eg = ExpanderEffect(sample_rate)
        self._comp = CompressorEffect(sample_rate)
        self._lim = LimiterEffect(sample_rate)
        self._enabled = False
        self._makeup = 0.0
        self._makeup_linear: float = 1.0
        self.last_gr_db = 0.0

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新三模块参数。子 dict 缺键由各子效果器的默认值承接（容错语义）。"""
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
        if params is None:
            self._enabled = False
            return

        self._enabled = bool(params.get("enabled", True))
        comp_src = dict(params.get("comp") or {})
        # DY-01: makeup 存储沿用 comp.makeup_gain（工程兼容），但消费移到
        # post-dynamics——不再注入 comp 子效果器（Comp 旁路时 makeup 仍生效）。
        self._makeup = _clamp_finite(
            params.get("makeup", comp_src.get("makeup_gain", 0.0)),
            0.0, 24.0, 0.0)
        self._makeup_linear = float(10.0 ** (self._makeup / 20.0))
        comp_src.pop("makeup_gain", None)
        # 子效果器按块向量化且跨块状态连续——set_params 只更新系数不丢状态
        self._eg.set_params(dict(params.get("eg") or {}), sample_rate=self._sample_rate)
        self._comp.set_params(comp_src, sample_rate=self._sample_rate)
        self._lim.set_params(dict(params.get("lim") or {}), sample_rate=self._sample_rate)

    def process(self, samples: np.ndarray,
                sidechain: Optional[np.ndarray] = None) -> np.ndarray:
        """EG → Comp（外部侧链可选）→ Lim → Make Up 串联处理。

        Args:
            samples: (channels, n) 样本块
            sidechain: 外部检源缓冲（sc_listen 时直接输出检源）

        Returns:
            处理后的 (channels, n)（旁路时原样返回）。
        """
        if not self._enabled:
            return samples
        out = self._eg.process(samples)
        out = self._comp.process(out, sidechain=sidechain)
        out = self._lim.process(out)
        # DY-01: Make Up = post-dynamics 节点（Lim 之后，Comp 旁路不失效）
        if abs(self._makeup_linear - 1.0) > 1e-9:
            out = out * self._makeup_linear
        # 聚合 GR 遥测（供统一 GR 表；子模块遥测详见各自 last_gr_db）
        grs = [
            getattr(self._eg, "last_gr_db", 0.0),
            getattr(self._comp, "last_gr_db", 0.0),
            getattr(self._lim, "last_gr_db", 0.0),
        ]
        try:
            self.last_gr_db = float(max(g for g in grs if g is not None))
        except (TypeError, ValueError):
            self.last_gr_db = 0.0
        return out

    def reset_state(self) -> None:
        """重置三模块的包络/延迟状态（seek/暂停后调用）。"""
        self._eg.reset_state()
        self._comp.reset_state()
        self._lim.reset_state()
        self.last_gr_db = 0.0

    @property
    def sc_source(self) -> str:
        """外部侧链检源 track_id（透传内部压缩器，供链依赖排序）。"""
        return getattr(self._comp, "sc_source", "internal")

    @property
    def is_active(self) -> bool:
        """总电源开启且子模块或独立 post-dynamics Make Up 生效。"""
        return self._enabled and (
            self._eg.is_active or self._comp.is_active or self._lim.is_active
            or abs(self._makeup_linear - 1.0) > 1e-9
        )


# ─────────────────────────────────────────────────────────────────────────────
# EffectChain 效果器链（FX 机架的 DSP 容器）
# ─────────────────────────────────────────────────────────────────────────────

# effect_type → AudioEffect 子类 的注册表（用于 EffectChain 重建实例）
# 延迟填充避免循环 import（timeline_v2 的 dataclass 在 commit 2 才接入）
_EFFECT_CLASS_REGISTRY: dict = {}


def _ensure_effect_registry() -> dict:
    """惰性初始化效果器类注册表。"""
    if not _EFFECT_CLASS_REGISTRY:
        _EFFECT_CLASS_REGISTRY["strip_eq"] = StripEQFilter
        _EFFECT_CLASS_REGISTRY["limiter"] = LimiterEffect
        _EFFECT_CLASS_REGISTRY["compressor"] = CompressorEffect
        _EFFECT_CLASS_REGISTRY["reverb"] = ReverbEffect
        _EFFECT_CLASS_REGISTRY["delay"] = DelayEffect
        _EFFECT_CLASS_REGISTRY["saturation"] = SaturationEffect
        _EFFECT_CLASS_REGISTRY["imager"] = ImagerEffect
        _EFFECT_CLASS_REGISTRY["expander"] = ExpanderEffect
        # CW-A1: NoiseGate 由 Expander(Gate) 无损承接（timeline_v2 侧已做键归一化）
        _EFFECT_CLASS_REGISTRY["noise_gate"] = ExpanderEffect
        # Dynamics 完整单元（EG→Comp→Lim 单实例复合）
        _EFFECT_CLASS_REGISTRY["dynamics"] = DynamicsEffect
        # NF（2026-11-15 计划 §6.3）：两款新复合 FX（适配器惰性导入，
        # 顶层无环）
        from src.features.edit.core.audio_effects.adapters import (
            SpectralManglerAdapter,
            VoiceChangerAdapter,
        )
        _EFFECT_CLASS_REGISTRY["spectral_mangler"] = SpectralManglerAdapter
        _EFFECT_CLASS_REGISTRY["voice_changer"] = VoiceChangerAdapter
        # P1（2026-11-19 计划第三阶段 #1/#3）：De-Esser / True Peak Limiter
        from src.features.edit.core.audio_effects.adapters import (
            DeEsserAdapter,
            DynamicEqAdapter,
            TruePeakLimiterAdapter,
        )
        _EFFECT_CLASS_REGISTRY["de_esser"] = DeEsserAdapter
        _EFFECT_CLASS_REGISTRY["dynamic_eq"] = DynamicEqAdapter
        _EFFECT_CLASS_REGISTRY["true_peak_limiter"] = TruePeakLimiterAdapter
    return _EFFECT_CLASS_REGISTRY


class EffectChain:
    """有序效果器链。

    按顺序串联多个 AudioEffect，每个独立维护状态。
    AudioMixer 每轨一个实例，在 _mutex 保护下访问。

    set_params 接收解析后的条目列表（不直接依赖 EffectChainParams dataclass，
    避免 audio_dsp ↔ timeline_v2 的循环依赖）。条目支持 3 元组
    ``(effect_type, params, enabled)`` 与 4 元组
    ``(effect_type, params, enabled, instance_id)``（CW-A1：instance_id 贯穿
    DSP 层，供 GR 遥测与侧链检源映射按实例定位）。

    process 支持按 instance_id 的侧链检源透传（压缩器外部检源）。
    """

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)
        # 有序 effect 实例列表 + 每个的 enabled 标志 + instance_id
        self._effects: list = []  # list[AudioEffect]
        self._enabled_flags: list = []  # list[bool]
        self._instance_ids: list = []  # list[str]
        # 缓存最后传入的 entries，供 set_sample_rate 等场景重新应用
        self._last_entries: Optional[list] = None
        # MX-02: 本块按实例 tap（process 时采样；tap_levels() 读取）
        self._tap_levels: dict = {}
        # R09: {effect_index: group_post_gain_db}——panel="dynamics" 组的
        # 组后 Make Up（在该组最后一个成员处理完后应用）。组内压缩器的
        # makeup_gain 已在 set_params 时清零，增益不在 Limiter 前施加。
        self._group_post_gain_db: dict = {}

    def set_params(self, entries: Optional[list], sample_rate: Optional[float] = None) -> None:
        """更新效果器链。

        Args:
            entries: [(effect_type, params, enabled) | (effect_type, params,
                     enabled, instance_id), ...] 按 order 排序；
                     None 或空列表表示清空（链不生效）。
            sample_rate: 可选新采样率，变化时重置所有效果器状态。
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)

        self._last_entries = list(entries) if entries else None

        if not entries:
            self._effects = []
            self._enabled_flags = []
            self._instance_ids = []
            return

        registry = _ensure_effect_registry()
        new_effects: list = []
        new_flags: list = []
        new_ids: list = []
        new_panels: list = []
        # R09: 组后 Make Up——panel="dynamics" 的压缩器携带 makeup_post=
        # True 时：实例参数的 makeup_gain 清零（不在 Limiter 前施加），
        # 增益记到其所在 Dynamics 组（连续 panel="dynamics" 段）的末位。
        group_post_gain: dict = {}
        # MX-06：实例身份池——优先按 (类型, instance_id) 精确匹配复用旧实例
        # （调序/删除/替换不串状态）；legacy 无 id 条目回退按类型匹配（旧工程
        # 语义：拖动参数不重建）。两类匹配都要求类型一致。
        old_pool = list(zip(self._effects, self._enabled_flags, self._instance_ids))
        used = [False] * len(old_pool)

        for entry in entries:
            if len(entry) >= 4:
                effect_type, params, enabled, instance_id = entry[0], entry[1], entry[2], entry[3]
            else:
                effect_type, params, enabled = entry[0], entry[1], entry[2]
                instance_id = ""
            # R01：5 元组携带 algorithm_version（0=未标注→legacy 桶）
            algorithm_version = int(entry[4]) if len(entry) >= 5 else 0
            # R09：6 元组携带 panel（DY 面板实例标记）
            panel = str(entry[5]) if len(entry) >= 6 else "fx"
            cls = registry.get(effect_type)
            if cls is None:
                # 审计修复：未知效果类型不再静默跳过（预览/导出会无声
                # 丢弃该效果，用户无从得知）——记录可定位警告
                logger.warning(
                    "EffectChain.set_params: unknown effect_type %r "
                    "skipped (effect silently dropped in preview/export)",
                    effect_type,
                )
                continue
            instance_id = str(instance_id)
            instance = None
            if instance_id:
                # 精确身份匹配：同类型 + 同 instance_id
                for j, (old_inst, _old_en, old_id) in enumerate(old_pool):
                    if not used[j] and type(old_inst) is cls and old_id == instance_id:
                        instance = old_inst
                        used[j] = True
                        break
            if instance is None and not instance_id:
                # legacy（无 id）：按类型匹配未使用的旧实例（保持拖动参数
                # 不重建的旧行为）
                for j, (old_inst, _old_en, _old_id) in enumerate(old_pool):
                    if not used[j] and type(old_inst) is cls:
                        instance = old_inst
                        used[j] = True
                        break
            if instance is None:
                instance = cls(self._sample_rate)
            inst_params = params
            _post_makeup_db = 0.0
            if (panel == "dynamics" and effect_type == "compressor"
                    and isinstance(params, dict)
                    and params.get("makeup_post")):
                try:
                    _post_makeup_db = float(params.get("makeup_gain", 0.0) or 0.0)
                except (TypeError, ValueError):
                    _post_makeup_db = 0.0
                if _post_makeup_db:
                    inst_params = dict(params)
                    inst_params["makeup_gain"] = 0.0
            _apply_params_with_version(instance, inst_params, self._sample_rate,
                                       algorithm_version)
            new_effects.append(instance)
            new_flags.append(bool(enabled))
            new_ids.append(instance_id)
            new_panels.append(panel)
            if _post_makeup_db and bool(enabled):
                group_post_gain[len(new_effects) - 1] = _post_makeup_db

        # R09: 把组内成员的组后增益挪到 Dynamics 组（连续 panel="dynamics"
        # 段）的最后一个成员之后
        self._group_post_gain_db = {}
        if group_post_gain:
            n_eff = len(new_effects)
            for i, db in group_post_gain.items():
                j = i
                while j + 1 < n_eff and new_panels[j + 1] == "dynamics":
                    j += 1
                self._group_post_gain_db[j] = \
                    self._group_post_gain_db.get(j, 0.0) + db

        self._effects = new_effects
        self._enabled_flags = new_flags
        self._instance_ids = new_ids

    def install_freeze_capture_by_id(self, instance_id: str,
                                     capture: dict) -> bool:
        """H09：向指定实例装载 Freeze 捕获资产（仅 spectral_mangler 适配
        器支持；找不到实例/类型不符返回 False）。"""
        for i, iid in enumerate(self._instance_ids):
            if iid != instance_id:
                continue
            inst = self._effects[i] if i < len(self._effects) else None
            installer = getattr(inst, "install_freeze_capture", None)
            if callable(installer):
                return bool(installer(capture))
            return False
        return False

    def process(self, samples: np.ndarray,
                sidechain_by_id: Optional[dict] = None) -> np.ndarray:
        """依次应用链中每个启用的效果器。

        MX-02: 每个实例处理前后真实采样 in/out 峰值（dBFS），与 GR/检源
        遥测一起按 instance_id 存入 self._tap_levels（tap_levels() 读取）。
        旁路/未启用实例记录 out=in（直通语义），不残留上一块读数。

        Args:
            samples: (channels, n) 样本块
            sidechain_by_id: {instance_id: (channels, n) 检源缓冲}，
                仅压缩器（外部侧链开启时）消费。
        """
        self._tap_levels = {}
        if not self._effects:
            return samples
        out = samples
        for i, (inst, enabled) in enumerate(zip(self._effects, self._enabled_flags)):
            iid = self._instance_ids[i] if i < len(self._instance_ids) else ""
            etype = getattr(inst, "effect_type", "")
            if not (enabled and inst.is_active):
                if iid:
                    # MX-02: 旁路实例直通语义（无过期 GR，UI 表归零）
                    self._tap_levels[iid] = {
                        "effect_type": etype, "in_db": -100.0,
                        "out_db": -100.0, "gr_db": 0.0, "sc_db": -100.0,
                        "bypassed": True,
                    }
                continue
            in_peak = float(np.max(np.abs(out))) if out.size else 0.0
            in_db = max(-100.0, 20.0 * float(np.log10(max(in_peak, 1e-9))))
            sc_buf = None
            # DynamicsEffect 内部含压缩器：侧链缓冲透传给其内部 comp
            if isinstance(inst, (CompressorEffect, DynamicsEffect)) and sidechain_by_id:
                sc = sidechain_by_id.get(iid) if iid else None
                if sc is not None and isinstance(sc, np.ndarray) and sc.size:
                    sc_buf = sc
                out = inst.process(out, sidechain=sc)
            else:
                out = inst.process(out)
            out_peak = float(np.max(np.abs(out))) if out.size else 0.0
            out_db = max(-100.0, 20.0 * float(np.log10(max(out_peak, 1e-9))))
            if iid:
                tap = {
                    "effect_type": etype,
                    "in_db": in_db, "out_db": out_db,
                    "gr_db": float(getattr(inst, "last_gr_db", 0.0) or 0.0),
                    "sc_db": float(getattr(inst, "last_detector_db", -100.0)
                                   if sc_buf is not None else -100.0),
                    "bypassed": False,
                }
                # DY-03: Limiter 过冲报告（有该遥测的实例才出键）
                overshoot = getattr(inst, "last_overshoot_db", None)
                if overshoot is not None:
                    tap["overshoot_db"] = float(overshoot)
                collect_analysis = getattr(inst, "collect_telemetry", None)
                if callable(collect_analysis):
                    # Processor snapshots contain measured scalar analysis;
                    # keep them tied to this instance instead of inventing
                    # module activity from the track's final output meter.
                    tap["analysis"] = dict(collect_analysis())
                self._tap_levels[iid] = tap
            # R09: Dynamics 组组后 Make Up——组末成员处理完后统一施加
            # （旁路的组不计：其压缩器的 enabled=False 时 set_params 已
            # 不登记该增益；仍登记的是组内其他启用压缩器）
            _gdb = self._group_post_gain_db.get(i)
            if _gdb:
                out = out * float(10.0 ** (_gdb / 20.0))
        return out

    def tap_levels(self) -> dict:
        """MX-02: 最近一次 process 的按实例 tap（{instance_id: {...}}）。

        未启用/旁路实例也在列（直通语义 + bypassed 标记），消费者可区分
        "真实读数" 与 "旁路归零"。process 未运行过时为空 dict。
        """
        return self._tap_levels

    def collect_gain_reduction(self) -> dict:
        """收集链内各实例的块内最大增益衰减（正 dB）。

        MX-06：旁路实例不再暴露上一块的过期 GR——统一回报 0.0（UI 清表），
        非 float / 缺失遥测的实例不出现在结果中。
        """
        result: dict = {}
        for i, inst in enumerate(self._effects):
            iid = self._instance_ids[i] if i < len(self._instance_ids) else ""
            if not iid:
                continue
            if not (self._enabled_flags[i] and inst.is_active):
                result[iid] = 0.0  # 旁路读数清零，不暴露上一块 GR
                continue
            gr = getattr(inst, "last_gr_db", None)
            if gr is None or not isinstance(gr, float) or not np.isfinite(gr):
                continue
            result[iid] = gr
        return result

    def collect_detector_levels(self) -> dict:
        """收集压缩器实例的检源电平（dBFS）。旁路实例同样清零。"""
        result: dict = {}
        for i, inst in enumerate(self._effects):
            iid = self._instance_ids[i] if i < len(self._instance_ids) else ""
            if not iid:
                continue
            if not (self._enabled_flags[i] and inst.is_active):
                result[iid] = -100.0
                continue
            det = getattr(inst, "last_detector_db", None)
            if det is None or not isinstance(det, float) or not np.isfinite(det):
                continue
            result[iid] = det
        return result

    def sidechain_sources(self) -> set:
        """链内启用外部侧链的检源 track_id 集合（供引擎排序依赖图）。"""
        sources: set = set()
        for inst in self._effects:
            src = getattr(inst, "sc_source", "internal")
            if src and src != "internal":
                sources.add(src)
        return sources

    def sidechain_map(self, taps: dict) -> dict:
        """把可用的检源缓冲映射到本链需要侧链的实例。

        Args:
            taps: {source_track_id: (channels, n) 缓冲}（引擎维护的已混轨输出）

        Returns:
            {instance_id: 缓冲}，仅含 sc_source 命中 taps 的压缩器实例；
            检源缺失（未解码/被删）的实例不出现在映射中 → 内部检源回退。
        """
        result: dict = {}
        for i, inst in enumerate(self._effects):
            src = getattr(inst, "sc_source", "internal")
            if not src or src == "internal":
                continue
            buf = taps.get(src)
            if buf is None:
                continue
            iid = self._instance_ids[i] if i < len(self._instance_ids) else ""
            if iid:
                result[iid] = buf
        return result

    def reset_state(self) -> None:
        """重置所有效果器状态。"""
        for inst in self._effects:
            inst.reset_state()

    def tail_estimate_samples(self) -> int:
        """Conservatively compose serial tails for feed-zero scheduling.

        A downstream delay starts its tail when the upstream tail reaches it;
        taking only the maximum can stop the chain before its first echo.
        Ignore invalid estimates rather than corrupting the scheduler budget.
        """
        est = 0
        for inst, enabled in zip(self._effects, self._enabled_flags):
            if not enabled:
                continue
            try:
                tail = float(inst.tail_estimate_samples())
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            if np.isfinite(tail) and tail > 0.0:
                est += int(np.ceil(tail))
        return est

    def latency_samples(self) -> int:
        """P0 · 链内算法延迟合计（串联求和；PDC 图级补偿的输入）。

        各启用实例的 ``latency_samples()`` 相加（WOLA/FIR 等块式算法的
        群延迟）。经典 DSP（delay/reverb/compressor…）无声明按 0 计。
        图级补偿由 ``AudioGraphRenderer`` 以"最迟路径对齐"消费——不同
        轨的脉冲在 Master 汇合处落在同一绝对样本。无效声明按 0 忽略。
        """
        total = 0
        for inst, enabled in zip(self._effects, self._enabled_flags):
            if not enabled:
                continue
            try:
                lat = int(inst.latency_samples())
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            if lat > 0:
                total += lat
        return total

    @property
    def is_active(self) -> bool:
        """链中是否至少有一个启用的效果器。"""
        for inst, enabled in zip(self._effects, self._enabled_flags):
            if enabled and inst.is_active:
                return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SpatialPanner 空间声像器（立体声域实现，CW-A1）
# ─────────────────────────────────────────────────────────────────────────────

# 等功率声像：源位置 p∈[-1,1] 对左右扬声器的增益
def _equal_power_gains(p: float) -> tuple:
    theta = (max(-1.0, min(1.0, p)) + 1.0) * 0.5 * (np.pi / 2.0)
    return float(np.cos(theta)), float(np.sin(theta))


def spatial_panner_matrix(pan: float, panner: Optional[dict]) -> np.ndarray:
    """计算空间声像器的 2×2 立体声增益矩阵（纯函数，供引擎/测试/可视化共用）。

    卫星节点数学与 UI 声场舞台一致：锚点 x = pan，卫星 ±spread 向量绕锚点
    旋转 rotate 度；L/R 声道内容位于两卫星位置，经等功率 VBAP 投影到 ±45°
    立体声扬声器；divergence 引入对侧串音。fb 参与 spread_auto 的收敛
    公式（立体声域唯一可闻的 fb 影响）。

    Args:
        pan: 声像位置 [-1, 1]（EditTrack.pan，lr 真源）
        panner: SpatialPannerParams.to_dict()；None/空 dict → 单位矩阵
            （由调用方回退普通 pan/balance）

    Returns:
        (2, 2) 矩阵 M：out = M @ [in_L, in_R]。
    """
    if not panner or not panner.get("enabled", True):
        return np.eye(2, dtype=np.float64)

    x = max(-1.0, min(1.0, float(pan)))
    y = max(-1.0, min(1.0, float(panner.get("fb", 100.0)) / 100.0))
    spread = max(0.0, min(1.0, float(panner.get("spread", 100.0)) / 100.0))
    if panner.get("spread_auto", False):
        # Auto Spread：越靠边界/角落越向点声源收敛（与 UI 一致）
        spread = max(0.0, min(1.0, (1.0 - abs(x)) * (1.0 - 0.35 * abs(y))))
    rotate_deg = float(panner.get("rotate", 0.0))
    theta = np.radians(-rotate_deg)
    dx = spread * float(np.cos(theta))
    dy = spread * float(np.sin(theta))

    # 卫星位置（x 轴：-1 左 .. +1 右；y 仅通过 spread_auto/dy 影响投影角度）
    sat_l = (x - dx, dy)
    sat_r = (x + dx, dy)

    def _proj(pos: tuple) -> tuple:
        # 卫星位置 → 位置标量 p（x 分量为主，y 轻微偏置宽度）
        p = pos[0] + 0.18 * pos[1]
        return _equal_power_gains(p)

    gl_l, gl_r = _proj(sat_l)   # L 声道内容 → 左/右扬声器
    gr_l, gr_r = _proj(sat_r)   # R 声道内容 → 左/右扬声器
    m = np.array([[gl_l, gr_l],
                  [gl_r, gr_r]], dtype=np.float64)

    # divergence：对侧串音（0=无，100=50% 交叉）
    div = max(0.0, min(1.0, float(panner.get("divergence", 0.0)) / 100.0)) * 0.5
    if div > 0.0:
        cross = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float64)
        m = (1.0 - div) * m + div * (cross @ m)
    return m


class SpatialPanner:
    """空间声像器 DSP（有状态：boom 低通滤波器跨块连续）。

    立体声域实现（引擎输出为 2ch）：
      - spread/rotate/divergence：2×2 矩阵（spatial_panner_matrix）
      - pan/balance：由引擎逐样本增益与矩阵复合（mono equal-power /
        stereo balance 语义保持不变）
      - boom：单声道低通（120Hz 二阶）×boom_gain 低频发送
        Pre（矩阵前）/Post（矩阵后）/Only（仅低频）三路由——立体声域
        对 LFE 发送的近似，多声道引擎后启用真实 LFE 声道
      - fb/elevation/format/space_mode：持久化 + 可视化，立体声域不直接
        可闻（fb 经 spread_auto 参与收敛公式）
    """

    BOOM_CUTOFF_HZ = 120.0

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)
        # boom 二阶低通 SOS 状态（单声道）
        self._boom_sos: Optional[np.ndarray] = None
        self._boom_zi: Optional[np.ndarray] = None
        self._panner: Optional[dict] = None
        self._boom_gain: float = 0.0
        self._boom_on: bool = False
        self._boom_route: str = "Pre"
        self._rebuild_boom_filter()

    def set_params(self, panner: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新声像器参数（dict 形式或 None=未使用回退普通 pan）。"""
        if sample_rate is not None and abs(sample_rate - self._sample_rate) > 0.5:
            self._sample_rate = float(sample_rate)
            self._boom_zi = None
            self._rebuild_boom_filter()
        self._panner = panner if isinstance(panner, dict) and panner else None
        if self._panner is None:
            self._boom_on = False
            return
        self._boom_on = bool(self._panner.get("boom_on", False))
        self._boom_route = str(self._panner.get("boom_route", "Pre") or "Pre")
        try:
            self._boom_gain = _db_to_lin_gain(float(self._panner.get("boom_gain", -60.0)))
        except (TypeError, ValueError):
            self._boom_gain = 0.0

    def _rebuild_boom_filter(self) -> None:
        fc = min(self.BOOM_CUTOFF_HZ, self._sample_rate * 0.45)
        b, a = _biquad_lowpass(fc, _BUTTERWORTH_Q, self._sample_rate)
        self._boom_sos = np.array([[b[0], b[1], b[2], 1.0, a[1], a[2]]], dtype=np.float64)
        self._boom_zi = None

    def _boom_bass(self, samples: np.ndarray) -> np.ndarray:
        """mono 求和 → 120Hz 低通 → boom 增益。返回 (n,)。"""
        mono = np.mean(samples, axis=0) if samples.shape[0] > 1 else samples[0]
        if self._boom_gain <= 0.0:
            return np.zeros_like(mono)
        if sosfilt is None or self._boom_sos is None:
            return mono * self._boom_gain
        if self._boom_zi is None:
            self._boom_zi = np.zeros((1, 1, 2), dtype=np.float64)
        out, zf = sosfilt(self._boom_sos, mono[np.newaxis, :], axis=-1, zi=self._boom_zi)
        self._boom_zi = zf
        return out[0] * self._boom_gain

    def process(self, samples: np.ndarray, pan: float) -> np.ndarray:
        """应用空间声像（矩阵 + boom）。pan 的逐样本增益由引擎叠加。

        Args:
            samples: (channels, n)；≥2 声道取前两路处理，其余声道直通
            pan: 块内声像位置 [-1, 1]（lr 真源）

        Returns:
            处理后的 samples（未启用时原样返回）。
        """
        if not self._panner or not self._panner.get("enabled", True):
            return samples
        if samples.ndim != 2 or samples.shape[0] < 2 or samples.shape[1] < 1:
            return samples
        out = samples.astype(np.float64, copy=True)
        n = out.shape[1]

        boom_only = self._boom_on and self._boom_route == "Only"
        if boom_only:
            bass = self._boom_bass(out[:2])
            out[0] = bass
            out[1] = bass
            return out.astype(samples.dtype, copy=False)

        pre_bass = self._boom_bass(out[:2]) if (self._boom_on and self._boom_route == "Pre") else None

        m = spatial_panner_matrix(pan, self._panner)
        l = out[0]
        r = out[1]
        # PAN-01：两行输出都读取未污染的原输入（out[0] 先覆写会让 out[1]
        # 读到已混合的 L，产生 0.1465 量级的矩阵偏差）。
        new_l = m[0, 0] * l + m[0, 1] * r
        new_r = m[1, 0] * l + m[1, 1] * r
        out[0] = new_l
        out[1] = new_r

        if pre_bass is not None:
            out[0] += pre_bass
            out[1] += pre_bass
        if self._boom_on and self._boom_route == "Post":
            post_bass = self._boom_bass(out[:2])
            out[0] += post_bass
            out[1] += post_bass
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置 boom 滤波状态。"""
        self._boom_zi = None

    @property
    def is_active(self) -> bool:
        return bool(self._panner and self._panner.get("enabled", True))


# ─────────────────────────────────────────────────────────────────────────────
# Master True-Peak Limiter（AM-09 阶段 2.5）
# ─────────────────────────────────────────────────────────────────────────────

class MasterLimiter:
    """连续状态的 Master 峰值限幅器（AM-09）。

    替代此前"按每个 buffer 最大峰值整体缩放"的无状态归一化——后者相邻 buffer
    缩放比例不同，造成 audible pumping。

    本 limiter 维护**连续的增益包络状态**（gain envelope），跨 buffer 平滑：
    - 当信号超过阈值时，按 attack 时间降低增益
    - 信号回落时，按 release 时间恢复增益
    - 增益变化平滑，无 block-boundary gain jump

    阈值默认 1.0（满量程），可配置以预留 headroom。
    gain 永远 ≤ 1.0（只衰减不放大），保护不削波。

    线程安全：实例不共享（每个渲染器一个）。
    """

    def __init__(self, sample_rate: float = 48000.0, threshold: float = 1.0,
                 attack_ms: float = 1.0, release_ms: float = 100.0) -> None:
        """初始化限幅器。

        Args:
            sample_rate: 采样率
            threshold: 阈值（线性振幅，信号超过此值时开始衰减），默认 1.0
            attack_ms: attack 时间（ms），越短响应越快，默认 1ms（快速保护防削波）
            release_ms: release 时间（ms），越长恢复越平滑，默认 100ms
        """
        self._sample_rate = float(sample_rate)
        self._threshold = float(threshold)
        # 连续增益状态（1.0=不衰减，<1.0=衰减中）
        self._gain: float = 1.0
        self._attack_ms = float(attack_ms)
        self._release_ms = float(release_ms)
        # attack/release 系数缓存（参数只在构造/采样率变化时更新）
        self._attack_coeff = self._compute_attack_coeff()
        self._release_coeff = self._compute_release_coeff()

    def _compute_attack_coeff(self) -> float:
        return float(np.exp(-1.0 / max(0.001, self._attack_ms * 0.001 * self._sample_rate)))

    def _compute_release_coeff(self) -> float:
        return float(np.exp(-1.0 / max(0.001, self._release_ms * 0.001 * self._sample_rate)))

    def set_sample_rate(self, sample_rate: float) -> None:
        """更新采样率（重置状态避免系数失配）。"""
        if abs(sample_rate - self._sample_rate) > 0.5:
            self._sample_rate = float(sample_rate)
            self._gain = 1.0
            self._attack_coeff = self._compute_attack_coeff()
            self._release_coeff = self._compute_release_coeff()

    def reset(self) -> None:
        """重置增益状态（seek/暂停后调用）。"""
        self._gain = 1.0

    @property
    def threshold(self) -> float:
        return self._threshold

    @threshold.setter
    def threshold(self, value: float) -> None:
        self._threshold = max(0.01, float(value))

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用连续状态限幅。

        逐样本维护增益包络：超过阈值时按 attack 系数降低增益，
        低于阈值时按 release 系数恢复。增益乘到所有声道。

        快路径：增益已恢复到 1.0 且块内无超阈值样本 → 目标增益恒为 1.0，
        包络保持 1.0 不动，可精确直通（混音输出缓冲为每次调用新建的数组，
        直接返回输入不产生别名问题）。

        Args:
            samples: (channels, n) 样本块

        Returns:
            限幅后的样本（同 shape）。
        """
        n = samples.shape[-1]
        if n == 0:
            return samples

        # 常见路径短路：无限幅需求
        if self._gain >= 1.0:
            peak_early = float(np.max(np.abs(samples))) if samples.size else 0.0
            if peak_early <= self._threshold:
                return samples

        # 跨声道的峰值（取所有声道绝对值最大）
        abs_max = np.max(np.abs(samples), axis=0) if samples.ndim > 1 else np.abs(samples)
        # 目标增益：信号超过阈值时 = 阈值/信号，否则 1.0
        target_gain = np.where(abs_max > self._threshold,
                               self._threshold / np.maximum(abs_max, 1e-9),
                               1.0)

        # 增益平滑（attack 下降快、release 上升慢）：分支方向与
        # _smooth_attack_release 的 rise/fall 相反（目标低于增益时衰减）。
        out_gain, self._gain = _smooth_attack_release(
            target_gain.astype(np.float64, copy=False), self._gain,
            self._release_coeff, self._attack_coeff)

        # 应用增益到所有声道
        out = (samples * out_gain).astype(samples.dtype, copy=False)
        # 硬保护：attack 期间可能仍有瞬时超阈值（attack limiter 固有特性，
        # 完全消除需 look-ahead buffer）。末端钳制确保绝对不削波，
        # 但因增益已平滑，钳制极少触发（仅 attack 瞬态）。
        peak = float(np.max(np.abs(out))) if out.size > 0 else 0.0
        if peak > self._threshold:
            # Guard each sample with a channel-linked gain. Scaling the whole
            # block by its largest peak attenuates earlier samples based on a
            # future transient and makes export depend on block partitioning.
            sample_peak = (np.max(np.abs(out), axis=0)
                           if out.ndim > 1 else np.abs(out))
            guard = self._threshold / np.maximum(sample_peak, self._threshold)
            out = out * guard
        return out
