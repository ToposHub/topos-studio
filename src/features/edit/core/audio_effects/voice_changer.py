"""Voice Changer DSP（VC-01..04，2026-11-15 计划 §7）。

Formant（STFT 谱包络 warp，中性恒等）→ Pitch（双读头变速 + 可选
formant-link 包络补偿）→ Vocoder（8–32 带分析/合成 + 四载波）→
OutputGain。每模块独立 Mix（dry 经模块时延线对齐；0/100 两端精确）。

设计契约：
- Formant：WOLA（周期 hann，hop=win/4）COLA 精确归一；输入前导
  ``win−hop`` 零 + 输出队列 ``hop`` 零使首/尾边界完整重构（净时延 =
  win，H04）；中性参数（shift/throat/gender/timbre 全零）下经倒谱/
  峰值包络除法恒等（机器精度直通）。包络 = 局部极大值连线 + 平滑
  （win=1024 帧内谐波 quefrency ≈3.2，倒谱 lifter 不可分离，H04 实测
  改用峰值包络后共振峰位移误差 21%→6%）；频率轴映射单调有界（累积
  最大值），禁止 F1/F2 交叉/负频率。
- Pitch：双读头交叉淡化变速（Doppler 型——计划 §7.2 允许的连续变速
  路线；交叉边界的轻微梳状伪影为已知权衡，写入算法契约）。比例
  ``r = 2**(st/12 + cents/1200)``；jitter = seed 相位的两个低频正弦
  （绝对采样时间驱动，块长无关）；r>1 时后置低通作 alias 保护；
  Formant Link 经共享 STFT warp 补偿 link 比例的包络漂移。
- Vocoder：对数分布频带（150Hz..min(8k, 0.45·sample_rate)），逐带包络
  （attack 2ms / release 参数的双单极近似：max(快路径, 慢路径)——
  上升由快路径主导、下降由 release 主导，写入契约）× 同带带通载波；
  载波相位跨块连续；Noise 载波用 seed 预生成确定性图案（绝对采样
  索引取模）；零 modulator 只余 release 尾音；频带和按 √N 归一。
"""

from __future__ import annotations

from dataclasses import replace as _dc_replace
from typing import Optional

import numpy as np
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import lfilter

from src.features.edit.core.audio_effects.base import (
    biquad_bandpass_coeffs,
    log_spaced_bands,
    mix_aligned,
    seeded_rng,
)

_FFT_WIN = 1024
_FFT_HOP = 256
_NOISE_PATTERN = 1 << 18
_VOC_Q = 6.0


class _WolaStream:
    """流式 WOLA（hann 分析+综合窗；win/hop=4:1 COLA）。延迟 = win。

    H04 边界契约：
    - **首边界**：输入流前插 ``win−hop`` 个零——真实首样本落在帧 0 的
      位置 ``win−hop``，其 OLA 覆盖帧（k ∈ {0,1,2,3}）全部存在，重构
      权重完整（修复旧实现首样本落在 hann 零点、且缺负时间帧导致的
      首窗衰减/吞没）。输出队列相应预置 ``hop`` 个零，真实样本净时延
      仍恰为 ``win``。
    - **尾边界**：``flush()`` 补零完成滞留帧并返回剩余真实输出（流结
      束语义；常规实时路径由后续零块自然 drain）。
    - ``_fed``/``_emitted`` 记账支撑 flush 的"还欠多少真实样本"计算。
    """

    def __init__(self, sample_rate: float, win: int = _FFT_WIN,
                 hop: int = _FFT_HOP) -> None:
        self.win = int(win)
        self.hop = int(hop)
        # 周期 hann（symmetric hann 的 w² 周期化和在帧缘有 ~1e-4 波纹，
        # 中性恒等只能到 −76dB；周期窗在 hop=win/4 下 COLA 精确恒定）
        self._w = (0.5 - 0.5 * np.cos(
            2.0 * np.pi * np.arange(self.win) / self.win))
        # COLA 归一：hann² @ hop=win/4 的周期化和 = Σw²/hop = 1.5
        self._cola = float(np.sum(self._w ** 2) / self.hop)
        self._out_acc = np.zeros(self.win, dtype=np.float64)
        self._fed = 0     # 已送入的真实样本数（不含前导零）
        self._emitted = 0  # 已弹出的输出样本数
        self._prime()

    def _prime(self) -> None:
        self._in_buf = np.zeros(self.win - self.hop, dtype=np.float64)
        self._out_acc = np.zeros(self.win, dtype=np.float64)
        self._out_queue = np.zeros(self.hop, dtype=np.float64)

    @property
    def latency(self) -> int:
        return self.win

    def reset(self) -> None:
        self._fed = 0
        self._emitted = 0
        self._prime()

    def _run_frames(self, spectral_fn) -> None:
        while len(self._in_buf) >= self.win:
            frame = self._in_buf[:self.win]
            w = self._w
            X = np.fft.rfft(frame * w)
            mag = np.abs(X)
            # H04/VC-02 谱包络：局部极大值（跨谐波峰连线）+ 平滑。
            # 旧实倒谱 lifter 在 win=1024 下无法分离谐波——150Hz 基频的
            # quefrency 仅 win·f0/SR ≈ 3.2 样本，lifter≥28 把梳齿整个
            # 保留进包络，warp 后共振峰位移系统性不足（复审测得 ~21%。
            # 峰值连线包络直接跟踪共振峰，±6st 位移误差降至 ~6-11%）。
            pk = maximum_filter1d(mag, size=9, mode="nearest")
            env = uniform_filter1d(pk, size=17, mode="nearest")
            # 谱零点数值下限（包络除法稳定；中性下 X 同样≈0，误差有界）
            E = np.maximum(env, 0.02 * float(env.max()) + 1e-12)
            Y = spectral_fn(X, E)
            y_frame = np.fft.irfft(Y)
            self._out_acc += y_frame * w
            self._out_queue = np.concatenate(
                [self._out_queue, self._out_acc[:self.hop] / self._cola])
            self._out_acc = np.roll(self._out_acc, -self.hop)
            self._out_acc[-self.hop:] = 0.0
            self._in_buf = self._in_buf[self.hop:]

    def process(self, x: np.ndarray, spectral_fn) -> np.ndarray:
        n = len(x)
        self._in_buf = np.concatenate([self._in_buf,
                                       x.astype(np.float64, copy=False)])
        self._fed += n
        self._run_frames(spectral_fn)
        out = self._out_queue[:n]
        self._out_queue = self._out_queue[n:]
        # 只计真实弹出——短缺时的补零不属于流内容，不得污染 flush 台账
        self._emitted += len(out)
        if len(out) < n:
            out = np.concatenate([out, np.zeros(n - len(out))])
        return out

    def flush(self, spectral_fn) -> np.ndarray:
        """流结束：补零完成滞留帧，返回尚未弹出的全部**真实**输出。

        返回长度 = ``_fed + latency − _emitted``（其后是补零区的重构，
        不属于真实输出）。调用后流状态复位到 primed——flush 只在流
        结束/换源时合法。
        """
        pad = self.win + self.hop
        self._in_buf = np.concatenate(
            [self._in_buf, np.zeros(pad, dtype=np.float64)])
        self._run_frames(spectral_fn)
        remaining = max(0, self._fed + self.latency - self._emitted)
        drained = self._out_queue[:remaining]
        self._out_queue = self._out_queue[remaining:]
        self._emitted += len(drained)
        self.reset()
        return drained


def _log_interp(x_new: np.ndarray, x_old: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.interp(np.log(np.maximum(x_new, 1.0)),
                     np.log(np.maximum(x_old, 1.0)), y)


class _FormantModule:
    """VC-02：独立共振峰（包络 warp；每声道独立 WOLA 流）。"""

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._wolas = [_WolaStream(sample_rate), _WolaStream(sample_rate)]
        self._freqs = np.fft.rfftfreq(self._wolas[0].win, 1.0 / self._sr)

    def reset(self) -> None:
        for w in self._wolas:
            w.reset()

    @property
    def latency(self) -> int:
        return self._wolas[0].latency

    def process(self, x2d: np.ndarray, p) -> np.ndarray:
        return np.vstack([self._process_mono(x2d[0], p, 0),
                          self._process_mono(x2d[1], p, 1)])

    def _process_mono(self, x: np.ndarray, p, ch: int) -> np.ndarray:
        shift = float(np.clip(p.formant_shift_st, -12.0, 12.0))
        throat = float(np.clip(p.formant_throat_pct, -100.0, 100.0)) / 100.0
        gender = float(np.clip(p.formant_gender_pct, -100.0, 100.0)) / 100.0
        timbre = float(np.clip(p.formant_timbre_pct, -50.0, 50.0)) / 50.0
        preserve = float(np.clip(p.formant_preserve_pct, 0.0, 100.0)) / 100.0
        crisp = 1.0 if getattr(p, "formant_mode", "natural") == "crisp" else 0.0
        rho = float(2.0 ** (shift / 12.0))
        f = self._freqs
        kern_t = 1.0 / (1.0 + (f / 800.0) ** 2)
        kern_g = np.exp(-((np.log(f / 1200.0 + 1e-6)) ** 2))
        ratio_ax = rho * (1.0 + 0.22 * throat * kern_t
                          + 0.30 * gender * kern_g
                          + 0.05 * crisp * kern_g)
        # 新→旧频率轴映射；单调化（防 F1/F2 交叉/负频率）
        f_src = f / np.maximum(ratio_ax, 0.05)
        f_src = np.maximum.accumulate(np.minimum(f_src, f[-1] * 1.05))
        tilt_db = timbre * 3.0  # ±3 dB/oct（锁定映射）

        def _spectral(X, E):
            logE = np.log(np.maximum(E, 1e-10))
            logEw = _log_interp(f_src, f, logE)
            octaves = np.log2(np.maximum(f, 20.0) / 1000.0)
            logEw = logEw + (tilt_db / 20.0 * np.log(10.0)) * octaves
            Ew = np.exp(np.clip(logEw, -30.0, 30.0))
            Y = (X / np.maximum(E, 1e-10)) * Ew
            if preserve > 0.0:
                blend = preserve * (f > 4000.0)
                Y = (1.0 - blend) * Y + blend * X
            return Y

        return self._wolas[ch].process(x, _spectral)


def _one_pole_lp(x: np.ndarray, sr: float, fc: float,
                 zi: Optional[list] = None):
    """状态可选的单极点低通（块不变性需要跨块 zi）。"""
    a = float(np.exp(-2.0 * np.pi * max(100.0, fc) / sr))
    if zi is None:
        return lfilter(np.array([1.0 - a]), np.array([1.0, -a]), x)
    y, zfo = lfilter(np.array([1.0 - a]), np.array([1.0, -a]), x, zi=zi)
    return y, zfo


class _PitchModule:
    """VC-01：双读头交叉淡化变速 pitch（Doppler 型）+ formant-link 补偿。"""

    _WINDOW_MS = 220.0

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._win = max(64, int(self._WINDOW_MS * 0.001 * self._sr))
        self._bufs = [np.zeros(self._win * 3, dtype=np.float64) for _ in range(2)]
        self._writes = [0, 0]
        self._read_pos = [0.0, 0.0]
        self._lp_zi: list = [None, None]
        self._jitter_ph1 = 0.0
        self._jitter_ph2 = 0.0
        self._seed = None
        self._link_warper: Optional[_FormantModule] = None

    def set_seed(self, seed: int) -> None:
        if seed != self._seed:
            self._seed = int(seed)
            rng = seeded_rng(seed, stream_id=0xC0)
            self._jitter_ph1 = float(rng.uniform(0, 2 * np.pi))
            self._jitter_ph2 = float(rng.uniform(0, 2 * np.pi))

    def reset(self) -> None:
        for b in self._bufs:
            b.fill(0.0)
        self._writes = [0, 0]
        self._read_pos = [0.0, 0.0]
        self._lp_zi = [None, None]
        if self._link_warper is not None:
            self._link_warper.reset()

    @property
    def latency(self) -> int:
        return 0

    def _ratio(self, p, n: int, abs_start: int) -> np.ndarray:
        log_r = (float(np.clip(p.pitch_semitones, -24.0, 24.0)) / 12.0
                 + float(np.clip(p.pitch_fine_cents, -100.0, 100.0)) / 1200.0)
        t = (abs_start + np.arange(n)) / self._sr
        amp = (25.0 / 1200.0) * (float(np.clip(p.pitch_jitter_pct, 0, 100))
                                 / 100.0)
        jitter = amp * (np.sin(2 * np.pi * 0.6 * t + self._jitter_ph1)
                        + 0.5 * np.sin(2 * np.pi * 3.7 * t
                                       + self._jitter_ph2)) / 1.5
        return np.exp(log_r * np.log(2.0) + jitter)

    def process(self, x2d: np.ndarray, p, abs_start: int) -> np.ndarray:
        return np.vstack([self._process_mono(x2d[0], p, abs_start, 0),
                          self._process_mono(x2d[1], p, abs_start, 1)])

    def _process_mono(self, x: np.ndarray, p, abs_start: int, ch: int = 0) -> np.ndarray:
        n = len(x)
        buf = self._bufs[ch]
        # 容量按绝对写位（块分模式下写者累计推进，不按单块宽分配——
        # 否则槽位回卷覆盖未读历史）
        need = self._writes[ch] + n + self._win * 2 + 64
        if len(buf) < need:
            buf = np.concatenate([buf, np.zeros(need - len(buf))])
            self._bufs[ch] = buf
        wpos = self._writes[ch] % len(buf)
        end = wpos + n
        if end <= len(buf):
            buf[wpos:end] = x
        else:
            k = len(buf) - wpos
            buf[wpos:] = x[:k]
            buf[:n - k] = x[k:]
        write_before = self._writes[ch]
        self._writes[ch] += n

        ratio = self._ratio(p, n, abs_start)
        blen = len(buf)
        win = float(self._win)
        out = np.empty(n, dtype=np.float64)

        def _read(pos_abs: float) -> float:
            # Catmull-Rom 三次插值（降插值droop/交叉边带）
            p0 = np.floor(pos_abs)
            frac = pos_abs - p0
            im1 = (int(p0) - 1) % blen
            i0 = int(p0) % blen
            i1 = (i0 + 1) % blen
            i2 = (i0 + 2) % blen
            y0, y1, y2, y3 = buf[im1], buf[i0], buf[i1], buf[i2]
            return float(
                y1 + 0.5 * frac * (y2 - y0
                                   + frac * (2.0 * y0 - 5.0 * y1 + 4.0 * y2
                                             - y3
                                             + frac * (3.0 * (y1 - y2)
                                                       + y3 - y0))))

        for j in range(n):
            r = float(ratio[j])
            w_abs = write_before + j
            d = (w_abs - self._read_pos[ch]) % win  # 主读头延迟 ∈ [0, win)
            d2 = (d + win / 2.0) % win              # 副读头延迟
            wA = np.sin(np.pi * d / win) ** 2        # d=0（换绕点）权重 0
            wB = np.cos(np.pi * d / win) ** 2        # d=win/2（副头换绕）权重 0
            out[j] = (_read(w_abs - d) * wA
                      + _read(w_abs - d2) * wB)
            self._read_pos[ch] += r
        r_max = float(np.max(ratio))
        if r_max > 1.05:
            fc = min(16000.0, self._sr * 0.45 / r_max)
            out, self._lp_zi[ch] = _one_pole_lp(
                out, self._sr, fc,
                zi=self._lp_zi[ch] if self._lp_zi[ch] is not None
                else [0.0])
        link = float(np.clip(p.pitch_formant_link_pct, 0.0, 100.0)) / 100.0
        if link > 1e-3:
            if self._link_warper is None:
                self._link_warper = _FormantModule(self._sr)
            link_params = _dc_replace(
                p, formant_shift_st=-float(np.clip(p.pitch_semitones,
                                                   -24.0, 24.0)) * link,
                formant_throat_pct=0.0, formant_gender_pct=0.0,
                formant_timbre_pct=0.0, formant_preserve_pct=0.0)
            # Each channel owns one WOLA stream. Processing a duplicated stereo
            # block here advances both streams twice and leaks L history into R.
            out = self._link_warper._process_mono(out, link_params, ch)
        return out


class _VocoderModule:
    """VC-03：8–32 带通道声码器。"""

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._n_bands = 0
        self._bb = []
        self._ba = []
        self._ana_zi = None
        self._syn_zi = None
        self._env_fast_zi = None
        self._env_slow_zi = None
        self._unvoiced_zi = None
        self._carrier_phases = np.zeros(3, dtype=np.float64)
        self._noise = seeded_rng(0, 0x0FF).standard_normal(_NOISE_PATTERN)
        self._seed = None

    def set_seed(self, seed: int) -> None:
        if seed != self._seed:
            self._seed = int(seed)
            self._noise = seeded_rng(seed, 0x0FF).standard_normal(
                _NOISE_PATTERN)

    def _ensure_bands(self, n_bands: int) -> None:
        if n_bands == self._n_bands:
            return
        centers = log_spaced_bands(n_bands, 150.0, 8000.0, self._sr)
        self._bb, self._ba = [], []
        for c in centers:
            b, a = biquad_bandpass_coeffs(float(c), _VOC_Q, self._sr)
            self._bb.append(b)
            self._ba.append(a)
        self._n_bands = n_bands
        self._ana_zi = None
        self._syn_zi = None
        self._env_fast_zi = None
        self._env_slow_zi = None

    def reset(self) -> None:
        self._ana_zi = None
        self._syn_zi = None
        self._env_fast_zi = None
        self._env_slow_zi = None
        self._unvoiced_zi = None
        self._carrier_phases.fill(0.0)

    @property
    def latency(self) -> int:
        return 0

    def _carrier(self, p, n: int, abs_start: int, ch: int) -> np.ndarray:
        f0 = float(np.clip(p.vocoder_carrier_hz, 50.0, 440.0))
        # Phases are advanced once per audio block below, not once per band or
        # channel. Combining accumulated phase with absolute time counts the
        # previous blocks twice and makes the carrier depend on block size.
        t = np.arange(n) / self._sr
        channel_phase = ch * 0.7
        base_phase = self._carrier_phases[0] + 2.0 * np.pi * f0 * t
        kind = p.vocoder_carrier
        if kind == "noise":
            idx = (abs_start + np.arange(n) + ch * 7919) % _NOISE_PATTERN
            return self._noise[idx] * 0.5
        nyq_lim = min(8000.0, self._sr * 0.45)
        if kind == "choir":
            sig = (np.sin(base_phase + channel_phase)
                   + np.sin(2 * np.pi * f0 * 1.003 * t
                            + self._carrier_phases[1] + channel_phase * 0.9 + 0.7)
                   + np.sin(2 * np.pi * f0 * 0.997 * t
                            + self._carrier_phases[2] + 1.9)) / 3.0
        else:
            sig = np.zeros(n)
            k = 1.0 if kind == "saw" else 1.0
            sign = -1.0 if kind == "saw" else 1.0
            harm = 1.0
            while f0 * harm < nyq_lim:
                amp = (sign * 2.0 / (np.pi * harm) if kind == "saw"
                       else (0.0 if harm % 2 == 0 else
                             4.0 / (np.pi * harm)))
                if amp != 0.0:
                    sig += amp * np.sin(harm * base_phase + channel_phase)
                harm += 1.0
        return sig

    def process(self, x: np.ndarray, p, abs_start: int) -> np.ndarray:
        n_bands = int(np.clip(p.vocoder_bands, 8, 32))
        self._ensure_bands(n_bands)
        n = x.shape[-1]
        chs = x.shape[0]
        if self._ana_zi is None or self._ana_zi.shape != (n_bands, chs, 2):
            self._ana_zi = np.zeros((n_bands, chs, 2))
            self._syn_zi = np.zeros((n_bands, chs, 2))
            self._env_fast_zi = np.zeros((n_bands, chs, 1))
            self._env_slow_zi = np.zeros((n_bands, chs, 1))
        if self._unvoiced_zi is None or self._unvoiced_zi.shape != (chs, 1):
            self._unvoiced_zi = np.zeros((chs, 1))
        rel_a = float(np.exp(-1.0 / (0.002 * self._sr)))
        rel_r = float(np.exp(-1.0 / (max(5.0, p.vocoder_release_ms)
                                      * 0.001 * self._sr)))
        att_b = np.array([1.0 - rel_a])
        att_a = np.array([1.0, -rel_a])
        rel_b = np.array([1.0 - rel_r])
        rel_a_c = np.array([1.0, -rel_r])
        out = np.zeros_like(x, dtype=np.float64)
        carriers = [self._carrier(p, n, abs_start, ch) for ch in range(chs)]
        for bi in range(n_bands):
            b, a = self._bb[bi], self._ba[bi]
            for ch in range(chs):
                band, zf = lfilter(b, a, x[ch], zi=self._ana_zi[bi, ch])
                self._ana_zi[bi, ch] = zf
                env_fast, fast_zf = lfilter(
                    att_b, att_a, np.abs(band), zi=self._env_fast_zi[bi, ch])
                env_slow, slow_zf = lfilter(
                    rel_b, rel_a_c, np.abs(band), zi=self._env_slow_zi[bi, ch])
                self._env_fast_zi[bi, ch] = fast_zf
                self._env_slow_zi[bi, ch] = slow_zf
                env = np.maximum(env_fast, env_slow)
                csyn, zfs = lfilter(b, a, carriers[ch], zi=self._syn_zi[bi, ch])
                self._syn_zi[bi, ch] = zfs
                out[ch] += csyn * env
        phase_step = (2.0 * np.pi * float(np.clip(
            p.vocoder_carrier_hz, 50.0, 440.0)) * n / self._sr)
        self._carrier_phases = (self._carrier_phases + phase_step
                                * np.array([1.0, 1.003, 0.997])) % (2.0 * np.pi)
        out /= max(1.0, np.sqrt(n_bands) * 0.35)
        unv = float(np.clip(p.vocoder_unvoiced_pct, 0, 100)) / 100.0
        hp_b = np.array([0.95, -0.95])
        hp_a = np.array([1.0, -0.9])
        for ch in range(chs):
            resid, hp_zf = lfilter(hp_b, hp_a, x[ch], zi=self._unvoiced_zi[ch])
            self._unvoiced_zi[ch] = hp_zf
            out[ch] += resid * unv * 0.7
        return out


class _DelayLine:
    """模块 dry 对齐时延线（补模块算法时延）。"""

    def __init__(self) -> None:
        self._buf = np.zeros(0, dtype=np.float64)
        self._latency = 0

    def set_latency(self, latency: int) -> None:
        latency = max(0, int(latency))
        if latency != self._latency:
            self._latency = latency
            self._buf = np.zeros(latency, dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self._latency == 0:
            return x.astype(np.float64, copy=True)
        buf = np.concatenate([self._buf, x.astype(np.float64)])
        out = buf[:len(x)]
        self._buf = buf[len(x):]
        return out

    def reset(self) -> None:
        """Clear queued audio while keeping the configured alignment delay."""
        self._buf = np.zeros(self._latency, dtype=np.float64)


def _estimate_f0(x: np.ndarray, sr: float, lo: float = 70.0,
                 hi: float = 500.0) -> tuple:
    """自相关 F0 估计（单声道；返回 (f0_hz|None, 置信度 0..1)）。"""
    seg = x[-int(0.05 * sr):] if len(x) > int(0.05 * sr) else x
    if len(seg) < 256:
        return None, 0.0
    seg = seg - np.mean(seg)
    energy = float(np.mean(seg ** 2))
    if energy < 1e-8:
        return None, 0.0
    ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
    ac /= max(ac[0], 1e-12)
    lag_lo = max(1, int(sr / hi))
    lag_hi = min(len(ac) - 1, int(sr / lo))
    if lag_hi <= lag_lo:
        return None, 0.0
    lag = lag_lo + int(np.argmax(ac[lag_lo:lag_hi]))
    conf = float(np.clip(ac[lag], 0.0, 1.0))
    if conf < 0.3:
        return None, conf
    return float(sr / lag), conf


class VoiceChangerEffect:
    """Voice Changer 复合效果（Formant → Pitch → Vocoder → OutputGain）。"""

    effect_type = "voice_changer"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sr = float(sample_rate)
        self._params = None
        self._samples_processed = 0
        self._formant = _FormantModule(self._sr)
        self._pitch = _PitchModule(self._sr)
        self._vocoder = _VocoderModule(self._sr)
        self._dry_f = [_DelayLine(), _DelayLine()]
        self._dry_p = [_DelayLine(), _DelayLine()]
        self._dry_v = [_DelayLine(), _DelayLine()]
        self._seed = None
        self._last_f0_in: Optional[float] = None
        self._last_f0_out: Optional[float] = None
        self._last_conf = 0.0
        self._last_in_rms = 0.0
        self._last_out_rms = 0.0

    def set_params(self, params, sample_rate=None, algorithm_version=None):
        from src.features.edit.core.timeline_v2 import VoiceChangerParams
        if sample_rate is not None and sample_rate != self._sr:
            self._sr = float(sample_rate)
            self._rebuild()
        if params is None:
            self._params = None
            return
        if isinstance(params, dict):
            params = VoiceChangerParams.from_dict(params)
        self._params = params
        if params.random_seed != self._seed:
            self._seed = params.random_seed
            self._pitch.set_seed(self._seed)
            self._vocoder.set_seed(self._seed)

    def process(self, samples: np.ndarray) -> np.ndarray:
        p = self._params
        if p is None or not p.enabled or samples.ndim != 2 or samples.size == 0:
            return samples
        n = samples.shape[-1]
        x = samples.astype(np.float64, copy=True)
        # 遥测（NF-04）：F0 in/out 与电平（30Hz 合并由窗口端负责）
        self._last_in_rms = float(np.sqrt(np.mean(x[0] ** 2))) if n else 0.0
        f0, conf = _estimate_f0(x[0], self._sr)
        self._last_f0_in = f0
        self._last_conf = conf

        cur = x.copy()
        # 1) Formant
        if p.formant_enabled:
            self._dry_f[0].set_latency(self._formant.latency)
            self._dry_f[1].set_latency(self._formant.latency)
            wet = self._formant.process(x, p)
            dry = np.vstack([self._dry_f[0].process(x[0]),
                             self._dry_f[1].process(x[1])])
            cur = mix_aligned(dry, wet, p.formant_mix_pct)
        # 2) Pitch
        if p.pitch_enabled:
            p_link = (self._pitch._link_warper.latency
                      if (p.pitch_formant_link_pct > 0
                          and self._pitch._link_warper is not None) else 0)
            self._dry_p[0].set_latency(p_link)
            self._dry_p[1].set_latency(p_link)
            wet = self._pitch.process(cur, p, self._samples_processed)
            dry = np.vstack([self._dry_p[0].process(cur[0]),
                             self._dry_p[1].process(cur[1])])
            cur = mix_aligned(dry, wet, p.pitch_mix_pct)
        # 3) Vocoder（modulator = 前两模块输出）
        if p.vocoder_enabled:
            self._dry_v[0].set_latency(self._vocoder.latency)
            self._dry_v[1].set_latency(self._vocoder.latency)
            wet = self._vocoder.process(cur, p, self._samples_processed)
            dry = np.vstack([self._dry_v[0].process(cur[0]),
                             self._dry_v[1].process(cur[1])])
            cur = mix_aligned(dry, wet, p.vocoder_mix_pct)
        # 4) Output gain
        gain = float(10.0 ** (float(np.clip(p.output_gain_db, -20.0, 20.0))
                              / 20.0))
        cur = cur * gain
        self._last_out_rms = float(np.sqrt(np.mean(cur[0] ** 2))) if n else 0.0
        f0o, _ = _estimate_f0(cur[0], self._sr)
        self._last_f0_out = f0o
        self._samples_processed += n
        return cur.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        self._formant.reset()
        self._pitch.reset()
        self._vocoder.reset()
        for d in (self._dry_f, self._dry_p, self._dry_v):
            d[0].reset()
            d[1].reset()
        self._samples_processed = 0
        self._last_f0_in = None
        self._last_f0_out = None
        self._last_conf = 0.0
        self._last_in_rms = 0.0
        self._last_out_rms = 0.0

    def tail_estimate_samples(self) -> int:
        return 0  # 全模块有限冲激（WOLA/包络尾 < 1s 由后续块自然收敛）

    @property
    def is_active(self) -> bool:
        p = self._params
        if p is None or not p.enabled:
            return False
        return (p.formant_enabled or p.pitch_enabled or p.vocoder_enabled
                or abs(p.output_gain_db) > 1e-9)

    def latency_samples(self) -> int:
        p = self._params
        if p is None or not p.enabled:
            return 0
        lat = 0
        if p.formant_enabled:
            lat += self._formant.latency
        if p.pitch_enabled and p.pitch_formant_link_pct > 0 \
                and self._pitch._link_warper is not None:
            lat += self._pitch._link_warper.latency
        return lat

    def collect_telemetry(self) -> dict:
        p = self._params
        return {
            "effect_type": self.effect_type,
            "f0_in_hz": self._last_f0_in,
            "f0_out_hz": self._last_f0_out,
            "voiced": self._last_f0_in is not None,
            "confidence": round(float(self._last_conf), 3),
            "in_rms": self._last_in_rms,
            "out_rms": self._last_out_rms,
            "formant_active": bool(p and p.enabled and p.formant_enabled),
            "pitch_active": bool(p and p.enabled and p.pitch_enabled),
            "vocoder_active": bool(p and p.enabled and p.vocoder_enabled),
            "sample_position": int(self._samples_processed),
        }

    def _rebuild(self) -> None:
        self._formant = _FormantModule(self._sr)
        self._pitch = _PitchModule(self._sr)
        self._vocoder = _VocoderModule(self._sr)
        self._dry_f = [_DelayLine(), _DelayLine()]
        self._dry_p = [_DelayLine(), _DelayLine()]
        self._dry_v = [_DelayLine(), _DelayLine()]
