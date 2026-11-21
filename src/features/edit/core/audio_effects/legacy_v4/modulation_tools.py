# Frozen compatibility implementation from 5edcfdeb^:src/features/edit/core/audio_effects/modulation_tools.py.
# Keep historical PCM behavior; current DSP lives in the parent package.
"""P1 · 瞬态塑形与调制类效果（2026-11-19 计划第三阶段 · #7）。

- ``TransientDesignerEffect``：差分包络瞬态塑形（attack/sustain 独立
  增益——Foley/动作音效优先）；
- ``TremoloAutoPanEffect``：LFO 颤音（幅度）/自动声像（L/R 反相）；
- ``ChorusFlangerEffect``：调制延迟线（chorus 多声部 / flanger 短延迟
  + 反馈——导出尾音按反馈预算声明）；
- ``PhaserEffect``：全通级联调制。
共享相位连续 LFO：跨块相位状态持久（tempo 同步由自动化以 rate_hz
注入；参数自动化/撤销后相位连续——LFO 相位不随参数重置）。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, lfilter  # noqa: F401
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False


class _Lfo:
    """相位连续的正弦 LFO（相位跨块持久；set_params 不重置相位）。"""

    def __init__(self, rate_hz: float = 2.0) -> None:
        self.rate_hz = float(rate_hz)
        self.phase = 0.0

    def advance(self, n: int, sample_rate: float) -> np.ndarray:
        steps = 2.0 * np.pi * self.rate_hz / sample_rate
        values = np.sin(self.phase + steps * np.arange(n, dtype=np.float64))
        self.phase = float((self.phase + steps * n) % (2.0 * np.pi))
        return values.astype(np.float32)

    def reset(self) -> None:
        self.phase = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 瞬态塑形
# ─────────────────────────────────────────────────────────────────────────────

class TransientDesignerEffect(AudioEffect):
    """差分包络瞬态塑形（attack 增益独立于 sustain）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {"enabled": True, "attack_db": 6.0,
                              "sustain_db": 0.0, "attack_ms": 8.0,
                              "sustain_ms": 80.0}
        self._fast_env = 0.0
        self._slow_env = 0.0

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
        self._fast_env = 0.0
        self._slow_env = 0.0

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        return 0

    def latency_samples(self) -> int:
        return 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        attack_ms = max(0.5, float(self._params.get("attack_ms", 8.0)))
        sustain_ms = max(10.0, float(self._params.get("sustain_ms", 80.0)))
        a_fast = float(np.exp(-1.0 / (attack_ms * self._sample_rate / 1000.0)))
        a_slow = float(np.exp(-1.0 / (sustain_ms * self._sample_rate / 1000.0)))
        attack_gain = 10.0 ** (float(self._params.get("attack_db", 6.0)) / 20.0)
        sustain_gain = 10.0 ** (float(self._params.get("sustain_db", 0.0)) / 20.0)

        mono = np.abs(samples.mean(axis=0)).astype(np.float64)
        gains = np.empty(n, dtype=np.float32)
        fast = self._fast_env
        slow = self._slow_env
        for i in range(n):
            fast = a_fast * fast + (1.0 - a_fast) * mono[i]
            slow = a_slow * slow + (1.0 - a_slow) * mono[i]
            # dB 域瞬态量（带 3 dB 膝）：稳态包络波动 < 膝 → weight 0，
            # attack 时 fast 领先 slow ≥9 dB → weight 1。
            ratio_db = 20.0 * np.log10(max(fast, 1e-9) / max(slow, 1e-9))
            weight = float(np.clip((ratio_db - 3.0) / 6.0, 0.0, 1.0))
            gains[i] = (sustain_gain * (1.0 - weight)
                        + attack_gain * weight)
        self._fast_env = float(fast)
        self._slow_env = float(slow)
        return np.ascontiguousarray(
            samples * gains[np.newaxis, :], dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# 颤音 / 自动声像
# ─────────────────────────────────────────────────────────────────────────────

class TremoloAutoPanEffect(AudioEffect):
    """LFO 颤音（幅度调制）与自动声像（L/R 反相）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {"enabled": True, "mode": "tremolo",
                              "rate_hz": 4.0, "depth": 0.7,
                              "shape": "sine"}
        self._lfo = _Lfo(4.0)

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        # 相位连续：只改速率，不重置相位（自动化/撤销后 LFO 连续）。
        self._lfo.rate_hz = float(merged.get("rate_hz", 4.0))

    def reset_state(self) -> None:
        self._lfo.reset()

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        return 0

    def latency_samples(self) -> int:
        return 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        depth = float(np.clip(self._params.get("depth", 0.7), 0.0, 1.0))
        mode = str(self._params.get("mode", "tremolo"))
        lfo = self._lfo.advance(n, self._sample_rate)
        if mode == "auto_pan" and samples.shape[0] >= 2:
            pan = depth * lfo
            left_gain = np.sqrt(0.5 * (1.0 - pan))
            right_gain = np.sqrt(0.5 * (1.0 + pan))
            out = samples.copy()
            out[0] *= left_gain
            out[1] *= right_gain
            return np.ascontiguousarray(out, dtype=np.float32)
        amplitude = 1.0 - depth * (0.5 - 0.5 * lfo)
        return np.ascontiguousarray(
            samples * amplitude[np.newaxis, :], dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Chorus / Flanger
# ─────────────────────────────────────────────────────────────────────────────

class ChorusFlangerEffect(AudioEffect):
    """调制延迟线：chorus（多声部长延迟无反馈）/ flanger（短延迟+反馈）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {
            "enabled": True, "mode": "chorus", "rate_hz": 0.6,
            "depth_ms": 3.0, "base_ms": 18.0, "feedback": 0.0,
            "voices": 2, "mix": 0.5,
        }
        self._lfo = _Lfo(0.6)
        self._delay_line = np.zeros((1, int(0.05 * sample_rate)),
                                    dtype=np.float32)

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
        self._lfo.rate_hz = float(merged.get("rate_hz", 0.6))

    def reset_state(self) -> None:
        self._lfo.reset()
        self._delay_line = np.zeros(
            (1, int(0.05 * self._sample_rate)), dtype=np.float32)

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        # 反馈尾音预算：flanger 反馈衰减到 −60 dB 的时间（上限 1 s）。
        fb = float(np.clip(self._params.get("feedback", 0.0), 0.0, 0.95))
        if fb <= 0.0:
            return 0
        cycles = max(1.0, -6.0 / (20.0 * np.log10(max(fb, 1e-6))))
        base_ms = float(self._params.get("base_ms", 18.0))
        tail = int(cycles * base_ms * self._sample_rate / 1000.0)
        return min(int(self._sample_rate), tail)

    def latency_samples(self) -> int:
        return 0

    def _read_delay(self, line: np.ndarray, delay_samples: float) -> float:
        idx = int(delay_samples)
        frac = delay_samples - idx
        idx = max(0, min(line.shape[-1] - 2, idx))
        return float(line[idx] * (1.0 - frac) + line[idx + 1] * frac)

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        params = self._params
        rate = float(np.clip(params.get("rate_hz", 0.6), 0.01, 20.0))
        depth = max(0.0, float(params.get("depth_ms", 3.0)))
        base = max(0.5, float(params.get("base_ms", 18.0)))
        feedback = float(np.clip(params.get("feedback", 0.0), 0.0, 0.95))
        voices = int(np.clip(params.get("voices", 2), 1, 4))
        mix = float(np.clip(params.get("mix", 0.5), 0.0, 1.0))

        mono = samples.mean(axis=0) if samples.shape[0] > 1 else samples[0]
        self._lfo.advance(n, self._sample_rate)
        line = self._delay_line.reshape(-1)
        max_delay = line.shape[-1] - 2
        wet = np.zeros(n, dtype=np.float32)
        base_samples = base * self._sample_rate / 1000.0
        depth_samples = depth * self._sample_rate / 1000.0
        for i in range(n):
            voice_sum = 0.0
            for voice in range(voices):
                phase = 2.0 * np.pi * voice / max(1, voices)
                mod = np.sin(
                    float(getattr(self._lfo, "phase", 0.0)) - 2.0 * np.pi
                    * rate * (n - i) / self._sample_rate + phase)
                delay = base_samples + depth_samples * mod
                delay = max(1.0, min(float(max_delay), delay))
                voice_sum += self._read_delay(line, delay)
            wet[i] = voice_sum / voices
            # 写入：干信号 + 反馈（flanger）。
            write = mono[i] + feedback * wet[i]
            line = np.roll(line, -1)
            line[-1] = write
        self._delay_line = line.reshape(1, -1)
        return np.ascontiguousarray(
            samples * (1.0 - mix)
            + (wet * mix)[np.newaxis, :] * (samples.shape[0] > 1 or 1),
            dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Phaser
# ─────────────────────────────────────────────────────────────────────────────

class PhaserEffect(AudioEffect):
    """全通级联调制移相器（一阶全通级联，LFO 调制中心频率）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {"enabled": True, "rate_hz": 0.5,
                              "stages": 4, "base_hz": 600.0,
                              "sweep_octaves": 2.5, "mix": 0.5,
                              "feedback": 0.3}
        self._lfo = _Lfo(0.5)
        self._ap_states = np.zeros((4, 2), dtype=np.float64)

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
        self._lfo.rate_hz = float(merged.get("rate_hz", 0.5))
        stages = int(np.clip(merged.get("stages", 4), 1, 4))
        if self._ap_states.shape[0] != stages:
            self._ap_states = np.zeros((stages, 2), dtype=np.float64)

    def reset_state(self) -> None:
        self._lfo.reset()
        self._ap_states = np.zeros((4, 2), dtype=np.float64)

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        return int(0.1 * self._sample_rate)

    def latency_samples(self) -> int:
        return 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        params = self._params
        stages = int(np.clip(params.get("stages", 4), 1, 4))
        base = float(params.get("base_hz", 600.0))
        sweep = float(params.get("sweep_octaves", 2.5))
        mix = float(np.clip(params.get("mix", 0.5), 0.0, 1.0))
        feedback = float(np.clip(params.get("feedback", 0.3), 0.0, 0.95))
        lfo = self._lfo.advance(n, self._sample_rate)

        mono = samples.mean(axis=0) if samples.shape[0] > 1 else samples[0]
        out = np.empty(n, dtype=np.float32)
        states = self._ap_states
        delayed = 0.0
        for i in range(n):
            freq = base * (2.0 ** (sweep * float(lfo[i])))
            freq = min(freq, self._sample_rate * 0.45)
            tan = np.tan(np.pi * freq / self._sample_rate)
            a1 = (tan - 1.0) / (tan + 1.0)
            x = float(mono[i]) + feedback * delayed
            for s in range(stages):
                y = a1 * x + states[s, 0] - a1 * states[s, 1]
                states[s, 0] = x
                states[s, 1] = y
                x = y
            delayed = x
            out[i] = x
        self._ap_states = states
        return np.ascontiguousarray(
            samples * (1.0 - mix) + (mix * out)[np.newaxis, :],
            dtype=np.float32)
