"""Spectral Mangler DSP（SM-01..04，2026-11-15 计划 §8）。

Ring Modulator（四模式连续相位载波）＋ Granular（有界历史 + 64 声部
颗粒池 + 种子确定性）＋ Distortion（四曲线 + 2× 过采样 + Tone/Q）＋
四路拓扑（串行三种顺序 / 并联 N 归一残差）＋ 全局 Mix/Gain/可选限幅。

设计契约（与计划逐条对应）：
- 载波相位连续累加，块/窗口/preset 回填不重置；LFO 频率调制映射锁定
  （深度 100% = ±60 cents）。
- 颗粒随机由 (seed, 触发序) 驱动：密度按绝对采样相位累积（块长无关），
  每 grain 触发时按固定顺序消耗随机数——同 seed 同绝对区间输出一致；
  读位恒 < 写者位置（不读未来）。
- 历史环容量 = 5.0s（≥ r_max 8 × 500ms 粒 + Reverse 写者前进 +
  position/spray 回看 + 块保护）。
- 并联：``y = x + Σ mᵢ(Fᵢ(x) − x)/N``（N=启用模块数，含 mix=0）；
  全恒等/全 mix0 → 严格 unity（计划 §10.2 oracle）。
- 限幅为实例内峰值限幅（阈值 0dBFS、release 50ms 固定，写入算法
  契约），不替代 Master 安全限幅。
- 零算法时延（无前瞻）；模块 Mix 的 dry/wet 自然对齐。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.signal import firwin, lfilter

from src.features.edit.core.audio_effects.base import (
    OnePoleDCBlocker,
    mix_aligned,
    seeded_rng,
)

_HISTORY_SECONDS = 5.0
_GRAIN_VOICES = 64
_FIR_TAPS = 63


def _lfo_cents(depth_pct: float) -> float:
    """LFO 深度 → 载波频率偏差（cents）。100% = ±60 cents（锁定映射）。"""
    return 60.0 * max(0.0, min(100.0, depth_pct)) / 100.0


class _RingModule:
    """SM-01：四模式环形调制（每声道独立相位，spread 为 R 路相位偏移）。"""

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._phase = 0.0
        self._lfo_phase = 0.0

    def reset(self) -> None:
        self._phase = 0.0
        self._lfo_phase = 0.0

    def process(self, x: np.ndarray, p) -> np.ndarray:
        n = x.shape[-1]
        if n <= 0:
            return x.astype(np.float64, copy=True)
        t = np.arange(n, dtype=np.float64)
        rate = max(0.05, p.ring_lfo_rate_hz)
        lfo = np.sin(2.0 * np.pi * (self._lfo_phase + rate * t / self._sr))
        self._lfo_phase = (self._lfo_phase + rate * n / self._sr) % 1.0
        cents = _lfo_cents(p.ring_lfo_depth_pct) * lfo
        fc = max(0.01, p.ring_carrier_hz) * np.power(2.0, cents / 1200.0)
        dphi = 2.0 * np.pi * np.cumsum(fc) / self._sr
        phi = self._phase + dphi
        self._phase = float((self._phase + dphi[-1]) % (2.0 * np.pi))
        spread_rad = (max(0.0, min(100.0, p.ring_spread_pct)) / 100.0) * np.pi
        warp_idx = (max(0.0, min(100.0, p.ring_warp_pct)) / 100.0) * 3.2
        mode = p.ring_mode

        def _carrier(phase: np.ndarray) -> np.ndarray:
            if mode == "sine":
                return np.sin(phase)
            if mode == "dual":
                return 0.5 * (np.sin(phase) + np.sin(1.035 * phase + 0.8))
            if mode == "fm_ring":
                return np.sin(phase + warp_idx * np.sin(1.5 * phase))
            return 2.0 * np.abs(np.sin(phase)) - 1.0

        out = np.empty_like(x, dtype=np.float64)
        out[0] = x[0] * _carrier(phi)
        if x.shape[0] > 1:
            out[1] = x[1] * _carrier(phi + spread_rad)
        return out


class _GrainVoice:
    __slots__ = ("active", "read_pos", "ratio", "length", "phase", "gain",
                 "channel", "start_out", "max_pos")


class _GrainModule:
    """SM-02：真实颗粒（有界历史 + Hann 窗 + 种子确定性 + Freeze）。"""

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._cap = max(1024, int(_HISTORY_SECONDS * self._sr))
        self._hist = np.zeros((2, self._cap), dtype=np.float64)
        self._written = 0
        self._voices: list = []
        for _ in range(_GRAIN_VOICES):
            v = _GrainVoice()
            v.active = False
            v.start_out = 0
            self._voices.append(v)
        self._density_phase = 0.0
        self._rng = seeded_rng(0, stream_id=0x5A1)
        self._window = np.hanning(2048)
        self._prev_out: Optional[np.ndarray] = None

    def set_seed(self, seed: int) -> None:
        self._rng = seeded_rng(seed, stream_id=0x5A1)

    def reset(self) -> None:
        self._hist.fill(0.0)
        self._written = 0
        for v in self._voices:
            v.active = False
        self._density_phase = 0.0
        self._prev_out = None

    def _hist_read(self, ch: int, positions: np.ndarray) -> np.ndarray:
        """向量化分数读。positions = 绝对样本位置（写入槽 = pos % cap，
        锚定触发时刻的写者——不随后续写者漂移，块边界无关）。"""
        cap = self._cap
        pos = np.clip(positions, 0.0, max(0.0, float(self._written - 2)))
        i0 = np.floor(pos).astype(np.int64)
        frac = pos - i0
        i1 = i0 + 1
        a = self._hist[ch, i0 % cap]
        b = self._hist[ch, i1 % cap]
        return a * (1.0 - frac) + b * frac

    def process(self, x: np.ndarray, p, n_abs: int) -> np.ndarray:
        n = x.shape[-1]
        out = np.zeros_like(x, dtype=np.float64)
        if n <= 0:
            return out
        rate = max(1.0, p.grain_density_hz)
        size_n = int(max(4.0, p.grain_size_ms * 0.001 * self._sr))
        base_ratio = float(2.0 ** (max(-24.0, min(24.0, p.grain_pitch_st)) / 12.0))
        jitter_cents = 600.0 * (max(0.0, min(100.0, p.grain_pitch_jitter_pct))
                                / 100.0)
        channels = max(1, x.shape[0])
        fb = max(0.0, min(95.0, p.grain_feedback_pct)) / 100.0
        freeze = bool(p.grain_freeze)

        # 1) 触发调度（密度绝对相位累积；随机消耗顺序只依赖触发序；
        #    读位锚定触发时刻写者位置——块边界无关）
        written_start = self._written
        for j in range(n):
            self._density_phase += rate / self._sr
            while self._density_phase >= 1.0:
                self._density_phase -= 1.0
                self._trigger(p, size_n, base_ratio, jitter_cents, channels,
                              written_start + j, n_abs + j)
        # 2) 历史写入（先写后读——声部读位指向已写入区域；反馈为上一块
        #    wet 的一块延迟回写：因果、有界、契约记录）
        if not freeze:
            write_src = x.astype(np.float64, copy=True)
            if fb > 0.0 and self._prev_out is not None \
                    and self._prev_out.shape[-1] == n:
                write_src = write_src + self._prev_out * fb
            slots = (self._written + np.arange(n)) % self._cap
            self._hist[0, slots] = write_src[0]
            self._hist[1, slots] = write_src[1] if channels > 1 else write_src[0]
            self._written += n
        # 3) 渲染活跃声部（读位为距写者的年龄——写入完成后全部可读；
        #    不读未来：年龄 ≥ 0 即可，插值 +1 由 min(., written−2) 钳制）
        for v in self._voices:
            if not v.active:
                continue
            local0 = max(0, v.start_out + int(v.phase) - n_abs)
            m = min(v.length - int(v.phase), n - local0)
            if m <= 0:
                if v.start_out + int(v.phase) >= n_abs + n:
                    v.active = False
                continue
            idx = np.arange(m)
            # 读位随声部绝对进度推进（v.phase + idx）——跨块续渲不回卷
            gpos = v.read_pos + (v.phase + idx) * v.ratio
            wpos = np.interp((v.phase + idx) / max(1, v.length),
                             np.linspace(0.0, 1.0, len(self._window)),
                             self._window)
            seg = self._hist_read(
                v.channel % channels,
                np.minimum(gpos, getattr(v, "max_pos", np.inf)))
            out[v.channel % channels, local0:local0 + m] += seg * wpos * v.gain
            v.phase += m
            if v.phase >= v.length:
                v.active = False
        self._prev_out = out
        np.clip(out, -8.0, 8.0, out=out)
        return out

    def _trigger(self, p, size_n: int, base_ratio: float, jitter_cents: float,
                 channels: int, writer_now: int = -1,
                 out_pos: int = 0) -> None:
        v = next((gv for gv in self._voices if not gv.active), None)
        if v is None:
            return  # 有界池满：丢弃并计数（策略写入契约）
        if writer_now < 0:
            writer_now = self._written
        jitter = float(self._rng.uniform(-jitter_cents, jitter_cents))
        spray = max(0.0, min(100.0, p.grain_spray_pct)) / 100.0
        position = max(0.0, min(100.0, p.grain_position_pct)) / 100.0
        max_age = float(max(0, writer_now - 2))
        need = size_n * base_ratio
        newest_age = 2.0
        oldest_age = min(max(newest_age + need, 0.0), max_age)
        span = max(0.0, oldest_age - newest_age)
        base_age = newest_age + position * span
        jitter_age = float(self._rng.uniform(0.0, spray * span)) if span > 0 else 0.0
        age = min(max_age, base_age + jitter_age)
        ratio = base_ratio * float(2.0 ** (jitter / 1200.0))
        v.active = True
        # 读位钳制：完整粒跨度（|ratio|·len）必须落在触发时刻写者之前——
        # 粒永不读取触发之后写入的内容（含本块后续），块边界无关
        span_cap = float(writer_now) - 2.0 - abs(ratio) * float(size_n)
        age = min(age, max(2.0, span_cap))
        v.read_pos = float(writer_now) - age
        v.start_out = int(out_pos)  # 绝对输出起点（触发时刻）
        v.max_pos = float(max(0, writer_now - 2))  # 读上界=触发时写者
        v.ratio = -ratio if p.grain_mode == "reverse" else ratio
        v.length = size_n
        v.phase = 0.0
        v.gain = 1.0 / max(1.0, np.sqrt(max(1.0, p.grain_density_hz) * 0.5))
        v.channel = int(self._rng.integers(0, channels))


def _apply_curve(u: np.ndarray, curve: str, fold: float) -> np.ndarray:
    """四曲线共享纯响应（UI 曲线预览与 DSP 同源由对话框另行复用本表）。"""
    if curve == "tape":
        return np.tanh(u)  # 小信号增益恰为 1（+6dB drive → ×1.995）
    if curve == "fold":
        freq = 1.0 + fold * 4.5
        return np.sin(u * freq * (np.pi * 0.5))
    if curve == "hard_clip":
        thresh = max(0.18, 1.0 - fold * 0.72)
        return np.clip(u, -thresh, thresh) / thresh
    # bitcrush：量化位数 12→~2 + sample-hold 1→~5（fold 双语义）
    levels = max(2.0, 12.0 * (1.0 - fold * 0.85))
    hold = 1 + int(round(fold * 4.0))
    q = np.round(np.clip(u, -1.0, 1.0) * levels) / levels
    if hold > 1:
        q = q[(np.arange(len(q)) // hold) * hold]
    return q


class _DistModule:
    """SM-03：四曲线失真（Bias 前置 + 每声道去 DC + Tone/Q + 2× 过采样）。

    过采样为历史衔接的 FIR 半带（63 阶）上/下变换，滤波状态跨块保留；
    群延迟在上/下两级抵消（同 tap 数）。bitcrush 的有意 sample-hold
    不做过采样（量化噪声是声音语义，计划 §8.4-4）。
    """

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._dc = [OnePoleDCBlocker(sample_rate) for _ in range(2)]
        self._fir = firwin(_FIR_TAPS, 0.45)
        self._up_zi: Optional[np.ndarray] = None
        self._dn_zi: Optional[np.ndarray] = None
        self._hist = np.zeros(32, dtype=np.float64)
        self._tone_b = np.array([1.0])
        self._tone_a = np.array([1.0])
        self._tone_zi: Optional[np.ndarray] = None

    def reset(self) -> None:
        for d in self._dc:
            d.reset()
        self._up_zi = None
        self._dn_zi = None
        self._tone_zi = None
        self._hist = np.zeros(32, dtype=np.float64)

    def _set_tone(self, p) -> None:
        from src.features.edit.core.audio_effects.base import (
            biquad_lowpass_coeffs,
        )
        res = max(0.0, min(100.0, p.dist_resonance_pct)) / 100.0
        q = 0.5 + res * 6.0  # 有界 Q（≤6.5，不自激）
        f = max(200.0, min(16000.0, p.dist_tone_hz))
        f = min(f, self._sr * 0.45)
        self._tone_b, self._tone_a = biquad_lowpass_coeffs(f, q, self._sr)

    def process(self, x: np.ndarray, p) -> np.ndarray:
        self._set_tone(p)
        drive = float(10.0 ** (max(0.0, min(36.0, p.dist_drive_db)) / 20.0))
        fold = max(0.0, min(100.0, p.dist_fold_pct)) / 100.0
        bias = (max(-100.0, min(100.0, p.dist_bias_pct)) / 100.0) * 0.45
        out = np.empty_like(x, dtype=np.float64)
        for ch in range(x.shape[0]):
            u = x[ch].astype(np.float64) * drive + bias
            if p.dist_curve == "bitcrush":
                y = _apply_curve(u, "bitcrush", fold)
            else:
                y = self._oversample(u, p.dist_curve, fold)
            y = self._dc[ch].process(y)
            if self._tone_zi is None or ch == 0:
                pass
            yf, zf = lfilter(self._tone_b, self._tone_a, y,
                             zi=(self._tone_zi
                                 if self._tone_zi is not None
                                 else np.zeros(max(len(self._tone_a),
                                                   len(self._tone_b)) - 1)))
            if ch == 0:
                self._tone_zi = zf
            out[ch] = yf
        return out

    def _oversample(self, u: np.ndarray, curve: str, fold: float) -> np.ndarray:
        n = len(u)
        padded = np.concatenate([self._hist, u])
        up = np.zeros(2 * len(padded))
        # ×2 增益补偿：零填充把基带能量均分给 L=2 份（插值器需乘 L）
        up[::2] = padded * 2.0
        if self._up_zi is None:
            self._up_zi = np.zeros(len(self._fir) - 1)
        up_f, zf = lfilter(self._fir, [1.0], up, zi=self._up_zi)
        self._up_zi = zf[-(len(self._fir) - 1):]
        y2 = _apply_curve(up_f, curve, fold)
        if self._dn_zi is None:
            self._dn_zi = np.zeros(len(self._fir) - 1)
        dn_f, zf2 = lfilter(self._fir, [1.0], y2, zi=self._dn_zi)
        self._dn_zi = zf2[-(len(self._fir) - 1):]
        # 群延迟补偿：两级 FIR 各 (T-1)/2，上采样域总延迟 T-1；取 u 起点
        # 之后的偶相位抽取
        start = 2 * len(self._hist) + (_FIR_TAPS - 1)
        y = dn_f[start:start + 2 * n:2]
        if len(y) < n:
            y = np.pad(y, (0, n - len(y)))
        self._hist = (u[-32:].copy() if len(u) >= 32
                      else np.concatenate([self._hist, u])[-32:])
        return y[:n]


class _MiniLimiter:
    """实例内峰值限幅：attack 即时、release 50ms 指数（固定，契约）。"""

    def __init__(self, sample_rate: float) -> None:
        self._rel = float(np.exp(-1.0 / (0.05 * max(1.0, sample_rate))))
        self._gain = 1.0

    def reset(self) -> None:
        self._gain = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        peak = float(np.max(np.abs(x))) if x.size else 0.0
        if peak > 1.0:
            target = 1.0 / peak
            self._gain = min(self._gain, target)
        else:
            self._gain = self._gain * self._rel + 1.0 * (1.0 - self._rel)
        if self._gain >= 0.999:
            self._gain = 1.0
            return x
        return x * self._gain


class SpectralManglerEffect:
    """Spectral Mangler 复合效果（EffectChain 工厂注册形态）。

    AudioEffect 契约（effect_type/set_params/process/reset_state/
    tail_estimate_samples/is_active）由 ``_ensure_effect_registry`` 侧的
    适配基类混入；本类实现处理本体。
    """

    effect_type = "spectral_mangler"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sr = float(sample_rate)
        self._params = None
        self._samples_processed = 0
        self._seed = 0
        self._ring = _RingModule(self._sr)
        self._grain = _GrainModule(self._sr)
        self._dist = _DistModule(self._sr)
        self._limiter = _MiniLimiter(self._sr)
        self._routing_fade = 0.0  # 路由切换短淡出保护（事件语义）
        self.last_clip = False

    # ── AudioEffect 契约 ──
    def set_params(self, params, sample_rate=None, algorithm_version=None):
        from src.features.edit.core.timeline_v2 import SpectralManglerParams
        if sample_rate is not None and sample_rate != self._sr:
            self._sr = float(sample_rate)
            self._rebuild()
        if params is None:
            self._params = None
            return
        if isinstance(params, dict):
            old_routing = getattr(self._params, "routing", None)
            params = SpectralManglerParams.from_dict(params)
            if old_routing is not None and params.routing != old_routing:
                self._routing_fade = 0.005 * self._sr  # 5ms 淡出保护
        self._params = params
        if params.random_seed != self._seed:
            self._seed = params.random_seed
            self._grain.set_seed(self._seed)

    def process(self, samples: np.ndarray) -> np.ndarray:
        p = self._params
        if p is None or not p.enabled or samples.ndim != 2 or samples.size == 0:
            return samples
        n = samples.shape[-1]
        x = samples.astype(np.float64, copy=True)
        # 路由切换淡出（事件边界防爆音；v1 短窗淡出）
        if self._routing_fade > 0:
            m = min(n, int(self._routing_fade))
            ramp = np.linspace(0.0, 1.0, max(2, m))
            x[:, :m] *= ramp[np.newaxis, :]
            self._routing_fade -= m
        wet = self._topology(x, p)
        wet = mix_aligned(x, wet, p.global_mix_pct)
        gain = float(10.0 ** (max(-24.0, min(12.0, p.output_gain_db)) / 20.0))
        wet = wet * gain
        if p.output_limiter_enabled:
            self.last_clip = bool(np.max(np.abs(wet)) >= 1.0) if wet.size else False
            wet = self._limiter.process(wet)
        self._samples_processed += n
        return wet.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        self._ring.reset()
        self._grain.reset()
        self._dist.reset()
        self._limiter.reset()
        self._samples_processed = 0
        self.last_clip = False

    def tail_estimate_samples(self) -> int:
        p = self._params
        if p is None or not p.enabled:
            return 0
        # 颗粒 = 有界尾音（最大粒长 + 反馈衰减圈）；其他模块无尾音
        if p.grain_enabled:
            size = int(max(4.0, p.grain_size_ms * 0.001 * self._sr))
            fb = max(0.0, min(95.0, p.grain_feedback_pct)) / 100.0
            if fb >= 0.01:
                import math
                circles = 3.0 / max(0.01, -math.log10(fb)) if fb < 1.0 else 8.0
                return int(min(4.0 * self._sr, size * max(1.0, circles)))
            return size
        return 0

    @property
    def is_active(self) -> bool:
        p = self._params
        if p is None or not p.enabled:
            return False
        return (p.ring_enabled or p.grain_enabled or p.dist_enabled
                or abs(p.output_gain_db) > 1e-9)

    def collect_telemetry(self) -> dict:
        """NF-04：模块活动/削波指示（窗口端 30Hz 合并消费）。"""
        p = self._params
        return {
            "effect_type": self.effect_type,
            "ring_active": bool(p and p.enabled and p.ring_enabled),
            "grain_active": bool(p and p.enabled and p.grain_enabled),
            "dist_active": bool(p and p.enabled and p.dist_enabled),
            "frozen": bool(p and p.grain_freeze),
            "clip": bool(self.last_clip),
            "limiter_on": bool(p and p.output_limiter_enabled),
            "sample_position": int(self._samples_processed),
        }

    # ── 拓扑（SM-04）──
    def _module_wet(self, kind: str, x: np.ndarray, p) -> np.ndarray:
        if kind == "ring":
            return self._ring.process(x, p)
        if kind == "grain":
            return self._grain.process(x, p, self._samples_processed)
        return self._dist.process(x, p)

    def _topology(self, x: np.ndarray, p) -> np.ndarray:
        enabled = [m for m in ("ring", "grain", "dist")
                   if getattr(p, f"{m}_enabled")]
        if not enabled:
            return x.copy()
        if p.routing == "parallel":
            # y = x + Σ mᵢ(Fᵢ(x) − x)/N（启用但 mix=0 仍计入 N）
            acc = np.zeros_like(x)
            for m in enabled:
                wet = self._module_wet(m, x, p)
                acc += (getattr(p, f"{m}_mix_pct") / 100.0) * (wet - x)
            return x + acc / float(len(enabled))
        # 串行：按 routing 顺序；每模块 wet-only 后做模块 mix
        order = {
            "ring_grain_dist": ("ring", "grain", "dist"),
            "grain_ring_dist": ("grain", "ring", "dist"),
            "dist_grain_ring": ("dist", "grain", "ring"),
        }[p.routing]
        cur = x
        for m in order:
            if m not in enabled:
                continue
            wet = self._module_wet(m, cur, p)
            cur = mix_aligned(cur, wet, getattr(p, f"{m}_mix_pct"))
        return cur

    def _rebuild(self) -> None:
        self._ring = _RingModule(self._sr)
        self._grain = _GrainModule(self._sr)
        self._dist = _DistModule(self._sr)
        self._limiter = _MiniLimiter(self._sr)
