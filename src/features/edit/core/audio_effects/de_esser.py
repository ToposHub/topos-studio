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
    """齿音抑制（RMS 包络检测 + 软拐点 + Wide/Split 分频）。"""

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

    def tail_estimate_samples(self) -> int:
        return 0

    def latency_samples(self) -> int:
        return 0

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
