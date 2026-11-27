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

import math
from typing import Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect, _clamp_finite
from src.features.edit.core.audio_effects.sidechain import detector_input, detector_source_id

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, lfilter, sosfilt
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

#: 新版百分比 → dB 映射（±100% → ±12 dB；FX-17 计划推荐曲线）。
PCT_TO_DB = 0.12
_TRANSIENT_DEFAULTS = {"enabled": True, "attack_db": 6.0, "sustain_db": 0.0,
                       "attack_ms": 8.0, "sustain_ms": 80.0}


def _transient_envelope(values: np.ndarray, coefficient: float, previous: np.ndarray) -> np.ndarray:
    if _SCIPY:
        envelope, _ = lfilter([1.0 - coefficient], [1.0, -coefficient], values, axis=-1,
                              zi=(coefficient * previous)[..., None])
        return envelope
    envelope = np.empty_like(values)
    for index in range(values.shape[-1]):
        previous = coefficient * previous + (1.0 - coefficient) * values[..., index]
        envelope[..., index] = previous
    return envelope


def _waveform_values(name: str, frac: np.ndarray, cycle: np.ndarray,
                     seed: int) -> np.ndarray:
    """FX-19 波形函数：frac ∈ [0,1) → [−1,1]；random 按整周期 S&H
    （default_rng([seed, cycle])——绝对周期索引，跨块确定性）。"""
    if name == "triangle":
        # 与正弦相位对齐：frac 0 → −1，0.5 → +1（分段线性）。
        return np.where(frac < 0.5, 4.0 * frac - 1.0, 3.0 - 4.0 * frac)
    if name == "square":
        return np.where(frac < 0.5, 1.0, -1.0)
    if name == "saw_up":
        return 2.0 * frac - 1.0
    if name == "saw_down":
        return 1.0 - 2.0 * frac
    if name == "random":
        values = np.empty_like(frac)
        # 每个跨过的整周期计算一次 S&H（周期数有限——廉价）。
        for c in np.unique(cycle):
            rng = np.random.default_rng([int(seed), int(c)])
            values[cycle == c] = rng.uniform(-1.0, 1.0)
        return values
    # 未知波形回退正弦。
    return np.sin(2.0 * np.pi * frac)


class TransientDesignerEffect(AudioEffect):
    """差分包络瞬态塑形（attack 增益独立于 sustain）。

    FX-17（2026-11-21 计划）：给了 ``attack_pct``/``sustain_pct`` 键 →
    新版支路（逐声道快/慢包络，聚合取各声道绝对包络最大——反相立体
    声不抵消；mix 干湿、output_gain_db 收尾）；否则旧 dB 音色冻结
    （声道平均检测语义保持）。
    """

    def __init__(self, sample_rate: float = 48000.0, *, params_are_snapshots: bool = False) -> None:
        super().__init__(sample_rate)
        self._params_are_snapshots = params_are_snapshots
        self._params: dict = dict(_TRANSIENT_DEFAULTS)
        self._fast_env = 0.0
        self._slow_env = 0.0
        # 新版支路：每声道快/慢包络状态。
        self._fast_env_ch: Optional[np.ndarray] = None
        self._slow_env_ch: Optional[np.ndarray] = None
        self._sidechain_status = "internal"
        self._sc_filter_key = None
        self._sc_sos = None
        self._sc_zi = None
        self._last_attack_db = 0.0
        self._last_sustain_db = 0.0

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self.reset_state()
        merged = dict(_TRANSIENT_DEFAULTS if self._params_are_snapshots else self._params)
        if isinstance(params, dict):
            merged.update(params)
        if detector_source_id(merged) != self.sc_source:
            self._sc_filter_key = None
        self._params = merged

    def reset_state(self) -> None:
        self._fast_env = 0.0
        self._slow_env = 0.0
        self._fast_env_ch = None
        self._slow_env_ch = None
        self._sc_zi = None
        self._sc_filter_key = None
        self._last_attack_db = self._last_sustain_db = 0.0

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        return 0

    def latency_samples(self) -> int:
        return 0

    @property
    def sc_source(self) -> str:
        return detector_source_id(self._params)

    def collect_telemetry(self) -> dict:
        return {"sidechain_source_id": self.sc_source, "sidechain_status": self._sidechain_status,
                "attack_gain_db": self._last_attack_db, "sustain_gain_db": self._last_sustain_db}

    def process(self, samples: np.ndarray,
                sidechain: Optional[np.ndarray] = None) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0 or samples.shape[0] == 0:
            return samples
        detector, self._sidechain_status = detector_input(samples, sidechain, self.sc_source)
        if (_SCIPY and self._params.get("sc_filter_enabled", True)
                and any(self._params.get(key, 0.0) for key in ("sc_hpf_hz", "sc_lpf_hz"))):
            hpf = float(np.clip(self._params.get("sc_hpf_hz", 0.), 0., self._sample_rate * .45))
            lpf = float(np.clip(self._params.get("sc_lpf_hz", 0.), 0., self._sample_rate * .45))
            order = int(np.clip(self._params.get("sc_filter_slope_db_oct", 12), 6, 24)) // 6
            key = (hpf, lpf, order, self._sample_rate, detector.shape[0])
            if key != self._sc_filter_key:
                sections = []
                if hpf > 0:
                    sections.extend(butter(order, hpf, btype="highpass", fs=self._sample_rate, output="sos"))
                if lpf > 0:
                    sections.extend(butter(order, lpf, btype="lowpass", fs=self._sample_rate, output="sos"))
                self._sc_sos = np.asarray(sections) if sections else None
                self._sc_zi = (np.zeros((len(sections), detector.shape[0], 2)) if sections else None)
                self._sc_filter_key = key
            if self._sc_sos is not None:
                detector, self._sc_zi = sosfilt(self._sc_sos, detector, axis=-1, zi=self._sc_zi)
        else:
            self._sc_filter_key = None
        if self._params.get("sc_listen", False):
            self._last_attack_db = self._last_sustain_db = 0.0
            monitor = detector if detector.shape[0] == samples.shape[0] else detector.mean(axis=0)
            return np.array(np.broadcast_to(monitor, samples.shape), dtype=np.float32, order="C")
        if self._params.get("attack_pct") is not None or self._params.get("sustain_pct") is not None:
            processed = self._process_percent(samples, detector)
        else:
            processed = self._process_legacy(samples, detector)
        mix = float(np.clip(self._params.get("mix", 1.0), 0.0, 1.0))
        out = (samples.astype(np.float64) * (1.0 - mix)
               + processed.astype(np.float64) * mix)
        out *= 10.0 ** (float(self._params.get("output_gain_db", 0.0)) / 20.0)
        return np.ascontiguousarray(out.astype(np.float32), dtype=np.float32)

    def _process_percent(self, samples: np.ndarray,
                         detector: Optional[np.ndarray] = None) -> np.ndarray:
        """新版支路：逐声道快/慢包络，聚合取各声道绝对包络最大。"""
        detector = samples if detector is None else detector
        channels = detector.shape[0]
        attack_ms = max(0.5, float(self._params.get("attack_ms", 8.0)))
        sustain_ms = max(10.0, float(self._params.get("sustain_ms", 80.0)))
        a_fast = float(np.exp(-1.0 / (attack_ms * self._sample_rate / 1000.0)))
        a_slow = float(np.exp(-1.0 / (sustain_ms * self._sample_rate / 1000.0)))
        attack_db = PCT_TO_DB * float(np.clip(
            float(self._params.get("attack_pct") or 0.0), -100.0, 100.0))
        sustain_db = PCT_TO_DB * float(np.clip(
            float(self._params.get("sustain_pct") or 0.0), -100.0, 100.0))

        if (self._fast_env_ch is None
                or self._fast_env_ch.shape != (channels,)):
            self._fast_env_ch = np.zeros(channels, dtype=np.float64)
            self._slow_env_ch = np.zeros(channels, dtype=np.float64)
        abs_in = np.abs(detector.astype(np.float64))
        fast = _transient_envelope(abs_in, a_fast, self._fast_env_ch)
        slow = _transient_envelope(abs_in, a_slow, self._slow_env_ch)
        self._fast_env_ch = fast[:, -1].copy()
        self._slow_env_ch = slow[:, -1].copy()
        return self._apply_envelopes(samples, fast.max(axis=0), slow.max(axis=0), attack_db, sustain_db)

    def _process_legacy(self, samples: np.ndarray, detector: np.ndarray) -> np.ndarray:
        attack_ms = max(0.5, float(self._params.get("attack_ms", 8.0)))
        sustain_ms = max(10.0, float(self._params.get("sustain_ms", 80.0)))
        a_fast = float(np.exp(-1.0 / (attack_ms * self._sample_rate / 1000.0)))
        a_slow = float(np.exp(-1.0 / (sustain_ms * self._sample_rate / 1000.0)))
        attack_db = float(self._params.get("attack_db", 6.0))
        sustain_db = float(self._params.get("sustain_db", 0.0))
        mono = np.abs(detector.mean(axis=0)).astype(np.float64)
        fast = _transient_envelope(mono, a_fast, np.asarray(self._fast_env))
        slow = _transient_envelope(mono, a_slow, np.asarray(self._slow_env))
        self._fast_env = float(fast[-1])
        self._slow_env = float(slow[-1])
        return self._apply_envelopes(samples, fast, slow, attack_db, sustain_db)

    def _apply_envelopes(
        self, samples: np.ndarray, fast: np.ndarray, slow: np.ndarray, attack_db: float, sustain_db: float,
    ) -> np.ndarray:
        ratio = np.maximum(fast, 1e-9) / np.maximum(slow, 1e-9)
        weight = np.clip((20.0 * np.log10(ratio) - 3.0) / 6.0, 0.0, 1.0)
        attack_gain = 10.0 ** (attack_db / 20.0)
        sustain_gain = 10.0 ** (sustain_db / 20.0)
        gains = (sustain_gain * (1.0 - weight) + attack_gain * weight).astype(np.float32)
        self._last_attack_db = attack_db * float(weight[-1])
        self._last_sustain_db = sustain_db * (1.0 - float(weight[-1]))
        return np.ascontiguousarray(samples * gains, dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# 颤音 / 自动声像
# ─────────────────────────────────────────────────────────────────────────────

_TREMOLO_DEFAULTS = {"enabled": True, "mode": "tremolo", "rate_hz": 4.0, "depth": 0.7, "shape": "sine"}
_TREMOLO_BEATS = {"1/1": 4.0, "1/2": 2.0, "1/4": 1.0, "1/8": 0.5,
                  "1/16": 0.25, "1/32": 0.125}


class TremoloAutoPanEffect(AudioEffect):
    """LFO 颤音（幅度调制）与自动声像（L/R 反相）。"""

    def __init__(self, sample_rate: float = 48000.0, *, params_are_snapshots: bool = False) -> None:
        super().__init__(sample_rate)
        self._params_are_snapshots = params_are_snapshots
        self._params: dict = dict(_TREMOLO_DEFAULTS)
        # FX-19 新波形支路状态：相位基准 + 绝对样本位置。
        self._phase_base: Optional[float] = None
        self._phase_pos_base = 0
        self._phase_pos_end = 0
        self._phase_rate: Optional[float] = None
        self._lfo = _Lfo(4.0)
        self._smooth_state: Optional[np.ndarray] = None
        self._smooth_state_right: Optional[np.ndarray] = None
        self._last_phase = 0.0
        self._sample_phase = 0.0
        self._last_gains = (1.0, 1.0)
        self._random_holds: dict[int, float] = {}
        self._seed = 0
        self._right_shift = 0.0
        self.set_params({})

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self.reset_state()
        merged = dict(_TREMOLO_DEFAULTS if self._params_are_snapshots else self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        self._enabled = bool(merged["enabled"])
        self._mode = str(merged["mode"])
        self._pans = self._mode in ("auto_pan", "dual")
        self._depth = _clamp_finite(merged["depth"], 0.0, 1.0, 0.7)
        self._waveform = str(merged.get("waveform") or ("sine" if merged.get("phase_offset_deg") is not None else ""))
        try:
            rate = float(merged["rate_hz"])
        except (TypeError, ValueError, OverflowError):
            rate = 4.0
        if not math.isfinite(rate):
            rate = 4.0
        self._lfo.rate_hz = rate
        self._rate = min(50.0, max(0.01, rate))
        self._sync = bool(merged.get("sync", False))
        if self._sync:
            division = str(merged.get("sync_div", "1/4"))
            suffix = division[-1:] if division[-1:] in ("D", "T") else ""
            beats = _TREMOLO_BEATS.get(division[:-1] if suffix else division, 1.0)
            beats *= 1.5 if suffix == "D" else (2 / 3 if suffix == "T" else 1.0)
            self._rate = _clamp_finite(merged.get("bpm", 120.0), 20.0, 300.0, 120.0) / 60.0 / beats
        self._duty = _clamp_finite(merged.get("duty_cycle_pct", 50.), 1., 99., 50.) / 100
        self._pan_center = _clamp_finite(merged.get("pan_center_pct", 0.), -100., 100., 0.) / 100
        self._pan_depth = self._depth * _clamp_finite(merged.get("pan_width_pct", 100.), 0., 100., 100.) / 100
        stereo_default = 180.0 if self._pans else 0.0
        phase = _clamp_finite(merged.get("phase_offset_deg", stereo_default), 0.0, 360.0, stereo_default)
        shift = ((phase - stereo_default) / 360) % 1.0
        if shift != self._right_shift:
            self._smooth_state_right = None
        self._right_shift = shift
        smooth = _clamp_finite(merged.get("smooth_ms", 0.), 0., 100., 0.)
        self._smoothing = float(np.exp(-1 / (smooth * self._sample_rate / 1000))) if smooth > 0 else 0.0
        try:
            seed = max(0, int(merged.get("random_seed", 0)))
        except (TypeError, ValueError, OverflowError):
            seed = 0
        if seed != self._seed:
            self._random_holds.clear()
        self._seed = seed
        if not self._waveform:
            self._phase_base = None
            self._smooth_state = self._smooth_state_right = None

    def reset_state(self) -> None:
        self._lfo.reset()
        self._phase_base = None
        self._phase_pos_base = 0
        self._phase_pos_end = 0
        self._phase_rate = None
        self._smooth_state = None
        self._smooth_state_right = None
        self._last_phase = 0.0
        self._sample_phase = 0.0
        self._last_gains = (1.0, 1.0)

    @property
    def is_active(self) -> bool:
        return self._enabled

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
        if self._waveform:
            return self._process_waveform(samples)
        lfo = self._lfo.advance(n, self._sample_rate)
        self._last_phase = self._lfo.phase / (2.0 * np.pi)
        self._sample_phase = (self._last_phase - self._lfo.rate_hz / self._sample_rate) % 1.0
        if self._pans and samples.shape[0] >= 2:
            pan = self._depth * lfo
            left_gain = np.sqrt(0.5 * (1.0 - pan))
            right_gain = np.sqrt(0.5 * (1.0 + pan))
            out = samples.copy()
            out[0] *= left_gain
            out[1] *= right_gain
            self._last_gains = (float(left_gain[-1]), float(right_gain[-1]))
            return np.ascontiguousarray(out, dtype=np.float32)
        amplitude = 1.0 - self._depth * (0.5 - 0.5 * lfo)
        self._last_gains = (float(amplitude[-1]), float(amplitude[-1]))
        return np.ascontiguousarray(
            samples * amplitude[np.newaxis, :], dtype=np.float32)

    def collect_telemetry(self) -> dict:
        return {"phase_cycles": self._last_phase, "sample_phase_cycles": self._sample_phase,
                "mod_gain_lr": self._last_gains, "mode": self._mode,
                "effective_rate_hz": self._rate if self._waveform else self._lfo.rate_hz,
                "bpm_source": "manual" if self._sync and self._waveform else "free"}

    # ── FX-19：波形扩展（绝对样本相位积分——跨块位精确） ────────
    def _values(self, positions: np.ndarray) -> np.ndarray:
        if self._waveform == "random":
            current = math.floor(positions[0])
            if current == math.floor(positions[-1]):
                if current not in self._random_holds:
                    if len(self._random_holds) >= 4:
                        self._random_holds.clear()
                    self._random_holds[current] = np.random.default_rng([self._seed, current]).uniform(-1.0, 1.0)
                return np.full_like(positions, self._random_holds[current])
            return _waveform_values("random", positions, np.floor(positions).astype(np.int64), self._seed)
        fractions = np.mod(positions, 1.0)
        if self._waveform == "square":
            return np.where(fractions < self._duty, 1.0, -1.0)
        return _waveform_values(self._waveform, fractions, positions, self._seed)

    def _smooth(self, values: np.ndarray, state: np.ndarray | None) -> tuple[np.ndarray, np.ndarray | None]:
        coefficient = self._smoothing
        if not coefficient:
            return values, None
        initial = state if state is not None else np.array([values[0] * coefficient])
        if _SCIPY:
            return lfilter([1 - coefficient], [1, -coefficient], values, zi=initial)
        previous = float(initial[0])
        for index, value in enumerate(values):
            values[index] = (1 - coefficient) * value + previous
            previous = coefficient * values[index]
        return values, np.array([previous])

    def _process_waveform(self, samples: np.ndarray) -> np.ndarray:
        """Integrate phase from absolute sample positions for stable block boundaries."""
        n = samples.shape[-1]
        rate = self._rate
        # 相位基准（rate 变化点重置——相位连续）。
        if self._phase_base is None or self._phase_rate != rate:
            if self._phase_base is not None:
                self._phase_base = (self._phase_base
                                    + (self._phase_pos_end
                                       - self._phase_pos_base) * self._phase_rate
                                    / self._sample_rate)
            else:
                self._phase_base = self._lfo.phase / (2 * np.pi)
            self._phase_pos_base = self._phase_pos_end
            self._phase_rate = rate
        start = self._phase_pos_end - self._phase_pos_base
        positions = np.arange(start, start + n, dtype=np.float64)
        positions *= rate
        positions /= self._sample_rate
        positions += self._phase_base
        values, self._smooth_state = self._smooth(self._values(positions), self._smooth_state)
        stereo = samples.shape[0] >= 2
        right_values = values
        if stereo and self._right_shift:
            right_values, self._smooth_state_right = self._smooth(
                self._values(positions + self._right_shift), self._smooth_state_right,
            )
        if self._pans and stereo:
            pan = np.clip(self._pan_center + self._pan_depth * values, -1., 1.)
            pan_right = (np.clip(self._pan_center + self._pan_depth * right_values, -1., 1.)
                         if self._right_shift else pan)
            left_gain = np.sqrt(0.5 * (1.0 - pan))
            right_gain = np.sqrt(0.5 * (1.0 + pan_right))
            out = samples.copy()
            out[0] *= left_gain
            out[1] *= right_gain
            meter_left, meter_right = float(left_gain[-1]), float(right_gain[-1])
            if self._mode == "dual":
                amplitude = 1.0 - self._depth * (0.5 - 0.5 * values)
                out *= amplitude[np.newaxis, :]
                meter_left *= float(amplitude[-1])
                meter_right *= float(amplitude[-1])
        else:
            amplitude = 1.0 - self._depth * (0.5 - 0.5 * values)
            out = samples * amplitude[np.newaxis, :]
            meter_left = meter_right = float(amplitude[-1])
            if stereo and self._right_shift:
                amplitude_right = 1.0 - self._depth * (0.5 - 0.5 * right_values)
                out[1] = samples[1] * amplitude_right
                meter_right = float(amplitude_right[-1])
        self._last_gains = (meter_left, meter_right)
        self._phase_pos_end += n
        self._last_phase = float((self._phase_base + (self._phase_pos_end - self._phase_pos_base) * rate / self._sample_rate) % 1)
        self._sample_phase = float(positions[-1] % 1.0)
        self._lfo.phase = self._last_phase * 2 * np.pi
        return np.ascontiguousarray(out, dtype=np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Chorus / Flanger
# ─────────────────────────────────────────────────────────────────────────────

class ChorusFlangerEffect(AudioEffect):
    """A causal fractional delay with phase-spaced voices and signed feedback.

    Voice read heads use a channel-local ring. Feedback-free processing can
    read the whole input block, while feedback slices stop before any tap
    depends on a write within the same slice. Both paths preserve the stream's
    state and its sample alignment across callback sizes.
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
        self._depth_ms = 3.0
        self._base_ms = 18.0
        self._feedback = 0.0
        self._voices = 2
        self._mix = 0.5
        self._channels = 1
        self._rebuild_line()

    def _rebuild_line(self) -> None:
        max_ms = self._BASE_MS_MAX + self._DEPTH_MS_MAX
        capacity = int(max_ms * self._sample_rate / 1000.0) + 8
        self._line = np.zeros((self._channels, capacity), dtype=np.float32)
        self._write_pos = 0

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._rebuild_line()
            self._lfo.reset()
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        self._lfo.rate_hz = _clamp_finite(merged["rate_hz"], 0.01, 20.0, 0.6)
        self._depth_ms = _clamp_finite(merged["depth_ms"], 0.0, self._DEPTH_MS_MAX, 3.0)
        self._base_ms = _clamp_finite(merged["base_ms"], 0.5, self._BASE_MS_MAX, 18.0)
        self._feedback = _clamp_finite(merged["feedback"], -0.95, 0.95, 0.0)
        self._voices = int(_clamp_finite(merged["voices"], 1.0, 4.0, 2.0))
        self._mix = _clamp_finite(merged["mix"], 0.0, 1.0, 0.5)

    def reset_state(self) -> None:
        self._lfo.reset()
        self._line.fill(0.0)
        self._write_pos = 0

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        # Allow the first tap and enough feedback cycles to reach −60 dB.
        fb = abs(self._feedback)
        cycles = (1 if fb <= 0.0 else
                  1 + int(np.ceil(-60.0 / (20.0 * np.log10(max(fb, 1e-6))))))
        max_ms = self._base_ms + self._depth_ms
        return int(np.ceil(cycles * max_ms * self._sample_rate / 1000.0)) + 1

    def latency_samples(self) -> int:
        # The modulated delay is audible processing, with zero host latency.
        return 0

    def collect_telemetry(self) -> dict:
        return {"phase": self._lfo.phase, "sample_rate": self._sample_rate}

    def _write_line(self, values: np.ndarray) -> None:
        """Copy the newest input samples into the circular history."""
        line = self._line
        capacity = line.shape[-1]
        end = (self._write_pos + values.shape[-1]) % capacity
        count = min(values.shape[-1], capacity)
        start = (end - count + 1) % capacity
        first = min(count, capacity - start)
        source = values.shape[-1] - count
        line[:, start:start + first] = values[:, source:source + first]
        if first < count:
            line[:, :count - first] = values[:, source + first:]
        self._write_pos = end

    def _process_short_feedback(
        self, samples: np.ndarray, write: np.ndarray, left: np.ndarray,
        right: np.ndarray, left_weight: np.ndarray, right_weight: np.ndarray,
        write_dtype: np.dtype,
    ) -> np.ndarray:
        """Process very short feedback dependencies without per-sample arrays."""
        taps = tuple(zip(write.tolist(), left.T.tolist(), right.T.tolist(),
                         left_weight.T.tolist(), right_weight.T.tolist()))
        wet = np.empty(samples.shape, dtype=np.float32)
        feedback = self._feedback
        voices = self._voices
        cast_feedback = write_dtype.type
        for channel in range(samples.shape[0]):
            line = memoryview(self._line[channel])
            output = memoryview(wet[channel])
            dry = samples[channel].tolist()
            for frame, (slot, low, high, lo_weight, hi_weight) in enumerate(taps):
                response = 0.0
                for lo, hi, w0, w1 in zip(low, high, lo_weight, hi_weight):
                    response += line[lo] * w0 + line[hi] * w1
                response /= voices
                output[frame] = response
                line[slot] = dry[frame] + float(cast_feedback(feedback * response))
        return wet

    def _process_feedback_segments(
        self, samples: np.ndarray, write: np.ndarray, left: np.ndarray,
        right: np.ndarray, left_weight: np.ndarray, right_weight: np.ndarray,
        lag: np.ndarray, write_dtype: np.dtype,
    ) -> np.ndarray:
        n = samples.shape[-1]
        required_start = np.maximum.accumulate(np.arange(n) + 1 - lag)
        wet = np.empty(samples.shape, dtype=np.float32)
        line = self._line
        start = 0
        while start < n:
            stop = int(np.searchsorted(required_start, start, side="right"))
            segment = slice(start, stop)
            response = ((line[:, left[:, segment]] * left_weight[:, segment]
                         + line[:, right[:, segment]] * right_weight[:, segment])
                        .sum(axis=1) / self._voices)
            wet[:, segment] = response
            fed = (response * self._feedback).astype(write_dtype, copy=False)
            line[:, write[segment]] = samples[:, segment] + fed
            start = stop
        return wet

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        channels, n = samples.shape
        if not n or not channels:
            return samples
        if channels != self._channels:
            self._channels = channels
            self._rebuild_line()
        line = self._line
        capacity = line.shape[-1]
        voices = self._voices
        feedback = self._feedback
        mix = self._mix
        phase_step = 2.0 * np.pi * self._lfo.rate_hz / self._sample_rate
        phase = self._lfo.phase
        self._lfo.phase = float((phase + phase_step * n) % (2.0 * np.pi))
        if mix == 0.0 and feedback == 0.0:
            self._write_line(samples)
            return np.ascontiguousarray(samples, dtype=np.float32)

        frame = np.arange(n)
        phases = phase + phase_step * frame
        base_samples = self._base_ms * self._sample_rate / 1000.0
        depth_samples = self._depth_ms * self._sample_rate / 1000.0
        offsets = (2.0 * np.pi * np.arange(voices) / voices)[:, None]
        delays = np.clip(base_samples + depth_samples * np.sin(phases + offsets),
                         1.0, capacity - 2)
        first_write = (self._write_pos + 1) % capacity
        write = (first_write + frame) % capacity
        positions = write - delays
        floors = np.floor(positions).astype(np.intp)
        right_weight = positions - floors
        left_weight = 1.0 - right_weight
        if feedback == 0.0:
            # Reads inside this block use its input. Earlier reads use saved
            # history, even when an export block is larger than the ring.
            relative = floors - write + frame
            history = max(0, -int(relative.min()))
            previous = line[:, (first_write + np.arange(-history, 0)) % capacity]
            source = np.concatenate((previous, samples.astype(np.float32, copy=False)), axis=-1)
            idx = relative + history
            wet = ((source[:, idx] * left_weight + source[:, idx + 1] * right_weight)
                   .sum(axis=1) / voices).astype(np.float32)
            self._write_line(samples)
        else:
            left = floors % capacity
            right = (left + 1) % capacity
            # Both interpolation samples must precede the first write of the
            # slice, except the zero-weight endpoint of an integer delay.
            lag = np.floor(delays.min(axis=0)).astype(np.intp)
            # Match the reference scalar-write rounding with either NumPy
            # 1.x or 2.x scalar promotion.
            write_dtype = np.asarray(samples.dtype.type(0) + 0.0).dtype
            if np.count_nonzero(lag <= 4) > n // 2:
                wet = self._process_short_feedback(
                    samples, write, left, right, left_weight, right_weight, write_dtype)
            else:
                wet = self._process_feedback_segments(
                    samples, write, left, right, left_weight, right_weight, lag, write_dtype)
            self._write_pos = int(write[-1])
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
    _DEFAULTS = {"enabled": True, "rate_hz": 0.5, "stages": 4,
                 "base_hz": 600.0, "sweep_octaves": 2.5, "mix": 0.5,
                 "feedback": 0.3, "stereo_processing": False}

    def __init__(self, sample_rate: float = 48000.0, *,
                 params_are_snapshots: bool = False) -> None:
        super().__init__(sample_rate)
        self._params = dict(self._DEFAULTS)
        self._params_are_snapshots = params_are_snapshots
        self._lfo = _Lfo(0.5)
        self._stage_count = 4
        self._stereo = False
        self._inputs: list[list[float]] = []
        self._outputs: list[list[float]] = []
        self._feedback_history: list[float] = []

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self.reset_state()
        merged = dict(self._DEFAULTS if self._params_are_snapshots else self._params)
        if isinstance(params, dict):
            merged.update(params)
        bounds = {"rate_hz": (0.0, 20.0), "base_hz": (10.0, 20000.0),
                  "sweep_octaves": (0.0, 5.0), "mix": (0.0, 1.0),
                  "feedback": (-0.95, 0.95), "stages": (1, self._STAGES_MAX)}
        for name, (minimum, maximum) in bounds.items():
            merged[name] = _clamp_finite(merged[name], minimum, maximum, self._DEFAULTS[name])
        stages = int(merged["stages"])
        stereo = merged["stereo_processing"] is True
        if stages != self._stage_count or stereo != self._stereo:
            self._stage_count = stages
            self._stereo = stereo
            self._clear_history()
        self._params = merged
        self._lfo.rate_hz = merged["rate_hz"]

    def _clear_history(self) -> None:
        self._inputs.clear()
        self._outputs.clear()
        self._feedback_history.clear()

    def reset_state(self) -> None:
        self._lfo.reset()
        self._clear_history()

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True))

    def tail_estimate_samples(self) -> int:
        p = self._params
        if not self.is_active or p["mix"] == 0.0:
            return 0
        rate = self._sample_rate
        lowest = max(10.0, p["base_hz"] * 2.0 ** -p["sweep_octaves"])
        highest = min(rate * 0.45, p["base_hz"] * 2.0 ** p["sweep_octaves"])
        poles = [(tangent - 1.0) / (tangent + 1.0) for tangent in (
            math.tan(math.pi * min(lowest, rate * 0.45) / rate),
            math.tan(math.pi * highest / rate),
        )]
        pole = max(abs(p) for p in poles)
        decay = math.log(0.001) / math.log(max(1e-9, pole))
        group_delay = self._stage_count * (1.0 + pole) / (1.0 - pole)
        feedback = abs(p["feedback"])
        repeats = math.ceil(math.log(0.001) / math.log(feedback)) if feedback else 0
        return min(int(rate), math.ceil(decay + group_delay + repeats * (group_delay + 1.0)))

    def collect_telemetry(self) -> dict[str, float]:
        return {"phase": self._lfo.phase, "sample_rate": self._sample_rate}

    def latency_samples(self) -> int:
        return 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2 or not samples.size:
            return samples
        channels, n = samples.shape
        params = self._params
        mix = params["mix"]
        feedback = params["feedback"]
        lfo = self._lfo.advance(n, self._sample_rate)
        freq = np.clip(params["base_hz"] * np.exp2(params["sweep_octaves"] * lfo.astype(np.float64)),
                       10.0, self._sample_rate * 0.45)
        tangent = np.tan(np.pi * freq / self._sample_rate)
        coefficients = ((tangent - 1.0) / (tangent + 1.0)).tolist()
        wet_input = samples
        if not self._stereo and channels > 1:
            wet_input = samples.mean(axis=0, keepdims=True)
        count = wet_input.shape[0]
        if len(self._inputs) != count:
            self._inputs = [[0.0] * self._stage_count for _ in range(count)]
            self._outputs = [[0.0] * self._stage_count for _ in range(count)]
            self._feedback_history = [0.0] * count
        out = np.empty((count, n), dtype=np.float32)
        stages = range(self._stage_count)
        # The time-varying cascade is recursive. Keep its inner loop on
        # Python floats to avoid scalar NumPy indexing for every stage/sample.
        for channel, source in enumerate(wet_input):
            x_prev, y_prev = self._inputs[channel], self._outputs[channel]
            previous = self._feedback_history[channel]
            wet = [0.0] * n
            for i, (value, a1) in enumerate(zip(source.tolist(), coefficients)):
                x = value + feedback * previous
                for stage in stages:
                    y = a1 * x + x_prev[stage] - a1 * y_prev[stage]
                    x_prev[stage], y_prev[stage] = x, y
                    x = y
                wet[i] = previous = x
            out[channel] = wet
            self._feedback_history[channel] = previous
        if mix == 0.0:
            return samples
        return np.ascontiguousarray(
            samples * (1.0 - mix) + mix * out, dtype=np.float32)
