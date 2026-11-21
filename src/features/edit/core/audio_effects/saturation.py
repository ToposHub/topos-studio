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

from typing import List, Optional, Tuple

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

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


class _ChannelFilters:
    """跨块滤波状态：DC 阻断（lfilter zi）与 Tone 高架（biquad zi）。"""

    def __init__(self, channels: int) -> None:
        self.channels = channels
        self.dc_zi = np.zeros((channels, 1), dtype=np.float64)
        self.tone_zi = np.zeros((channels, 2), dtype=np.float64)
        self.tone_key: Optional[tuple] = None

    def reset(self) -> None:
        self.dc_zi[:] = 0.0
        self.tone_zi[:] = 0.0
        self.tone_key = None


class MultibandSaturationEffect(AudioEffect):
    """三段互补分频饱和器（新模型；legacy 由 SaturationEffect 保留）。"""

    effect_type = "saturation_multiband"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {
            "enabled": True,
            "xover_low_hz": 250.0,
            "xover_high_hz": 2500.0,
            "oversampling": 1,
            "mix": 1.0,
            "output_gain_db": 0.0,
            "bands": (
                {"enabled": False, "model": "tape", "drive_db": 0.0,
                 "tone_db": 0.0, "bias_pct": 0.0, "level_db": 0.0},
                {"enabled": False, "model": "tube", "drive_db": 0.0,
                 "tone_db": 0.0, "bias_pct": 0.0, "level_db": 0.0},
                {"enabled": False, "model": "transistor", "drive_db": 0.0,
                 "tone_db": 0.0, "bias_pct": 0.0, "level_db": 0.0},
            ),
        }
        self._channels = 0
        self._oversampling = 1
        self._lp_sos = None          # 4 阶 Butterworth（两级 biquad）
        self._zi_low = None          # low 段分频状态
        self._zi_mid = None          # mid 段分频状态
        self._up_taps = None
        self._down_taps = None
        self._up_hist: Optional[np.ndarray] = None    # (ch, 20)
        self._down_hist: Optional[np.ndarray] = None  # (ch, 20R)
        self._filters: List[_ChannelFilters] = []
        self._dry_delay: Optional[np.ndarray] = None
        self._crossover_key: tuple = ()
        self._rebuild()

    # ── 拓扑 ─────────────────────────────────────────────────────
    def _oversampling_of(self, params: dict) -> int:
        value = int(params.get("oversampling", 1))
        return value if value in (1, 2, 4) else 1

    def _rebuild(self) -> None:
        if not self._channels:
            return
        oversampling = self._oversampling_of(self._params)
        fs = float(self._sample_rate) * oversampling
        low_hz = float(np.clip(self._params.get("xover_low_hz", 250.0),
                               20.0, 0.45 * fs))
        high_hz = float(np.clip(self._params.get("xover_high_hz", 2500.0),
                                20.0, 0.45 * fs))
        key = (self._channels, oversampling, float(self._sample_rate),
               round(low_hz, 6), round(high_hz, 6))
        if key == self._crossover_key:
            return
        self._crossover_key = key
        self._oversampling = oversampling
        self._lp_sos = np.asarray(
            butter(4, low_hz, btype="lowpass", fs=fs, output="sos"),
            dtype=np.float64)
        n_sections = self._lp_sos.shape[0]
        self._zi_low = np.zeros((n_sections, self._channels, 2),
                                dtype=np.float64)
        self._zi_mid = np.zeros((n_sections, self._channels, 2),
                                dtype=np.float64)
        r = oversampling
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
    }

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._crossover_key = ()
        merged = dict(self._params)
        if isinstance(params, dict):
            for key, value in params.items():
                merged[self._KEY_ALIASES.get(key, key)] = value
        self._params = merged
        # 过采样档位立即生效（latency_samples 在首块前就要正确）；
        # 滤波系数与历史缓冲在首块（声道数已知）时重建。
        self._oversampling = self._oversampling_of(self._params)
        self._crossover_key = ()  # 强制下次 _rebuild 应用新系数
        self._rebuild()

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
        return bool(self._params.get("enabled", True))

    def latency_samples(self) -> int:
        return 2 * OVERSAMPLE_LATENCY if self._oversampling > 1 else 0

    def tail_estimate_samples(self) -> int:
        return 0  # 无记忆整形；DC/AA 滤波瞬态可忽略

    # ── 处理 ─────────────────────────────────────────────────────
    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        channels = samples.shape[0]
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
        low, self._zi_low = sosfilt(self._lp_sos, x, axis=-1, zi=self._zi_low)
        res1 = x - low
        mid, self._zi_mid = sosfilt(self._lp_sos, res1, axis=-1,
                                    zi=self._zi_mid)
        high = res1 - mid
        band_signals = (low, mid, high)

        bands = self._params.get("bands", ())
        wet = np.zeros_like(x)
        for band_idx in range(3):
            band = bands[band_idx] if band_idx < len(bands) else {}
            if bool(band.get("enabled", False)):
                processed = self._process_band(
                    band_signals[band_idx], band, self._filters[band_idx], r)
            else:
                processed = band_signals[band_idx]  # 禁用段保留干声
            wet = wet + processed

        if r > 1:
            wet = self._downsample(wet, n)  # 内容再延迟 10（合计 20）
        else:
            wet = wet

        mix = float(np.clip(self._params.get("mix", 1.0), 0.0, 1.0))
        out_gain = _db(self._params.get("output_gain_db", 0.0))
        dry = self._delay_dry(samples, n)
        out = dry * (1.0 - mix) + wet * mix
        return np.ascontiguousarray((out * out_gain).astype(np.float32),
                                    dtype=np.float32)

    def _process_band(self, band_signal: np.ndarray, band: dict,
                      filters: _ChannelFilters, r: int) -> np.ndarray:
        """单段处理：drive → shape → DC → tone → level。"""
        drive = _db(np.clip(float(band.get("drive_db", 0.0)), 0.0, 36.0))
        x = band_signal * drive
        model = str(band.get("model", "tape")).lower()
        bias = float(np.clip(float(band.get("bias_pct", 0.0)),
                             -100.0, 100.0)) / 100.0
        if model == "tube":
            y = shape_tube(x, bias)
        elif model == "transistor":
            y = shape_transistor(x + bias) if bias != 0.0 else \
                shape_transistor(x)
        else:
            y = shape_tape(x + bias) if bias != 0.0 else shape_tape(x)

        # DC 阻断（tube / 带 bias 输入时必要）：一极高通 lfilter，zi 跨块。
        if model == "tube" or bias != 0.0:
            fs = float(self._sample_rate) * r
            a = float(np.exp(-2.0 * np.pi * _DC_CUTOFF_HZ / fs))
            blocked = np.empty_like(y)
            for ch in range(y.shape[0]):
                blocked[ch], filters.dc_zi[ch] = lfilter(
                    np.array([1.0, -1.0]), np.array([1.0, -a]), y[ch],
                    zi=filters.dc_zi[ch])
            y = blocked
        # Tone 高架（RBJ，1 kHz 角频；系数变化即重置状态）。
        tone_db = float(np.clip(float(band.get("tone_db", 0.0)),
                                -12.0, 12.0))
        if abs(tone_db) > 1e-6:
            fs = float(self._sample_rate) * r
            b, a = _rbj_high_shelf(fs, _TONE_CORNER_HZ, tone_db)
            key = (round(tone_db, 4), round(fs, 3))
            if filters.tone_key != key:
                filters.tone_key = key
                filters.tone_zi[:] = 0.0
            for ch in range(y.shape[0]):
                y[ch], filters.tone_zi[ch] = lfilter(
                    b, a, y[ch], zi=filters.tone_zi[ch])
        level = _db(np.clip(float(band.get("level_db", 0.0)),
                            -24.0, 12.0))
        return y * level

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
