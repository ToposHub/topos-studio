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
    """调制延迟线：chorus（多声部长延迟无反馈）/ flanger（短延迟+反馈）。

    2026-11-21 计划 FX-18 修正：读头 = 写游标 − 期望延时（旧实现从
    线首计数而写入在线尾，5 ms 首跳实测 2160 样本）；预分配环形缓冲，
    移除逐样本整线 ``np.roll``；反馈样本持久、符号保留；声部相位间隔
    跨块保持。延时钳制区间：``[1 样本, 容量−2]``，参数超界按界内取值。
    """

    _BASE_MS_MAX = 100.0
    _DEPTH_MS_MAX = 50.0

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {
            "enabled": True, "mode": "chorus", "rate_hz": 0.6,
            "depth_ms": 3.0, "base_ms": 18.0, "feedback": 0.0,
            "voices": 2, "mix": 0.5,
        }
        self._lfo = _Lfo(0.6)
        self._channels = 1
        self._line: Optional[np.ndarray] = None  # (ch, capacity) 环形
        self._write_pos = 0
        self._rebuild_line()

    def _capacity(self) -> int:
        max_ms = self._BASE_MS_MAX + self._DEPTH_MS_MAX
        return int(max_ms * self._sample_rate / 1000.0) + 8

    def _rebuild_line(self) -> None:
        cap = self._capacity()
        self._line = np.zeros((self._channels, cap), dtype=np.float32)
        self._write_pos = 0

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
        self._line = np.zeros_like(self._line)
        self._write_pos = 0

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        # 反馈尾音预算：|feedback| 每圈衰减到 −60 dB 的时间（上限 1 s）。
        # （修正旧实现的 −6 目标与 −60 dB 注释不一致。）
        fb = abs(float(np.clip(self._params.get("feedback", 0.0),
                               -0.95, 0.95)))
        if fb <= 0.0:
            return 0
        cycles = max(1.0, -60.0 / (20.0 * np.log10(max(fb, 1e-6))))
        base_ms = float(self._params.get("base_ms", 18.0))
        tail = int(cycles * base_ms * self._sample_rate / 1000.0)
        return min(int(self._sample_rate), tail)

    def latency_samples(self) -> int:
        # 调制延迟是效果本体（干湿内部对齐），非算法实现延迟。
        return 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        params = self._params
        rate = float(np.clip(params.get("rate_hz", 0.6), 0.01, 20.0))
        depth = float(np.clip(params.get("depth_ms", 3.0),
                              0.0, self._DEPTH_MS_MAX))
        base = float(np.clip(params.get("base_ms", 18.0),
                             0.5, self._BASE_MS_MAX))
        feedback = float(np.clip(params.get("feedback", 0.0),
                                 -0.95, 0.95))
        voices = int(np.clip(params.get("voices", 2), 1, 4))
        mix = float(np.clip(params.get("mix", 0.5), 0.0, 1.0))

        channels = samples.shape[0]
        if channels != self._channels:
            self._channels = channels
            self._rebuild_line()
        line = self._line
        cap = line.shape[-1]
        max_delay = cap - 2

        # 每样本 LFO 相位（ voices 均分相位间隔，跨块连续）。
        steps = 2.0 * np.pi * rate / self._sample_rate
        phases = self._lfo.phase + steps * np.arange(n, dtype=np.float64)
        self._lfo.phase = float((self._lfo.phase + steps * n)
                                % (2.0 * np.pi))
        base_samples = base * self._sample_rate / 1000.0
        depth_samples = depth * self._sample_rate / 1000.0

        wet = np.zeros((channels, n), dtype=np.float32)
        write_pos = self._write_pos
        for i in range(n):
            write_pos = (write_pos + 1) % cap
            voice_sum = 0.0
            delays = []
            for voice in range(voices):
                offset = 2.0 * np.pi * voice / max(1, voices)
                mod = np.sin(phases[i] + offset)
                delays.append(float(np.clip(
                    base_samples + depth_samples * mod, 1.0, max_delay)))
            for ch in range(channels):
                acc = 0.0
                for delay in delays:
                    # 读头 = 写游标 − 延时（分数采样线性内插）。
                    pos = write_pos - delay
                    idx = int(np.floor(pos))
                    frac = pos - idx
                    i0 = idx % cap
                    i1 = (idx + 1) % cap
                    acc += (float(line[ch, i0]) * (1.0 - frac)
                            + float(line[ch, i1]) * frac)
                w = acc / voices
                wet[ch, i] = w
                # 写入 = 干 + 反馈（符号保留；读段不含当前槽，delay≥1）。
                line[ch, write_pos] = samples[ch, i] + feedback * w
        self._write_pos = write_pos
        return np.ascontiguousarray(
            samples * (1.0 - mix) + wet * mix, dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Phaser
# ─────────────────────────────────────────────────────────────────────────────

class PhaserEffect(AudioEffect):
    """全通级联调制移相器（一阶全通级联，LFO 调制中心频率）。

    2026-11-21 计划 FX-18 修正：反馈样本跨块持久（旧实现为块内临时
    变量，501 样本切块与整块最大差 0.154）；级数放开到面板可存的
    1–12 级（旧实例 ≤4 级行为不变）；频率钳制 [10 Hz, 0.45·fs]。
    """

    _STAGES_MAX = 12

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {"enabled": True, "rate_hz": 0.5,
                              "stages": 4, "base_hz": 600.0,
                              "sweep_octaves": 2.5, "mix": 0.5,
                              "feedback": 0.3}
        self._lfo = _Lfo(0.5)
        self._ap_states = np.zeros((4, 2), dtype=np.float64)
        self._fb_state = 0.0  # 反馈样本跨块持久（旧实现为块内临时）

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
        stages = int(np.clip(merged.get("stages", 4), 1, self._STAGES_MAX))
        if self._ap_states.shape[0] != stages:
            # 拓扑变化：级数状态重建（后续批次再做交叉淡化）。
            self._ap_states = np.zeros((stages, 2), dtype=np.float64)
            self._fb_state = 0.0

    def reset_state(self) -> None:
        self._lfo.reset()
        self._ap_states = np.zeros_like(self._ap_states)
        self._fb_state = 0.0

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        fb = abs(float(self._params.get("feedback", 0.3)))
        if fb <= 1e-6:
            return 0
        cycles = max(1.0, -6.0 / (20.0 * np.log10(max(fb, 1e-6))))
        tail = int(cycles * 0.01 * self._sample_rate)
        return min(int(self._sample_rate), max(1, tail))

    def latency_samples(self) -> int:
        return 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        params = self._params
        stages = int(np.clip(params.get("stages", 4), 1, self._STAGES_MAX))
        base = float(np.clip(params.get("base_hz", 600.0), 10.0, 20000.0))
        sweep = float(params.get("sweep_octaves", 2.5))
        mix = float(np.clip(params.get("mix", 0.5), 0.0, 1.0))
        feedback = float(np.clip(params.get("feedback", 0.3),
                                 -0.95, 0.95))
        lfo = self._lfo.advance(n, self._sample_rate)

        channels = samples.shape[0]
        mono = samples.mean(axis=0) if channels > 1 else samples[0]
        out = np.empty(n, dtype=np.float32)
        states = self._ap_states
        delayed = self._fb_state
        freq_ceiling = self._sample_rate * 0.45
        for i in range(n):
            freq = float(np.clip(
                base * (2.0 ** (sweep * float(lfo[i]))),
                10.0, freq_ceiling))
            tan = np.tan(np.pi * freq / self._sample_rate)
            a1 = (tan - 1.0) / (tan + 1.0)
            x = float(mono[i]) + feedback * delayed
            for s in range(states.shape[0]):
                y = a1 * x + states[s, 0] - a1 * states[s, 1]
                states[s, 0] = x
                states[s, 1] = y
                x = y
            delayed = x
            out[i] = x
        self._ap_states = states
        self._fb_state = delayed
        return np.ascontiguousarray(
            samples * (1.0 - mix) + (mix * out)[np.newaxis, :],
            dtype=np.float32)
