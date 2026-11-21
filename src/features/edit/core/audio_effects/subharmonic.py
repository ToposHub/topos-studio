"""P2 · 次谐波发生器（FX-16 · 2026-11-21 计划）。

首版面向单一低频音源（贝斯/简单乐器）：

- **检测**：单声道检测信号（立体声取能量较高声道——反相素材不抵消）
  在 45–180 Hz 目标范围做归一化自相关，取首个可信局部峰（>0.8）；
  低能量/无峰/频率突跳拒绝；进入/退出不同门限（滞回）+ 最少持锁
  帧数；检测窗按**绝对样本时钟**的 hop 网格对齐（跨块一致）；
- **生成**：相位累加器（f0/2、f0/4，跨块连续）× 各自增益（dB），
  失锁在 release_ms 内平滑退回干声；
- **合成**：次谐波经 sub_lpf 低通后按 mix 混入（干声按声明延迟对齐
  ——检测窗引入的固有延迟据实声明）；output_gain_db 收尾；
- 注册：``subharmonic``（Clip/Track 立体声首批）。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, sosfilt
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False

#: 检测目标范围与窗参数（计划建议 45–180 Hz、100 ms 窗）。
_F0_LOW = 45.0
_F0_HIGH = 180.0
_DETECT_WIN = 4800          # 100 ms @48k（按采样率缩放）
_HOP = 512
_ENTER_CONF = 0.8           # 进入锁定置信
_EXIT_CONF = 0.55           # 退出锁定（滞回）
_MIN_LOCK_FRAMES = 3        # 最少持锁帧数
_DETECT_LATENCY = _DETECT_WIN - _HOP  # 检测窗固有延迟（样本）


def estimate_subharmonic_f0(frame: np.ndarray, sample_rate: float) -> Optional[float]:
    """归一化自相关检测：45–180 Hz 首个可信局部峰（计划实验口径）。"""
    x = frame - np.mean(frame)
    if x.size < 3 * int(sample_rate / _F0_LOW):
        return None
    if float(np.sqrt(np.mean(x * x))) < 0.01:
        return None
    first = max(2, int(np.floor(sample_rate / _F0_HIGH)))
    last = int(np.ceil(sample_rate / _F0_LOW))
    if last + 2 >= x.size:
        return None
    corr = []
    for lag in range(first - 1, last + 2):
        a, b = x[:-lag], x[lag:]
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        corr.append(float(np.dot(a, b) / max(norm, 1e-12)))
    for index in range(1, len(corr) - 1):
        left, center, right = corr[index - 1], corr[index], corr[index + 1]
        if center > _ENTER_CONF and center > left and center >= right:
            return sample_rate / (first - 1 + index)
    return None


class SubharmonicEffect(AudioEffect):
    """次谐波发生器（置信度门控的八度生成支路；FX-16 首版）。"""

    effect_type = "subharmonic"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {
            "enabled": True,
            "source_center_hz": 90.0,
            "source_bandwidth_hz": 60.0,
            "octave1_gain_db": -6.0,
            "octave2_gain_db": -12.0,
            "sub_lpf_hz": 120.0,
            "attack_ms": 20.0,
            "release_ms": 150.0,
            "mix": 0.5,
            "output_gain_db": 0.0,
        }
        self._channels = 0
        self._detect_ring: Optional[np.ndarray] = None
        self._ring_pos = 0
        self._pos = 0
        self._locked = False
        self._lock_frames = 0
        self._f0 = 0.0
        self._env = 0.0
        self._phase1 = 0.0
        self._phase2 = 0.0
        self._sub_sos = None
        self._sub_zi = None
        self._next_hop = 0      # 下一检测点（绝对样本时钟）
        self._rebuild()

    def _rebuild(self) -> None:
        lpf = float(np.clip(self._params.get("sub_lpf_hz", 120.0),
                            40.0, 0.45 * self._sample_rate))
        self._sub_sos = np.asarray(
            butter(2, lpf, btype="lowpass", fs=self._sample_rate,
                   output="sos"), dtype=np.float64)
        self._sub_zi = None

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._detect_ring = None
        merged = dict(self._params)
        if isinstance(params, dict):
            for key, value in params.items():
                merged[self._KEY_ALIASES.get(key, key)] = value
        self._params = merged
        self._rebuild()

    _KEY_ALIASES = {
        "sh_source_center_hz": "source_center_hz",
        "sh_source_bandwidth_hz": "source_bandwidth_hz",
        "sh_octave1_gain_db": "octave1_gain_db",
        "sh_octave2_gain_db": "octave2_gain_db",
        "sh_sub_lpf_hz": "sub_lpf_hz",
        "sh_attack_ms": "attack_ms",
        "sh_release_ms": "release_ms",
        "sh_mix": "mix",
        "sh_output_gain_db": "output_gain_db",
    }

    def reset_state(self) -> None:
        self._detect_ring = None
        self._ring_pos = 0
        self._pos = 0
        self._locked = False
        self._lock_frames = 0
        self._f0 = 0.0
        self._env = 0.0
        self._phase1 = 0.0
        self._phase2 = 0.0
        self._next_hop = 0
        self._sub_zi = None

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True)) and _SCIPY

    def latency_samples(self) -> int:
        return _DETECT_LATENCY

    def tail_estimate_samples(self) -> int:
        release = max(1.0, float(self._params.get("release_ms", 150.0)))
        return int(release * self._sample_rate / 1000.0)

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
            self._detect_ring = None
            self._rebuild()
        sr = float(self._sample_rate)
        win = int(_DETECT_WIN * sr / 48000.0)
        win = max(1024, win)
        if self._detect_ring is None or self._detect_ring.shape != (
                channels, win):
            self._detect_ring = np.zeros((channels, win), dtype=np.float64)
            self._ring_pos = 0
        mix = float(np.clip(self._params.get("mix", 0.5), 0.0, 1.0))
        attack = max(1.0, float(self._params.get("attack_ms", 20.0)))
        release = max(1.0, float(self._params.get("release_ms", 150.0)))
        a_attack = float(np.exp(-1.0 / (attack * sr / 1000.0)))
        a_release = float(np.exp(-1.0 / (release * sr / 1000.0)))
        g1 = 10.0 ** (float(self._params.get("octave1_gain_db", -6.0)) / 20.0)
        g2 = 10.0 ** (float(self._params.get("octave2_gain_db", -12.0)) / 20.0)

        sub_block = np.empty(n, dtype=np.float64)
        for i in range(n):
            # 检测环喂入（绝对时钟 hop 网格对齐——跨块一致）。
            self._detect_ring[:, self._ring_pos] = samples[:, i]
            self._ring_pos = (self._ring_pos + 1) % win
            if self._pos + i >= self._next_hop:
                self._detect_at_hop(sr, win)
                self._next_hop = self._pos + i + _HOP
            # 生成：锁定的 f0 的 1/2 与 1/4，相位跨块连续。
            sub1 = float(np.sin(2.0 * np.pi * self._phase1))
            sub2 = float(np.sin(2.0 * np.pi * self._phase2))
            if self._locked and self._f0 > 0.0:
                step1 = self._f0 / 2.0 / sr
                step2 = self._f0 / 4.0 / sr
            else:
                step1 = step2 = 0.0
            self._phase1 = (self._phase1 + step1) % 1.0
            self._phase2 = (self._phase2 + step2) % 1.0
            target_env = 1.0 if self._locked else 0.0
            self._env = (a_attack * self._env
                         + (1.0 - a_attack) * target_env
                         if target_env > self._env
                         else a_release * self._env
                         + (1.0 - a_release) * target_env)
            sub_block[i] = (g1 * sub1 + g2 * sub2) * self._env
        self._pos += n
        # sub_lpf 只作用于**生成支路**（mono 块滤波后广播）。
        if self._sub_zi is None or self._sub_zi.shape != (
                self._sub_sos.shape[0], 1, 2):
            self._sub_zi = np.zeros(
                (self._sub_sos.shape[0], 1, 2), dtype=np.float64)
        sub_filtered, self._sub_zi = sosfilt(
            self._sub_sos, sub_block[None, :], axis=-1, zi=self._sub_zi)
        out_gain = 10.0 ** (float(self._params.get("output_gain_db", 0.0))
                            / 20.0)
        out = (samples.astype(np.float64) * (1.0 - mix)
               + sub_filtered[0][np.newaxis, :] * mix)
        return np.ascontiguousarray(
            (out * out_gain).astype(np.float32), dtype=np.float32)

    def _detect_at_hop(self, sr: float, win: int) -> None:
        """按检测环估计 F0（能量较高声道——反相不抵消）+ 滞回。"""
        ring = self._detect_ring
        powers = np.mean(ring ** 2, axis=1)
        ch = int(np.argmax(powers))
        frame = ring[ch]
        f0 = estimate_subharmonic_f0(frame, sr)
        if self._locked:
            if f0 is None or float(np.abs(f0 - self._f0)) > 0.25 * self._f0:
                self._lock_frames = 0
                self._locked = False   # 突跳/失锁 → 释放到干声
            else:
                self._f0 = 0.9 * self._f0 + 0.1 * f0  # 平滑跟随微漂
        else:
            if f0 is not None:
                self._lock_frames += 1
                if self._lock_frames >= _MIN_LOCK_FRAMES:
                    self._locked = True
                    self._f0 = f0
            else:
                self._lock_frames = 0

    def collect_telemetry(self) -> dict:
        return {
            "locked": bool(self._locked),
            "f0_hz": round(float(self._f0), 2),
            "lock_frames": int(self._lock_frames),
            "env": round(float(self._env), 4),
        }
