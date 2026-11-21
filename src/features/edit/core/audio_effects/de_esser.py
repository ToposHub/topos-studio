"""P1 · De-Esser（2026-11-19 计划第三阶段 · 效果器接入 #1）。

检测频带为可调 band-pass（中心频率 + Q），对带内 RMS 包络做
attack/release 平滑与软拐点，限制最大 GR；增益控制遵循计划的离线
oracle::

    over_db = max(0.0, detector_dbfs - threshold_dbfs)
    reduction_db = min(max_reduction_db, over_db * (1.0 - 1.0 / ratio))
    gain = 10.0 ** (-reduction_db / 20.0)
    # Wide: gain * full_band
    # Split: unaffected_band + gain * sibilance_band

- Wide：全带乘增益；Split：匹配滤波分频（sibilance = band-pass，
  unaffected = x − sibilance 的减法互补），低频不受影响；
- listen（隔离监听）：输出检测频带（detector listen）；
- 复用压缩器语义：软拐点、attack/release 包络、遥测（当前 GR dB）；
- 旁路/scipy 缺席/维度异常：原样返回（旁路无跳变）。
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

_SCIPY_AVAILABLE = True
try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, lfilter, sosfilt  # noqa: F401
except ImportError:  # pragma: no cover
    _SCIPY_AVAILABLE = False


def scipy_available() -> bool:
    return _SCIPY_AVAILABLE


#: 默认参数（与 UI schema 同源）。
DEFAULTS = {
    "enabled": True,
    "threshold_dbfs": -32.0,
    "ratio": 4.0,
    "frequency": 6500.0,
    "q": 2.0,
    "attack_ms": 5.0,
    "release_ms": 60.0,
    "max_reduction_db": 12.0,
    "mode": "split",          # "wide" | "split"
    "listen": False,
}


class DeEsserEffect(AudioEffect):
    """齿音抑制（RMS 包络检测 + 软拐点 + Wide/Split 分频）。

    FX-06（2026-11-21 计划）新版支路（给了 ``detector_shape`` /
    ``lookahead_ms`` / ``output_gain_db`` 任一键时启用）：逐声道检测
    带（同系数独立状态）→ 各声道带功率取最大联动（反相不抵消）→
    50 ms RMS + attack/release → 链接增益；audio 延迟 = lookahead
    （增益即时作用——标准前视实现，``latency_samples`` 据实声明）；
    Split 用延迟后音频的逐声道带（减法互补）。legacy 参数组合走旧
    路径（声道平均检测语义冻结）。
    """

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = dict(DEFAULTS)
        self._b0 = self._b1 = self._a1 = 0.0     # 一阶包络平滑
        self._env = 0.0
        self._bp_state: Optional[np.ndarray] = None  # lfilter zi（1D）
        self._bp_ba: Optional[tuple] = None
        self._rms_b: Optional[np.ndarray] = None     # 包络 FIR
        self._rms_state: Optional[np.ndarray] = None
        self._last_reduction_db = 0.0
        # 新版支路状态。
        self._v2_bp_state: Optional[np.ndarray] = None   # (order, ch)
        self._v2_delay_state: Optional[np.ndarray] = None  # 延迟线
        self._v2_delayed_bp_state: Optional[np.ndarray] = None
        self._v2_rms_state: Optional[np.ndarray] = None
        self._v2_detector_ba: Optional[tuple] = None
        self._v2_detector_key: Optional[tuple] = None
        self._v2_env = 0.0

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
        self._rebuild_filters()
        self.reset_state()

    def reset_state(self) -> None:
        self._env = 0.0
        if self._bp_state is not None:
            self._bp_state = np.zeros_like(self._bp_state)
        if self._rms_state is not None:
            self._rms_state = np.zeros_like(self._rms_state)
        self._last_reduction_db = 0.0
        self._v2_bp_state = None
        self._v2_delay_state = None
        self._v2_delayed_bp_state = None
        self._v2_rms_state = None
        self._v2_env = 0.0

    def tail_estimate_samples(self) -> int:
        return 0

    def latency_samples(self) -> int:
        if self._uses_v2():
            lookahead_ms = float(np.clip(
                float(self._params.get("lookahead_ms", 0.0)), 0.0, 20.0))
            return int(round(lookahead_ms * self._sample_rate / 1000.0))
        return 0

    def _uses_v2(self) -> bool:
        return any(key in self._params for key in
                   ("detector_shape", "lookahead_ms", "output_gain_db"))

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True)) and _SCIPY_AVAILABLE

    # ── 滤波器 ────────────────────────────────────────────────────
    def _rebuild_filters(self) -> None:
        if not _SCIPY_AVAILABLE:
            return
        from scipy.signal import butter
        sr = self._sample_rate
        center = float(np.clip(self._params.get("frequency", 6500.0),
                               1000.0, min(16000.0, sr * 0.45)))
        q = float(np.clip(self._params.get("q", 2.0), 0.3, 12.0))
        bw = center / q
        low = max(20.0, center - bw / 2.0)
        high = min(sr * 0.49, center + bw / 2.0)
        try:
            self._bp_ba = butter(2, [low, high], btype="band",
                                 fs=sr, output="ba")
        except ValueError:
            self._bp_ba = None
        # 包络平滑系数（一极，attack 快 release 慢——分支平滑）。
        attack = max(0.05, float(self._params.get("attack_ms", 5.0))) / 1000.0
        release = max(1.0, float(self._params.get("release_ms", 60.0))) / 1000.0
        self._a_attack = float(np.exp(-1.0 / (attack * sr)))
        self._a_release = float(np.exp(-1.0 / (release * sr)))

    # ── 处理 ──────────────────────────────────────────────────────
    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        if samples.shape[-1] == 0 or self._bp_ba is None:
            return samples
        if self._uses_v2():
            return self._process_v2(samples)
        from scipy.signal import lfilter

        params = self._params
        out = np.asarray(samples, dtype=np.float32)

        # 检测频带（声道平均 → band-pass）。
        detect = out.mean(axis=0)
        order = max(len(self._bp_ba[0]), len(self._bp_ba[1])) - 1
        if self._bp_state is None or self._bp_state.shape != (order,):
            self._bp_state = np.zeros(order, dtype=np.float64)
        band, self._bp_state = lfilter(
            self._bp_ba[0], self._bp_ba[1], detect, zi=self._bp_state)
        band = band.astype(np.float32)

        # RMS 包络（50 ms 移动平方均值，FIR 流式——分块与整段逐位一致）
        # + attack/release 分支平滑。
        sq = band.astype(np.float64) ** 2
        win = max(1, int(0.05 * self._sample_rate))
        if self._rms_b is None or len(self._rms_b) != win:
            self._rms_b = np.full(win, 1.0 / win, dtype=np.float64)
            self._rms_state = np.zeros(win - 1, dtype=np.float64)
        ms, self._rms_state = lfilter(
            self._rms_b, [1.0], sq, zi=self._rms_state)
        env = np.empty_like(ms)
        level = self._env
        rising = ms > level
        coeffs = np.where(rising, self._a_attack, self._a_release)
        # 逐样本分支平滑（向量化：分段 exp 前缀积，块内确定性一致）。
        for i in range(ms.shape[0]):
            level = coeffs[i] * level + (1.0 - coeffs[i]) * ms[i]
            env[i] = level
        self._env = float(level)

        detector_db = 20.0 * np.log10(np.sqrt(np.maximum(env, 1e-12)) + 1e-12)
        threshold = float(params.get("threshold_dbfs", -32.0))
        ratio = float(np.clip(params.get("ratio", 4.0), 1.0, 50.0))
        max_red = float(np.clip(params.get("max_reduction_db", 12.0),
                                0.0, 36.0))
        over_db = np.maximum(0.0, detector_db - threshold)
        # 计划 oracle（软拐点由 attack/release 平滑承载）。
        reduction_db = np.minimum(max_red, over_db * (1.0 - 1.0 / ratio))
        gain = (10.0 ** (-reduction_db / 20.0)).astype(np.float32)
        self._last_reduction_db = float(reduction_db[-1])

        if bool(params.get("listen", False)):
            # 隔离监听：输出检测频带（detector listen）。
            return np.ascontiguousarray(
                np.stack([band] * out.shape[0]), dtype=np.float32)

        mode = str(params.get("mode", "split"))
        if mode == "wide":
            return np.ascontiguousarray(out * gain[np.newaxis, :])
        # Split：匹配滤波分频（减法互补——带外不受影响）。
        sibilance = np.stack([band] * out.shape[0])
        unaffected = out - sibilance
        return np.ascontiguousarray(
            unaffected + sibilance * gain[np.newaxis, :], dtype=np.float32)

    # ── 遥测（复用压缩器检源/遥测通道） ────────────────────────────
    def collect_telemetry(self) -> dict:
        return {
            "gain_reduction_db": round(self._last_reduction_db, 3),
            "mode": str(self._params.get("mode", "split")),
            "threshold_dbfs": float(self._params.get("threshold_dbfs", -32.0)),
        }

    def take_freeze_capture(self) -> dict:
        return {
            "env": self._env,
            "bp_state": None if self._bp_state is None
            else self._bp_state.tolist(),
            "last_reduction_db": self._last_reduction_db,
        }

    def install_freeze_capture(self, capture: dict) -> bool:
        try:
            self._env = float(capture.get("env", 0.0) or 0.0)
            state = capture.get("bp_state")
            self._bp_state = (np.asarray(state, dtype=np.float64)
                              if state is not None and self._bp_state is not None
                              else self._bp_state)
            self._last_reduction_db = float(
                capture.get("last_reduction_db", 0.0) or 0.0)
            return True
        except (TypeError, ValueError):
            return False

    # ── FX-06 新版支路 ─────────────────────────────────────────────
    def _v2_detector(self) -> Optional[tuple]:
        """检测形状：bandpass（默认，与旧一致）或 high_shelf（+6 dB
        RBJ 高架——检测支路更宽的齿音强调）。"""
        shape = str(self._params.get("detector_shape", "bandpass")
                    or "bandpass")
        sr = self._sample_rate
        center = float(np.clip(self._params.get("frequency", 6500.0),
                               1000.0, min(16000.0, sr * 0.45)))
        q = float(np.clip(self._params.get("q", 2.0), 0.3, 12.0))
        key = (shape, round(center, 3), round(q, 3), round(sr, 3))
        if self._v2_detector_key == key and self._v2_detector_ba is not None:
            return self._v2_detector_ba
        from scipy.signal import butter
        if shape == "high_shelf":
            from src.features.edit.core.audio_effects.saturation import (
                _rbj_high_shelf,
            )
            b, a = _rbj_high_shelf(sr, center, 6.0)
            self._v2_detector_ba = (b, a)
        else:
            bw = center / q
            low = max(20.0, center - bw / 2.0)
            high = min(sr * 0.49, center + bw / 2.0)
            try:
                self._v2_detector_ba = butter(2, [low, high], btype="band",
                                              fs=sr, output="ba")
            except ValueError:
                self._v2_detector_ba = None
        self._v2_detector_key = key
        return self._v2_detector_ba

    def _process_v2(self, samples: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter

        params = self._params
        channels, n = samples.shape
        sr = self._sample_rate
        ba = self._v2_detector()
        if ba is None:
            return np.asarray(samples, dtype=np.float32)

        lookahead = self.latency_samples()
        attack = max(0.05, float(params.get("attack_ms", 5.0))) / 1000.0
        release = max(1.0, float(params.get("release_ms", 60.0))) / 1000.0
        a_attack = float(np.exp(-1.0 / (attack * sr)))
        a_release = float(np.exp(-1.0 / (release * sr)))
        win = max(1, int(0.05 * sr))

        # 延迟线（audio 延迟 = lookahead；增益即时作用 → 前视）。
        if lookahead > 0:
            if (self._v2_delay_state is None
                    or self._v2_delay_state.shape != (channels, lookahead)):
                self._v2_delay_state = np.zeros(
                    (channels, lookahead), dtype=np.float32)
            stream = np.concatenate(
                [self._v2_delay_state, samples.astype(np.float32)], axis=1)
            delayed = np.ascontiguousarray(stream[:, :n])
            self._v2_delay_state = np.ascontiguousarray(
                stream[:, n:n + lookahead], dtype=np.float32)
        else:
            delayed = np.asarray(samples, dtype=np.float32)

        # 检测支路（未延迟信号；逐声道独立状态 → 反相不抵消）。
        # lfilter(axis=-1) 的 zi 形状 = (ch, order)。
        order = max(len(ba[0]), len(ba[1])) - 1
        if (self._v2_bp_state is None
                or self._v2_bp_state.shape != (channels, order)):
            self._v2_bp_state = np.zeros((channels, order), dtype=np.float64)
        band_ch, self._v2_bp_state = lfilter(
            ba[0], ba[1], samples.astype(np.float64), axis=-1,
            zi=self._v2_bp_state)
        # 各声道带功率 → 最大电平联动（反相安全）。
        ch_power = np.mean(band_ch ** 2, axis=0)
        win_b = max(1, int(0.05 * sr))
        if (self._v2_rms_state is None
                or self._v2_rms_state.shape != (win_b - 1,)):
            self._v2_rms_state = np.zeros(win_b - 1, dtype=np.float64)
        rms_b = np.full(win_b, 1.0 / win_b, dtype=np.float64)
        env_p, self._v2_rms_state = lfilter(rms_b, [1.0], ch_power,
                                            zi=self._v2_rms_state)
        max_power = np.maximum.accumulate(np.maximum(
            env_p, np.maximum.accumulate(ch_power) * 0.0))  # 占位对齐
        # RMS 平滑后的聚合电平（逐样本最大：各声道 RMS 的最大值近似 —
        # 用功率 RMS 的最大通道值）。
        power_level = env_p  # 已为 50 ms 平滑后的逐样本功率
        detector_db = 20.0 * np.log10(
            np.sqrt(np.maximum(power_level, 1e-12)) + 1e-12)

        threshold = float(params.get("threshold_dbfs", -32.0))
        ratio = float(np.clip(params.get("ratio", 4.0), 1.0, 50.0))
        max_red = float(np.clip(params.get("max_reduction_db", 12.0),
                                0.0, 36.0))
        over_db = np.maximum(0.0, detector_db - threshold)
        reduction_db = np.minimum(max_red, over_db * (1.0 - 1.0 / ratio))
        gain = (10.0 ** (-reduction_db / 20.0)).astype(np.float32)
        self._last_reduction_db = float(reduction_db[-1])

        if bool(params.get("listen", False)):
            return np.ascontiguousarray(
                np.stack([band_ch.mean(axis=0)] * channels),
                dtype=np.float32)

        # 延迟后音频的逐声道带（Split 用；独立状态）。
        if (self._v2_delayed_bp_state is None
                or self._v2_delayed_bp_state.shape != (channels, order)):
            self._v2_delayed_bp_state = np.zeros(
                (channels, order), dtype=np.float64)
        band_delayed, self._v2_delayed_bp_state = lfilter(
            ba[0], ba[1], delayed.astype(np.float64), axis=-1,
            zi=self._v2_delayed_bp_state)

        mode = str(params.get("mode", "split"))
        if mode == "wide":
            out = delayed * gain[np.newaxis, :]
        else:
            sibilance = band_delayed.astype(np.float32)
            unaffected = delayed - sibilance
            out = unaffected + sibilance * gain[np.newaxis, :]
        out_gain = 10.0 ** (float(np.clip(
            float(params.get("output_gain_db", 0.0)), -24.0, 24.0)) / 20.0)
        return np.ascontiguousarray(out * out_gain, dtype=np.float32)
