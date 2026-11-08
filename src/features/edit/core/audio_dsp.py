"""
Audio DSP utilities — Graphic EQ biquad filters (W7/W26).

多频段图形均衡器（Graphic EQ），可选 3 / 7 / 10 / 15 频段模式。
每频段为一个 RBJ Audio EQ Cookbook biquad：
  - lowshelf  （最低频段）
  - peaking   （中间频段，独立 Q 值）
  - highshelf （最高频段）

可选 Low-Cut（highpass）和 High-Cut（lowpass）滤波器在频段链之前。

滤波器系数基于 RBJ Audio EQ Cookbook（Robert Bristow-Johnson）：
  https://www.musicdsp.org/en/latest/Filters/135-rbj-audio-eq-cookbook.html

使用 scipy.signal.lfilter 做实时 IIR 滤波（C 实现，性能足够）。
所有系数按 StripEQParams 预算并缓存（参数变化时重算）。

线程安全：本模块无状态，滤波器对象本身不共享（每轨一个实例）。
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
    from scipy.signal import lfilter, sosfilt
except ImportError:  # pragma: no cover
    lfilter = None  # type: ignore[assignment]
    sosfilt = None  # type: ignore[assignment]

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

def _biquad_lowshelf(freq_hz: float, gain_db: float, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """LowShelf 滤波器系数（b, a）。

    Args:
        freq_hz: 中心/转折频率（Hz）
        gain_db: 增益（dB），正值提升、负值衰减
        sample_rate: 采样率（Hz）

    Returns:
        (b, a) IIR 系数，长度均为 3，scipy.signal.lfilter 格式。
    """
    A = 10.0 ** (gain_db / 40.0)  # gain_db/2 转 linear，再开方（RBJ 公式）
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    # shelf slope factor S=1（标准 12dB/oct）
    alpha = sin_w0 / 2.0 * np.sqrt((A + 1.0 / A) * (1.0 / 1.0 - 1.0) + 2.0)
    # 上式 sqrt 内 (1/S - 1) + 2，S=1 时简化为 sqrt(2)
    alpha = sin_w0 / 2.0 * np.sqrt(2.0)

    b0 = A * ((A + 1) - (A - 1) * cos_w0 + 2.0 * np.sqrt(A) * alpha)
    b1 = 2.0 * A * ((A - 1) - (A + 1) * cos_w0)
    b2 = A * ((A + 1) - (A - 1) * cos_w0 - 2.0 * np.sqrt(A) * alpha)
    a0 = (A + 1) + (A - 1) * cos_w0 + 2.0 * np.sqrt(A) * alpha
    a1 = -2.0 * ((A - 1) + (A + 1) * cos_w0)
    a2 = (A + 1) + (A - 1) * cos_w0 - 2.0 * np.sqrt(A) * alpha

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


def _biquad_highshelf(freq_hz: float, gain_db: float, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """HighShelf 滤波器系数（b, a）。"""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * freq_hz / sample_rate
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    alpha = sin_w0 / 2.0 * np.sqrt(2.0)  # S=1

    b0 = A * ((A + 1) + (A - 1) * cos_w0 + 2.0 * np.sqrt(A) * alpha)
    b1 = -2.0 * A * ((A - 1) + (A + 1) * cos_w0)
    b2 = A * ((A + 1) + (A - 1) * cos_w0 - 2.0 * np.sqrt(A) * alpha)
    a0 = (A + 1) - (A - 1) * cos_w0 + 2.0 * np.sqrt(A) * alpha
    a1 = 2.0 * ((A - 1) - (A + 1) * cos_w0)
    a2 = (A + 1) - (A - 1) * cos_w0 - 2.0 * np.sqrt(A) * alpha

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


# ─────────────────────────────────────────────────────────────────────────────
# EQ 频段配置（移植自 audio_pro EQBands）
# ─────────────────────────────────────────────────────────────────────────────
# 每频段: {"name", "freq", "type", "q"}，type ∈ {"lowshelf", "peaking", "highshelf"}
# 第一段固定 lowshelf，最后一段固定 highshelf，中间为 peaking。

EQ_BAND_CONFIGS: dict = {
    "3-band": (
        {"name": "Low",  "freq": 320.0,  "type": "lowshelf",  "q": 0.7},
        {"name": "Mid",  "freq": 1000.0, "type": "peaking",   "q": 0.5},
        {"name": "High", "freq": 3200.0, "type": "highshelf", "q": 0.7},
    ),
    "7-band": (
        {"name": "32Hz",  "freq": 32.0,    "type": "lowshelf",  "q": 0.7},
        {"name": "64Hz",  "freq": 64.0,    "type": "peaking",   "q": 1.0},
        {"name": "250Hz", "freq": 250.0,   "type": "peaking",   "q": 1.0},
        {"name": "1kHz",  "freq": 1000.0,  "type": "peaking",   "q": 0.5},
        {"name": "4kHz",  "freq": 4000.0,  "type": "peaking",   "q": 1.0},
        {"name": "8kHz",  "freq": 8000.0,  "type": "peaking",   "q": 1.0},
        {"name": "16kHz", "freq": 16000.0, "type": "highshelf", "q": 0.7},
    ),
    "10-band": (
        {"name": "31Hz",  "freq": 31.0,    "type": "lowshelf",  "q": 0.7},
        {"name": "63Hz",  "freq": 63.0,    "type": "peaking",   "q": 1.4},
        {"name": "125Hz", "freq": 125.0,   "type": "peaking",   "q": 1.4},
        {"name": "250Hz", "freq": 250.0,   "type": "peaking",   "q": 1.4},
        {"name": "500Hz", "freq": 500.0,   "type": "peaking",   "q": 1.4},
        {"name": "1kHz",  "freq": 1000.0,  "type": "peaking",   "q": 1.0},
        {"name": "2kHz",  "freq": 2000.0,  "type": "peaking",   "q": 1.4},
        {"name": "4kHz",  "freq": 4000.0,  "type": "peaking",   "q": 1.4},
        {"name": "8kHz",  "freq": 8000.0,  "type": "peaking",   "q": 1.4},
        {"name": "16kHz", "freq": 16000.0, "type": "highshelf", "q": 0.7},
    ),
    "15-band": (
        {"name": "20Hz",   "freq": 20.0,    "type": "lowshelf",  "q": 0.7},
        {"name": "32Hz",   "freq": 32.0,    "type": "peaking",   "q": 1.4},
        {"name": "50Hz",   "freq": 50.0,    "type": "peaking",   "q": 1.4},
        {"name": "80Hz",   "freq": 80.0,    "type": "peaking",   "q": 1.4},
        {"name": "125Hz",  "freq": 125.0,   "type": "peaking",   "q": 1.4},
        {"name": "200Hz",  "freq": 200.0,   "type": "peaking",   "q": 1.4},
        {"name": "320Hz",  "freq": 320.0,   "type": "peaking",   "q": 1.4},
        {"name": "500Hz",  "freq": 500.0,   "type": "peaking",   "q": 1.4},
        {"name": "1kHz",   "freq": 1000.0,  "type": "peaking",   "q": 1.0},
        {"name": "2kHz",   "freq": 2000.0,  "type": "peaking",   "q": 1.4},
        {"name": "3.2k",   "freq": 3200.0,  "type": "peaking",   "q": 1.4},
        {"name": "5kHz",   "freq": 5000.0,  "type": "peaking",   "q": 1.4},
        {"name": "8kHz",   "freq": 8000.0,  "type": "peaking",   "q": 1.4},
        {"name": "12.5k",  "freq": 12500.0, "type": "peaking",   "q": 1.4},
        {"name": "20kHz",  "freq": 20000.0, "type": "highshelf", "q": 0.7},
    ),
}

# Butterworth 平滑 Q 值（Low-Cut / High-Cut 用）
_BUTTERWORTH_Q = 0.7071


def get_band_count(mode: str) -> int:
    """返回指定 EQ 模式的频段数。未知模式回退到 3-band。"""
    return len(EQ_BAND_CONFIGS.get(mode, EQ_BAND_CONFIGS["3-band"]))


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


def _band_biquad(band_cfg: dict, gain_db: float, sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """按频段配置（type/freq/q）+ gain_db 构造对应 biquad 系数。"""
    btype = band_cfg["type"]
    freq = float(band_cfg["freq"])
    q = float(band_cfg.get("q", 1.0))
    if btype == "lowshelf":
        return _biquad_lowshelf(freq, gain_db, sample_rate)
    elif btype == "highshelf":
        return _biquad_highshelf(freq, gain_db, sample_rate)
    else:  # peaking
        return _biquad_peaking(freq, gain_db, q, sample_rate)


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

    @property
    def is_active(self) -> bool:
        """是否启用并生效。子类必须实现。"""
        raise NotImplementedError


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
# Graphic EQ 滤波器（多频段，带系数缓存）
# ─────────────────────────────────────────────────────────────────────────────

class StripEQFilter(AudioEffect):
    """多频段 Graphic EQ 滤波器（3/7/10/15 band）。

    按 EQ_BAND_CONFIGS[mode] 级联多个 biquad（lowshelf / peaking / highshelf）。
    可选 Low-Cut（highpass）和 High-Cut（lowpass）在频段链之前。
    可选整体增益（gain，dB）在频段链之后。

    系数按 StripEQParams 预算并缓存（参数 + 采样率变化时重算）。
    全部 biquad 段堆叠为 SOS 矩阵，每块一次 sosfilt 调用完成级联滤波
    （比逐段 lfilter 少一个数量级的调用开销，结果一致到 ~1e-12）。
    每段每声道维护 IIR 状态 zi（跨样本块连续滤波，无边界咔哒声）；
    状态按段身份（'lc'/'hc'/'b0'...）在参数变化时对位保留。

    线程安全：实例不共享。AudioMixer 在 _mutex 保护下每轨一个实例。
    """

    effect_type = "strip_eq"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)
        self._params: Optional[StripEQParams] = None
        # SOS 级联矩阵：(sections, 6)，行序 = Low-Cut → High-Cut → 频段链
        self._sos: Optional[np.ndarray] = None
        # 每行的段身份（参数变化时按身份对位保留 zi）
        self._section_keys: tuple = ()
        # 每段每声道的 IIR 状态：{key: (channels, 2)}
        self._zi_by_key: dict = {}
        # 整体增益（linear）
        self._gain_linear: float = 1.0

    def set_params(self, params: object, sample_rate: Optional[float] = None) -> None:
        """更新 EQ 参数（触发系数重算）。

        Args:
            params: StripEQParams 或其 to_dict() 形式的 dict，None 表示旁路（直通）。
                    dict 形式由 EffectChain 转发（FX 机架链中 EQ 条目以 dict 存储）。
            sample_rate: 可选新采样率
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            # 采样率变化，重置所有滤波器状态
            self._zi_by_key = {}
        # dict 形式（来自 EffectChain / 序列化）转换为 StripEQParams
        if isinstance(params, dict):
            params = StripEQParams.from_dict(params)
        old_mode = self._params.mode if (self._params is not None) else None
        self._params = params
        new_mode = params.mode if (params is not None) else None
        if old_mode != new_mode:
            # mode 变化 → 频段身份变化 → 丢弃频段状态（与旧实现一致）。
            # AUD-15302: _zi_by_key 的键只可能是 "lc"/"hc"/"b{i}"，旧条件保留
            # "既非 lc/hc 也非 b 开头"的键——恒为空集，连 Low-Cut/High-Cut 的
            # IIR 状态也一并清零，播放中切换 EQ 模式会因滤波器状态跳变产生
            # 可闻瞬态。正确语义：lc/hc 的滤波器结构不随 mode 变化，保留其状态。
            self._zi_by_key = {k: v for k, v in self._zi_by_key.items()
                               if k in ("lc", "hc")}
        self._recompute_coefficients()

    def _recompute_coefficients(self) -> None:
        """根据当前 params 和 sample_rate 重算所有系数（SOS 矩阵）。"""
        p = self._params
        if p is None or not p.enabled:
            self._sos = None
            self._section_keys = ()
            self._gain_linear = 1.0
            return

        sr = self._sample_rate
        band_cfgs = EQ_BAND_CONFIGS.get(p.mode, EQ_BAND_CONFIGS["3-band"])

        rows: list = []
        keys: list = []

        # Low-Cut（highpass）
        if p.low_cut_enable:
            # 截止频率不超过奈奎斯特的 0.9 倍
            lc_freq = min(float(p.low_cut), sr * 0.45)
            b, a = _biquad_highpass(lc_freq, _BUTTERWORTH_Q, sr)
            rows.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
            keys.append("lc")

        # High-Cut（lowpass）
        if p.high_cut_enable:
            hc_freq = min(float(p.high_cut), sr * 0.45)
            b, a = _biquad_lowpass(hc_freq, _BUTTERWORTH_Q, sr)
            rows.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
            keys.append("hc")

        # 频段链（按 bands 增益构造）
        for i, cfg in enumerate(band_cfgs):
            gain = float(p.bands[i]) if i < len(p.bands) else 0.0
            b, a = _band_biquad(cfg, gain, sr)
            rows.append([b[0], b[1], b[2], 1.0, a[1], a[2]])
            keys.append(f"b{i}")

        self._sos = np.array(rows, dtype=np.float64) if rows else None
        self._section_keys = tuple(keys)

        # 整体增益（dB → linear）
        self._gain_linear = float(10.0 ** (p.gain / 20.0))

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块音频样本应用 EQ 链（一次 sosfilt 级联）。

        处理顺序：Low-Cut → High-Cut → 频段链 → 整体增益。

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
# 频响曲线计算（供 UI 预览用，非实时路径）
# ─────────────────────────────────────────────────────────────────────────────

def compute_eq_response(
    params: StripEQParams,
    sample_rate: float,
    freqs_hz: np.ndarray,
) -> np.ndarray:
    """计算 EQ 链在给定频率点的总增益（dB）。

    用于 Graphic EQ 对话框的频响曲线预览（对数频率轴）。
    包含 Low-Cut / High-Cut / 频段链 / 整体增益的累计响应。

    Args:
        params: StripEQParams
        sample_rate: 采样率
        freqs_hz: 频率点数组（Hz），建议对数分布 20..20000

    Returns:
        gain_db: 各频率点的总增益（dB），同 shape
    """
    if params is None or not params.enabled:
        return np.zeros_like(freqs_hz, dtype=np.float64)

    try:
        from scipy.signal import freqz
    except ImportError:
        return np.zeros_like(freqs_hz, dtype=np.float64)

    total = np.zeros_like(freqs_hz, dtype=np.float64)

    # Low-Cut
    if params.low_cut_enable:
        lc_freq = min(float(params.low_cut), sample_rate * 0.45)
        b, a = _biquad_highpass(lc_freq, _BUTTERWORTH_Q, sample_rate)
        _, h = freqz(b, a, worN=freqs_hz, fs=sample_rate)
        total += 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))

    # High-Cut
    if params.high_cut_enable:
        hc_freq = min(float(params.high_cut), sample_rate * 0.45)
        b, a = _biquad_lowpass(hc_freq, _BUTTERWORTH_Q, sample_rate)
        _, h = freqz(b, a, worN=freqs_hz, fs=sample_rate)
        total += 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))

    # 频段链
    band_cfgs = EQ_BAND_CONFIGS.get(params.mode, EQ_BAND_CONFIGS["3-band"])
    for i, cfg in enumerate(band_cfgs):
        gain = float(params.bands[i]) if i < len(params.bands) else 0.0
        if abs(gain) > 1e-9:
            b, a = _band_biquad(cfg, gain, sample_rate)
            _, h = freqz(b, a, worN=freqs_hz, fs=sample_rate)
            total += 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))

    # 整体增益（dB，所有频率一致）
    if abs(params.gain) > 1e-9:
        total = total + params.gain

    return total


# ─────────────────────────────────────────────────────────────────────────────
# Limiter 限幅器（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Limiter 默认参数（与 LimiterParams dataclass 保持一致，避免循环 import）
_LIMITER_DEFAULT_THRESHOLD_DB = -3.0
_LIMITER_DEFAULT_RELEASE_MS = 100.0


class LimiterEffect(AudioEffect):
    """峰值限幅器（brick-wall limiter with release）。

    超过阈值的信号被压到阈值（硬限制），随后按 release 时间指数恢复增益。
    所有声道联动（link channels），取最大绝对值作为包络，避免立体声声像漂移。

    参数（LimiterParams，但本类不直接依赖以避免循环 import；接收 dict）：
      - threshold_db: 阈值（dB），[-20, 0]，信号超过此值被限制
      - release_ms:  释放时间（ms），[10, 1000]，增益恢复速度
      - enabled:     是否启用（False=旁路直通）

    线程安全：实例不共享。
    """

    effect_type = "limiter"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._threshold_db: float = _LIMITER_DEFAULT_THRESHOLD_DB
        self._release_ms: float = _LIMITER_DEFAULT_RELEASE_MS
        self._enabled: bool = False
        # 峰值包络（标量，跨块连续）。声道联动，故只需一个值。
        self._envelope: float = 0.0
        self._release_coeff: float = self._compute_release_coeff(self._release_ms)

    @staticmethod
    def _compute_release_coeff(release_ms: float, sample_rate: float = 48000.0) -> float:
        """由 release_ms 计算每采样指数衰减系数（0..1，越小衰减越快）。

        标准 RT60 式： coeff = exp(-1 / (release_ms * 0.001 * sr))
        """
        if release_ms <= 0.0 or sample_rate <= 0.0:
            return 0.0
        return float(np.exp(-1.0 / (release_ms * 0.001 * sample_rate)))

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新限幅器参数。

        Args:
            params: dict 形式的 LimiterParams（{"threshold_db":..., "release_ms":..., "enabled":...}），
                    None 表示旁路。
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
                                           -20.0, 0.0, _LIMITER_DEFAULT_THRESHOLD_DB)
        self._release_ms = _clamp_finite(params.get("release_ms", _LIMITER_DEFAULT_RELEASE_MS),
                                         10.0, 1000.0, _LIMITER_DEFAULT_RELEASE_MS)
        self._enabled = bool(params.get("enabled", True))
        self._release_coeff = self._compute_release_coeff(self._release_ms, self._sample_rate)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用限幅。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            限幅后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        threshold_linear = float(10.0 ** (self._threshold_db / 20.0))
        if threshold_linear <= 0.0:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 声道联动：取所有声道每采样的最大绝对值作为包络驱动信号
        peak_input = np.max(np.abs(out), axis=0)  # shape (n,)

        # 包络平滑（attack 瞬时 = rise_c 0，release 指数）
        envelope_arr, self._envelope = _smooth_attack_release(
            peak_input, self._envelope, 0.0, self._release_coeff)

        # 快路径：块内无超阈值样本且包络始终低于阈值 → 增益恒为 1，直通
        if not bool((envelope_arr > threshold_linear).any()):
            return samples

        # 增益衰减：使峰值不超过阈值（np.where 两分支都会求值，分母
        # 用 max 防静音包络除零告警——该分支本就选 1.0）
        gain = np.where(envelope_arr > threshold_linear,
                        threshold_linear / np.maximum(envelope_arr, 1e-30), 1.0)

        # 同一 gain 应用到所有声道（link channels，保持声像）
        out *= gain[np.newaxis, :]
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置包络状态（seek/暂停后调用）。"""
        self._envelope = 0.0

    @property
    def is_active(self) -> bool:
        return self._enabled


# ─────────────────────────────────────────────────────────────────────────────
# Compressor 压缩器（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Compressor 默认参数（与 CompressorParams dataclass 一致，避免循环 import）
_COMP_DEFAULT_THRESHOLD_DB = -24.0
_COMP_DEFAULT_RATIO = 4.0
_COMP_DEFAULT_ATTACK_MS = 3.0
_COMP_DEFAULT_RELEASE_MS = 250.0
_COMP_DEFAULT_KNEE_DB = 30.0
_COMP_DEFAULT_MAKEUP_DB = 0.0

# 软拐点宽度（dB），用于 knee 区域的平滑过渡计算
_KNEE_HALF_WIDTH_FACTOR = 0.5


class CompressorEffect(AudioEffect):
    """动态范围压缩器（peak/RMS 混合包络跟随）。

    信号超过 threshold 时按 ratio 压缩，knee 区域平滑过渡（soft knee）。
    attack/release 控制包络跟随速度。makeup_gain 补偿输出电平。
    所有声道联动（link channels），取最大绝对值作为包络驱动信号，避免声像漂移。

    实现：逐采样更新包络（向量化困难，因包络依赖前一采样），gain 计算向量化。
    参考标准 feed-forward 压缩器拓扑（与 WebAudio DynamicsCompressorNode 一致）。

    参数（CompressorParams，接收 dict）：
      - threshold_db: 阈值（dB），[-60, 0]
      - ratio:        压缩比，[1, 20]（1=不压缩，20=近限制）
      - attack_ms:    启动时间（ms），[0, 1000]
      - release_ms:   释放时间（ms），[10, 5000]
      - knee_db:      软拐点宽度（dB），[0, 40]（0=硬拐点）
      - makeup_gain:  输出补偿（dB），[0, +24]
      - enabled:      是否启用（False=旁路直通）

    线程安全：实例不共享。
    """

    effect_type = "compressor"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._threshold_db: float = _COMP_DEFAULT_THRESHOLD_DB
        self._ratio: float = _COMP_DEFAULT_RATIO
        self._attack_ms: float = _COMP_DEFAULT_ATTACK_MS
        self._release_ms: float = _COMP_DEFAULT_RELEASE_MS
        self._knee_db: float = _COMP_DEFAULT_KNEE_DB
        self._makeup_db: float = _COMP_DEFAULT_MAKEUP_DB
        self._enabled: bool = False
        # 包络（dB 域，跨块连续）。声道联动，故只需一个值。
        # 用 -200 dB 作为静默下限（避免 -inf 在平滑公式中传播）
        self._env_db: float = -200.0
        self._attack_coeff: float = self._time_to_coeff(self._attack_ms)
        self._release_coeff: float = self._time_to_coeff(self._release_ms)
        self._makeup_linear: float = 1.0

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
            self._env_db = -200.0  # 采样率变化重置状态

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 CompressorParams 声明范围钳位。未钳位时 ratio=0 会在
        # _compute_gain_db 的 1.0/ratio 抛 ZeroDivisionError（渲染线程），
        # 负 ratio/NaN 产生无声或异常增益；损坏/手改工程文件可直接触发。
        self._threshold_db = _clamp_finite(params.get("threshold_db", _COMP_DEFAULT_THRESHOLD_DB),
                                           -60.0, 0.0, _COMP_DEFAULT_THRESHOLD_DB)
        self._ratio = _clamp_finite(params.get("ratio", _COMP_DEFAULT_RATIO),
                                    1.0, 20.0, _COMP_DEFAULT_RATIO)
        self._attack_ms = _clamp_finite(params.get("attack_ms", _COMP_DEFAULT_ATTACK_MS),
                                        0.0, 1000.0, _COMP_DEFAULT_ATTACK_MS)
        self._release_ms = _clamp_finite(params.get("release_ms", _COMP_DEFAULT_RELEASE_MS),
                                         10.0, 5000.0, _COMP_DEFAULT_RELEASE_MS)
        self._knee_db = _clamp_finite(params.get("knee_db", _COMP_DEFAULT_KNEE_DB),
                                      0.0, 40.0, _COMP_DEFAULT_KNEE_DB)
        self._makeup_db = _clamp_finite(params.get("makeup_gain", _COMP_DEFAULT_MAKEUP_DB),
                                        0.0, 24.0, _COMP_DEFAULT_MAKEUP_DB)
        self._enabled = bool(params.get("enabled", True))
        self._attack_coeff = self._time_to_coeff(self._attack_ms, self._sample_rate)
        self._release_coeff = self._time_to_coeff(self._release_ms, self._sample_rate)
        self._makeup_linear = float(10.0 ** (self._makeup_db / 20.0))

    def _compute_gain_db(self, input_db: np.ndarray) -> np.ndarray:
        """根据输入电平（dB）计算压缩增益（dB）。

        soft-knee 压缩曲线：
          - 低于 (threshold - knee/2)：不压缩（gain=0）
          - 在 knee 区域 [threshold - knee/2, threshold + knee/2]：平滑过渡
          - 高于 (threshold + knee/2)：按 ratio 线性压缩
        """
        threshold = self._threshold_db
        ratio = self._ratio
        knee_half = self._knee_db * _KNEE_HALF_WIDTH_FACTOR
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
            gain_db[in_knee] = -((x - knee_lower) ** 2) / (2.0 * self._knee_db) * (1.0 - 1.0 / ratio)

        return gain_db

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用压缩。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            压缩后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 声道联动：取所有声道每采样的最大绝对值作为包络驱动
        peak_input = np.max(np.abs(out), axis=0)  # shape (n,)
        # 转 dB（加 epsilon 避免 log(0)）
        input_db = 20.0 * np.log10(np.maximum(peak_input, 1e-9))

        # 包络平滑（attack/release 分支平滑，静音/衰减块走 lfilter 快路径）
        env_smoothed, self._env_db = _smooth_attack_release(
            input_db, self._env_db, self._attack_coeff, self._release_coeff)

        # 快路径：包络全程低于 knee 下界 → 增益恰为 0dB（无压缩）。
        # 状态已照常推进，数学上与完整计算逐点等价。
        knee_lower = self._threshold_db - self._knee_db * _KNEE_HALF_WIDTH_FACTOR
        if not bool((env_smoothed > knee_lower).any()):
            if abs(self._makeup_linear - 1.0) > 1e-9:
                out *= self._makeup_linear
                return out.astype(samples.dtype, copy=False)
            return samples

        # 向量化计算压缩增益
        gain_db = self._compute_gain_db(env_smoothed)
        gain_linear = 10.0 ** (gain_db / 20.0)

        # 应用增益 + makeup
        out *= gain_linear[np.newaxis, :]
        if abs(self._makeup_linear - 1.0) > 1e-9:
            out *= self._makeup_linear

        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置包络状态（seek/暂停后调用）。"""
        self._env_db = -200.0

    @property
    def is_active(self) -> bool:
        return self._enabled


# ─────────────────────────────────────────────────────────────────────────────
# Reverb 混响器（Schroeder FDN，FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Reverb 默认参数（与 ReverbParams dataclass 一致，避免循环 import）
_REVERB_DEFAULT_MIX = 30.0
_REVERB_DEFAULT_DECAY = 2.0
_REVERB_DEFAULT_PRE_DELAY_MS = 0.0
_REVERB_DEFAULT_DAMPING_HZ = 5000.0

# Schroeder 经典结构参数（Freeverb 风格）：
# 4 个并联 comb filter 的延迟时间（ms，对应 ~50ms 内的素数样本数 @ 44.1kHz）
# 8 个串联 allpass filter 的延迟（ms）
# 这些值取自 Freeverb 的经典调音，产生无染色的大空间感。
_COMB_DELAY_MS = (29.7, 37.1, 41.1, 43.7)
_ALLPASS_DELAY_MS = (5.0, 1.7)
# comb 反馈基础系数由 decay 决定，allpass 反馈固定（Freeverb g=0.7）
_ALLPASS_FEEDBACK = 0.7
# comb 阻尼系数由 damping 决定


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


class ReverbEffect(AudioEffect):
    """Schroeder 反馈延迟网络混响器。

    经典 4-comb + 2-allpass 拓扑（Freeverb 风格）：
      input → [4 个并联 comb（带阻尼）] 求和 → 2 个串联 allpass → wet 输出
    dry/wet 由 mix 控制。pre_delay 在 wet 路径前加一个纯延迟。

    参数（ReverbParams，接收 dict）：
      - mix:          湿信号比例（0..100，%）
      - decay:        混响时间 RT60（秒，0.1..10）
      - pre_delay_ms: 预延迟（ms，0..200），湿信号到达前的纯延迟
      - damping_hz:   高频阻尼频率（Hz，1000..20000），越低高频衰减越快
      - enabled:      是否启用（False=旁路直通）

    性能：4 comb + 2 allpass 均已按块向量化（读段 lfilter 阻尼 + 切片读写），
    与逐样本实现结果一致（~1 ulp），单块成本约为旧逐样本循环的 1/30。

    线程安全：实例不共享。
    """

    effect_type = "reverb"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._mix: float = _REVERB_DEFAULT_MIX
        self._decay: float = _REVERB_DEFAULT_DECAY
        self._pre_delay_ms: float = _REVERB_DEFAULT_PRE_DELAY_MS
        self._damping_hz: float = _REVERB_DEFAULT_DAMPING_HZ
        self._enabled: bool = False
        # 构建 comb / allpass 实例（按 sample_rate 重算延迟样本数）
        self._combs: list = []
        self._allpasses: list = []
        self._predelay_buf: Optional[np.ndarray] = None
        self._predelay_idx: int = 0
        self._predelay_size: int = 0
        self._rebuild_filters()

    def _rebuild_filters(self) -> None:
        """按当前 sample_rate 重建 comb / allpass / predelay 缓冲。"""
        sr = self._sample_rate
        self._combs = [_CombFilter(int(round(d * 0.001 * sr))) for d in _COMB_DELAY_MS]
        self._allpasses = [_AllpassFilter(int(round(d * 0.001 * sr))) for d in _ALLPASS_DELAY_MS]
        # predelay 缓冲
        pd_samples = int(round(self._pre_delay_ms * 0.001 * sr))
        self._predelay_size = max(pd_samples, 1)
        self._predelay_buf = np.zeros(self._predelay_size, dtype=np.float64)
        self._predelay_idx = 0
        # 应用当前参数到 comb
        self._apply_comb_params()

    def _apply_comb_params(self) -> None:
        """把 decay / damping 应用到所有 comb。"""
        damping_linear = _damping_hz_to_linear(self._damping_hz, self._sample_rate)
        for i, comb in enumerate(self._combs):
            fb = _decay_to_feedback(self._decay, _COMB_DELAY_MS[i], self._sample_rate)
            comb.set_params(fb, damping_linear)

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新混响器参数。

        Args:
            params: dict 形式的 ReverbParams，None 表示旁路。
            sample_rate: 可选新采样率
        """
        sr_changed = sample_rate is not None and sample_rate != self._sample_rate
        if sr_changed:
            self._sample_rate = float(sample_rate)

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 ReverbParams 声明范围钳位（裸 dict 外部数据防护；
        # decay<=0 会让 _decay_to_feedback 产生 NaN/发散反馈，damping 异常
        # 偏移阻尼特性）。
        new_mix = _clamp_finite(params.get("mix", _REVERB_DEFAULT_MIX),
                                0.0, 100.0, _REVERB_DEFAULT_MIX)
        new_decay = _clamp_finite(params.get("decay", _REVERB_DEFAULT_DECAY),
                                  0.1, 10.0, _REVERB_DEFAULT_DECAY)
        new_predelay = _clamp_finite(params.get("pre_delay_ms", _REVERB_DEFAULT_PRE_DELAY_MS),
                                     0.0, 200.0, _REVERB_DEFAULT_PRE_DELAY_MS)
        new_damping = _clamp_finite(params.get("damping_hz", _REVERB_DEFAULT_DAMPING_HZ),
                                    1000.0, 20000.0, _REVERB_DEFAULT_DAMPING_HZ)

        # AUD-15300: 先提交 decay/damping 再判断重建分支。旧顺序下 predelay/
        # 采样率变化分支经 _rebuild_filters → _apply_comb_params 用**旧**
        # decay/damping 重建 comb；随后属性虽被更新为新值，但唯一按新值
        # 重算的 elif 分支已被跳过，之后同值 set_params 的差值判据恒为 0
        # 不再重算——工程加载/预设恢复/undo 时只要 predelay 与 decay/
        # damping 同时偏离默认值，comb 的反馈系数与阻尼就永远停在旧值，
        # 实际混响 RT60/高频衰减与用户设置不符。
        predelay_changed = abs(new_predelay - self._pre_delay_ms) > 0.1
        decay_changed = abs(new_decay - self._decay) > 1e-6
        damping_changed = abs(new_damping - self._damping_hz) > 1e-6
        self._decay = new_decay
        self._damping_hz = new_damping
        if sr_changed or predelay_changed:
            self._pre_delay_ms = new_predelay
            self._rebuild_filters()
        elif decay_changed or damping_changed:
            self._apply_comb_params()

        self._mix = new_mix
        self._pre_delay_ms = new_predelay
        self._enabled = bool(params.get("enabled", True))

    def _process_predelay(self, inp: np.ndarray) -> np.ndarray:
        """单声道 pre-delay（环形缓冲，向量化双段拷贝）。"""
        if self._predelay_size <= 1:
            return inp.copy()
        n = inp.shape[0]
        out = np.empty_like(inp)
        buf = self._predelay_buf
        size = self._predelay_size
        idx = self._predelay_idx
        pos = 0
        while pos < n:
            m = min(n - pos, size)
            end = idx + m
            if end <= size:
                out[pos:pos + m] = buf[idx:end]
                buf[idx:end] = inp[pos:pos + m]
            else:
                k = size - idx
                out[pos:pos + k] = buf[idx:]
                out[pos + k:pos + m] = buf[:m - k]
                buf[idx:] = inp[pos:pos + k]
                buf[:m - k] = inp[pos + k:pos + m]
            pos += m
            idx = end % size
        self._predelay_idx = idx
        return out

    def _process_wet(self, mono: np.ndarray) -> np.ndarray:
        """处理 wet 路径：predelay → 4 comb 并联求和 → 2 allpass 串联。"""
        x = self._process_predelay(mono)
        # 4 个 comb 并联，输出求和（每个 comb 独立处理同一输入）
        wet = np.zeros_like(x)
        for comb in self._combs:
            wet += comb.process_block(x)
        # 2 个 allpass 串联
        for ap in self._allpasses:
            wet = ap.process_block(wet)
        # Freeverb 风格：comb 求和后除以 4 防过载（固定 gain）
        wet *= 0.25
        return wet

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用混响。

        采用 Freeverb 风格：立体声输入先求和为单声道，送入单一混响网络，
        产生的 wet 信号平均分配回所有声道（自然扩散，无 L/R 偏置）。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            混响后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # wet 混合系数
        wet_gain = self._mix / 100.0
        dry_gain = 1.0 - wet_gain

        # 多声道求和为单声道送入混响（Freeverb 标准）
        mono = np.mean(out, axis=0) if channels > 1 else out[0]
        wet = self._process_wet(mono)
        for ch in range(channels):
            out[ch] = out[ch] * dry_gain + wet * wet_gain

        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置所有延迟线状态（seek/暂停后调用）。"""
        for comb in self._combs:
            comb.reset()
        for ap in self._allpasses:
            ap.reset()
        if self._predelay_buf is not None:
            self._predelay_buf.fill(0.0)
        self._predelay_idx = 0

    @property
    def is_active(self) -> bool:
        return self._enabled


# ─────────────────────────────────────────────────────────────────────────────
# Delay 延迟器（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Delay 默认参数（与 DelayParams dataclass 一致，避免循环 import）
_DELAY_DEFAULT_TIME_MS = 300.0
_DELAY_DEFAULT_FEEDBACK = 40.0
_DELAY_DEFAULT_MIX = 30.0
_DELAY_DEFAULT_PINGPONG = False
_DELAY_DEFAULT_LOWCUT_HZ = 100.0


class DelayEffect(AudioEffect):
    """延迟效果器（环形缓冲 + 反馈 + 低切 + ping-pong）。

    拓扑：
      input → [delay line] → feedback（含 highpass 低切去隆隆声）→ output
      wet/dry 由 mix 控制。ping-pong 模式下立体声的反馈交叉到对侧声道，
      产生左右跳动的回声。

    单声道时 ping-pong 退化为普通反馈延迟。

    参数（DelayParams，接收 dict）：
      - time_ms:    延迟时间（ms，1..2000）
      - feedback:   反馈量（%，0..90，0=单次回声，90=长尾）
      - mix:        湿信号比例（%，0..100）
      - ping_pong:  乒乓模式（True=立体声反馈交叉）
      - low_cut_hz: 反馈回路低切频率（Hz，20..1000），去除低频隆隆
      - enabled:    是否启用（False=旁路直通）

    线程安全：实例不共享。
    """

    effect_type = "delay"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._time_ms: float = _DELAY_DEFAULT_TIME_MS
        self._feedback: float = _DELAY_DEFAULT_FEEDBACK
        self._mix: float = _DELAY_DEFAULT_MIX
        self._ping_pong: bool = _DELAY_DEFAULT_PINGPONG
        self._low_cut_hz: float = _DELAY_DEFAULT_LOWCUT_HZ
        self._enabled: bool = False
        # 每声道独立延迟线（环形缓冲）。ping-pong 需要两声道独立。
        self._delay_bufs: list = []  # list[np.ndarray]
        self._delay_idx: int = 0
        self._delay_size: int = 0
        # 每声道反馈低切状态（单极点 highpass: y[n] = a*(y[n-1] + x[n] - x[n-1])）
        self._hp_prev_in: list = []   # list[float]
        self._hp_prev_out: list = []  # list[float]
        self._hp_coeff: float = 1.0   # highpass 系数（1=直通）
        self._rebuild_delay_line()

    def _rebuild_delay_line(self, num_channels: int = 2) -> None:
        """按当前 sample_rate 和 time_ms 重建延迟缓冲。"""
        sr = self._sample_rate
        delay_samples = int(round(self._time_ms * 0.001 * sr))
        self._delay_size = max(delay_samples, 1)
        self._delay_bufs = [np.zeros(self._delay_size, dtype=np.float64) for _ in range(num_channels)]
        self._delay_idx = 0
        self._hp_prev_in = [0.0] * num_channels
        self._hp_prev_out = [0.0] * num_channels
        self._recompute_hp_coeff()

    def _recompute_hp_coeff(self) -> None:
        """由 low_cut_hz 计算单极点 highpass 反馈系数（0..1）。

        标准 one-pole RC highpass 差分方程：
          y[n] = alpha * y[n-1] + alpha * (x[n] - x[n-1])
        简化为存储 alpha = exp(-2*pi*fc/sr)（越小截止频率越高）。
        同时预构建 lfilter 系数（b=[a,-a], a=[1,-a]）供向量化路径使用。
        """
        if self._low_cut_hz <= 0.0 or self._sample_rate <= 0.0:
            self._hp_coeff = 0.0
            self._hp_b = np.array([0.0, 0.0], dtype=np.float64)
            self._hp_a2 = np.array([1.0, 0.0], dtype=np.float64)
            return
        fc = min(self._low_cut_hz, self._sample_rate * 0.45)
        self._hp_coeff = float(np.exp(-2.0 * np.pi * fc / self._sample_rate))
        self._hp_b = np.array([self._hp_coeff, -self._hp_coeff], dtype=np.float64)
        self._hp_a2 = np.array([1.0, -self._hp_coeff], dtype=np.float64)

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新延迟器参数。

        Args:
            params: dict 形式的 DelayParams，None 表示旁路。
            sample_rate: 可选新采样率
        """
        sr_changed = sample_rate is not None and sample_rate != self._sample_rate
        if sr_changed:
            self._sample_rate = float(sample_rate)

        if params is None:
            self._enabled = False
            return

        # AUD-15304: dict 参数按 DelayParams 声明范围钳位（上游
        # TrackEffectInstance.params 是未校验裸 dict，工程文件反序列化内容
        # 可原样进入 DSP；NaN/负值会破坏延迟线重建与 HP 系数）。
        # low_cut_hz 下界钳到 0 而非 20：<=0 有明确 DSP 语义（反馈被完全抑制）。
        new_time = _clamp_finite(params.get("time_ms", _DELAY_DEFAULT_TIME_MS),
                                1.0, 2000.0, _DELAY_DEFAULT_TIME_MS)
        new_lowcut = _clamp_finite(params.get("low_cut_hz", _DELAY_DEFAULT_LOWCUT_HZ),
                                   0.0, 1000.0, _DELAY_DEFAULT_LOWCUT_HZ)

        # AUD-15301: 先提交 low_cut_hz 再判断重建分支。旧顺序下 time/采样率变化
        # 分支经 _rebuild_delay_line → _recompute_hp_coeff 用**旧** low_cut_hz 计算
        # HP 系数；随后属性虽被更新为新值，但唯一按新值重算的 elif 分支已被跳过，
        # 之后同值 set_params 的差值判据恒为 0 不再重算——工程加载/预设恢复时
        # 只要 time 与 low_cut 同时偏离默认值，反馈回路低切就永远停在旧截止频率。
        time_changed = abs(new_time - self._time_ms) > 0.1
        lowcut_changed = abs(new_lowcut - self._low_cut_hz) > 1e-6
        self._low_cut_hz = new_lowcut
        if sr_changed or time_changed:
            self._time_ms = new_time
            self._rebuild_delay_line(len(self._delay_bufs) if self._delay_bufs else 2)
        elif lowcut_changed:
            self._recompute_hp_coeff()

        self._time_ms = new_time
        self._feedback = _clamp_finite(params.get("feedback", _DELAY_DEFAULT_FEEDBACK),
                                       0.0, 90.0, _DELAY_DEFAULT_FEEDBACK)
        self._mix = _clamp_finite(params.get("mix", _DELAY_DEFAULT_MIX),
                                  0.0, 100.0, _DELAY_DEFAULT_MIX)
        self._ping_pong = bool(params.get("ping_pong", _DELAY_DEFAULT_PINGPONG))
        self._low_cut_hz = new_lowcut
        self._enabled = bool(params.get("enabled", True))

    def _apply_highpass(self, ch: int, x: float) -> float:
        """单极点 highpass（反馈回路去低频隆隆）。

        差分方程：y[n] = alpha * y[n-1] + alpha * (x[n] - x[n-1])
        等价于 y[n] = x[n] - x[n-1] + alpha*y[n-1] 的归一化形式。
        """
        prev_in = self._hp_prev_in[ch]
        prev_out = self._hp_prev_out[ch]
        a = self._hp_coeff
        y = a * prev_out + a * (x - prev_in)
        self._hp_prev_in[ch] = x
        self._hp_prev_out[ch] = y
        return y

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用延迟（向量化）。

        块内按 min(n, delay_size) 分段：段内读区与写区槽位相同但全部
        先读后写（与逐样本语义一致，含块内绕回时读到本块先前写入的值）。
        反馈回路的一阶 highpass 是常系数 IIR，对每段的反馈源做一次
        lfilter（状态经 prev_in/prev_out 精确交接）。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            延迟后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 声道数变化时重建
        if len(self._delay_bufs) != channels:
            self._rebuild_delay_line(channels)

        wet_gain = self._mix / 100.0
        dry_gain = 1.0 - wet_gain
        feedback_gain = self._feedback / 100.0
        size = self._delay_size
        bufs = self._delay_bufs
        ping_pong = self._ping_pong and channels >= 2
        # ping-pong 声道交换索引：ch -> ch^1（与逐样本 other = ch ^ 1 一致）。
        # AUD-15303: 奇数声道时末声道 ch^1 越界（3 声道 swap=[1,0,3] → 渲染线程
        # IndexError）。偶数声道按对交换；奇数声道把末声道映射到自身（防御风格
        # 与 ImagerEffect 对 >2 声道只处理前两声道一致）。
        swap = np.minimum(np.arange(channels) ^ 1, channels - 1)
        hp_a = self._hp_coeff

        delayed = np.empty((channels, n), dtype=np.float64)
        pos = 0
        idx = self._delay_idx
        while pos < n:
            m = min(n - pos, size)
            end = idx + m
            # 1) 读出本段延迟值
            seg = np.empty((channels, m), dtype=np.float64)
            for c in range(channels):
                b = bufs[c]
                if end <= size:
                    seg[c] = b[idx:end]
                else:
                    k = size - idx
                    seg[c, :k] = b[idx:]
                    seg[c, k:] = b[:m - k]
            delayed[:, pos:pos + m] = seg
            # 2) 反馈源（ping-pong 交叉 / 本声道），经一阶 highpass 低切
            src = seg[swap] if ping_pong else seg
            if hp_a > 0.0 and lfilter is not None:
                zi = hp_a * (np.asarray(self._hp_prev_out, dtype=np.float64)
                             - np.asarray(self._hp_prev_in, dtype=np.float64))[:, np.newaxis]
                fb_sig, zf = lfilter(self._hp_b, self._hp_a2, src, axis=1, zi=zi)
                # 状态回写：y = a*(prev_out + x - prev_in) ↔ prev_in=x[-1], prev_out=y[-1]
                last_in = src[:, -1]
                self._hp_prev_in = last_in.tolist()
                self._hp_prev_out = (last_in + zf[:, 0] / hp_a).tolist()
            elif hp_a > 0.0:
                # 无 scipy：逐样本回退（与 _apply_highpass 相同算式）
                fb_sig = np.empty_like(src)
                for c in range(channels):
                    pi = self._hp_prev_in[c]
                    po = self._hp_prev_out[c]
                    row = src[c]
                    for i in range(m):
                        v = row[i]
                        y = hp_a * po + hp_a * (v - pi)
                        pi, po = v, y
                        fb_sig[c, i] = y
                    self._hp_prev_in[c] = pi
                    self._hp_prev_out[c] = po
            else:
                # low_cut_hz <= 0 时系数为 0 → 反馈信号被完全抑制（与旧实现一致）
                fb_sig = np.zeros_like(src)
                self._hp_prev_in = src[:, -1].tolist()
                self._hp_prev_out = [0.0] * channels
            # 3) 写回：dry 输入 + 滤波后的反馈
            newv = out[:, pos:pos + m] + fb_sig * feedback_gain
            for c in range(channels):
                b = bufs[c]
                if end <= size:
                    b[idx:end] = newv[c]
                else:
                    k = size - idx
                    b[idx:] = newv[c, :k]
                    b[:m - k] = newv[c, k:]
            pos += m
            idx = end % size
        self._delay_idx = idx

        # 4) 输出 = dry + wet
        out *= dry_gain
        out += delayed * wet_gain
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置延迟线 + highpass 状态（seek/暂停后调用）。"""
        for buf in self._delay_bufs:
            buf.fill(0.0)
        self._delay_idx = 0
        self._hp_prev_in = [0.0] * len(self._hp_prev_in)
        self._hp_prev_out = [0.0] * len(self._hp_prev_out)

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
# Noise Gate 噪声门（FX 机架效果器）
# ─────────────────────────────────────────────────────────────────────────────

# Noise Gate 默认参数（与 NoiseGateParams dataclass 一致）
_GATE_DEFAULT_THRESHOLD_DB = -40.0
_GATE_DEFAULT_ATTACK_MS = 1.0
_GATE_DEFAULT_RELEASE_MS = 100.0


class NoiseGateEffect(AudioEffect):
    """噪声门（peak 包络跟随 + attack/release 平滑）。

    信号低于 threshold 时被衰减到 0（关门），高于 threshold 时直通（开门）。
    attack/release 控制开门/关门的过渡速度，避免咔哒声。
    所有声道联动（link channels），取最大绝对值作为包络驱动，避免声像漂移。

    实现：逐样本更新包络（dB 域，attack/release 指数平滑），gain 计算向量化。
    与 Compressor 拓扑对称：Compressor 压缩高电平，Noise Gate 衰减低电平。

    参数（NoiseGateParams，接收 dict）：
      - threshold_db: 阈值（dB），[-60, 0]，信号低于此值被关门
      - attack_ms:    开门时间（ms），[0, 50]，从关到开的过渡速度
      - release_ms:   关门时间（ms），[10, 1000]，从开到关的过渡速度
      - enabled:      是否启用（False=旁路直通）

    线程安全：实例不共享。
    """

    effect_type = "noise_gate"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._threshold_db: float = _GATE_DEFAULT_THRESHOLD_DB
        self._attack_ms: float = _GATE_DEFAULT_ATTACK_MS
        self._release_ms: float = _GATE_DEFAULT_RELEASE_MS
        self._enabled: bool = False
        # 门增益（0..1，跨块连续）。声道联动，故只需一个值。
        # 初值 1.0（默认开门，避免开头静音）
        self._gate_gain: float = 1.0
        self._attack_coeff: float = self._time_to_coeff(self._attack_ms)
        self._release_coeff: float = self._time_to_coeff(self._release_ms)

    @staticmethod
    def _time_to_coeff(time_ms: float, sample_rate: float = 48000.0) -> float:
        """由时间常数（ms）计算每采样指数平滑系数（0..1，越小越快）。"""
        if time_ms <= 0.0 or sample_rate <= 0.0:
            return 0.0
        return float(np.exp(-1.0 / (time_ms * 0.001 * sample_rate)))

    def set_params(self, params: Optional[dict], sample_rate: Optional[float] = None) -> None:
        """更新噪声门参数。

        Args:
            params: dict 形式的 NoiseGateParams，None 表示旁路。
            sample_rate: 可选新采样率
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._gate_gain = 1.0  # 采样率变化重置状态

        if params is None:
            self._enabled = False
            return

        # AUD-15304: 按 NoiseGateParams 声明范围钳位（裸 dict 外部数据防护）。
        self._threshold_db = _clamp_finite(params.get("threshold_db", _GATE_DEFAULT_THRESHOLD_DB),
                                           -60.0, 0.0, _GATE_DEFAULT_THRESHOLD_DB)
        self._attack_ms = _clamp_finite(params.get("attack_ms", _GATE_DEFAULT_ATTACK_MS),
                                        0.0, 50.0, _GATE_DEFAULT_ATTACK_MS)
        self._release_ms = _clamp_finite(params.get("release_ms", _GATE_DEFAULT_RELEASE_MS),
                                         10.0, 1000.0, _GATE_DEFAULT_RELEASE_MS)
        self._enabled = bool(params.get("enabled", True))
        self._attack_coeff = self._time_to_coeff(self._attack_ms, self._sample_rate)
        self._release_coeff = self._time_to_coeff(self._release_ms, self._sample_rate)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """对一块样本应用噪声门（分段闭式向量化，精确）。

        目标增益 t ∈ {0,1} 为分段常量；段内增益向 t 单调逼近且不会越过分
        目标（几何逼近），因此段的平滑方向由段入口的 gain 与 t 的大小关系
        唯一决定——每段可用闭式 ``t + d_in * c**k`` 一次算出，无需逐样本。

        Args:
            samples: shape (channels, num_samples)，float32/float64

        Returns:
            门控后的 samples，同 shape（旁路时原样返回）。
        """
        if not self._enabled or samples.ndim != 2 or samples.shape[0] < 1 or samples.shape[1] < 1:
            return samples

        out = samples.astype(np.float64, copy=True)
        channels, n = out.shape

        # 声道联动：取所有声道每采样的最大绝对值作为包络驱动
        peak_input = np.max(np.abs(out), axis=0)
        input_db = 20.0 * np.log10(np.maximum(peak_input, 1e-9))

        threshold = self._threshold_db
        att_c = self._attack_coeff  # 开门过渡（gain 0→1）
        rel_c = self._release_coeff  # 关门过渡（gain 1→0）

        # 目标增益：高于阈值=1（开），低于阈值=0（关）
        target = input_db >= threshold

        # target 恒定段（run）：段内方向不变，闭式计算
        change = np.flatnonzero(target[1:] != target[:-1]) + 1
        starts = np.concatenate(([0], change))
        ends = np.concatenate((change, [n]))
        lengths = (ends - starts).astype(np.int64)
        t_vals = target[starts].astype(np.float64)

        # 沿段传播入口偏差（标量循环，段数 = target 翻转次数，通常极少）
        gate_gain = self._gate_gain
        num_runs = starts.shape[0]
        d_starts = np.empty(num_runs, dtype=np.float64)
        c_runs = np.empty(num_runs, dtype=np.float64)
        for k in range(num_runs):
            t = t_vals[k]
            d_in = gate_gain - t
            c = att_c if t > gate_gain else rel_c
            d_starts[k] = d_in
            c_runs[k] = c
            # 段末增益（偏差按 c^L 收缩；c==0 时瞬时段末即 t）
            gate_gain = t + d_in * (c ** int(lengths[k]))

        # 向量化填充：gains[i] = t + d_in * c**(段内序号)
        run_id = np.repeat(np.arange(num_runs), lengths)
        k_exp = (np.arange(n) - starts[run_id] + 1).astype(np.float64)
        gains = t_vals[run_id] + d_starts[run_id] * np.power(c_runs[run_id], k_exp)
        self._gate_gain = gate_gain

        # 同一 gain 应用到所有声道（link channels）
        out *= gains[np.newaxis, :]
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置门增益状态（seek/暂停后调用）。"""
        self._gate_gain = 1.0

    @property
    def is_active(self) -> bool:
        return self._enabled


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
        _EFFECT_CLASS_REGISTRY["noise_gate"] = NoiseGateEffect
    return _EFFECT_CLASS_REGISTRY


class EffectChain:
    """有序效果器链。

    按顺序串联多个 AudioEffect，每个独立维护状态。
    AudioMixer 每轨一个实例，在 _mutex 保护下访问。

    set_params 接收解析后的条目列表（不直接依赖 EffectChainParams dataclass，
    避免 audio_dsp ↔ timeline_v2 的循环依赖）。调用方负责把
    EffectChainParams.effects 转成 [(effect_type, params_dict, enabled), ...]。
    """

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)
        # 有序 effect 实例列表 + 每个的 enabled 标志
        self._effects: list = []  # list[AudioEffect]
        self._enabled_flags: list = []  # list[bool]
        # 缓存最后传入的 entries，供 set_sample_rate 等场景重新应用
        self._last_entries: Optional[list] = None

    def set_params(self, entries: Optional[list], sample_rate: Optional[float] = None) -> None:
        """更新效果器链。

        Args:
            entries: [(effect_type: str, params: dict|None, enabled: bool), ...] 按 order 排序；
                     None 或空列表表示清空（链不生效）。
            sample_rate: 可选新采样率，变化时重置所有效果器状态。
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)

        self._last_entries = list(entries) if entries else None

        if not entries:
            self._effects = []
            self._enabled_flags = []
            return

        registry = _ensure_effect_registry()
        new_effects: list = []
        new_flags: list = []
        # 复用已有实例（按 effect_type 匹配，保持 DSP 状态连续），避免拖动参数时重建
        old_pool = list(zip(self._effects, self._enabled_flags))
        used = [False] * len(old_pool)

        for effect_type, params, enabled in entries:
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
            # 尝试复用同类型且未使用的旧实例
            instance = None
            for j, (old_inst, _old_en) in enumerate(old_pool):
                if not used[j] and type(old_inst) is cls:
                    instance = old_inst
                    used[j] = True
                    break
            if instance is None:
                instance = cls(self._sample_rate)
            instance.set_params(params, sample_rate=self._sample_rate)
            new_effects.append(instance)
            new_flags.append(bool(enabled))

        self._effects = new_effects
        self._enabled_flags = new_flags

    def process(self, samples: np.ndarray) -> np.ndarray:
        """依次应用链中每个启用的效果器。"""
        if not self._effects:
            return samples
        out = samples
        for inst, enabled in zip(self._effects, self._enabled_flags):
            if enabled and inst.is_active:
                out = inst.process(out)
        return out

    def reset_state(self) -> None:
        """重置所有效果器状态。"""
        for inst in self._effects:
            inst.reset_state()

    @property
    def is_active(self) -> bool:
        """链中是否至少有一个启用的效果器。"""
        for inst, enabled in zip(self._effects, self._enabled_flags):
            if enabled and inst.is_active:
                return True
        return False


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
        if peak > self._threshold * 1.0:
            out = out * (self._threshold / peak)
        return out
