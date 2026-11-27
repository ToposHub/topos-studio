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
from src.features.edit.core.audio_effects.base import moving_mean_power
from src.features.edit.core.audio_effects.sidechain import (
    detector_input, detector_source_id,
)

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

    def __init__(self, sample_rate: float = 48000.0, *, params_are_snapshots: bool = False) -> None:
        super().__init__(sample_rate)
        self._params_are_snapshots = params_are_snapshots
        self._params: dict = dict(DEFAULTS)
        self._bp_ba: Optional[tuple] = None
        self._bp_state: Optional[np.ndarray] = None
        self._rms_b: Optional[np.ndarray] = None
        self._rms_state: Optional[np.ndarray] = None
        self._sos_state: Optional[np.ndarray] = None   # (1, ch, 2)
        self._gain_state: float = 0.0                  # 平滑后的动态 dB
        self._last_detector_db = -120.0
        self._last_dyn_db = 0.0
        # FX-02 多段支路：每段独立 detector/包络/输出滤波状态。
        self._mb_states: list = []
        self._sidechain_status = "internal"

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        rate_changed = sample_rate is not None and sample_rate != self._sample_rate
        if rate_changed:
            self._sample_rate = float(sample_rate)
            self.reset_state()
        merged = dict(DEFAULTS if self._params_are_snapshots else self._params)
        if isinstance(params, dict):
            merged.update(params)
        if not rate_changed and merged == self._params and self._bp_ba is not None:
            return
        if bool(self._bands_of(merged)) != bool(self._bands_of(self._params)):
            self.reset_state()
        self._params = merged
        # 段身份不变——滤波器状态保留（自动化区间可重开/参数微调无跳变）。
        self._rebuild_detector()

    def reset_state(self) -> None:
        self._bp_state = None
        self._rms_state = None
        self._sos_state = None
        self._gain_state = 0.0
        self._last_detector_db = -120.0
        self._last_dyn_db = 0.0
        for state in self._mb_states:
            state.reset()
        self._mb_states = []
        self._sidechain_status = "internal"

    @property
    def sc_source(self) -> str:
        return detector_source_id(self._params)

    @property
    def last_detector_db(self) -> float:
        return self._last_detector_db

    @property
    def is_active(self) -> bool:
        p = self._params
        if self._bands_of(p):
            return bool(p.get("enabled", True)) and _SCIPY and any(
                bool(band.get("enabled", True))
                for band in p["bands"])
        return (bool(p.get("enabled", True)) and _SCIPY
                and (bool(p.get("dynamic_enabled", True))
                     or abs(float(p.get("gain_db", 0.0))) > 1e-6))

    @staticmethod
    def _bands_of(params: dict) -> list:
        bands = params.get("bands") or ()
        return [band for band in bands if isinstance(band, dict)]

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

    def process(self, samples: np.ndarray,
                sidechain: Optional[np.ndarray] = None) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        detect, self._sidechain_status = detector_input(samples, sidechain, self.sc_source)
        if self._bands_of(self._params):
            return self._process_multiband(samples, detector_samples=detect)
        return self._process_single(samples, detector_samples=detect)

    def _process_single(self, samples: np.ndarray,
                        detector_samples: Optional[np.ndarray] = None) -> np.ndarray:
        n = samples.shape[-1]
        if n == 0 or self._bp_ba is None:
            return samples
        from scipy.signal import lfilter, sosfilt

        params = self._params
        channels = samples.shape[0]
        detect = (samples if detector_samples is None else detector_samples).mean(
            axis=0).astype(np.float64)
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
            "sidechain_source_id": self.sc_source,
            "sidechain_status": self._sidechain_status,
            "bands": [{"band_id": band.get("id", index),
                       "detector_dbfs": state.detector_db,
                       "dynamic_gain_db": state.gain_state if band.get("enabled", True) else 0.0}
                      for index, (band, state) in enumerate(
                          zip(self._bands_of(self._params), self._mb_states))],
        }


class _BandState:
    """FX-02 多段支路的每段跨块状态（detector/包络/输出滤波独立）。"""

    def __init__(self, band_index: int) -> None:
        self.band_index = band_index
        self.bp_ba: Optional[tuple] = None
        self.coeff_key: Optional[tuple] = None
        self.bp_state: Optional[np.ndarray] = None
        self.power_history: Optional[np.ndarray] = None
        self.sos_state: Optional[np.ndarray] = None  # (1, ch, 2)
        self.gain_state = 0.0
        self.sample_pos = 0  # 绝对样本时钟（增益网格对齐）
        self.cell_gain = 0.0  # 当前网格格的增益（格续块复用）
        self.detector_db = -120.0

    def reset(self) -> None:
        self.bp_state = None
        self.power_history = None
        self.sos_state = None
        self.gain_state = 0.0
        self.sample_pos = 0
        self.cell_gain = 0.0
        self.detector_db = -120.0


def _band_detector_coeffs(band: dict, sample_rate: float) -> Optional[tuple]:
    """段检测带通（butter2，带宽由 q 推导）；非法频点返回 None。"""
    freq = float(np.clip(float(band.get("frequency_hz", 3000.0)),
                         20.0, sample_rate * 0.45))
    q = float(np.clip(float(band.get("q", 1.4)), 0.1, 20.0))
    bw = freq / q
    low = max(10.0, freq - bw / 2.0)
    high = min(sample_rate * 0.49, freq + bw / 2.0)
    try:
        return butter(2, [low, high], btype="band", fs=sample_rate,
                      output="ba")
    except ValueError:
        return None


def _mb_process_band(band: dict, state: _BandState, samples: np.ndarray,
                     sample_rate: float,
                     detector_samples: Optional[np.ndarray] = None) -> np.ndarray:
    """单段处理：功率聚合检源（反相安全）→ 动态增益 → 段 biquad。

    与旧单段路径的波形平均检源不同：多段支路对每声道独立带通后按
    **功率平均**聚合（反相立体声不抵消——计划 FX-02 合同）。
    """
    from scipy.signal import lfilter, sosfilt

    n = samples.shape[-1]
    channels = samples.shape[0]
    # 检测系数缓存键（频率/q 决定；避免逐块比较 numpy 数组元组）。
    coeff_key = (round(float(band.get("frequency_hz", 3000.0)), 4),
                 round(float(band.get("q", 1.4)), 4),
                 round(float(sample_rate), 3))
    if state.bp_ba is None or state.coeff_key != coeff_key:
        state.bp_ba = _band_detector_coeffs(band, sample_rate)
        state.coeff_key = coeff_key
        state.bp_state = None
        state.power_history = None
    if state.bp_ba is None:
        return samples.astype(np.float32)
    order = max(len(state.bp_ba[0]), len(state.bp_ba[1])) - 1
    detector_samples = samples if detector_samples is None else detector_samples
    detector_channels = detector_samples.shape[0]
    # scipy lfilter(axis=-1) 的 zi 形状 = (ch, order)（axis 维替换为阶数）。
    if (state.bp_state is None
            or state.bp_state.shape != (detector_channels, order)):
        state.bp_state = np.zeros((detector_channels, order),
                                  dtype=np.float64)
    # 逐声道带通 + 功率聚合（同声道功率平均——反相不抵消）。
    detector_input = samples if detector_samples is None else detector_samples
    band_sig, state.bp_state = lfilter(
        state.bp_ba[0], state.bp_ba[1], detector_input.astype(np.float64),
        axis=-1, zi=state.bp_state)
    mean_power = np.mean(band_sig ** 2, axis=0)
    ms, state.power_history = moving_mean_power(
        mean_power, state.power_history, max(1, int(0.05 * sample_rate)))
    detector_db = 20.0 * np.log10(np.sqrt(np.maximum(ms, 1e-16)) + 1e-12)
    if n:
        state.detector_db = float(detector_db[-1])

    threshold = float(band.get("threshold_dbfs", -30.0))
    slope = float(np.clip(float(band.get("slope", 0.5)), 0.0, 1.0))
    max_cut = float(np.clip(float(band.get("max_cut_db", 9.0)), 0.0, 30.0))
    max_boost = float(np.clip(float(band.get("max_boost_db", 0.0)),
                              0.0, 24.0))
    over = detector_db - threshold
    target_db = np.where(
        over > 0.0,
        -np.minimum(max_cut, over * slope),
        np.minimum(max_boost, -over * slope))
    if not bool(band.get("dynamic_enabled", True)):
        target_db = np.zeros_like(target_db)

    attack = max(0.05, float(band.get("attack_ms", 8.0))) / 1000.0
    release = max(1.0, float(band.get("release_ms", 120.0))) / 1000.0
    a_attack = float(np.exp(-1.0 / (attack * sample_rate)))
    a_release = float(np.exp(-1.0 / (release * sample_rate)))
    # attack/release 判定按**运行态**逐样本比较（旧单段路径用调用入口
    # 状态整块判定——分块语义不同，按计划 v0–v4 冻结不动）。
    smooth_db = np.empty_like(target_db)
    gain_state = state.gain_state
    for i in range(n):
        coeff = a_attack if target_db[i] < gain_state else a_release
        gain_state = coeff * gain_state + (1.0 - coeff) * target_db[i]
        smooth_db[i] = gain_state
    state.gain_state = float(gain_state)

    static_gain = float(band.get("static_gain_db", 0.0))
    freq = float(np.clip(float(band.get("frequency_hz", 3000.0)),
                         20.0, sample_rate * 0.45))
    q = float(np.clip(float(band.get("q", 1.4)), 0.1, 20.0))
    out = np.empty_like(samples)
    if state.sos_state is None or state.sos_state.shape != (1, channels, 2):
        state.sos_state = np.zeros((1, channels, 2), dtype=np.float64)
    # 增益子块按**绝对样本时钟**对齐 GAIN_BLOCK 网格；每格系数取格首
    # 样本的平滑增益（瞬时值——跨块分块一致；计划 FX-02："任意块长下
    # 按持续样本时钟更新动态系数"）。
    pos = state.sample_pos
    offset = pos % GAIN_BLOCK
    cell_gain = state.cell_gain
    start = 0
    while start < n:
        if offset == 0:
            # 新格：增益取**格首样本**的平滑值（跨块一致——格续块
            # 复用携带值，不以续块起点重取）。
            cell_gain = static_gain + float(smooth_db[start])
        take = min(GAIN_BLOCK - offset, n - start)
        sos = rbj_bell_sos(freq, q, cell_gain, sample_rate)
        seg, state.sos_state = sosfilt(
            sos, samples[:, start:start + take].astype(np.float64),
            axis=-1, zi=state.sos_state)
        out[:, start:start + take] = seg.astype(np.float32)
        start += take
        offset = (offset + take) % GAIN_BLOCK
    state.sample_pos = pos + n
    state.cell_gain = cell_gain
    return np.ascontiguousarray(out, dtype=np.float32)


def _mb_process_multiband(self, samples: np.ndarray,
                        detector_samples: Optional[np.ndarray] = None) -> np.ndarray:
    """多段动态 EQ（FX-02 新支路）：稳定段 ID = 列表序号；每段独立
    detector/包络/输出滤波状态；禁用段直通。"""
    bands = self._bands_of(self._params)
    if len(self._mb_states) != len(bands):
        self._mb_states = [_BandState(index) for index in range(len(bands))]
    out = np.array(samples, dtype=np.float32, copy=True)
    for band, state in zip(bands, self._mb_states):
        if not bool(band.get("enabled", True)):
            continue
        out = _mb_process_band(band, state, out, self._sample_rate,
                               detector_samples=(samples if detector_samples is None
                                                 else detector_samples))
    active_states = [state for band, state in zip(bands, self._mb_states)
                     if bool(band.get("enabled", True))]
    if active_states:
        self._last_detector_db = max(state.detector_db for state in active_states)
        self._last_dyn_db = min(state.gain_state for state in active_states)
    return np.ascontiguousarray(out, dtype=np.float32)


# 挂到类上（保持模块级实现便于直测）。
DynamicEqEffect._process_multiband = _mb_process_multiband
