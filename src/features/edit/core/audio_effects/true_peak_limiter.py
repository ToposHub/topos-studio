"""P1 · True Peak Limiter（2026-11-19 计划第三阶段 · 效果器接入 #3）。

独立版本化效果器（``true_peak_limiter``）——现有 ``LimiterEffect`` 与
``MasterLimiter`` 保持旧工程语义不变：

- 有状态 4× 多相 FIR 过采样（``scipy.resample_poly`` 内置抗混叠原型
  滤波）测 True Peak（BS.1770 家族的采样间峰值）；FIR 跨块上下文按
  过采样因子推导、逐声道分配——分块与整段的检测逐位一致；
- 逐声道一致过采样相位测样本间峰；链接 = 各声道峰值的上界
  （max，不做声道平均——反相输入不抵消）；独立 = 声道各自检测；
- lookahead：对原始目标做 O(N) 后向最小窗（``minimum_filter1d``，
  窗 [i-L, i]），配合 L 样本输出延迟构成标准前视限幅；跨块保存
  **原始**目标尾（非平滑后值——旧实现存滤波值会逐块复合加深衰减）；
- 链接模式全声道共享一个 release 状态；独立模式每声道独立状态数组
  （旧实现折叠为单一最小值，声道间串增益）；
- 声明 group delay（lookahead；实测过采样 FIR 无净输入域群延迟）
  → ``latency_samples()`` 接 PDC；
- Dither 是最终 PCM 量化选项（交付层责任），不写入本浮点效果器；
- 旁路原样返回（音量不变）；上限由 dBTP 检测路径独立复核。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

try:  # pragma: no cover - 环境探测
    from scipy.ndimage import minimum_filter1d
    from scipy.signal import firwin, upfirdn
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False

OVERSAMPLE = 4

DEFAULTS = {
    "enabled": True,
    "ceiling_dbtp": -1.0,
    "lookahead_ms": 5.0,
    "release_ms": 80.0,
    "linked": True,
}

# 流式 4× 过采样原型（与 scipy.resample_poly(up,1) 同设计：81 阶
# kaiser 5.0 低通，f_c = 1/up 相对上采样 Nyquist）——``resample_poly``
# 的输出长度/对齐依赖输入总长，分块结果不保形；改用固定原型 +
# overlap-save 历史，跨块与整段逐位一致。对称 FIR 群延迟 = 40 上采样
# tap = D=10 输入样本：桶 b 需要流输入 [b-2D, b] → 检测历史取 2D，
# 峰值序列滞后 D 完整产出；增益窗口以 R[k-L..k] 覆盖真实峰位，
# 输出延迟取 lookahead + D，``latency_samples`` 据实声明。
_FIR_TAPS: Optional[np.ndarray] = None
_FIR_GROUP_DELAY = 10          # 输入样本域（= 40 上采样 tap / OVERSAMPLE）
_FIR_HISTORY = 2 * _FIR_GROUP_DELAY  # 检测流历史深度（桶有效性需 2D）


def _prototype_taps() -> np.ndarray:
    global _FIR_TAPS
    if _FIR_TAPS is None:
        taps = firwin(
            2 * 10 * OVERSAMPLE + 1, 1.0 / OVERSAMPLE,
            window=("kaiser", 5.0))
        # 内插器增益：每个多相行和须为 1（等价 resample_poly 的 h×up）。
        _FIR_TAPS = (taps * OVERSAMPLE).astype(np.float64)
    return _FIR_TAPS


def _sliding_min(values: np.ndarray, window: int) -> np.ndarray:
    """后向最小窗：``out[i] = min(values[max(0, i-window+1) .. i])``。

    O(N)（``minimum_filter1d``）；``window=1`` 恒等。左侧越界以 +inf
    填充（等价"无历史峰约束"）。
    """
    if window <= 1:
        return values
    origin = window - 1 - window // 2
    return minimum_filter1d(
        values, size=window, origin=origin, mode="constant", cval=np.inf)


class TruePeakLimiterEffect(AudioEffect):
    """True Peak 限幅器（4× 过采样 + lookahead + 平滑 release）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = dict(DEFAULTS)
        # release 平滑后的当前增益（链接=单值；独立=每声道）。
        self._gain_state = 1.0
        self._gain_states: Optional[np.ndarray] = None
        self._delay_line: Optional[np.ndarray] = None  # (ch, lookahead+D)
        # 过采样 FIR 跨块上下文（overlap-save：块前接 2D 历史；分块与
        # 整段的过采样峰值逐位一致）。
        self._fir_context: Optional[np.ndarray] = None  # (ch, 2D)
        # 峰值序列 R 的滚动尾（min 窗前缀；R[k] = 输入 k 的真实峰，
        # 检测滞后 D 完整产出）与目标尾（跨块连续；首块为 1.0）。
        self._raw_tail: Optional[np.ndarray] = None
        self._last_true_peak_dbtp = -120.0
        self._last_reduction_db = 0.0
        self._last_output_peak_dbfs = -120.0

    # ── 参数与状态 ────────────────────────────────────────────────
    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self.reset_state()
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged

    def reset_state(self) -> None:
        self._gain_state = 1.0
        self._gain_states = None
        self._delay_line = None
        self._fir_context = None
        self._raw_tail = None
        self._last_reduction_db = 0.0

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True)) and _SCIPY

    def tail_estimate_samples(self) -> int:
        # release 期间残余增益恢复（声明给 drain 预算；上限 1 s）。
        release_ms = float(self._params.get("release_ms", 80.0))
        return min(int(self._sample_rate), int(
            release_ms * self._sample_rate / 1000.0))

    def latency_samples(self) -> int:
        """总延迟 = lookahead + 检测 FIR 群延迟 D（PDC 消费）。

        对称内插 FIR 需 ±D 未来上下文：峰值序列滞后 D 完整产出，增益窗
        ``min(R[t-L..t])`` 恰覆盖"播放样本 + 后续 L 样本"，音频延迟取
        L + D 才能让被播放样本的峰进入自身增益窗（声明据实）。
        """
        lookahead = int(self._params.get("lookahead_ms", 5.0)
                        * self._sample_rate / 1000.0)
        return int(lookahead) + _FIR_GROUP_DELAY

    # ── 处理 ──────────────────────────────────────────────────────
    def _target_gain(self, peak: float) -> float:
        ceiling = float(self._params.get("ceiling_dbtp", -1.0))
        ceiling_linear = 10.0 ** (ceiling / 20.0)
        if peak <= ceiling_linear:
            return 1.0
        return ceiling_linear / peak

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        lookahead = int(self._params.get("lookahead_ms", 5.0)
                        * self._sample_rate / 1000.0)
        release_ms = max(1.0, float(self._params.get("release_ms", 80.0)))
        release_coeff = float(np.exp(
            -1.0 / (release_ms * self._sample_rate / 1000.0)))
        linked = bool(self._params.get("linked", True))
        channels = samples.shape[0]

        # 过采样测 True Peak（固定原型 + 2D overlap-save 历史；逐声道
        # 一致相位——峰值序列 R[k] = 输入 k 的真峰，滞后 D 完整产出）。
        hist = self._fir_context
        if hist is None or hist.shape != (channels, _FIR_HISTORY):
            hist = np.zeros((channels, _FIR_HISTORY), dtype=np.float32)
        taps = _prototype_taps()
        # 逐声道峰值桶；链接取各声道峰值上界（max）——反相不抵消。
        per_channel = np.zeros((channels, n), dtype=np.float64)
        offset = _FIR_HISTORY * OVERSAMPLE  # 桶 b 从流索引 b-D 开始（b≥2D）
        for ch in range(channels):
            stream = np.concatenate([hist[ch], samples[ch]])
            up_full = upfirdn(taps, stream, up=OVERSAMPLE)
            up = up_full[offset:offset + n * OVERSAMPLE]
            padded = np.pad(np.abs(up), (0, OVERSAMPLE))
            per_channel[ch] = padded[:n * OVERSAMPLE].reshape(
                n, OVERSAMPLE).max(axis=1)
        self._fir_context = np.ascontiguousarray(
            np.concatenate([hist, samples], axis=1)[:, -_FIR_HISTORY:],
            dtype=np.float32)
        peaks = per_channel.max(axis=0) if linked else per_channel

        # 原始目标增益（前视 min 窗只作用于应用值，不回写检测值）。
        if linked:
            raw_target = np.array(
                [self._target_gain(float(p)) for p in peaks],
                dtype=np.float64)
        else:
            raw_target = np.empty((channels, n), dtype=np.float64)
            for ch in range(channels):
                raw_target[ch] = [self._target_gain(float(p))
                                  for p in per_channel[ch]]

        if lookahead > 0:
            # 前视：应用值 = 峰值序列 R 上的后向最小窗 [t-L, t]——覆盖
            # "播放样本（t−L−D）+ 后续 L 样本"；跨块接 R 的**原始**尾
            # （首块为 1.0；不存平滑后值，避免逐块复合加深衰减）。
            rows = (1 if linked else channels)
            if self._raw_tail is None or self._raw_tail.shape != (
                    rows, lookahead):
                self._raw_tail = np.ones((rows, lookahead), dtype=np.float64)
            if linked:
                stream = np.concatenate([self._raw_tail[0], raw_target])
                applied = _sliding_min(
                    stream, lookahead + 1)[lookahead:].reshape(1, n)
                self._raw_tail[0] = raw_target[-lookahead:] if n >= lookahead \
                    else np.concatenate(
                        [self._raw_tail[0, n:], raw_target])
            else:
                applied = np.empty((channels, n), dtype=np.float64)
                for ch in range(channels):
                    stream = np.concatenate(
                        [self._raw_tail[ch], raw_target[ch]])
                    applied[ch] = _sliding_min(
                        stream, lookahead + 1)[lookahead:]
                    self._raw_tail[ch] = (
                        raw_target[ch, -lookahead:] if n >= lookahead
                        else np.concatenate(
                            [self._raw_tail[ch, n:], raw_target[ch]]))
        else:
            applied = (raw_target.reshape(1, n) if linked else raw_target)

        # 输出延迟线对齐 lookahead + 检测群延迟 D（块间连续）。
        total_delay = lookahead + _FIR_GROUP_DELAY
        if total_delay > 0:
            if self._delay_line is None or self._delay_line.shape != (
                    channels, total_delay):
                self._delay_line = np.zeros(
                    (channels, total_delay), dtype=np.float32)
            stream = np.concatenate([self._delay_line, samples], axis=1)
            delayed = stream[:, :n].copy()
            self._delay_line = np.ascontiguousarray(
                stream[:, n:n + total_delay], dtype=np.float32)
        else:
            delayed = samples

        # release 平滑（下降即时、回升一极）；链接共享单状态，独立逐声道。
        n_rows = 1 if linked else channels
        if linked:
            states = np.full(n_rows, self._gain_state, dtype=np.float64)
        else:
            if self._gain_states is None or self._gain_states.shape != (
                    channels,):
                self._gain_states = np.full(
                    channels, self._gain_state, dtype=np.float64)
            states = self._gain_states.copy()
        gains = np.empty((channels, n), dtype=np.float32)
        for idx in range(n):
            for ch in range(channels):
                row = 0 if linked else ch
                g = float(applied[row, idx]) if linked else float(
                    applied[ch, idx])
                if g < states[row]:
                    states[row] = g  # attack：即时下降
                else:
                    states[row] = (release_coeff * states[row]
                                   + (1 - release_coeff) * g)
                gains[ch, idx] = states[row]
        if linked:
            self._gain_state = float(states[0])
            self._gain_states = None
        else:
            self._gain_states = states
            self._gain_state = float(np.min(states))

        peak_linear = float(np.max(peaks)) if peaks.size else 0.0
        self._last_true_peak_dbtp = (
            20.0 * np.log10(max(peak_linear, 1e-9)))
        # 遥测 GR = 最深衰减（独立模式取各声道最小增益）。
        self._last_reduction_db = (
            20.0 * np.log10(max(self._gain_state, 1e-9)))
        result = np.ascontiguousarray(delayed * gains, dtype=np.float32)
        out_peak = float(np.max(np.abs(result))) if result.size else 0.0
        self._last_output_peak_dbfs = 20.0 * np.log10(max(out_peak, 1e-9))
        return result

    # ── 遥测（dBTP 独立复核路径共用读数） ──────────────────────────
    def collect_telemetry(self) -> dict:
        return {
            "true_peak_dbtp": round(self._last_true_peak_dbtp, 3),
            "output_peak_dbfs": round(self._last_output_peak_dbfs, 3),
            "gain_reduction_db": round(-self._last_reduction_db, 3),
            "ceiling_dbtp": float(self._params.get("ceiling_dbtp", -1.0)),
            "latency_samples": self.latency_samples(),
        }
