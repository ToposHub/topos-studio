"""P1 · 多段饱和器（FX-14 · 2026-11-21 计划）。

- **版本化模型**：``model_profile`` = ``legacy`` | ``multiband``（缺键
  → legacy，旧声音逐样本保留；legacy 数学留在
  ``audio_dsp.SaturationEffect``，本模块只做新模型）；
- **信号链**：``输入增益(drive) → 有界整形 → DC 阻断(按需) → Tone
  高架(RBJ) → Level``；三段用减法互补分频（精确重构、零延迟），
  2×/4× 因果过采样（每实例每声道滤波历史），干声按声明延迟对齐后
  按 Mix 混合、输出增益收尾；
- **整形模型**（斜率/峰值校准）：
  - ``tape``：``tanh(x)``（奇次为主，上界 ±1）；
  - ``tube``：偏置 tanh ``(tanh(x+b) − tanh(b)) / (1 − tanh²b)``（x=0
    斜率归一为 1，含偶次，DC 阻断必须开启）；
  - ``transistor``：softsign ``x / (1 + |x|)``（斜率 1，上界 ±1）；
- **Solo 是监听语义**，DSP 不消费；禁用段保留该频段干声（参与求和
  重构）；Mix 为持久化处理参数；
- 延迟：R>1 时上/下采样 FIR 各贡献 10 样本 → ``latency_samples()``
  = 20（R=1 时 0）；PDC 与干湿对齐共用同一声明。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Optional, Tuple

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect, _clamp_finite

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, firwin, lfilter, sosfilt, upfirdn
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False

#: 过采样档位的声明延迟（上/下采样 FIR 各 10 输入样本）。
OVERSAMPLE_LATENCY = 10
#: 旧面板 drive（0..10）→ drive_db 的映射（UI 绑定层共用）。
PANEL_DRIVE_TO_DB = 3.0
_TONE_CORNER_HZ = 1000.0
_DC_CUTOFF_HZ = 5.0
_MODELS = ("tape", "tube", "transistor")
_DC_NUMERATOR = np.array([1.0, -1.0], dtype=np.float64)


def _db(x: float) -> float:
    return 10.0 ** (float(x) / 20.0)


def shape_tape(x: np.ndarray) -> np.ndarray:
    """对称 tanh：奇次谐波为主，上界 ±1。"""
    return np.tanh(x)


def shape_tube(x: np.ndarray, bias: float) -> np.ndarray:
    """偏置 tanh：x=0 斜率归一 1，偶次谐波 + 不对称上/下界（有界）。"""
    b = float(np.clip(bias, -0.9, 0.9))
    tb = float(np.tanh(b))
    denom = max(1.0 - tb * tb, 1e-6)
    return (np.tanh(x + b) - tb) / denom


def shape_transistor(x: np.ndarray) -> np.ndarray:
    """softsign：x=0 斜率 1，上界 ±1（soft-knee）。"""
    return x / (1.0 + np.abs(x))


@lru_cache(maxsize=128)
def _rbj_high_shelf(fs: float, f0: float, gain_db: float
                    ) -> Tuple[np.ndarray, np.ndarray]:
    """RBJ high shelf biquad（cookbook，S=1）。返回归一化 (b, a)。"""
    a_gain = 10.0 ** (float(gain_db) / 40.0)
    w0 = 2.0 * np.pi * float(f0) / float(fs)
    cw = float(np.cos(w0))
    alpha = (float(np.sin(w0)) / 2.0
             * np.sqrt((a_gain + 1.0 / a_gain) * 2.0))
    sq = 2.0 * np.sqrt(a_gain) * alpha
    b0 = a_gain * ((a_gain + 1.0) + (a_gain - 1.0) * cw + sq)
    b1 = -2.0 * a_gain * ((a_gain - 1.0) + (a_gain + 1.0) * cw)
    b2 = a_gain * ((a_gain + 1.0) + (a_gain - 1.0) * cw - sq)
    a0 = (a_gain + 1.0) - (a_gain - 1.0) * cw + sq
    a1 = 2.0 * ((a_gain - 1.0) - (a_gain + 1.0) * cw)
    a2 = (a_gain + 1.0) - (a_gain - 1.0) * cw - sq
    return (np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0]))


@dataclass(frozen=True)
class _Band:
    enabled: bool
    model: str
    drive: float
    bias: float
    tube_offset: float
    tube_divisor: float
    tone_db: float
    level: float

    @property
    def blocks_dc(self) -> bool:
        return self.model == "tube" or self.bias != 0.0


class _ChannelFilters:
    """跨块滤波状态：DC 阻断（lfilter zi）与 Tone 高架（biquad zi）。"""

    def __init__(self, channels: int) -> None:
        self.dc_zi = np.zeros((channels, 1), dtype=np.float64)
        self.tone_zi = np.zeros((channels, 2), dtype=np.float64)
        self.tone_key: float | None = None

    def reset(self) -> None:
        self.dc_zi[:] = 0.0
        self.tone_zi[:] = 0.0
        self.tone_key = None


class MultibandSaturationEffect(AudioEffect):
    """三段互补分频饱和器（新模型；legacy 由 SaturationEffect 保留）。"""

    effect_type = "saturation_multiband"

    _DEFAULT_PARAMS = {
        "enabled": True, "xover_low_hz": 250.0, "xover_high_hz": 2500.0,
        "oversampling": 1, "mix": 1.0, "output_gain_db": 0.0, "bands": (),
        "crossover_mode": "legacy",
    }

    def __init__(self, sample_rate: float = 48000.0, *,
                 params_are_snapshots: bool = False) -> None:
        super().__init__(sample_rate)
        self._params = dict(self._DEFAULT_PARAMS)
        self._params_are_snapshots = params_are_snapshots
        self._channels = 0
        self._oversampling = 1
        self._low_sos = None
        self._mid_sos = None
        self._zi_low = None          # low 段分频状态
        self._zi_mid = None          # mid 段分频状态
        self._up_taps = None
        self._down_taps = None
        self._up_hist: Optional[np.ndarray] = None    # (ch, 20)
        self._down_hist: Optional[np.ndarray] = None  # (ch, 20R)
        self._filters: list[_ChannelFilters] = []
        self._dry_delay: Optional[np.ndarray] = None
        self._crossover_key: tuple = ()
        self._stream_key: tuple = ()
        self._dc_denominator = np.ones(2, dtype=np.float64)
        self._mix = 1.0
        self._output_gain = 1.0
        self._bands: tuple[_Band, ...] = ()
        self._active_bands: tuple[int, ...] = ()
        self._configure_parameters()

    # ── 拓扑 ─────────────────────────────────────────────────────
    def _oversampling_of(self, params: dict) -> int:
        try:
            value = int(params["oversampling"])
        except (TypeError, ValueError, OverflowError):
            return 1
        return value if value in (1, 2, 4) else 1

    def _rebuild(self) -> None:
        if not self._channels or not _SCIPY:
            return
        fs = self._sample_rate * self._oversampling
        stream_key = (self._channels, self._oversampling, self._sample_rate)
        if stream_key != self._stream_key:
            self._rebuild_stream()
            self._stream_key = stream_key
            self._crossover_key = ()
        low_hz, mid_hz = self._crossover_frequencies()
        key = (fs, low_hz, mid_hz, self._channels)
        if key == self._crossover_key:
            return
        self._crossover_key = key
        self._low_sos = butter(4, low_hz, btype="lowpass", fs=fs, output="sos")
        self._mid_sos = (self._low_sos if mid_hz == low_hz else
                         butter(4, mid_hz, btype="lowpass", fs=fs, output="sos"))
        n_sections = self._low_sos.shape[0]
        self._zi_low = np.zeros((n_sections, self._channels, 2),
                                dtype=np.float64)
        self._zi_mid = np.zeros((n_sections, self._channels, 2),
                                dtype=np.float64)
    def _crossover_frequencies(self) -> tuple[float, float]:
        if self._params["crossover_mode"] != "dual":
            # Preserve the original single low-pass coefficient in both paths.
            low = _clamp_finite(self._params["xover_low_hz"], 20.0,
                                0.45 * self._sample_rate * self._oversampling, 250.0)
            return low, low
        ceiling = max(21.0, 0.45 * self._sample_rate)
        low = _clamp_finite(self._params["xover_low_hz"], 20.0, ceiling - 1.0, 250.0)
        high_default = max(low + 1.0, min(2500.0, ceiling))
        high = _clamp_finite(self._params["xover_high_hz"], low + 1.0, ceiling, high_default)
        return low, high

    def _rebuild_stream(self) -> None:
        r = self._oversampling
        if r > 1:
            # 上采样（内插）增益 ×R；下采样（抗混叠抽取）增益 1。
            proto = firwin(2 * 10 * r + 1, 1.0 / r, window=("kaiser", 5.0))
            self._up_taps = np.asarray(proto * r, dtype=np.float64)
            self._down_taps = np.asarray(proto, dtype=np.float64)
            self._up_hist = np.zeros((self._channels, 20), dtype=np.float64)
            self._down_hist = np.zeros((self._channels, 20 * r),
                                       dtype=np.float64)
        else:
            self._up_taps = None
            self._down_taps = None
            self._up_hist = None
            self._down_hist = None
        # 各段滤波状态（拓扑变化时重建，含跨块 zi 清零）。
        self._filters = [_ChannelFilters(self._channels) for _ in range(3)]
        self._dry_delay = np.zeros(
            (self._channels, self.latency_samples()), dtype=np.float32)

    # ── 参数与状态 ───────────────────────────────────────────────
    #: 生产参数键（mb_*，来自 SaturationParams.to_dict）→ 内部键。
    _KEY_ALIASES = {
        "mb_xover_low_hz": "xover_low_hz",
        "mb_xover_high_hz": "xover_high_hz",
        "mb_oversampling": "oversampling",
        "mb_mix": "mix",
        "mb_output_gain_db": "output_gain_db",
        "mb_bands": "bands",
        "mb_crossover_mode": "crossover_mode",
    }

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        changed_rate = sample_rate is not None and sample_rate != self._sample_rate
        if changed_rate:
            self._sample_rate = float(sample_rate)
        merged = dict(self._DEFAULT_PARAMS if self._params_are_snapshots else self._params)
        if isinstance(params, dict):
            for key, value in params.items():
                merged[self._KEY_ALIASES.get(key, key)] = value
        if merged != self._params or changed_rate:
            self._params = merged
            self._configure_parameters()
            self._rebuild()

    def _configure_parameters(self) -> None:
        params = self._params
        self._oversampling = self._oversampling_of(params)
        self._mix = _clamp_finite(params["mix"], 0.0, 1.0, 1.0)
        self._output_gain = _db(_clamp_finite(params["output_gain_db"], -24.0, 24.0, 0.0))
        fs = self._sample_rate * self._oversampling
        self._dc_denominator[1] = -float(np.exp(-2.0 * np.pi * _DC_CUTOFF_HZ / fs))
        bands = params["bands"] if isinstance(params["bands"], (tuple, list)) else ()
        prepared = []
        for index in range(3):
            band = bands[index] if index < len(bands) and isinstance(bands[index], dict) else {}
            model = str(band.get("model", "tape")).lower()
            if model not in _MODELS:
                model = "tape"
            bias = _clamp_finite(band.get("bias_pct", 0.0), -100.0, 100.0, 0.0) / 100.0
            tube_offset = float(np.tanh(np.clip(bias, -0.9, 0.9))) if model == "tube" else 0.0
            prepared.append(_Band(
                bool(band.get("enabled", False)), model,
                _db(_clamp_finite(band.get("drive_db", 0.0), 0.0, 36.0, 0.0)),
                bias, tube_offset, max(1.0 - tube_offset * tube_offset, 1e-6),
                _clamp_finite(band.get("tone_db", 0.0), -12.0, 12.0, 0.0),
                _db(_clamp_finite(band.get("level_db", 0.0), -24.0, 12.0, 0.0)),
            ))
        self._bands = tuple(prepared)
        self._active_bands = tuple(i for i, band in enumerate(self._bands) if band.enabled)

    def reset_state(self) -> None:
        if self._zi_low is not None:
            self._zi_low[:] = 0.0
        if self._zi_mid is not None:
            self._zi_mid[:] = 0.0
        if self._up_hist is not None:
            self._up_hist[:] = 0.0
        if self._down_hist is not None:
            self._down_hist[:] = 0.0
        for filters in self._filters:
            filters.reset()
        if self._dry_delay is not None:
            self._dry_delay[:] = 0.0

    @property
    def is_active(self) -> bool:
        return _SCIPY and bool(self._params["enabled"])

    def latency_samples(self) -> int:
        return 2 * OVERSAMPLE_LATENCY if _SCIPY and self._oversampling > 1 else 0

    def tail_estimate_samples(self) -> int:
        if not self.is_active:
            return 0
        latency = self.latency_samples()
        if self._mix == 0.0:
            return latency
        fir_tail = 2 * latency
        if not self._active_bands:
            return fir_tail
        low, _ = self._crossover_frequencies()
        slowest_pole_hz = low * math.sin(math.pi / 8.0)
        if any(self._bands[i].blocks_dc for i in self._active_bands):
            slowest_pole_hz = min(slowest_pole_hz, _DC_CUTOFF_HZ)
        decay = math.log(1000.0) / (2.0 * math.pi * slowest_pole_hz)
        return math.ceil(decay * self._sample_rate) + fir_tail

    # ── 处理 ─────────────────────────────────────────────────────
    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2 or not samples.size:
            return samples
        channels, n = samples.shape
        if channels != self._channels:
            self._channels = channels
            self._crossover_key = ()
            self._rebuild()

        r = self._oversampling
        if r > 1:
            x = self._upsample(samples)  # (ch, n·r)，内容延迟 10
        else:
            x = samples.astype(np.float64)

        # 三段减法互补分割：low = LP(x)；res1 = x − low；
        # mid = LP(res1)；high = res1 − mid（求和精确重构）。
        low, self._zi_low = sosfilt(self._low_sos, x, axis=-1, zi=self._zi_low)
        res1 = x - low
        mid, self._zi_mid = sosfilt(self._mid_sos, res1, axis=-1,
                                    zi=self._zi_mid)
        high = res1 - mid
        band_signals = (low, mid, high)

        processed = list(band_signals)
        for index in self._active_bands:
            processed[index] = self._process_band(
                band_signals[index], self._bands[index], self._filters[index])
        wet = processed[0] + processed[1] + processed[2]

        if r > 1:
            wet = self._downsample(wet, n)  # 内容再延迟 10（合计 20）
        dry = x if r == 1 else self._delay_dry(samples, n)
        out = dry * (1.0 - self._mix) + wet * self._mix
        return np.ascontiguousarray(out * self._output_gain, dtype=np.float32)

    def _process_band(self, band_signal: np.ndarray, band: _Band,
                      filters: _ChannelFilters) -> np.ndarray:
        """单段处理：drive → shape → DC → tone → level。"""
        x = band_signal * band.drive
        if band.model == "tube":
            y = (np.tanh(x + max(-0.9, min(0.9, band.bias))) - band.tube_offset) / band.tube_divisor
        elif band.model == "transistor":
            y = shape_transistor(x + band.bias) if band.bias != 0.0 else \
                shape_transistor(x)
        else:
            y = shape_tape(x + band.bias) if band.bias != 0.0 else shape_tape(x)

        # DC 阻断（tube / 带 bias 输入时必要）：一极高通 lfilter，zi 跨块。
        if band.blocks_dc:
            y, filters.dc_zi = lfilter(
                _DC_NUMERATOR, self._dc_denominator, y, axis=-1, zi=filters.dc_zi)
        # Tone 高架（RBJ，1 kHz 角频；系数变化即重置状态）。
        if abs(band.tone_db) > 1e-6:
            fs = self._sample_rate * self._oversampling
            b, a = _rbj_high_shelf(fs, _TONE_CORNER_HZ, band.tone_db)
            if filters.tone_key != band.tone_db:
                filters.tone_key = band.tone_db
                filters.tone_zi[:] = 0.0
            y, filters.tone_zi = lfilter(b, a, y, axis=-1, zi=filters.tone_zi)
        return y * band.level

    # ── 过采样（因果 FIR；每声道历史跨块） ───────────────────────
    def _upsample(self, x: np.ndarray) -> np.ndarray:
        r = self._oversampling
        n = x.shape[-1]
        stream = np.concatenate([self._up_hist, x.astype(np.float64)],
                                axis=-1)
        y_full = upfirdn(self._up_taps, stream, up=r, axis=-1)
        # z(t) = x_abs(s − 30 + t/R)（hist 20 + FIR 群延迟 10）；
        # 目标内容延迟 10 → 输出索引 [20R, 20R+nR)：
        # out[m·R+p] = x_abs(s + m − 10 + p/R)。
        start = 20 * r
        out = y_full[..., start:start + n * r]
        self._up_hist = np.ascontiguousarray(stream[..., -20:])
        return out

    def _downsample(self, u: np.ndarray, n: int) -> np.ndarray:
        r = self._oversampling
        stream = np.concatenate([self._down_hist, u], axis=-1)
        y_full = upfirdn(self._down_taps, stream, up=1, down=r, axis=-1)
        # y_full[k] = Σ taps[j]·u_stream[kR−j] = u_base(k − 30)；
        # u 内容已延迟 10（上采样）→ y[k] = x_abs(s + k − 40)，
        # 切片 [20, 20+n)：out[m] = x_abs(s + m − 20)（合计延迟 20）。
        out = y_full[..., 20:20 + n]
        self._down_hist = np.ascontiguousarray(stream[..., -20 * r:])
        return out

    def _delay_dry(self, samples: np.ndarray, n: int) -> np.ndarray:
        """干声按声明延迟对齐（延迟线跨块）。"""
        latency = self.latency_samples()
        if latency <= 0:
            return samples.astype(np.float64)
        stream = np.concatenate([self._dry_delay, samples], axis=1)
        delayed = stream[:, :n].astype(np.float64)
        self._dry_delay = np.ascontiguousarray(
            stream[:, n:n + latency], dtype=np.float32)
        return delayed
