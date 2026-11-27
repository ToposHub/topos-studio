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
from uuid import uuid4

import numpy as np
from scipy.signal import firwin, lfilter

from src.features.edit.core.audio_effects.base import (
    OnePoleDCBlocker,
    SampleDelayLine,
    mix_aligned,
    seeded_rng,
)

_HISTORY_SECONDS = 8.0
_GRAIN_VOICES = 64
_FIR_TAPS = 63
_GRAIN_BLOCK = 256
_GRAIN_FRAMES = np.arange(_GRAIN_BLOCK)
_GRAIN_WINDOW = np.hanning(2048)
_GRAIN_WINDOW_PHASES = np.linspace(0.0, 1.0, _GRAIN_WINDOW.size)
_DIST_FIR = firwin(_FIR_TAPS, 0.45)


def _lfo_cents(depth_pct: float) -> float:
    """LFO 深度 → 载波频率偏差（cents）。100% = ±60 cents（锁定映射）。"""
    return 60.0 * max(0.0, min(100.0, depth_pct)) / 100.0


class _RingModule:
    """SM-01：四模式环形调制（每声道独立相位，spread 为 R 路相位偏移）。"""

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._phase = 0.0
        self._dual_phase = 0.0
        self._fm_phase = 0.0
        self._lfo_phase = 0.0

    def reset(self) -> None:
        self._phase = 0.0
        self._dual_phase = 0.0
        self._fm_phase = 0.0
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
        dual_phi = self._dual_phase + 1.035 * dphi
        fm_phi = self._fm_phase + 1.5 * dphi
        self._phase = float((self._phase + dphi[-1]) % (2.0 * np.pi))
        # Noninteger frequency ratios cannot be reconstructed from a wrapped
        # fundamental phase. Keep each oscillator continuous across blocks.
        self._dual_phase = float(dual_phi[-1] % (2.0 * np.pi))
        self._fm_phase = float(fm_phi[-1] % (2.0 * np.pi))
        spread_rad = (max(0.0, min(100.0, p.ring_spread_pct)) / 100.0) * np.pi
        warp_idx = (max(0.0, min(100.0, p.ring_warp_pct)) / 100.0) * 3.2
        mode = p.ring_mode

        def _carrier(phase: np.ndarray, dual_phase: np.ndarray,
                     fm_phase: np.ndarray) -> np.ndarray:
            if mode == "sine":
                return np.sin(phase)
            if mode == "dual":
                return 0.5 * (np.sin(phase) + np.sin(dual_phase + 0.8))
            if mode == "fm_ring":
                return np.sin(phase + warp_idx * np.sin(fm_phase))
            return 2.0 * np.abs(np.sin(phase)) - 1.0

        out = np.empty_like(x, dtype=np.float64)
        out[0::2] = x[0::2] * _carrier(phi, dual_phi, fm_phi)
        if x.shape[0] > 1:
            out[1::2] = x[1::2] * _carrier(phi + spread_rad,
                                           dual_phi + 1.035 * spread_rad,
                                           fm_phi + 1.5 * spread_rad)
        return out


class _GrainVoice:
    __slots__ = ("active", "read_pos", "ratio", "length", "phase", "gain",
                 "channel", "start_out", "max_pos", "window")


class _GrainModule:
    """SM-02：真实颗粒（有界历史 + Hann 窗 + 种子确定性 + Freeze）。"""

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._cap = max(1024, int(_HISTORY_SECONDS * self._sr))
        self._hist: Optional[np.ndarray] = None
        self._written = 0
        self._voices: list = []
        for _ in range(_GRAIN_VOICES):
            v = _GrainVoice()
            v.active = False
            v.start_out = 0
            v.window = None
            self._voices.append(v)
        self._density_phase = 0.0
        self._seed = 0
        self._rng = seeded_rng(self._seed, stream_id=0x5A1)
        self._freeze_start = 0
        self._window: np.ndarray | None = None
        # H08：反馈环（wet 的 D=size_n 样本延迟回写）+ 丢弃计数
        self._fb_ring: Optional[np.ndarray] = None
        self._fb_capacity = max(_GRAIN_BLOCK, int(self._sr * 0.5) + _GRAIN_BLOCK)
        self._fb_written = 0
        self.dropped_events = 0
        self._event_count = 0
        self._last_p = None

    def set_seed(self, seed: int) -> None:
        self._seed = int(seed)
        self._rng = seeded_rng(self._seed, stream_id=0x5A1)

    # ── H09：Freeze 捕获/装载（资产化重放）──

    def capture_freeze(self) -> dict:
        """H09：捕获当前历史环（freeze 锚定时刻的输入内容）。

        返回资产 dict（id/anchor/seed/history/params）；由宿主经
        ``FreezeCaptureStore`` 落盘——raw PCM 不进参数 JSON。
        """
        import time as _time
        if self._hist is None or self._written == 0:
            return {}
        return {
            "id": f"fz-{uuid4().hex}",
            "anchor": int(self._written),
            "seed": int(self._seed),
            "sample_rate": float(self._sr),
            "history": self._hist.copy(),
            "params": {
                "grain_density_hz": float(getattr(
                    self._last_p, "grain_density_hz", 24.0)),
                "grain_size_ms": float(getattr(
                    self._last_p, "grain_size_ms", 85.0)),
            },
            "created_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def install_capture(self, capture: dict) -> bool:
        """H09：装载捕获资产（确定性重放：历史/锚点/seed 逐一恢复）。

        FX-22（2026-11-21 计划）修复：
        - 回卷环：写入超过一圈后锚点按 ``anchor % cap`` 恢复相位
          （旧实现 clamp 到 cap → mod 后为 0，读位错一格相位）；
        - 异采样率：捕获 ``sample_rate`` 与当前不一致时按内容时长
          重采样历史（保持冻结内容的时长与音高），锚点同比例缩放。
        """
        hist = capture.get("history")
        if not isinstance(hist, np.ndarray) or hist.ndim != 2:
            return False
        ch, length = hist.shape
        if (ch not in (1, 2) or length == 0 or hist.dtype.kind not in "fiu"
                or not np.isfinite(hist).all()):
            return False
        try:
            capture_rate = float(capture.get("sample_rate") or self._sr)
            anchor = int(capture.get("anchor", length))
            seed = int(capture.get("seed", self._seed))
        except (TypeError, ValueError, OverflowError):
            return False
        if not np.isfinite(capture_rate) or not 1.0 <= capture_rate <= 384000.0 or anchor <= 0:
            return False
        if abs(capture_rate - self._sr) > 1.0 or length != self._cap:
            from scipy.signal import resample_poly
            from math import gcd
            # Linearize the valid chronological history before changing rate.
            # Resampling the physical ring creates a splice at the old cursor.
            valid = min(anchor, length)
            if valid == 0:
                return False
            positions = np.arange(anchor - valid, anchor) % length
            chronological = hist[:, positions]
            source_rate = int(round(capture_rate))
            target_rate = int(round(self._sr))
            g = gcd(source_rate, target_rate)
            chronological = resample_poly(chronological, target_rate // g,
                                           source_rate // g, axis=-1)
            anchor = int(round(anchor * self._sr / capture_rate))
            n = min(chronological.shape[-1], self._cap, anchor)
            prepared = np.zeros((2, self._cap), dtype=np.float64)
            destination = np.arange(anchor - n, anchor) % self._cap
            prepared[0, destination] = chronological[0, -n:]
            prepared[1, destination] = chronological[min(1, ch - 1), -n:]
        else:
            prepared = np.zeros((2, self._cap), dtype=np.float64)
            prepared[0] = hist[0]
            prepared[1] = hist[min(1, ch - 1)]
        self._hist = prepared
        self._written = anchor
        self.set_seed(seed)
        self.reset(preserve_history=True)
        return True

    def reset(self, *, preserve_history: bool = False) -> None:
        if not preserve_history:
            if self._hist is not None:
                self._hist.fill(0.0)
            self._written = 0
        for v in self._voices:
            v.active = False
            v.window = None
        self._density_phase = 0.0
        self._freeze_start = 0
        self._fb_ring = None
        self._fb_written = 0
        self.dropped_events = 0
        self._event_count = 0
        self.set_seed(self._seed)

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

    def _render_subblock(self, out: np.ndarray, start: int, channels: int) -> None:
        """Gather active voice reads together for high-overlap grain settings."""
        length = out.shape[-1]
        voices = []
        for voice in self._voices:
            if not voice.active:
                continue
            phase = int(voice.phase)
            onset = max(0, voice.start_out + phase - start)
            count = min(voice.length - phase, length - onset)
            if count <= 0:
                if voice.start_out + phase >= start + length:
                    voice.active = False
                    voice.window = None
                continue
            voices.append((voice, onset, count, phase, voice.channel % channels))

        # Use the same fractional interpolation and summation order as the
        # low-voice path, while amortizing NumPy calls across the dense cloud.
        batch = None
        if len(voices) >= 6:
            frames = _GRAIN_FRAMES[:max(count for _, _, count, _, _ in voices)]
            phase = np.array([phase for _, _, _, phase, _ in voices])[:, None]
            bases = np.array([voice.read_pos for voice, *_ in voices])[:, None]
            ratios = np.array([voice.ratio for voice, *_ in voices])[:, None]
            limits = np.array([min(voice.max_pos, max(0, self._written - 2))
                               for voice, *_ in voices])[:, None]
            pos = np.clip(bases + (phase + frames) * ratios, 0.0, limits)
            floor = np.floor(pos).astype(np.intp)
            fraction = pos - floor
            left = floor % self._cap
            right = (left + 1) % self._cap
            channel_idx = np.array([channel for _, _, _, _, channel in voices])[:, None]
            batch = (self._hist[channel_idx, left] * (1.0 - fraction)
                     + self._hist[channel_idx, right] * fraction)

        for row, (voice, onset, count, phase, channel) in enumerate(voices):
            if batch is None:
                positions = voice.read_pos + (phase + _GRAIN_FRAMES[:count]) * voice.ratio
                segment = self._hist_read(channel, np.minimum(positions, voice.max_pos))
            else:
                segment = batch[row, :count]
            out[channel, onset:onset + count] += (
                segment * voice.window[phase:phase + count] * voice.gain)
            voice.phase += count
            if voice.phase >= voice.length:
                voice.active = False
                voice.window = None

    def process(self, x: np.ndarray, p, n_abs: int) -> np.ndarray:
        """H08：事件驱动的颗粒调度。

        修前缺陷（复审 §5 H08）：
        - 整块先触发后渲染——1s 块 @80 grains/s 一次触发 80 粒，64 池满
          丢 16、物理重叠只有 40。现按 **子块推进**（≤256 样本）：子块内
          先按密度相位触发到子块边界，再渲染子块——触发/渲染按采样时间
          交错，池占用 = 真实重叠；触发时刻与随机消耗顺序不变（块长
          无关，事件序号不依赖块数量）。
        - feedback「上块同 shape 才反馈」→ 固定**样本延迟** D=size_n 的
          wet 环形回写（因果、有界、定义明确）。
        - 读头年龄上限显式覆盖 ring 容量（不足历史 → 延后触发并计数，
          不 clamp 到常量采样、不读已覆盖数据）。
        - Cloud/Scatter 分离：cloud=高斯年龄抖动+随机声道；
          scatter=均匀宽撒+交替声道+增益抖动。
        """
        n = x.shape[-1]
        out = np.zeros_like(x, dtype=np.float64)
        if n <= 0:
            return out
        rate = max(1.0, min(80.0, p.grain_density_hz))
        size_n = int(max(4.0, min(500.0, p.grain_size_ms) * 0.001 * self._sr))
        base_ratio = float(2.0 ** (max(-24.0, min(24.0, p.grain_pitch_st)) / 12.0))
        jitter_cents = 600.0 * (max(0.0, min(100.0, p.grain_pitch_jitter_pct))
                                / 100.0)
        channels = min(2, x.shape[0])
        out[2:] = x[2:]
        fb = max(0.0, min(95.0, p.grain_feedback_pct)) / 100.0
        self._last_p = p  # H09：捕获元数据引用
        if self._hist is None:
            self._hist = np.zeros((2, self._cap), dtype=np.float64)
        if self._fb_ring is None:
            self._fb_ring = np.zeros((2, self._fb_capacity), dtype=np.float64)

        # A preset can start frozen without an asset. Fill one short, fixed
        # span first so it can hold real audio independent of host block size.
        if not p.grain_freeze:
            self._freeze_start = 0
        elif self._written == 0 and not self._freeze_start:
            self._freeze_start = min(size_n, max(4, round(self._sr * 0.1)))
        sub = min(_GRAIN_BLOCK, size_n) if fb > 0.0 else _GRAIN_BLOCK
        event_step = rate / self._sr
        pos = 0
        while pos < n:
            m = min(sub, n - pos)
            freeze = p.grain_freeze and self._written >= self._freeze_start
            if p.grain_freeze and not freeze:
                m = min(m, self._freeze_start - self._written)
            idx = _GRAIN_FRAMES[:m]
            xb = x[:channels, pos:pos + m]
            out_b = out[:, pos:pos + m]
            # 1) 触发到子块边界（密度绝对相位按样本推进）
            written_start = self._written
            for j in range(m):
                self._density_phase += event_step
                while self._density_phase >= 1.0:
                    self._density_phase -= 1.0
                    self._trigger(p, size_n, base_ratio, jitter_cents,
                                  channels, written_start if freeze else written_start + j,
                                  n_abs + pos + j)
            # 2) 历史写入（freeze 停写；反馈 = wet 经 D=size_n 样本延迟
            #    的环形回读——不再依赖"上块同 shape"）
            if not freeze:
                write_src = xb
                if fb > 0.0:
                    # wet 延迟 D=size_n：当前块 [w, w+m) 回读 [w−D, w−D+m)
                    fb_delay = size_n
                    fb_pos = idx + self._fb_written - fb_delay
                    fb_val = self._fb_read(fb_pos)[:channels]
                    write_src = write_src + fb_val * fb
                slots = (self._written + idx) % self._cap
                self._hist[0, slots] = write_src[0]
                self._hist[1, slots] = write_src[1] if channels > 1 else write_src[0]
                self._written += m
            self._render_subblock(out_b, n_abs + pos, channels)
            # 4) wet 写入反馈环（绝对位推进）
            fslots = (self._fb_written + idx) % self._fb_capacity
            self._fb_ring[0, fslots] = out_b[0]
            self._fb_ring[1, fslots] = out_b[1] if channels > 1 else out_b[0]
            self._fb_written += m
            pos += m
        np.clip(out[:channels], -8.0, 8.0, out=out[:channels])
        return out

    def _fb_read(self, abs_positions: np.ndarray) -> np.ndarray:
        """反馈环读取（绝对位 → (2, n)；未写区域返回零）。"""
        cap = self._fb_capacity
        if abs_positions[0] >= max(0, self._fb_written - cap):
            return self._fb_ring[:, abs_positions % cap]
        out = np.zeros((2, len(abs_positions)), dtype=np.float64)
        valid = (abs_positions >= max(0, self._fb_written - cap)) & (abs_positions < self._fb_written)
        if valid.any():
            idx = abs_positions[valid].astype(np.int64) % cap
            out[0, valid] = self._fb_ring[0, idx]
            out[1, valid] = self._fb_ring[1, idx]
        return out

    def _trigger(self, p, size_n: int, base_ratio: float, jitter_cents: float,
                 channels: int, writer_now: int = -1,
                 out_pos: int = 0) -> None:
        v = next((gv for gv in self._voices if not gv.active), None)
        if v is None:
            self.dropped_events += 1  # 有界池满（真实重叠超 64 才发生）
            return
        if writer_now < 0:
            writer_now = self._written
        spray = max(0.0, min(100.0, p.grain_spray_pct)) / 100.0
        position = max(0.0, min(100.0, p.grain_position_pct)) / 100.0
        max_age = float(max(0, writer_now - 2))
        need = size_n * base_ratio
        newest_age = 2.0
        # H08：历史容量守恒——读头跨度（含 position/spray 回看）必须落在
        # ring 覆盖内；不足历史（容量 < 跨度）→ 延后触发（丢弃计数），
        # 不 clamp 到常量采样、不读已覆盖数据
        oldest_age = min(max(newest_age + need, 0.0), max_age,
                         float(self._cap) - need - 8.0)
        if oldest_age <= newest_age:
            self.dropped_events += 1
            return
        span = max(0.0, oldest_age - newest_age)
        # H08：Cloud/Scatter 契约——不同的事件/位置分布：
        # - cloud：以 position 为中心的高斯年龄抖动（σ=spray·span/2），
        #   随机声道，音高抖动；
        # - scatter：全程均匀撒布（忽略 position 中心），交替声道，
        #   额外增益抖动（散粒更稀疏对比）。
        mode = str(getattr(p, "grain_mode", "cloud"))
        if mode == "scatter":
            base_age = newest_age
            jitter_age = (float(self._rng.uniform(0.0, max(1e-6, span)))
                          if span > 0 else 0.0)
        else:
            base_age = newest_age + position * span
            sigma = spray * span * 0.5
            jitter_age = (float(abs(self._rng.standard_normal()) * sigma)
                          if span > 0 else 0.0)
        age = min(max_age, base_age + jitter_age)
        if mode == "scatter":
            jitter = float(self._rng.uniform(-jitter_cents, jitter_cents))
        else:
            jitter = float(self._rng.uniform(-jitter_cents, jitter_cents))
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
        if self._window is None or self._window.size != size_n:
            self._window = np.interp(np.arange(size_n) / size_n,
                                     _GRAIN_WINDOW_PHASES, _GRAIN_WINDOW)
        v.window = self._window
        v.phase = 0.0
        v.gain = 1.0 / max(1.0, np.sqrt(max(1.0, p.grain_density_hz) * 0.5))
        if mode == "scatter":
            self._event_count += 1
            v.channel = self._event_count % channels
            v.gain *= float(self._rng.uniform(0.6, 1.0))
        else:
            v.channel = int(self._rng.integers(0, channels))


def _apply_curve(u: np.ndarray, curve: str, fold: float) -> np.ndarray:
    """四曲线共享纯响应（UI 曲线预览与 DSP 同源由对话框另行复用本表）。

    H07：本函数保持**无状态**（量化网格/位深与曲线形状的唯一来源）；
    bitcrush 的 sample-hold 是时间域效果，由 ``_DistModule`` 按绝对流
    索引逐声道应用（跨块相位连续，块长无关）。
    """
    if curve == "tape":
        return np.tanh(u)  # 小信号增益恰为 1（+6dB drive → ×1.995）
    if curve == "fold":
        freq = 1.0 + fold * 4.5
        return np.sin(u * freq * (np.pi * 0.5))
    if curve == "hard_clip":
        thresh = max(0.18, 1.0 - fold * 0.72)
        return np.clip(u, -thresh, thresh) / thresh
    # bitcrush 量化：位数 12→~2（fold 语义）；hold 1→~5 由模块应用
    levels = max(2.0, 12.0 * (1.0 - fold * 0.85))
    return np.round(np.clip(u, -1.0, 1.0) * levels) / levels


class _DistModule:
    """SM-03：四曲线失真（Bias 前置 + 逐声道去 DC + Tone/Q + 2× 过采样）。

    H07 契约（复审 §5 H07）：
    - **逐声道状态**：up/down FIR zi、tone zi、bitcrush hold 相位/保持值
      逐声道维护（只共享不可变系数）——静音声道不再因另一声道输入
      产生声音（复审测得 R 峰 0.0037..0.24 的串音源）。
    - **有状态 2× 过采样**：零填充→FIR 插值→非线性→FIR 抗混叠→保相位
      抽取；每声道输出 FIFO 以固定群延迟 ``(T−1)``@2× = ``(T−1)/2``@1×
      样本对齐输入——不再按块裁未来样本后拿 0 补块尾（复审：每块尾
      31 采样空洞，整块/256 块误差 ~2.1）。
    - **bitcrush sample-hold 按绝对流索引**：跨块相位/保持值连续，
      块长无关；有意不做过采样（量化噪声是声音语义，计划 §8.4-4）。
    - ``latency`` 报告群延迟（31 样本@48k），供拓扑 dry 对齐。
    """

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._dc = [OnePoleDCBlocker(sample_rate) for _ in range(2)]
        self._fir = _DIST_FIR
        self._nz = _FIR_TAPS - 1
        self._up_zi: list = [None, None]
        self._dn_zi: list = [None, None]
        self._tone_zi: list = [None, None]
        self._abs_pos: list = [0, 0]      # bitcrush hold 相位基准
        self._hold_idx: list = [-1, -1]   # 上一采样所处 hold 段号
        self._hold_val: list = [0.0, 0.0] # 跨块沿用的保持值
        self._tone_b = np.array([1.0])
        self._tone_a = np.array([1.0])
        self._tone_delay = 0
        self._tone_key: tuple[float, float] | None = None
        # 按默认 tone 初始化系数/群延迟（首个 process 前 latency 报告
        # 即为真值，PDC/对齐不因"尚未处理"而少报 1 样本）
        from src.features.edit.core.timeline_v2 import SpectralManglerParams
        self._set_tone(SpectralManglerParams())

    @property
    def latency(self) -> int:
        """模块总群延迟（1× 域样本）：两级 FIR (T−1)/2 + tone 低通群延迟。"""
        return self._nz // 2 + int(getattr(self, "_tone_delay", 0))

    def processing_latency(self, curve: str, legacy_timing: bool) -> int:
        # Bitcrush runs at the source rate and never enters the FIR pair.
        return self._tone_delay if curve == "bitcrush" and not legacy_timing else self.latency

    def _ensure_channels(self, ch: int) -> None:
        while ch >= len(self._dc):
            self._dc.append(OnePoleDCBlocker(self._sr))
            self._up_zi.append(None)
            self._dn_zi.append(None)
            self._tone_zi.append(None)
            self._abs_pos.append(0)
            self._hold_idx.append(-1)
            self._hold_val.append(0.0)

    def reset(self) -> None:
        for d in self._dc:
            d.reset()
        self._up_zi = [None] * len(self._up_zi)
        self._dn_zi = [None] * len(self._dn_zi)
        self._tone_zi = [None] * len(self._tone_zi)
        self._abs_pos = [0] * len(self._abs_pos)
        self._hold_idx = [-1] * len(self._hold_idx)
        self._hold_val = [0.0] * len(self._hold_val)

    def _set_tone(self, p) -> None:
        from src.features.edit.core.audio_effects.base import (
            biquad_lowpass_coeffs,
        )
        res = max(0.0, min(100.0, p.dist_resonance_pct)) / 100.0
        q = 0.5 + res * 6.0  # 有界 Q（≤6.5，不自激）
        f = max(200.0, min(16000.0, p.dist_tone_hz))
        f = min(f, self._sr * 0.45)
        if (f, q) == self._tone_key:
            return
        self._tone_key = (f, q)
        self._tone_b, self._tone_a = biquad_lowpass_coeffs(f, q, self._sr)
        # tone 低通群延迟（1kHz 处数值相位差分；H07 时延报告的一部分）
        w = 2.0 * np.pi * 1000.0 / self._sr

        def _ph(ww: float) -> float:
            e1, e2 = np.exp(-1j * ww), np.exp(-2j * ww)
            return float(np.angle((self._tone_b[0] + self._tone_b[1] * e1
                                   + self._tone_b[2] * e2)
                                  / (self._tone_a[0] + self._tone_a[1] * e1
                                     + self._tone_a[2] * e2)))

        dw = 1e-4
        tau = -(_ph(w + dw) - _ph(w - dw)) / (2.0 * dw)
        self._tone_delay = int(round(max(0.0, tau)))

    def process(self, x: np.ndarray, p) -> np.ndarray:
        self._set_tone(p)
        drive = float(10.0 ** (max(0.0, min(36.0, p.dist_drive_db)) / 20.0))
        fold = max(0.0, min(100.0, p.dist_fold_pct)) / 100.0
        bias = (max(-100.0, min(100.0, p.dist_bias_pct)) / 100.0) * 0.45
        out = np.empty_like(x, dtype=np.float64)
        tone_order = max(len(self._tone_a), len(self._tone_b)) - 1
        for ch in range(x.shape[0]):
            self._ensure_channels(ch)
            u = x[ch] * drive + bias
            if p.dist_curve == "bitcrush":
                y = self._bitcrush_hold(u, fold, ch)
            else:
                y = self._oversample(u, p.dist_curve, fold, ch)
            y = self._dc[ch].process(y)
            zi = (self._tone_zi[ch] if self._tone_zi[ch] is not None
                  else np.zeros(tone_order))
            yf, zf = lfilter(self._tone_b, self._tone_a, y, zi=zi)
            self._tone_zi[ch] = zf
            out[ch] = yf
        return out

    def _bitcrush_hold(self, u: np.ndarray, fold: float, ch: int) -> np.ndarray:
        """量化（_apply_curve 同源网格）+ 绝对流索引 sample-hold。"""
        n = len(u)
        q = _apply_curve(u, "bitcrush", fold)
        hold = 1 + int(round(fold * 4.0))
        self._abs_pos[ch] += n
        if hold <= 1 or n == 0:
            return q
        seg = (self._abs_pos[ch] - n + np.arange(n)) // hold
        starts = np.empty(n, dtype=bool)
        starts[0] = seg[0] != self._hold_idx[ch]
        starts[1:] = seg[1:] != seg[:-1]
        vals = q[starts]
        starts_idx = np.flatnonzero(starts)
        if not starts[0]:
            # 首段延续上一块：段起点=0，值沿用跨块保持值
            vals = np.concatenate([[self._hold_val[ch]], vals])
            starts_idx = np.concatenate([[0], starts_idx])
        counts = np.diff(np.append(starts_idx, n))
        y = np.repeat(vals, counts)
        self._hold_idx[ch] = int(seg[-1])
        self._hold_val[ch] = float(y[-1])
        return y

    def _oversample(self, u: np.ndarray, curve: str, fold: float,
                    ch: int) -> np.ndarray:
        n = len(u)
        up = np.zeros(2 * n)
        # ×2 增益补偿：零填充把基带能量均分给 L=2 份（插值器需乘 L）
        up[::2] = u * 2.0
        if self._up_zi[ch] is None:
            self._up_zi[ch] = np.zeros(self._nz)
        up_f, zf = lfilter(self._fir, [1.0], up, zi=self._up_zi[ch])
        self._up_zi[ch] = zf
        y2 = _apply_curve(up_f, curve, fold)
        if self._dn_zi[ch] is None:
            self._dn_zi[ch] = np.zeros(self._nz)
        dn_f, zf2 = lfilter(self._fir, [1.0], y2, zi=self._dn_zi[ch])
        self._dn_zi[ch] = zf2
        # Carry both FIR states across blocks and take the same even phase.
        return dn_f[::2]


class _MiniLimiter:
    """实例内峰值限幅：attack 即时、release 50ms 指数（固定，契约）。

    H10：逐采样包络（修前"每块一个采样系数"——1 秒静音后增益按块长
    0.5..1 漂移：block=1 → ≈1，48000 → 0.5002）。声道联动（同增益），
    向量化精确实现 min-decay 扫描：
    ``E[n] = min(E[n-1]·rel, target[n]) = min_k target[k]·rel^(n-k)``，
    log 域 running-min 一次 accumulate 完成，块长无关、逐位一致。
    """

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        # release 增长率（每样本）：E 向 1 恢复
        self._d = 1.0 / (0.05 * max(1.0, sample_rate))
        self._log_e0 = 0.0  # log(E[−1])，E=1 起步

    def reset(self) -> None:
        self._log_e0 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0 or x.ndim != 2:
            return x
        n = x.shape[-1]
        # 声道联动目标：target = 1/peak（peak ≤ 1 → 1）
        peak = np.max(np.abs(x), axis=0)
        log_t = np.where(peak > 1.0, -np.log(np.maximum(peak, 1e-12)), 0.0)
        # E[n] = min(1, min_{k≤n} t[k]·e^{(n−k)d})——逐采样恢复的精确包络：
        # log 域 running-min（u[k]=logt[k]−k·d 有负漂移，数值有界）
        j = np.arange(n, dtype=np.float64)
        u = log_t - j * self._d
        c = np.minimum.accumulate(u)
        inner = c + j * self._d
        outer = self._log_e0 + (j + 1.0) * self._d
        log_e = np.minimum(np.minimum(outer, inner), 0.0)
        self._log_e0 = float(log_e[-1])
        env = np.exp(np.clip(log_e, -40.0, 0.0))
        return x * env[np.newaxis, :]


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
        self._pre_limiter_peak = 0.0
        self._output_peak = 0.0
        self._capture_status = "empty"
        self._installed_capture: dict | None = None
        self._legacy_timing = False
        # H07：模块 dry 对齐线（dist 2× 过采样群延迟 31 样本）+ 总 wet 对齐。
        # 逐声道成对——单线双声道会压 2/弹 2（同内容滞后减半、异内容互串）。
        self._reset_delay_paths()

    # ── H09：Freeze 资产化（捕获由宿主拉取；装载由宿主在渲染前注入）──

    def take_freeze_capture(self) -> dict:
        """宿主拉取当前冻结内容（实时窗口冻结按钮/导出前固化用）。"""
        return self._grain.capture_freeze()

    def install_freeze_capture(self, capture: dict) -> bool:
        """装载捕获资产（重开工程/选区/Stem 重放的确定性恢复）。"""
        if capture is self._installed_capture and self._capture_status == "installed":
            return True
        installed = self._grain.install_capture(capture)
        self._installed_capture = capture if installed else None
        self._capture_status = "installed" if installed else "failed"
        return installed

    # ── AudioEffect 契约 ──
    def set_params(self, params: object, sample_rate: float | None = None,
                   algorithm_version: int | None = None) -> None:
        from src.features.edit.core.timeline_v2 import SpectralManglerParams
        if sample_rate is not None and sample_rate != self._sr:
            self._sr = float(sample_rate)
            self._rebuild()
        self._legacy_timing = algorithm_version is not None and algorithm_version <= 4
        if params is None:
            self._params = None
            return
        old_routing = getattr(self._params, "routing", None)
        if isinstance(params, dict):
            params = SpectralManglerParams.from_dict(params)
        if old_routing is not None and params.routing != old_routing:
            self._routing_fade = max(1, int(round(0.005 * self._sr)))
        if (params.freeze_capture_id
                and params.freeze_capture_id != getattr(self._params, "freeze_capture_id", "")
                and self._installed_capture is not None
                and self._installed_capture.get("id") != params.freeze_capture_id):
            self._installed_capture = None
            self._grain.reset()
        self._params = params
        if params.dist_enabled:
            self._dist._set_tone(params)
        if not params.grain_freeze:
            if self._installed_capture is not None:
                self._installed_capture = None
            self._capture_status = "empty"
        elif params.freeze_capture_id and self._installed_capture is None:
            self._capture_status = "missing"
        if params.random_seed != self._seed:
            self._seed = params.random_seed
            self._grain.set_seed(self._seed)

    def process(self, samples: np.ndarray) -> np.ndarray:
        p = self._params
        if p is None or not p.enabled or samples.ndim != 2 or samples.size == 0:
            return samples
        n = samples.shape[-1]
        x = samples.astype(np.float64, copy=False)
        wet = self._topology(x, p)
        dry = self._align(self._dry_align, x, self._wet_latency(p))
        # Keep routing protection on the wet branch: global_mix=0 must remain
        # dry. Use one continuous ramp, including one-sample output blocks.
        if self._routing_fade > 0:
            m = min(n, int(self._routing_fade))
            total = max(1, int(round(0.005 * self._sr)))
            start = total - int(self._routing_fade)
            ramp = np.arange(start, start + m, dtype=np.float64) / max(1, total - 1)
            if self._legacy_timing:
                wet[:, :m] *= ramp[np.newaxis, :]
            else:
                wet[:, :m] = dry[:, :m] + (wet[:, :m] - dry[:, :m]) * ramp
            self._routing_fade -= m
        wet = mix_aligned(dry, wet, p.global_mix_pct)
        gain = float(10.0 ** (max(-24.0, min(12.0, p.output_gain_db)) / 20.0))
        if gain != 1.0:
            wet *= gain
        self._pre_limiter_peak = float(np.max(np.abs(wet))) if wet.size else 0.0
        self.last_clip = self._pre_limiter_peak >= 1.0
        if p.output_limiter_enabled and (self.last_clip or self._limiter._log_e0 < 0.0):
            wet = self._limiter.process(wet)
            self._output_peak = float(np.max(np.abs(wet)))
        else:
            self._output_peak = self._pre_limiter_peak
        self._samples_processed += n
        return wet.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        self._ring.reset()
        frozen = bool(self._params and self._params.grain_freeze and self._grain._written)
        self._grain.reset(preserve_history=frozen)
        self._dist.reset()
        self._limiter.reset()
        for path in (*self._module_dry.values(), *self._par_residual.values(),
                     self._dry_align, self._par_align):
            for line in path:
                line.reset()
        self._samples_processed = 0
        self._routing_fade = 0
        self.last_clip = False
        self._pre_limiter_peak = 0.0
        self._output_peak = 0.0
        if not frozen and self._capture_status != "missing":
            self._capture_status = "empty"

    def tail_estimate_samples(self) -> int:
        p = self._params
        if p is None or not p.enabled:
            return 0
        tails = [0]
        # 颗粒 = 有界尾音（最大粒长 + 反馈衰减圈）
        if p.grain_enabled:
            size = int(max(4.0, min(p.grain_size_ms, 500.0) * 0.001 * self._sr))
            fb = max(0.0, min(95.0, p.grain_feedback_pct)) / 100.0
            if fb >= 0.01:
                import math
                circles = 3.0 / max(0.01, -math.log10(fb)) if fb < 1.0 else 8.0
                tails.append(int(min(4.0 * self._sr, size * max(1.0, circles))))
            else:
                tails.append(size)
        # H03：dist 过采样 FIFO/滤波滞留（时延即 flush 需求）
        if p.dist_enabled:
            tails.append(self._module_latency("dist"))
        return max(tails)

    @property
    def is_active(self) -> bool:
        p = self._params
        if p is None or not p.enabled:
            return False
        return (p.ring_enabled or p.grain_enabled or p.dist_enabled
                or p.output_limiter_enabled or abs(p.output_gain_db) > 1e-9)

    def collect_telemetry(self) -> dict:
        """NF-04：模块活动/削波指示（窗口端 30Hz 合并消费）。"""
        p = self._params
        return {
            "effect_type": self.effect_type,
            "ring_active": bool(p and p.enabled and p.ring_enabled),
            "grain_active": bool(p and p.enabled and p.grain_enabled),
            "dist_active": bool(p and p.enabled and p.dist_enabled),
            "frozen": bool(p and p.grain_freeze and self._grain._written >= self._grain._freeze_start
                           and self._grain._written > 0),
            "clip": bool(self.last_clip),
            "limiter_on": bool(p and p.output_limiter_enabled),
            "sample_position": int(self._samples_processed),
            "active_grains": sum(bool(v.active) for v in self._grain._voices) if p and p.grain_enabled else 0,
            "dropped_grains": int(self._grain.dropped_events),
            "capture_status": self._capture_status,
            "capture_ready": self._grain._written > 0,
            "pre_limiter_peak": self._pre_limiter_peak,
            "output_peak": self._output_peak,
        }

    def measure_output(self, samples: np.ndarray) -> dict:
        """Measure the bounded preview requested by a visible effect window."""
        result = self.collect_telemetry()
        n = samples.shape[-1]
        positions = np.linspace(0, n - 1, min(128, n), dtype=np.intp)
        result["waveform"] = samples[0, positions].tolist() if n and samples.shape[0] else []
        history = self._grain
        visible_history = max(1, min(history._written, history._cap))
        grains = []
        if result["grain_active"]:
            for voice in history._voices:
                if not voice.active:
                    continue
                age = (history._written - voice.read_pos - voice.phase * voice.ratio) / visible_history
                grains.append({"x": max(0.0, min(1.0, 1.0 - age)),
                               "y": -0.42 if voice.channel else 0.42,
                               "age": voice.phase / voice.length})
        result["grains"] = grains
        return result

    # ── 拓扑（SM-04；H07 dry 对齐）──
    def _module_wet(self, kind: str, x: np.ndarray, p) -> np.ndarray:
        if kind == "ring":
            return self._ring.process(x, p)
        if kind == "grain":
            return self._grain.process(x, p, self._samples_processed)
        return self._dist.process(x, p)

    def _module_latency(self, m: str) -> int:
        """模块算法时延（当前仅 dist 的 2× FIR 群延迟非零）。"""
        if m != "dist":
            return 0
        return self._dist.processing_latency(self._params.dist_curve, self._legacy_timing)

    @staticmethod
    def _align(lines: list[SampleDelayLine], x: np.ndarray, latency: int) -> np.ndarray:
        """Keep one alignment line per channel and reuse zero-delay input."""
        if latency <= 0:
            for line in lines:
                line.set_latency(0)
            return x
        while len(lines) < x.shape[0]:
            lines.append(SampleDelayLine())
        out = np.empty_like(x)
        for channel, values in enumerate(x):
            line = lines[channel]
            line.set_latency(latency)
            out[channel] = line.process(values)
        return out

    def _wet_latency(self, p) -> int:
        """拓扑 wet 分支总时延：串行=Σ启用模块，并联=max。"""
        return self._module_latency("dist") if p.dist_enabled else 0

    def _topology(self, x: np.ndarray, p) -> np.ndarray:
        enabled = [m for m in ("ring", "grain", "dist")
                   if getattr(p, f"{m}_enabled")]
        if not enabled:
            return x.copy()
        if p.routing == "parallel":
            # y = delay_max(x) + Σ mᵢ(Fᵢ(x) − dryᵢ)/N（启用但 mix=0 仍计
            # 入 N；H07：各支路 dry 按该模块时延对齐，直通分量也延迟到
            # 公共 max 时延——整条 wet 共享同一时间轴，mix 扫动无跳变）
            lat_max = self._wet_latency(p)
            acc = np.zeros_like(x)
            for m in enabled:
                lat_m = self._module_latency(m)
                dry = self._align(self._module_dry[m], x, lat_m)
                wet = self._module_wet(m, x, p)
                residual = wet - dry
                if not self._legacy_timing:
                    residual = self._align(self._par_residual[m], residual, lat_max - lat_m)
                acc += (getattr(p, f"{m}_mix_pct") / 100.0) * residual
            x_al = self._align(self._par_align, x, lat_max)
            return x_al + acc / float(len(enabled))
        # 串行：按 routing 顺序；每模块 dry 按模块时延对齐后做 mix
        order = {
            "ring_grain_dist": ("ring", "grain", "dist"),
            "grain_ring_dist": ("grain", "ring", "dist"),
            "dist_grain_ring": ("dist", "grain", "ring"),
        }[p.routing]
        cur = x
        for m in order:
            if m not in enabled:
                continue
            dry = self._align(self._module_dry[m], cur, self._module_latency(m))
            wet = self._module_wet(m, cur, p)
            cur = mix_aligned(dry, wet, getattr(p, f"{m}_mix_pct"))
        return cur

    def latency_samples(self) -> int:
        p = self._params
        if p is None or not p.enabled:
            return 0
        return self._wet_latency(p)

    def _rebuild(self) -> None:
        self._ring = _RingModule(self._sr)
        self._grain = _GrainModule(self._sr)
        self._grain.set_seed(self._seed)
        if self._installed_capture is not None:
            self._capture_status = "empty"
            self.install_freeze_capture(self._installed_capture)
        self._dist = _DistModule(self._sr)
        self._limiter = _MiniLimiter(self._sr)
        self._routing_fade = 0.0
        self._reset_delay_paths()

    def _reset_delay_paths(self) -> None:
        self._module_dry: dict[str, list[SampleDelayLine]] = {m: [] for m in ("ring", "grain", "dist")}
        self._par_residual: dict[str, list[SampleDelayLine]] = {m: [] for m in ("ring", "grain", "dist")}
        self._dry_align: list[SampleDelayLine] = []
        self._par_align: list[SampleDelayLine] = []
