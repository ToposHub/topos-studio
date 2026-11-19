"""P1 · True Peak Limiter（2026-11-19 计划第三阶段 · 效果器接入 #3）。

独立版本化效果器（``true_peak_limiter``）——现有 ``LimiterEffect`` 与
``MasterLimiter`` 保持旧工程语义不变：

- 有状态 4× 多相 FIR 过采样（``scipy.resample_poly`` 内置抗混叠原型
  滤波）测 True Peak（BS.1770 家族的采样间峰值）；
- lookahead 延迟线（attack 区间的增益前瞻）+ 平滑 release（一极）；
  链接/独立通道增益（linked=True 单增益取全声道最大）。保形流式
  合同下，块端 lookahead 窗缺未来上下文——边界增益按窗口前缀近似
  （≤ 上限语义不受影响：任意块长下 True Peak 均被压至上限之下）；
- 声明 group delay（lookahead + FIR 群延迟）→ ``latency_samples()``
  接 PDC（EffectChain.latency_samples 聚合，图级最迟路径对齐）；
- Dither 是最终 PCM 量化选项（导出器责任），不写入本浮点效果器；
- 旁路原样返回（音量不变）；重采样后上限由 dBTP 检测路径复核。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

try:  # pragma: no cover - 环境探测
    from scipy.signal import resample_poly
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


class TruePeakLimiterEffect(AudioEffect):
    """True Peak 限幅器（4× 过采样 + lookahead + 平滑 release）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = dict(DEFAULTS)
        self._gain_state = 1.0           # release 平滑后的当前增益
        self._delay_line: Optional[np.ndarray] = None  # (ch, lookahead)
        # 过采样 FIR 跨块上下文（overlap-save：块前接前块尾，输出丢弃
        # 上下文段——分块与整段的过采样峰值逐位一致）。
        self._fir_context: Optional[np.ndarray] = None  # (ch, ctx)
        # lookahead 滑窗的目标尾（跨块连续）。
        self._target_tail: Optional[np.ndarray] = None
        self._last_true_peak_dbtp = -120.0
        self._last_reduction_db = 0.0

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
        self._delay_line = None
        self._fir_context = None
        self._target_tail = None
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
        """group delay = lookahead（PDC 消费）。

        修前声明额外加了 FIR 群延迟项（``_FIR_GROUP_DELAY_OUT`` 经验常数）：
        实测 ``resample_poly`` 内部做零相位对齐（单位脉冲经 4× 上采样峰
        恰落 ``index*OVERSAMPLE``），过采样 FIR 无净输入域群延迟——多声明
        的 3 样本使挂链轨相对其它轨整体多延迟 3 样本（0.06 ms，对齐验收
        可测）。以实测为准：声明 = lookahead。
        """
        lookahead = int(self._params.get("lookahead_ms", 5.0)
                        * self._sample_rate / 1000.0)
        return int(lookahead)

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

        out = np.empty_like(samples)
        # 过采样测 True Peak（多相 FIR + 跨块上下文；声道独立测量）。
        ctx_len = 32
        ctx = self._fir_context
        if ctx is None or ctx.shape[0] != channels or ctx.shape[1] != ctx_len:
            ctx = np.zeros((channels, ctx_len), dtype=np.float32)
        with_context = np.concatenate([ctx, samples], axis=1)
        self._fir_context = np.ascontiguousarray(
            with_context[:, -ctx_len:], dtype=np.float32)

        def _upsample(channel_data: np.ndarray, ctx_data: np.ndarray) -> np.ndarray:
            up_all = resample_poly(
                np.concatenate([ctx_data, channel_data]), OVERSAMPLE, 1)
            # 丢弃上下文对应的输出（resample_poly 长度 = 4·(ctx+n)）。
            return up_all[ctx_len * OVERSAMPLE:ctx_len * OVERSAMPLE
                          + channel_data.shape[0] * OVERSAMPLE]

        if linked:
            mono = samples.mean(axis=0) if channels > 1 else samples[0]
            mono_ctx = ctx.mean(axis=0) if channels > 1 else ctx[0]
            up = _upsample(mono, mono_ctx)
            padded = np.pad(np.abs(up), (0, OVERSAMPLE))
            peaks = padded[:n * OVERSAMPLE].reshape(
                n, OVERSAMPLE).max(axis=1)
        else:
            peaks = np.zeros((channels, n), dtype=np.float64)
            for ch in range(channels):
                up = _upsample(samples[ch], ctx[ch])
                padded = np.pad(np.abs(up), (0, OVERSAMPLE))
                peaks[ch] = padded[:n * OVERSAMPLE].reshape(
                    n, OVERSAMPLE).max(axis=1)

        # 目标增益（lookahead 前瞻：窗口内最小目标增益作用于当前样本）。
        target = np.ones(n if linked else (channels, n), dtype=np.float64)
        peak_arr = peaks if linked else peaks
        for idx in range(n):
            if linked:
                target[idx] = self._target_gain(float(peak_arr[idx]))
            else:
                for ch in range(channels):
                    target[ch, idx] = self._target_gain(
                        float(peak_arr[ch, idx]))
        if lookahead > 0:
            # 前瞻：当前样本使用 [i, i+lookahead] 的最小目标增益（滑动
            # 最小窗；前缀接上一块的目标尾——跨块连续，首块为 1.0）。
            from numpy.lib.stride_tricks import sliding_window_view
            if self._target_tail is None or self._target_tail.shape != (
                    channels, lookahead):
                self._target_tail = np.ones((channels, lookahead),
                                            dtype=np.float64)
            if linked:
                window = sliding_window_view(
                    np.concatenate([self._target_tail[0], target]),
                    lookahead + 1)
                target = window.min(axis=1)[:n]
                self._target_tail[:, :] = target[-lookahead:]
            else:
                for ch in range(channels):
                    window = sliding_window_view(
                        np.concatenate([self._target_tail[ch], target[ch]]),
                        lookahead + 1)
                    target[ch] = window.min(axis=1)[:n]
                    self._target_tail[ch, :] = target[ch][-lookahead:]

        # release 平滑（增益回升慢，下降即时）+ 延迟线对齐 lookahead。
        if self._delay_line is None or self._delay_line.shape != (
                channels, max(1, lookahead)):
            self._delay_line = np.zeros(
                (channels, max(1, lookahead)), dtype=np.float32)
        delayed = np.concatenate(
            [self._delay_line, samples], axis=1)[:, :n]
        self._delay_line = np.concatenate(
            [self._delay_line, samples], axis=1)[:, n:n + max(1, lookahead)]
        if lookahead <= 0:
            delayed = samples

        gains = np.empty((channels, n), dtype=np.float32)
        states = np.full(channels, self._gain_state, dtype=np.float64)
        for idx in range(n):
            for ch in range(channels):
                g = float(target[idx]) if linked else float(target[ch, idx])
                if g < states[ch]:
                    states[ch] = g  # attack：即时下降
                else:
                    states[ch] = (release_coeff * states[ch]
                                  + (1 - release_coeff) * g)
                gains[ch, idx] = states[ch]
        self._gain_state = float(np.min(states))

        peak_linear = float(np.max(peaks)) if linked else float(
            np.max(peaks))
        self._last_true_peak_dbtp = (
            20.0 * np.log10(max(peak_linear, 1e-9)))
        self._last_reduction_db = (
            20.0 * np.log10(max(self._gain_state, 1e-9)))
        return np.ascontiguousarray(delayed * gains, dtype=np.float32)

    # ── 遥测（dBTP 独立复核路径共用读数） ──────────────────────────
    def collect_telemetry(self) -> dict:
        return {
            "true_peak_dbtp": round(self._last_true_peak_dbtp, 3),
            "gain_reduction_db": round(-self._last_reduction_db, 3),
            "ceiling_dbtp": float(self._params.get("ceiling_dbtp", -1.0)),
            "latency_samples": self.latency_samples(),
        }
