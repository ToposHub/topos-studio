"""P1 · Dynamic EQ（2026-11-19 计划第三阶段 · 效果器接入 #4）。

复用 ``StripEQFilter`` 的 Bell 段形状（RBJ cookbook 系数，同一数学），
为单一目标频段接动态：段内 detector（band-pass + 流式 RMS 包络 +
attack/release 平滑）驱动该段增益在 [−max_cut, +max_boost] dB 内浮动；
静态基线 ``gain_db`` 关闭动态时与同参数的 ``StripEQFilter`` Bell 段
一致（同版本 EQ）。

- 第一版最小相位、内检源（外部检源待 sidechain 稳定、线性相位待
  PDC 齐备——见计划）；
- 增益按 512 样本子块刷新（biquad 系数重算、zi 跨块保留——块边缘
  无跳变；参数变化同样保留段身份状态，自动化区间可重开）；
- 旁路/维度异常原样返回。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, lfilter, sosfilt  # noqa: F401
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False

#: 增益刷新粒度（样本；子块内系数恒定，块间平滑重算）。
GAIN_BLOCK = 512

DEFAULTS = {
    "enabled": True,
    "frequency": 3000.0,
    "q": 1.4,
    "gain_db": 0.0,            # 静态基线（同版本 EQ 的段增益）
    "threshold_dbfs": -30.0,
    "max_cut_db": 9.0,         # 动态最大衰减
    "max_boost_db": 0.0,       # 动态最大提升（v1 默认只压不升）
    "attack_ms": 8.0,
    "release_ms": 120.0,
    "dynamic_enabled": True,
    "slope": 0.5,              # detector 超阈 → 增益变化比例
}


def rbj_bell_sos(freq: float, q: float, gain_db: float,
                 sample_rate: float) -> np.ndarray:
    """RBJ cookbook Bell 段（与 StripEQFilter 的 Bell 段同一数学）。"""
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * freq / sample_rate
    alpha = np.sin(w0) / (2.0 * max(1e-6, q))
    b0 = 1.0 + alpha * a
    b1 = -2.0 * np.cos(w0)
    b2 = 1.0 - alpha * a
    a0 = 1.0 + alpha / a
    a1 = -2.0 * np.cos(w0)
    a2 = 1.0 - alpha / a
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]],
                    dtype=np.float64)


class DynamicEqEffect(AudioEffect):
    """单段动态 EQ（Bell 形状复用 StripEQ；内检源，最小相位）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = dict(DEFAULTS)
        self._bp_ba: Optional[tuple] = None
        self._bp_state: Optional[np.ndarray] = None
        self._rms_b: Optional[np.ndarray] = None
        self._rms_state: Optional[np.ndarray] = None
        self._sos_state: Optional[np.ndarray] = None   # (1, ch, 2)
        self._gain_state: float = 0.0                  # 平滑后的动态 dB
        self._last_detector_db = -120.0
        self._last_dyn_db = 0.0

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
        # 段身份不变——滤波器状态保留（自动化区间可重开/参数微调无跳变）。
        self._rebuild_detector()

    def reset_state(self) -> None:
        self._bp_state = None
        self._rms_state = None
        self._sos_state = None
        self._gain_state = 0.0

    @property
    def is_active(self) -> bool:
        p = self._params
        return (bool(p.get("enabled", True)) and _SCIPY
                and (bool(p.get("dynamic_enabled", True))
                     or abs(float(p.get("gain_db", 0.0))) > 1e-6))

    def tail_estimate_samples(self) -> int:
        return 0

    def latency_samples(self) -> int:
        return 0

    def _rebuild_detector(self) -> None:
        if not _SCIPY:
            return
        freq = float(np.clip(self._params.get("frequency", 3000.0),
                             20.0, self._sample_rate * 0.45))
        q = float(np.clip(self._params.get("q", 1.4), 0.1, 20.0))
        bw = freq / q
        low = max(10.0, freq - bw / 2.0)
        high = min(self._sample_rate * 0.49, freq + bw / 2.0)
        try:
            self._bp_ba = butter(2, [low, high], btype="band",
                                 fs=self._sample_rate, output="ba")
        except ValueError:
            self._bp_ba = None
        win = max(1, int(0.05 * self._sample_rate))
        self._rms_b = np.full(win, 1.0 / win, dtype=np.float64)
        attack = max(0.05, float(self._params.get("attack_ms", 8.0))) / 1000.0
        release = max(1.0, float(self._params.get("release_ms", 120.0))) / 1000.0
        self._a_attack = float(np.exp(-1.0 / (attack * self._sample_rate)))
        self._a_release = float(np.exp(-1.0 / (release * self._sample_rate)))

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0 or self._bp_ba is None:
            return samples
        from scipy.signal import lfilter, sosfilt

        params = self._params
        channels = samples.shape[0]
        detect = samples.mean(axis=0).astype(np.float64)
        order = max(len(self._bp_ba[0]), len(self._bp_ba[1])) - 1
        if self._bp_state is None or self._bp_state.shape != (order,):
            self._bp_state = np.zeros(order, dtype=np.float64)
        band, self._bp_state = lfilter(
            self._bp_ba[0], self._bp_ba[1], detect, zi=self._bp_state)
        if self._rms_state is None:
            self._rms_state = np.zeros(
                self._rms_b.shape[0] - 1, dtype=np.float64)
        ms, self._rms_state = lfilter(
            self._rms_b, [1.0], band ** 2, zi=self._rms_state)
        detector_db = 20.0 * np.log10(np.sqrt(np.maximum(ms, 1e-16)) + 1e-12)
        self._last_detector_db = float(detector_db[-1]) if n else -120.0

        # 动态增益（检测超阈 → 段增益向 −cut/＋boost 浮动；oracle 斜率）。
        threshold = float(params.get("threshold_dbfs", -30.0))
        slope = float(np.clip(params.get("slope", 0.5), 0.0, 1.0))
        max_cut = float(np.clip(params.get("max_cut_db", 9.0), 0.0, 30.0))
        max_boost = float(np.clip(params.get("max_boost_db", 0.0), 0.0, 24.0))
        over = detector_db - threshold
        target_db = np.where(
            over > 0.0,
            -np.minimum(max_cut, over * slope),
            np.minimum(max_boost, -over * slope))
        if not bool(params.get("dynamic_enabled", True)):
            target_db = np.zeros_like(target_db)

        # attack/release 平滑（dB 域，向目标收敛）。
        rising = target_db < self._gain_state  # 更深的衰减 = 快 attack
        coeffs = np.where(rising, self._a_attack, self._a_release)
        smooth_db = np.empty_like(target_db)
        state = self._gain_state
        for i in range(n):
            state = coeffs[i] * state + (1.0 - coeffs[i]) * target_db[i]
            smooth_db[i] = state
        self._gain_state = float(state)
        self._last_dyn_db = float(smooth_db[-1]) if n else 0.0

        base_gain = float(params.get("gain_db", 0.0))
        freq = float(np.clip(params.get("frequency", 3000.0),
                             20.0, self._sample_rate * 0.45))
        q = float(np.clip(params.get("q", 1.4), 0.1, 20.0))

        out = np.empty_like(samples)
        if self._sos_state is None or self._sos_state.shape != (1, channels, 2):
            self._sos_state = np.zeros((1, channels, 2), dtype=np.float64)
        sos_state = self._sos_state
        for start in range(0, n, GAIN_BLOCK):
            end = min(n, start + GAIN_BLOCK)
            seg_gain = base_gain + float(np.mean(smooth_db[start:end]))
            sos = rbj_bell_sos(freq, q, seg_gain, self._sample_rate)
            seg, sos_state = sosfilt(
                sos, samples[:, start:end].astype(np.float64), axis=-1,
                zi=sos_state)
            out[:, start:end] = seg.astype(np.float32)
        self._sos_state = sos_state
        return np.ascontiguousarray(out, dtype=np.float32)

    def collect_telemetry(self) -> dict:
        return {
            "detector_dbfs": round(self._last_detector_db, 3),
            "dynamic_gain_db": round(self._last_dyn_db, 3),
            "frequency": float(self._params.get("frequency", 3000.0)),
        }
