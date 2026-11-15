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
    """VC-01/H05：双读头交叉淡化变速 pitch（Doppler 型）+ formant-link 补偿。

    H05 契约（复审 §5 H05）：
    - **有界环形**：物理容量 = win + 子块上限 + 边距（与整片时长无关；
      修前按累计写位扩容，float64 立体声 ~2.6GiB/小时的结构性增长）。
      块内按 ≤4096 子块推进，绝对读/写索引与物理槽位解耦（% 容量）。
    - **固定中心延迟**：read_pos 初始化在 w−win/2——r=1（0st 无 jitter）
      时 d 恒为 win/2，wet 延迟恒定；此时走精确直通旁路（mix 任意值
      只有同一对齐脉冲，修前 0st/50% 出现 sample0+5280 双脉冲）。
      latency 恒报 win/2（pitch 开启即报，含 0st——dry/旁路经时延线对齐）。
    - **抗混叠预滤波**：升调（r>1）时输入先经 fc=0.45·SR/r_max 的两级
      单极点低通再入环（折叠前滤除，修前 20kHz+12st 强折叠到 8kHz、
      后置低通无法删除）。
    - **Link 方向**：link=0 → 完全补偿重采样带来的包络上移（保原始谱
      包络）；link=100 → 不补偿（包络随 pitch）。修前 −st·link 与契约
      相反。fine cents 计入。
    """

    _WINDOW_MS = 220.0
    _SUB_CHUNK = 4096
    _XFADE = 720  # 绕点交叉淡化窗（15ms @48k，按采样率换算）

    def __init__(self, sample_rate: float) -> None:
        self._sr = float(sample_rate)
        self._win = max(64, int(self._WINDOW_MS * 0.001 * self._sr))
        cap = self._win + self._SUB_CHUNK + 16
        self._cap = cap
        self._bufs = [np.zeros(cap, dtype=np.float64) for _ in range(2)]
        # 写计数逐声道（共享会把 ch1 写到 ch0 之后的绝对位——立体声时
        # 延迟语义完全错位）
        self._writes = [0, 0]
        # 读头初始在原点后 win/2（负绝对位）：delay = w−read_pos ≈ +win/2
        # （读后于写；修前 +win/2 使 delay 为负 → 开流即后跳 → 双脉冲）
        self._read_pos = [-float(self._win / 2.0), -float(self._win / 2.0)]
        self._old_read = [0.0, 0.0]
        self._fade_total = max(8, int(self._XFADE * self._sr / 48000.0))
        self._fade_left = [0, 0]
        # 预滤波（每声道两级单极点，状态独立）
        self._pre_zi: list = [[0.0] * 4, [0.0] * 4]
        self._post_zi: list = [[0.0], [0.0]]
        self._jitter_ph1 = 0.0
        self._jitter_ph2 = 0.0
        self._seed = None
        # 急切创建：延迟报告在首个 process 前就须含 warp 的 WOLA 时延
        #（惰性创建使首块 dry 少对齐 1024 → wet/dry 双脉冲）。
        self._link_warper: Optional[_FormantModule] = _FormantModule(self._sr)
        self._glide_target_log = None   # H05：Glide 目标（log 域）
        self._glide_from_log = 0.0      # 目标变更时的起始值
        self._glide_started = 0         # 目标变更时的绝对样本位
        self._glide_cur_log = 0.0       # 当前有效 log-ratio（glide 平滑）

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
        self._read_pos = [-float(self._win / 2.0), -float(self._win / 2.0)]
        self._old_read = [0.0, 0.0]
        self._fade_left = [0, 0]
        self._pre_zi = [[0.0] * 4, [0.0] * 4]
        self._post_zi = [[0.0], [0.0]]
        self._glide_target_log = None
        self._glide_from_log = 0.0
        self._glide_started = 0
        self._glide_cur_log = 0.0
        if self._link_warper is not None:
            self._link_warper.reset()

    @property
    def latency(self) -> int:
        """wet 基线延迟（读头中心）——pitch 开启即报（含 0st）。"""
        return self._win // 2

    def _pre_filter(self, x: np.ndarray, fc: float, ch: int) -> np.ndarray:
        """两级单极点低通（抗混叠；fc=0 表示旁路）。"""
        if fc <= 0.0:
            return x.astype(np.float64, copy=True)
        a = float(np.exp(-2.0 * np.pi * fc / self._sr))
        b0 = 1.0 - a
        y = x.astype(np.float64, copy=True)
        from scipy.signal import lfilter as _lf
        for stage in range(4):
            y, zf = _lf(np.array([b0]), np.array([1.0, -a]), y,
                        zi=[self._pre_zi[ch][stage]])
            self._pre_zi[ch][stage] = float(zf[0])
        return y

    def _ratio(self, p, n: int, abs_start: int) -> np.ndarray:
        log_r = (float(np.clip(p.pitch_semitones, -24.0, 24.0)) / 12.0
                 + float(np.clip(p.pitch_fine_cents, -100.0, 100.0)) / 1200.0)
        t = (abs_start + np.arange(n)) / self._sr
        amp = (25.0 / 1200.0) * (float(np.clip(p.pitch_jitter_pct, 0, 100))
                                 / 100.0)
        jitter = amp * (np.sin(2 * np.pi * 0.6 * t + self._jitter_ph1)
                        + 0.5 * np.sin(2 * np.pi * 3.7 * t
                                       + self._jitter_ph2)) / 1.5
        # H05：Glide——log-ratio 向目标按 pitch_glide_ms 滑动（绝对流位置
        # 驱动，块长无关：目标变更时记 from/起始样本，进度 = 绝对推进量）
        glide_ms = float(np.clip(getattr(p, "pitch_glide_ms", 0.0) or 0.0,
                                 0.0, 2000.0))
        if self._glide_target_log is None or abs(log_r - self._glide_target_log) > 1e-12:
            self._glide_from_log = self._glide_target_log if                 self._glide_target_log is not None else log_r
            self._glide_target_log = log_r
            self._glide_started = abs_start
        if glide_ms <= 0.0:
            self._glide_cur_log = log_r
            glide = np.full(n, log_r)
        else:
            glide_n = max(1.0, glide_ms * 0.001 * self._sr)
            prog = np.clip((abs_start + np.arange(n)
                            - self._glide_started) / glide_n, 0.0, 1.0)
            glide = (self._glide_from_log * (1.0 - prog)
                     + self._glide_target_log * prog)
            self._glide_cur_log = float(glide[-1])
        return np.exp(glide * np.log(2.0) + jitter)

    def process(self, x2d: np.ndarray, p, abs_start: int) -> np.ndarray:
        return np.vstack([self._process_mono(x2d[0], p, abs_start, 0),
                          self._process_mono(x2d[1], p, abs_start, 1)])

    def _process_mono(self, x: np.ndarray, p, abs_start: int, ch: int = 0) -> np.ndarray:
        n = len(x)
        if n == 0:
            return np.zeros(0)
        buf = self._bufs[ch]
        cap = self._cap
        out = np.empty(n, dtype=np.float64)

        # 抗混叠预滤波：升调时按 r_max 折叠前滤除（与声道无关的截止）
        log_r_base = (float(np.clip(p.pitch_semitones, -24.0, 24.0)) / 12.0
                      + float(np.clip(p.pitch_fine_cents,
                                      -100.0, 100.0)) / 1200.0)
        jit_amp = (25.0 / 1200.0) * (float(np.clip(p.pitch_jitter_pct,
                                                   0, 100)) / 100.0)
        r_max = 2.0 ** (log_r_base + jit_amp)
        fc_pre = (min(18000.0, 0.45 * self._sr / r_max) if r_max > 1.001
                  else 0.0)
        xf = self._pre_filter(x, fc_pre, ch)

        pos = 0
        while pos < n:
            m = min(self._SUB_CHUNK, n - pos)
            seg = xf[pos:pos + m]
            wpos = self._writes[ch] % cap
            end = wpos + m
            if end <= cap:
                buf[wpos:end] = seg
            else:
                k = cap - wpos
                buf[wpos:] = seg[:k]
                buf[:m - k] = seg[k:]
            write_before = self._writes[ch]
            self._writes[ch] += m

            ratio = self._ratio(p, m, abs_start + pos)
            win = float(self._win)

            # H13：向量化读取（逐样本 Python 循环曾占整链块预算 ~42%）。
            # Catmull-Rom 四抽头 gather；段内非 fade 全向量，fade 窗内双
            # 头亦向量（sin/cos 等和权重）。跳点只在延迟越界的采样发生，
            # 段切分按跳点/ fade 边界推进。
            def _read_vec(positions: np.ndarray) -> np.ndarray:
                p0 = np.floor(positions)
                frac = positions - p0
                i0 = p0.astype(np.int64) % cap
                im1 = (i0 - 1) % cap
                i1 = (i0 + 1) % cap
                i2 = (i0 + 2) % cap
                y0 = buf[im1]
                y1 = buf[i0]
                y2 = buf[i1]
                y3 = buf[i2]
                return (y1 + 0.5 * frac * (
                    y2 - y0
                    + frac * (2.0 * y0 - 5.0 * y1 + 4.0 * y2 - y3
                              + frac * (3.0 * (y1 - y2) + y3 - y0))))

            j = 0
            while j < m:
                if self._fade_left[ch] > 0:
                    # fade 段：向量处理到 fade 结束/子块尾
                    f = min(self._fade_left[ch], m - j)
                    g_end = self._fade_left[ch]
                    idx = np.arange(f, dtype=np.float64)
                    prog = (g_end - idx) / float(self._fade_total)
                    w_old = np.sin(0.5 * np.pi * prog) ** 2
                    w_new = np.cos(0.5 * np.pi * prog) ** 2
                    adv = np.cumsum(ratio[j:j + f]) - ratio[j]
                    old_pos = self._old_read[ch] + adv
                    new_pos = self._read_pos[ch] + adv
                    out[pos + j:pos + j + f] = (
                        _read_vec(old_pos) * w_old
                        + _read_vec(new_pos) * w_new)
                    self._old_read[ch] = float(old_pos[-1]) + float(ratio[j + f - 1])
                    self._read_pos[ch] = float(new_pos[-1]) + float(ratio[j + f - 1])
                    self._fade_left[ch] -= f
                    j += f
                    continue
                # 干净段：直到延迟越界或子块尾
                adv = np.cumsum(ratio[j:j + m - j]) - ratio[j]
                positions = self._read_pos[ch] + adv
                w_abs = write_before + j + np.arange(m - j, dtype=np.float64)
                delay = w_abs - (positions + float(ratio[j + m - j - 1]))
                # 近似：用段末延迟判定（延迟漂移率 = 1−r ≤ 1/样本）
                seg = m - j
                # 找首个越界采样（延迟超 [win/4, 3win/4]）
                delays = w_abs - positions
                over = np.flatnonzero((delays > 0.75 * win)
                                      | (delays < 0.25 * win))
                if over.size:
                    k = int(over[0])
                    seg = k  # 该采样走跳点分支
                if seg > 0:
                    out[pos + j:pos + j + seg] = _read_vec(positions[:seg])
                    if seg > 1:
                        self._read_pos[ch] = float(positions[seg - 1]) \
                            + float(ratio[j + seg - 1])
                    else:
                        self._read_pos[ch] = float(positions[0])
                    j += seg
                    continue
                # 跳点：本采样先按旧位读出，再移位并开 fade
                out[pos + j] = _read_vec(
                    np.array([self._read_pos[ch]]))[0]
                delay_now = (write_before + j) - self._read_pos[ch]
                self._read_pos[ch] += float(ratio[j])
                if delay_now > 0.75 * win:
                    self._old_read[ch] = self._read_pos[ch]
                    self._read_pos[ch] += 0.5 * win
                    self._fade_left[ch] = self._fade_total
                elif delay_now < 0.25 * win:
                    self._old_read[ch] = self._read_pos[ch]
                    self._read_pos[ch] -= 0.5 * win
                    self._fade_left[ch] = self._fade_total
                j += 1
            pos += m

        r_eff_max = r_max
        if r_eff_max > 1.05:
            from scipy.signal import lfilter as _lf
            fc = min(18000.0, 0.45 * self._sr / r_eff_max)
            a = float(np.exp(-2.0 * np.pi * fc / self._sr))
            out, zf = _lf(np.array([1.0 - a]), np.array([1.0, -a]), out,
                          zi=[self._post_zi[ch][0]])
            self._post_zi[ch] = [float(zf[0])]

        # H05：Link 契约——0=完全补偿（保原始包络），100=不补偿（随 pitch）
        link = float(np.clip(p.pitch_formant_link_pct, 0.0, 100.0)) / 100.0
        if link < 1.0 - 1e-3:
            if self._link_warper is None:
                self._link_warper = _FormantModule(self._sr)
            st_total = log_r_base * 12.0  # semitones（含 cents）
            link_params = _dc_replace(
                p, formant_shift_st=-st_total * (1.0 - link),
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
        self._syn_ratio = 1.0
        self._bb = []
        self._ba = []
        self._sbb = []
        self._sba = []
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

    def _ensure_bands(self, n_bands: int, syn_ratio: float = 1.0) -> None:
        """分析/合成频带系数（H06：合成轴可独立于载波按 formant_st 缩放）。"""
        if n_bands == self._n_bands and syn_ratio == self._syn_ratio:
            return
        centers = log_spaced_bands(n_bands, 150.0, 8000.0, self._sr)
        self._bb, self._ba = [], []
        for c in centers:
            b, a = biquad_bandpass_coeffs(float(c), _VOC_Q, self._sr)
            self._bb.append(b)
            self._ba.append(a)
        # H06：vocoder_formant_st —— 合成频带中心整体缩放（声码器"嗓音
        # 共振"位移；不改载波基频）。上限受 Nyquist 钳制。
        self._sbb, self._sba = [], []
        syn_lo = 80.0
        syn_hi = self._sr * 0.45
        for c in centers:
            sc = min(max(float(c) * syn_ratio, syn_lo), syn_hi)
            b, a = biquad_bandpass_coeffs(sc, _VOC_Q, self._sr)
            self._sbb.append(b)
            self._sba.append(a)
        self._n_bands = n_bands
        self._syn_ratio = syn_ratio
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
        syn_ratio = float(2.0 ** (float(np.clip(
            getattr(p, "vocoder_formant_st", 0.0), -12.0, 12.0)) / 12.0))
        self._ensure_bands(n_bands, syn_ratio)
        # H06：清音门控——按输入高频占比决定 unvoiced 残差注入量
        # （纯浊音不再被无条件叠加高通嘶声）
        hp_share = 0.0
        for ch in range(x.shape[0]):
            seg = x[ch]
            if seg.size >= 16:
                X = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
                fr = np.fft.rfftfreq(seg.size, 1.0 / self._sr)
                hi = float(np.sum(X[fr > 4000.0] ** 2))
                tot = float(np.sum(X ** 2)) + 1e-12
                hp_share = max(hp_share, hi / tot)
        unvoiced_gate = float(np.clip(hp_share * 3.0, 0.0, 1.0))
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
                sb, sa_ = self._sbb[bi], self._sba[bi]
                csyn, zfs = lfilter(sb, sa_, carriers[ch],
                                    zi=self._syn_zi[bi, ch])
                self._syn_zi[bi, ch] = zfs
                out[ch] += csyn * env
        phase_step = (2.0 * np.pi * float(np.clip(
            p.vocoder_carrier_hz, 50.0, 440.0)) * n / self._sr)
        self._carrier_phases = (self._carrier_phases + phase_step
                                * np.array([1.0, 1.003, 0.997])) % (2.0 * np.pi)
        out /= max(1.0, np.sqrt(n_bands) * 0.35)
        unv = (float(np.clip(p.vocoder_unvoiced_pct, 0, 100)) / 100.0
               * unvoiced_gate)
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


#: H06：pitch_tracking 模式 → F0 分析范围（vocal=人声域，wide=扩展域）
_F0_RANGES = {"vocal": (70.0, 500.0), "wide": (50.0, 1500.0)}


def _estimate_f0(x: np.ndarray, sr: float, lo: float = 70.0,
                 hi: float = 500.0) -> tuple:
    """自相关 F0 估计（单声道；返回 (f0_hz|None, 检测置信度 0..1)）。

    H06：
    - 八度保护——在峰 ≥ 0.9·max 的候选里取**最长 lag**（基频而非谐波；
      修前固定 70–500Hz 只看左声道，880/1760Hz 输入都报 ~440 谐波陷阱）；
    - ``confidence`` 是**检测置信度**（归一化自相关），不是语言可懂度
      或声音质量百分比；
    - 范围由 pitch_tracking 决定（vocal 70–500 / wide 50–1500）。
    """
    seg = x[-int(0.05 * sr):] if len(x) > int(0.05 * sr) else x
    if len(seg) < 256:
        return None, 0.0
    seg = seg - np.mean(seg)
    energy = float(np.mean(seg ** 2))
    if energy < 1e-8:
        return None, 0.0
    # H13：FFT 自相关（O(n log n)）；np.correlate O(n²) 曾占块预算 ~5%
    nfft = 1 << int(np.ceil(np.log2(2 * len(seg))))
    spec = np.fft.rfft(seg, nfft)
    ac_full = np.fft.irfft(spec * np.conj(spec), nfft)
    ac = ac_full[:len(seg)]
    ac /= max(float(ac[0]), 1e-12)
    lag_lo = max(1, int(sr / hi))
    lag_hi = min(len(ac) - 1, int(sr / lo))
    if lag_hi <= lag_lo:
        return None, 0.0
    band = ac[lag_lo:lag_hi + 1]
    # 局部极大检测（斜肩穿越会造成 ~7% 高估——只认真峰）
    if band.size < 3:
        return None, 0.0
    is_peak = (band[1:-1] > band[:-2]) & (band[1:-1] >= band[2:])
    peaks = np.flatnonzero(is_peak) + 1
    if peaks.size == 0:
        return None, 0.0
    vals = band[peaks]
    peak = float(np.max(vals))
    # 八度保护：≥0.9·max 的局部极大里取最短 lag（域内最高频基频；
    # 纯周期信号的多重周期峰是自相关固有歧义，wide 模式扩真值域）
    strong = peaks[vals >= 0.9 * peak]
    sel = int(strong[0]) if strong.size else int(peaks[int(np.argmax(vals))])
    lag = lag_lo + sel
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
        # 遥测（NF-04）：F0 in/out 与电平（30Hz 合并由窗口端负责）。
        # H06：参考声道按能量选择（修前恒用左声道——仅右声道人声报
        # 无声）；分析范围由 pitch_tracking 决定。
        self._last_in_rms = float(np.sqrt(np.mean(x[0] ** 2))) if n else 0.0
        f0_lo, f0_hi = _F0_RANGES.get(
            str(getattr(p, "pitch_tracking", "vocal")), _F0_RANGES["vocal"])
        ref_ch = 0
        if x.shape[0] > 1:
            e0 = float(np.mean(x[0, -int(0.05 * self._sr):] ** 2))
            e1 = float(np.mean(x[1, -int(0.05 * self._sr):] ** 2))
            ref_ch = 1 if e1 > e0 * 1.5 else 0
        f0, conf = _estimate_f0(x[ref_ch], self._sr, lo=f0_lo, hi=f0_hi)
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
            # H05：wet 基线延迟 = 读头中心 win/2（0st 也成立——r=1 时 d 恒
            # 定）；link<1 时补偿 warp 的 WOLA 时延叠加。
            p_link = (self._pitch._link_warper.latency
                      if (p.pitch_formant_link_pct < 100.0
                          and self._pitch._link_warper is not None) else 0)
            p_lat = self._pitch.latency + p_link
            self._dry_p[0].set_latency(p_lat)
            self._dry_p[1].set_latency(p_lat)
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
        """H03：尾音预算 = 报告时延（WOLA/FIFO 滞留内容的 flush 需求）。

        修前返回 0：末端脉冲滞留在 win 长度的 WOLA 队列里，drain 不喂零
        就永远丢失（复审测得"末端脉冲下一零块 0.50002，节点报 tail=0"）。
        """
        return self.latency_samples()

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
        if p.pitch_enabled:
            lat += self._pitch.latency
            if p.pitch_formant_link_pct < 100.0 \
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
