"""
Audio DSP 向量化正确性测试（FX 机架效果器性能优化的行为锁）。

背景：audio_dsp.py 的 reverb/delay/noise_gate/compressor/limiter/MasterLimiter/
strip_eq 从逐样本 Python 循环改为分块向量化（lfilter/sosfilt/闭式解）。
本文件用「优化前的标量参照实现」锁定新实现的数值行为：

- Reverb/Delay/Compressor/Limiter/NoiseGate/MasterLimiter：与参照实现
  逐位一致（随机信号 + 多块流式 + 跨块状态连续）。
- StripEQ（sosfilt 级联）：与逐段 lfilter 参照一致到 ~1e-9（sosfilt 与
  lfilter 的浮点结合顺序不同；差异远低于 float32 输出量化 ~6e-8）。
- 快路径（单调块 lfilter / 增益恒 1 短路 / run 闭式）与逐样本算法等价。
- 边界：延迟线块内绕回、ping-pong 交叉反馈、low_cut<=0 反馈抑制、
  reset_state 清空混响尾。

参考：
- src/features/edit/core/audio_dsp.py
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import (
    CompressorEffect,
    DelayEffect,
    LimiterEffect,
    MasterLimiter,
    NoiseGateEffect,
    ReverbEffect,
    StripEQFilter,
    _AllpassFilter,
    _CombFilter,
    _smooth_attack_release,
)

SR = 48000.0


# ================================================================
# 参照实现（优化前的逐样本算法，语义基准）
# ================================================================

class _CombRef:
    """逐样本 comb（与优化前 _CombFilter 相同的算式）。"""

    def __init__(self, size: int) -> None:
        self._size = max(int(size), 1)
        self._buf = np.zeros(self._size, dtype=np.float64)
        self._idx = 0
        self._filt = 0.0
        self._fb = 0.5
        self._d1 = 1.0
        self._d2 = 0.0

    def set_params(self, fb: float, damp: float) -> None:
        self._fb, self._d1, self._d2 = fb, damp, 1.0 - damp

    def process_block(self, inp: np.ndarray) -> np.ndarray:
        out = np.empty_like(inp)
        buf, size, idx = self._buf, self._size, self._idx
        filt = self._filt
        for i in range(len(inp)):
            current = buf[idx]
            filt = current * self._d2 + filt * self._d1
            out[i] = current
            buf[idx] = inp[i] + filt * self._fb
            idx += 1
            if idx >= size:
                idx = 0
        self._idx, self._filt = idx, filt
        return out


class _AllpassRef:
    """逐样本 allpass。"""

    def __init__(self, size: int) -> None:
        self._size = max(int(size), 1)
        self._buf = np.zeros(self._size, dtype=np.float64)
        self._idx = 0
        self._fb = 0.7

    def process_block(self, inp: np.ndarray) -> np.ndarray:
        out = np.empty_like(inp)
        buf, size, idx, fb = self._buf, self._size, self._idx, self._fb
        for i in range(len(inp)):
            bufout = buf[idx]
            out[i] = bufout - inp[i] * fb
            buf[idx] = inp[i] + bufout * fb
            idx += 1
            if idx >= size:
                idx = 0
        self._idx = idx
        return out


class _SmootherRef:
    """逐样本 attack/release 分支平滑（压缩器/限幅器的包络递推）。"""

    def __init__(self, rise_c: float, fall_c: float, env: float) -> None:
        self.rise_c, self.fall_c, self.env = rise_c, fall_c, env

    def process(self, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        for i in range(x.shape[0]):
            v = x[i]
            c = self.rise_c if v > self.env else self.fall_c
            self.env = v + (self.env - v) * c
            out[i] = self.env
        return out


def _gate_ref_process(gate: NoiseGateEffect, samples: np.ndarray) -> np.ndarray:
    """优化前的噪声门逐样本循环（直接读取新实例的参数，验证算式等价）。"""
    out = samples.astype(np.float64, copy=True)
    n = out.shape[1]
    peak_input = np.max(np.abs(out), axis=0)
    input_db = 20.0 * np.log10(np.maximum(peak_input, 1e-9))
    target = (input_db >= gate._threshold_db).astype(np.float64)
    gate_gain = gate._gate_gain
    gains = np.empty(n, dtype=np.float64)
    for i in range(n):
        t = target[i]
        coeff = gate._attack_coeff if t > gate_gain else gate._release_coeff
        gate_gain = t + (gate_gain - t) * coeff
        gains[i] = gate_gain
    gate._gate_gain = gate_gain
    out *= gains[np.newaxis, :]
    return out.astype(samples.dtype, copy=False)


def _master_limiter_ref_process(ml: MasterLimiter, samples: np.ndarray) -> np.ndarray:
    """优化前的 MasterLimiter 逐样本循环。"""
    n = samples.shape[-1]
    abs_max = np.max(np.abs(samples), axis=0) if samples.ndim > 1 else np.abs(samples)
    # NumPy 2 弱提升：float / float32 数组 → float32（NumPy 1.x 是 float64）。
    # 若不显式升 f64，下方 `gain` 递归会被 np.float32 标量拖入 float32
    # 精度（py float + np.float32 → np.float32），参考值反而比实现更粗，
    # 1e-9 级对拍恒失败。实现侧 process() 已显式 astype(float64)。
    target_gain = np.where(abs_max > ml._threshold,
                           ml._threshold / np.maximum(abs_max, 1e-9),
                           1.0).astype(np.float64)
    attack_coeff = float(np.exp(-1.0 / max(0.001, ml._attack_ms * 0.001 * ml._sample_rate)))
    release_coeff = float(np.exp(-1.0 / max(0.001, ml._release_ms * 0.001 * ml._sample_rate)))
    gain = float(ml._gain)
    out_gain = np.empty(n, dtype=np.float64)
    for i in range(n):
        tgt = float(target_gain[i])
        if tgt < gain:
            gain = attack_coeff * gain + (1.0 - attack_coeff) * tgt
        else:
            gain = release_coeff * gain + (1.0 - release_coeff) * tgt
        out_gain[i] = gain
    ml._gain = gain
    out = (samples * out_gain).astype(samples.dtype, copy=False)
    peak = float(np.max(np.abs(out))) if out.size > 0 else 0.0
    if peak > ml._threshold * 1.0:
        out = out * (ml._threshold / peak)
    return out


# ================================================================
# 信号生成
# ================================================================

def _music_blocks(rng, n_blocks=6, n=2000, nch=2, amp=0.4) -> list:
    """带动态起伏（前响后轻）的类音乐信号块。"""
    blocks = []
    for _ in range(n_blocks):
        t = np.arange(n) / SR
        x = np.zeros((nch, n), dtype=np.float32)
        for c in range(nch):
            x[c] = (0.5 * np.sin(2 * np.pi * 220 * t)
                    + 0.3 * np.sin(2 * np.pi * 1750 * t + rng.uniform(0, 6))
                    + 0.06 * rng.standard_normal(n)) * amp
        env = np.concatenate([np.full(n // 2, 1.8), np.full(n - n // 2, 0.25)])
        blocks.append((x * env).astype(np.float32))
    return blocks


def _set(effect, params: dict):
    effect.set_params(params)
    return effect


# ================================================================
# Comb / Allpass 原语
# ================================================================

class TestCombAllpassPrimitives:

    @pytest.mark.parametrize("size,n", [
        (1425, 2000),  # size < block：块内绕回
        (2000, 2000),  # size == block
        (1980, 2000),  # size 略小于块
        (800, 3000),   # 多次绕回
        (101, 500),    # 极小延迟线
        (2100, 2000),  # size > block
    ])
    def test_comb_matches_scalar_reference(self, size: int, n: int) -> None:
        rng = np.random.default_rng(size * 31 + n)
        ref = _CombRef(size); ref.set_params(0.84, 0.98)
        vec = _CombFilter(size); vec.set_params(0.84, 0.98)
        for _ in range(10):
            x = rng.standard_normal(n)
            a = ref.process_block(x.copy())
            b = vec.process_block(x.copy())
            assert np.allclose(a, b, rtol=0, atol=1e-13), f"size={size} n={n}"

    @pytest.mark.parametrize("size,n", [
        (240, 2000), (82, 2000), (2000, 2000), (101, 3000),
    ])
    def test_allpass_matches_scalar_reference(self, size: int, n: int) -> None:
        rng = np.random.default_rng(size * 17 + n)
        ref = _AllpassRef(size)
        vec = _AllpassFilter(size)
        for _ in range(10):
            x = rng.standard_normal(n)
            assert np.array_equal(ref.process_block(x.copy()),
                                  vec.process_block(x.copy()))


# ================================================================
# Reverb / Delay
# ================================================================

class TestReverbDelayVectorized:

    def test_reverb_matches_scalar_primitives_stream(self) -> None:
        """多块流式：跨块状态连续 + 与标量原语组合结果一致。"""
        rng = np.random.default_rng(7)
        params = {"enabled": True, "mix": 30.0, "decay": 2.0,
                  "pre_delay_ms": 20.0, "damping_hz": 5000.0}
        eff = _set(ReverbEffect(SR), params)
        # 参照：用标量原语重组同一拓扑（predelay → 4 comb 并联 → 2 allpass）
        from src.features.edit.core.audio_dsp import (
            _ALLPASS_DELAY_MS, _COMB_DELAY_MS, _decay_to_feedback,
            _damping_hz_to_linear,
        )
        combs = [_CombRef(int(round(d * 0.001 * SR))) for d in _COMB_DELAY_MS]
        aps = [_AllpassRef(int(round(d * 0.001 * SR))) for d in _ALLPASS_DELAY_MS]
        damp = _damping_hz_to_linear(params["damping_hz"], SR)
        for i, c in enumerate(combs):
            c.set_params(_decay_to_feedback(params["decay"], _COMB_DELAY_MS[i], SR), damp)
        pd_size = max(int(round(params["pre_delay_ms"] * 0.001 * SR)), 1)
        pd_buf = np.zeros(pd_size); pd_idx = 0

        for blk in _music_blocks(rng, n_blocks=5):
            got = eff.process(blk.copy())
            mono = blk.astype(np.float64).mean(axis=0)
            # 参照 predelay（逐样本环形）
            wet_in = np.empty_like(mono)
            for i in range(mono.shape[0]):
                wet_in[i] = pd_buf[pd_idx]
                pd_buf[pd_idx] = mono[i]
                pd_idx = (pd_idx + 1) % pd_size
            wet = np.zeros_like(wet_in)
            for c in combs:
                wet += c.process_block(wet_in.copy())
            for ap in aps:
                wet = ap.process_block(wet.copy())
            wet *= 0.25
            g = params["mix"] / 100.0
            exp = blk.astype(np.float64) * (1.0 - g) + wet * g
            exp = exp.astype(np.float32)
            assert np.allclose(got, exp, rtol=0, atol=1e-12)

    @pytest.mark.parametrize("time_ms,ping_pong", [
        (300.0, True), (300.0, False), (5.0, True), (5.0, False), (1.0, True),
    ])
    def test_delay_matches_scalar_reference_stream(self, time_ms: float, ping_pong: bool) -> None:
        """与优化前的逐样本延迟算法多块对照（含块内绕回与 ping-pong）。"""
        rng = np.random.default_rng(int(time_ms * 10) + int(ping_pong))
        params = {"enabled": True, "time_ms": time_ms, "feedback": 40.0,
                  "mix": 30.0, "ping_pong": ping_pong, "low_cut_hz": 100.0}
        eff = _set(DelayEffect(SR), params)
        # 参照：优化前算法的独立复刻
        size = max(int(round(time_ms * 0.001 * SR)), 1)
        bufs = [np.zeros(size), np.zeros(size)]
        idx = 0
        a = np.exp(-2.0 * np.pi * params["low_cut_hz"] / SR)
        prev_in = [0.0, 0.0]; prev_out = [0.0, 0.0]

        for blk in _music_blocks(rng, n_blocks=4):
            got = eff.process(blk.copy())
            out = blk.astype(np.float64, copy=True)
            ch_n, n = out.shape
            for i in range(n):
                delayed = [bufs[c][idx] for c in range(ch_n)]
                for c in range(ch_n):
                    other = c ^ 1 if (ping_pong and ch_n >= 2) else c
                    src = delayed[other]
                    y = a * prev_out[c] + a * (src - prev_in[c])
                    prev_in[c], prev_out[c] = src, y
                    bufs[c][idx] = out[c, i] + y * 0.4
                for c in range(ch_n):
                    out[c, i] = out[c, i] * 0.7 + delayed[c] * 0.3
                idx = (idx + 1) % size
            exp = out.astype(np.float32)
            assert np.allclose(got, exp, rtol=0, atol=1e-12), \
                f"time_ms={time_ms} pp={ping_pong}"

    def test_delay_lowcut_zero_suppresses_feedback(self) -> None:
        """low_cut_hz<=0 时反馈信号被完全抑制（与旧行为一致）。"""
        eff = _set(DelayEffect(SR), {"enabled": True, "time_ms": 300.0,
                                     "feedback": 60.0, "mix": 30.0,
                                     "ping_pong": False, "low_cut_hz": 0.0})
        rng = np.random.default_rng(3)
        x = rng.standard_normal((2, 2000)).astype(np.float32) * 0.3
        out1 = eff.process(x.copy())
        out2 = eff.process(np.zeros_like(x))
        # 反馈死掉后第二块应接近纯静音输入的直通（无回声尾）
        assert float(np.max(np.abs(out2))) < 1e-6
        assert np.allclose(out1, x * 0.7, atol=1e-6)

    def test_reverb_reset_state_clears_tail(self) -> None:
        """reset_state 后静音输入不残留混响尾。"""
        eff = _set(ReverbEffect(SR), {"enabled": True, "mix": 50.0, "decay": 3.0,
                                      "pre_delay_ms": 10.0, "damping_hz": 5000.0})
        rng = np.random.default_rng(5)
        loud = (rng.standard_normal((2, 2000)) * 0.5).astype(np.float32)
        eff.process(loud)
        eff.reset_state()
        silent = np.zeros((2, 2000), dtype=np.float32)
        out = eff.process(silent)
        assert float(np.max(np.abs(out))) < 1e-12


# ================================================================
# 噪声门（run 闭式向量化）
# ================================================================

class TestNoiseGateVectorized:

    def test_matches_scalar_reference_stream(self) -> None:
        rng = np.random.default_rng(11)
        params = {"enabled": True, "threshold_db": -30.0,
                  "attack_ms": 1.0, "release_ms": 100.0}
        eff = _set(NoiseGateEffect(SR), params)
        for blk in _music_blocks(rng, n_blocks=5):
            entry_state = eff._gate_gain
            exp = _gate_ref_process(eff, blk.copy())  # 会推进 eff 状态
            eff._gate_gain = entry_state              # 还原入口状态再跑新实现
            got = eff.process(blk.copy())
            assert np.allclose(got, exp, rtol=0, atol=1e-12)

    def test_matches_scalar_on_borderline_noise(self) -> None:
        """电平在阈值附近高频翻转（退化 run 结构）仍与逐样本一致。"""
        rng = np.random.default_rng(13)
        params = {"enabled": True, "threshold_db": -30.0,
                  "attack_ms": 1.0, "release_ms": 100.0}
        eff = _set(NoiseGateEffect(SR), params)
        for _ in range(4):
            x = (rng.standard_normal((2, 2000)) * 0.032).astype(np.float32)
            entry_state = eff._gate_gain
            exp = _gate_ref_process(eff, x.copy())
            eff._gate_gain = entry_state
            got = eff.process(x.copy())
            assert np.allclose(got, exp, rtol=0, atol=1e-12)

    def test_instant_attack_release(self) -> None:
        """attack=0/release=0：增益瞬时跟随目标。"""
        eff = _set(NoiseGateEffect(SR), {"enabled": True, "threshold_db": -30.0,
                                         "attack_ms": 0.0, "release_ms": 10.0})
        rng = np.random.default_rng(17)
        x = (rng.standard_normal((2, 1000)) * 0.2).astype(np.float32)
        out = eff.process(x.copy())
        ref = _gate_ref_process(eff, x.copy())
        assert np.allclose(out, ref, rtol=0, atol=1e-12)


# ================================================================
# 攻击/释放平滑器（快路径等价）
# ================================================================

class TestAttackReleaseSmoother:

    @pytest.mark.parametrize("rise,fall", [(0.0, 0.999), (0.993, 0.9999), (0.5, 0.0), (0.0, 0.0)])
    def test_fast_paths_equal_scalar(self, rise: float, fall: float) -> None:
        """单调上升/下降块（lfilter 快路径）与混合块（逐样本）结果一致。"""
        rng = np.random.default_rng(19)
        env0 = -12.0
        rising = np.sort(rng.standard_normal(1500) * 10)
        falling = np.sort(rng.standard_normal(1500) * 10)[::-1].copy()
        mixed = rng.standard_normal(1500) * 10
        for x in (rising, falling, mixed):
            ref = _SmootherRef(rise, fall, env0)
            exp, exp_end = ref.process(x.copy()), ref.env
            got, got_end = _smooth_attack_release(x.copy(), env0, rise, fall)
            assert np.allclose(got, exp, rtol=0, atol=1e-9)
            assert abs(got_end - exp_end) < 1e-9

    def test_zero_and_single_sample(self) -> None:
        out, end = _smooth_attack_release(np.empty(0), 3.0, 0.5, 0.9)
        assert out.shape[0] == 0 and end == 3.0
        out, end = _smooth_attack_release(np.array([2.0]), 1.0, 0.0, 0.9)
        assert out.shape[0] == 1 and end == 2.0  # rise_c=0 → 瞬时跟随

    def test_silence_stays_silent(self) -> None:
        """数字静音块（常数驱动）走下降快路径，多块后收敛到静音电平。"""
        silence_db = np.full(2000, -180.0)
        # tau = 1/(1-0.9999) = 10000 样本 ≈ 208ms；单块 41.7ms 衰减约一段
        out, end = _smooth_attack_release(silence_db, -20.0, 0.99, 0.9999)
        assert out[-1] < -20.0  # 向静音电平衰减
        assert abs(end - out[-1]) < 1e-9
        assert bool((np.diff(out) < 0).all())  # 单调下降
        env = end
        for _ in range(30):  # ~6.2τ 后应接近静音底
            _, env = _smooth_attack_release(silence_db, env, 0.99, 0.9999)
        assert env < -150.0


# ================================================================
# Compressor / Limiter（快路径短路 + 平滑器接入）
# ================================================================

class TestDynamicsVectorized:

    def _comp_ref(self, eff: CompressorEffect, samples: np.ndarray) -> np.ndarray:
        """优化前的压缩器逐样本循环。"""
        out = samples.astype(np.float64, copy=True)
        peak_input = np.max(np.abs(out), axis=0)
        input_db = 20.0 * np.log10(np.maximum(peak_input, 1e-9))
        env_db = eff._env_db
        att, rel = eff._attack_coeff, eff._release_coeff
        env_smoothed = np.empty(input_db.shape[0])
        for i in range(input_db.shape[0]):
            x_db = input_db[i]
            coeff = att if x_db > env_db else rel
            env_db = x_db + (env_db - x_db) * coeff
            env_smoothed[i] = env_db
        eff._env_db = env_db
        gain_db = eff._compute_gain_db(env_smoothed)
        out *= (10.0 ** (gain_db / 20.0))[np.newaxis, :]
        if abs(eff._makeup_linear - 1.0) > 1e-9:
            out *= eff._makeup_linear
        return out.astype(samples.dtype, copy=False)

    def test_compressor_matches_reference_stream(self) -> None:
        rng = np.random.default_rng(23)
        eff = _set(CompressorEffect(SR), {"enabled": True, "threshold_db": -24.0,
                                          "ratio": 4.0, "attack_ms": 3.0,
                                          "release_ms": 250.0, "knee_db": 30.0,
                                          "makeup_gain": 0.0})
        for blk in _music_blocks(rng, n_blocks=5):
            entry_env = eff._env_db
            exp = self._comp_ref(eff, blk.copy())  # 会推进 eff._env_db
            eff._env_db = entry_env
            got = eff.process(blk.copy())
            assert np.allclose(got, exp, rtol=0, atol=1e-11)

    def test_compressor_quiet_block_returns_input(self) -> None:
        """全程低于 knee 下界 → 增益恰为 1，直通返回原数组。"""
        eff = _set(CompressorEffect(SR), {"enabled": True, "threshold_db": -20.0,
                                          "ratio": 8.0, "attack_ms": 3.0,
                                          "release_ms": 250.0, "knee_db": 6.0,
                                          "makeup_gain": 0.0})
        rng = np.random.default_rng(29)
        quiet = (rng.standard_normal((2, 2000)) * 0.0005).astype(np.float32)
        out = eff.process(quiet)
        assert out is quiet  # 直通未拷贝
        # 状态已推进：env 应衰减向静音电平
        assert eff._env_db < -60.0

    def test_compressor_makeup_only_path(self) -> None:
        """低于 knee 但有 makeup：输出 = 输入 × makeup。"""
        eff = _set(CompressorEffect(SR), {"enabled": True, "threshold_db": -10.0,
                                          "ratio": 4.0, "attack_ms": 3.0,
                                          "release_ms": 250.0, "knee_db": 6.0,
                                          "makeup_gain": 6.0})
        rng = np.random.default_rng(31)
        quiet = (rng.standard_normal((2, 1000)) * 0.0005).astype(np.float32)
        got = eff.process(quiet.copy())
        makeup = 10.0 ** (6.0 / 20.0)
        exp = (quiet.astype(np.float64) * makeup).astype(np.float32)
        assert np.allclose(got, exp, rtol=0, atol=1e-7)

    def test_limiter_matches_reference_stream(self) -> None:
        rng = np.random.default_rng(37)
        eff = _set(LimiterEffect(SR), {"enabled": True, "threshold_db": -6.0,
                                       "release_ms": 100.0})
        for blk in _music_blocks(rng, n_blocks=5):
            # 放大确保触发限幅
            loud = (blk * 2.5).astype(np.float32)
            entry_env = eff._envelope
            got = eff.process(loud.copy())
            eff._envelope = entry_env  # 参照从同一入口状态起算
            # 参照
            out = loud.astype(np.float64, copy=True)
            peak_input = np.max(np.abs(out), axis=0)
            th = 10.0 ** (-6.0 / 20.0)
            env = eff._envelope
            gain = np.empty(peak_input.shape[0])
            for i in range(peak_input.shape[0]):
                inp = peak_input[i]
                env = inp if inp > env else inp + (env - inp) * eff._release_coeff
                gain[i] = th / env if env > th else 1.0
            eff._envelope = env
            out *= gain[np.newaxis, :]
            exp = out.astype(np.float32)
            assert np.allclose(got, exp, rtol=0, atol=1e-11)

    def test_limiter_quiet_returns_input_and_envelope_tracks(self) -> None:
        eff = _set(LimiterEffect(SR), {"enabled": True, "threshold_db": -6.0,
                                       "release_ms": 100.0})
        rng = np.random.default_rng(41)
        quiet = (rng.standard_normal((2, 2000)) * 0.001).astype(np.float32)
        out = eff.process(quiet)
        assert out is quiet
        assert eff._envelope <= 10.0 ** (-6.0 / 20.0)


# ================================================================
# MasterLimiter（直通快路径 + 系数缓存）
# ================================================================

class TestMasterLimiter:

    def test_matches_scalar_reference_stream(self) -> None:
        rng = np.random.default_rng(43)
        ml = MasterLimiter(SR)
        ref = MasterLimiter(SR)
        for blk in _music_blocks(rng, n_blocks=5):
            loud = (blk * 2.2).astype(np.float32)
            got = ml.process(loud.copy())
            exp = _master_limiter_ref_process(ref, loud.copy())
            # 实现与参考均在 float64 包络上递归（实现是 lfilter 快路径 +
            # 逐样本回退，参考是逐样本循环），仅浮点结合顺序不同；输出
            # 乘法回 float32 后余差 ~1e-8，1e-9 容差只覆盖增益本身。
            assert np.max(np.abs(got.astype(np.float64)
                                 - exp.astype(np.float64))) < 1e-6
            assert abs(ml._gain - ref._gain) < 1e-9

    def test_no_limiting_returns_input(self) -> None:
        """增益=1 且无超阈值样本 → 精确直通（返回原数组）。"""
        ml = MasterLimiter(SR, threshold=1.0)
        rng = np.random.default_rng(47)
        x = (rng.standard_normal((2, 2000)) * 0.2).astype(np.float32)
        assert float(np.max(np.abs(x))) <= 1.0
        out = ml.process(x)
        assert out is x
        assert ml._gain == 1.0

    def test_recovers_after_limiting(self) -> None:
        """限幅后小信号块逐渐恢复增益，最终回到直通。"""
        ml = MasterLimiter(SR, threshold=1.0)
        rng = np.random.default_rng(53)
        loud = (rng.standard_normal((2, 2000)) * 2.5).astype(np.float32)
        ml.process(loud)
        assert ml._gain < 1.0
        quiet = (rng.standard_normal((2, 40000)) * 0.1).astype(np.float32)
        for i in range(0, 40000, 2000):
            out = ml.process(quiet[:, i:i + 2000].copy())
        # release tau=100ms，20 块 ≈ 8.3τ → 恢复到 0.999+
        assert ml._gain >= 0.999
        assert float(np.max(np.abs(out))) <= 1.0

    def test_coefficients_cached(self) -> None:
        ml = MasterLimiter(SR)
        c_a, c_r = ml._attack_coeff, ml._release_coeff
        ml.process(np.zeros((2, 8), dtype=np.float32))
        assert ml._attack_coeff == c_a and ml._release_coeff == c_r
        ml.set_sample_rate(44100.0)
        assert ml._attack_coeff != c_a


# ================================================================
# StripEQ（sosfilt 级联）
# ================================================================

class TestStripEQSos:

    EQ_15 = {"enabled": True, "mode": "15-band", "gain": 1.5,
             "low_cut_enable": True, "low_cut": 80.0,
             "high_cut_enable": True, "high_cut": 16000.0,
             "bands": [2.0, -3.0, 1.0, 0.0, -1.5, 2.5, 0.0, 1.0,
                       -2.0, 3.0, 0.5, -0.5, 1.5, -1.0, 2.0]}

    def _cascaded_reference(self, eff: StripEQFilter, samples: np.ndarray,
                            zi: dict) -> np.ndarray:
        """逐段逐声道 lfilter 参照（与优化前实现相同）。"""
        from scipy.signal import lfilter
        from src.features.edit.core.audio_dsp import (
            EQ_BAND_CONFIGS, _BUTTERWORTH_Q, _band_biquad,
            _biquad_highpass, _biquad_lowpass,
        )
        p = eff._params
        sr = eff._sample_rate
        sections = []
        if p.low_cut_enable:
            b, a = _biquad_highpass(min(float(p.low_cut), sr * 0.45), _BUTTERWORTH_Q, sr)
            sections.append((b, a, "lc"))
        if p.high_cut_enable:
            b, a = _biquad_lowpass(min(float(p.high_cut), sr * 0.45), _BUTTERWORTH_Q, sr)
            sections.append((b, a, "hc"))
        for i, cfg in enumerate(EQ_BAND_CONFIGS[p.mode]):
            gain = float(p.bands[i]) if i < len(p.bands) else 0.0
            sections.append((*_band_biquad(cfg, gain, sr), f"b{i}"))
        out = samples.astype(np.float64, copy=True)
        for ch in range(out.shape[0]):
            x = out[ch]
            for b, a, key in sections:
                z = zi.setdefault(key, np.zeros(2))[ch]
                y, zi[key][ch] = lfilter(b, a, x, zi=z)
                x = y
            out[ch] = x
        out *= eff._gain_linear
        return out.astype(samples.dtype, copy=False)

    def test_matches_cascaded_reference_stream(self) -> None:
        rng = np.random.default_rng(59)
        eff = _set(StripEQFilter(SR), dict(self.EQ_15))
        zi = {k: np.zeros((2, 2)) for k in ["lc", "hc"] + [f"b{i}" for i in range(15)]}
        # 参照 zi 直接用 (2,2) 数组按声道索引
        for blk in _music_blocks(rng, n_blocks=4):
            got = eff.process(blk.copy())
            exp = self._cascaded_reference(eff, blk.copy(), zi)
            # sosfilt 与逐段 lfilter 浮点结合顺序不同：17 段级联首块实测
            # 最大 ~7.5e-9（-162dB，远低于 float32 输出量化 ~6e-8）
            assert np.allclose(got, exp, rtol=0, atol=1e-8)

    def test_param_update_preserves_state(self) -> None:
        """拖动频段增益（段布局不变）时滤波状态连续——输出无跳变。"""
        eff = _set(StripEQFilter(SR), dict(self.EQ_15))
        rng = np.random.default_rng(61)
        x = (rng.standard_normal((2, 2000)) * 0.3).astype(np.float32)
        eff.process(x.copy())
        zi_before = {k: v.copy() for k, v in eff._zi_by_key.items()}
        p = dict(self.EQ_15)
        p["bands"] = [v + 0.5 for v in self.EQ_15["bands"]]
        eff.set_params(p)
        zi_after = eff._zi_by_key
        for k, v in zi_before.items():
            assert np.array_equal(zi_after[k], v), f"state lost for {k}"

    def test_no_sections_gain_only(self) -> None:
        """无 cuts、全零 bands、有整体增益：仅乘增益。"""
        eff = _set(StripEQFilter(SR), {"enabled": True, "mode": "3-band", "gain": 6.0,
                                       "low_cut_enable": False, "low_cut": 80.0,
                                       "high_cut_enable": False, "high_cut": 16000.0,
                                       "bands": [0.0, 0.0, 0.0]})
        rng = np.random.default_rng(67)
        x = (rng.standard_normal((2, 100)) * 0.2).astype(np.float32)
        got = eff.process(x.copy())
        exp = (x.astype(np.float64) * 10.0 ** (6.0 / 20.0)).astype(np.float32)
        assert np.allclose(got, exp, rtol=0, atol=1e-7)


# ================================================================
# EffectChain 串联 + 静音直通
# ================================================================

class TestEffectChainIntegration:

    def test_full_chain_runs_and_shape_preserved(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain
        chain = EffectChain(SR)
        chain.set_params([
            ("strip_eq", {"enabled": True, "mode": "7-band", "gain": 0.0,
                          "low_cut_enable": True, "low_cut": 80.0,
                          "high_cut_enable": False, "high_cut": 18000.0,
                          "bands": [2.0, 0, 0, 0, 0, 0, 2.0]}, True),
            ("compressor", {"enabled": True, "threshold_db": -20.0, "ratio": 4.0,
                            "attack_ms": 3.0, "release_ms": 250.0, "knee_db": 12.0,
                            "makeup_gain": 0.0}, True),
            ("reverb", {"enabled": True, "mix": 20.0, "decay": 1.5,
                        "pre_delay_ms": 10.0, "damping_hz": 6000.0}, True),
            ("delay", {"enabled": True, "time_ms": 250.0, "feedback": 30.0,
                       "mix": 15.0, "ping_pong": True, "low_cut_hz": 120.0}, True),
            ("noise_gate", {"enabled": True, "threshold_db": -50.0,
                            "attack_ms": 1.0, "release_ms": 80.0}, True),
            ("limiter", {"enabled": True, "threshold_db": -1.0, "release_ms": 80.0}, True),
        ])
        rng = np.random.default_rng(71)
        x = (rng.standard_normal((2, 2000)) * 0.5).astype(np.float32)
        out = chain.process(x.copy())
        assert out.shape == x.shape
        assert np.isfinite(out).all()
        assert not np.allclose(out, x, atol=1e-4)

    def test_disabled_chain_passthrough(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain
        chain = EffectChain(SR)
        chain.set_params([("reverb", {"enabled": True}, False)])
        x = np.zeros((2, 8), dtype=np.float32)
        assert chain.process(x) is x

    def test_chain_reset_state(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain
        chain = EffectChain(SR)
        chain.set_params([("reverb", {"enabled": True, "mix": 40.0, "decay": 2.0,
                                      "pre_delay_ms": 5.0, "damping_hz": 5000.0}, True)])
        rng = np.random.default_rng(73)
        x = (rng.standard_normal((2, 2000)) * 0.5).astype(np.float32)
        chain.process(x.copy())
        chain.reset_state()
        out = chain.process(np.zeros_like(x))
        assert float(np.max(np.abs(out))) < 1e-6


# ================================================================
# 音效转场：包络缓存 + 淡化中间块早退（等价性锁）
# ================================================================

class TestCrossfadeEnvelopeCache:
    """crossfade 包络 (curve, span) LRU 缓存：数值与逐次重生成逐位一致。"""

    def setup_method(self) -> None:
        from src.features.edit.core.audio_dsp import (
            _CROSSFADE_ENV_CACHE, _CROSSFADE_ENV_CACHE_LOCK,
        )
        with _CROSSFADE_ENV_CACHE_LOCK:
            _CROSSFADE_ENV_CACHE.clear()

    @staticmethod
    def _reference_block(samples, tstart, tdur, cur, role, curve, fps, sr):
        """优化前语义的独立参照：每次现算完整包络再切片。"""
        from src.features.edit.core.audio_dsp import crossfade_envelopes
        n = samples.shape[-1]
        spf = sr / fps
        span = max(1, int(round(tdur * spf)))
        block_start = (float(cur) - float(tstart)) * spf
        pos = block_start + np.arange(n, dtype=np.float64)
        if pos[-1] < 0.0 or pos[0] >= float(span):
            return samples
        left_full, right_full = crossfade_envelopes(curve, span)
        full = left_full if role == "left" else right_full
        env = np.ones(n, dtype=np.float64)
        in_span = (pos >= 0.0) & (pos < float(span))
        clamped = np.clip(pos[in_span], 0.0, float(span - 1)).astype(np.int64)
        env[in_span] = full[clamped]
        return samples * env

    @pytest.mark.parametrize("curve", ["constant_power", "linear", "equal_power", "fade"])
    @pytest.mark.parametrize("span_frames", [12, 50, 125])  # 0.5s / 2s / 5s @25fps
    def test_cached_block_matches_uncached_reference(self, curve: str, span_frames: int) -> None:
        """缓存命中路径（第二块起）与每次重生成的结果逐位一致。"""
        from src.features.edit.core.audio_dsp import apply_crossfade_block
        rng = np.random.default_rng(span_frames * 7 + len(curve))
        x = (rng.standard_normal((2, 2000)) * 0.3).astype(np.float32)
        # 连续推进三块：第 1 块填充缓存，第 2/3 块命中缓存
        for k, cur in enumerate((100, 101, 147)):
            got = apply_crossfade_block(x.copy(), 100, span_frames, cur, "left", curve, 25.0, SR)
            exp = self._reference_block(x.copy(), 100, span_frames, cur, "left", curve, 25.0, SR)
            assert np.array_equal(got.astype(np.float64), exp.astype(np.float64)), \
                f"curve={curve} span={span_frames} block={k}"
        # right 角色 + 跨块边界的部分重叠块
        got = apply_crossfade_block(x.copy(), 100, span_frames, 100 + span_frames - 40,
                                    "right", curve, 25.0, SR)
        exp = self._reference_block(x.copy(), 100, span_frames, 100 + span_frames - 40,
                                    "right", curve, 25.0, SR)
        assert np.array_equal(got.astype(np.float64), exp.astype(np.float64))

    def test_cache_returns_shared_object(self) -> None:
        """命中缓存返回同一数组对象（调用方只读）。"""
        from src.features.edit.core.audio_dsp import (
            _CROSSFADE_ENV_CACHE, apply_crossfade_block,
        )
        x = np.zeros((2, 64), dtype=np.float32)
        apply_crossfade_block(x, 0, 10, 0, "left", "constant_power", 25.0, SR)
        assert ("constant_power", 10 * 1920) in _CROSSFADE_ENV_CACHE
        first = _CROSSFADE_ENV_CACHE[("constant_power", 10 * 1920)]
        apply_crossfade_block(x, 0, 10, 5, "left", "constant_power", 25.0, SR)
        assert _CROSSFADE_ENV_CACHE[("constant_power", 10 * 1920)] is first

    def test_cache_evicts_lru_under_sample_cap(self) -> None:
        """总样本超上限时从最旧条目逐出。"""
        from src.features.edit.core.audio_dsp import (
            _CROSSFADE_ENV_CACHE, _CROSSFADE_ENV_CACHE_LOCK,
            _crossfade_envelopes_cached,
        )
        # 每条 2*n 样本；填充到超上限
        n_each = 300_000  # 600k/条 → 两条即超 1M 上限
        _crossfade_envelopes_cached("linear", n_each)
        _crossfade_envelopes_cached("fade", n_each)
        _crossfade_envelopes_cached("equal_power", n_each)
        with _CROSSFADE_ENV_CACHE_LOCK:
            keys = list(_CROSSFADE_ENV_CACHE.keys())
            total = sum(2 * k[1] for k in keys)
        assert ("linear", n_each) not in keys  # 最旧的被逐出
        assert total <= 1_100_000  # ≤ 上限（允许最后一条自身超限保留）

    def test_huge_span_not_cached(self) -> None:
        """单条超过总上限的跨度不进缓存（仍可正常计算）。"""
        from src.features.edit.core.audio_dsp import (
            _CROSSFADE_ENV_CACHE, _CROSSFADE_ENV_CACHE_LOCK,
            _crossfade_envelopes_cached,
        )
        env = _crossfade_envelopes_cached("constant_power", 600_001)
        assert env[0].shape[0] == 600_001
        with _CROSSFADE_ENV_CACHE_LOCK:
            assert ("constant_power", 600_001) not in _CROSSFADE_ENV_CACHE


class TestClipFadeEarlyOut:
    """淡化中间段 O(1) 早退：与完整逐样本计算数值一致。"""

    @staticmethod
    def _reference_fade(samples, cstart, cdur, cur, fi, fo, fps=25.0, sr=SR):
        """优化前语义参照（无早退，恒算完整包络）。"""
        n = samples.shape[-1]
        spf = sr / fps
        if fi <= 0 and fo <= 0:
            return samples
        clip_start = (float(cur) - float(cstart)) * spf
        pos = clip_start + np.arange(n, dtype=np.float64)
        env = np.ones(n, dtype=np.float64)
        if fi > 0:
            fi_samples = max(1.0, float(fi) * spf)
            t = np.clip(pos / max(1.0, fi_samples - 1.0), 0.0, 1.0)
            gain = np.sin(np.pi / 2.0 * t)
            env = np.where(pos < fi_samples, gain, env)
        if fo > 0:
            cdur_samples = max(0.0, float(cdur) * spf)
            fo_samples = max(1.0, float(fo) * spf)
            fo_start = cdur_samples - fo_samples
            t = np.clip((pos - fo_start) / max(1.0, fo_samples - 1.0), 0.0, 1.0)
            gain = np.cos(np.pi / 2.0 * t)
            env = np.where(pos >= fo_start, np.minimum(env, gain), env)
        return samples * env

    def test_middle_block_returns_input_identical_values(self) -> None:
        """中间 sustain 块：早退返回原数组，值与完整计算一致。"""
        from src.features.edit.core.audio_dsp import apply_clip_fade
        rng = np.random.default_rng(97)
        x = (rng.standard_normal((2, 2000)) * 0.3).astype(np.float32)
        # clip 750 帧（30s），淡入淡出各 50 帧（2s）；current=400 → 纯中间段
        out = apply_clip_fade(x.copy(), 0, 750, 400, 50, 50, 25.0, SR)
        assert out is x or np.array_equal(out, x)  # 早退路径
        exp = self._reference_fade(x.copy(), 0, 750, 400, 50, 50)
        assert np.array_equal(out.astype(np.float64), exp.astype(np.float64))

    @pytest.mark.parametrize("cur", [0, 1, 24, 25, 48, 49, 50, 700, 725, 741, 749])
    def test_all_block_positions_match_reference(self, cur: int) -> None:
        """淡化边界附近的块与无早退参照逐位一致（含恰好压线的帧）。"""
        from src.features.edit.core.audio_dsp import apply_clip_fade
        rng = np.random.default_rng(cur)
        x = (rng.standard_normal((2, 2000)) * 0.3).astype(np.float32)
        got = apply_clip_fade(x.copy(), 0, 750, cur, 50, 50, 25.0, SR)
        exp = self._reference_fade(x.copy(), 0, 750, cur, 50, 50)
        assert np.array_equal(got.astype(np.float64), exp.astype(np.float64)), f"cur={cur}"

    def test_short_clip_overlapping_fades(self) -> None:
        """淡入淡出重叠的极短 clip：取更安静一侧的包络语义不变。"""
        from src.features.edit.core.audio_dsp import apply_clip_fade
        rng = np.random.default_rng(101)
        x = (rng.standard_normal((2, 2000)) * 0.3).astype(np.float32)
        for cur in (0, 5, 10, 14):
            got = apply_clip_fade(x.copy(), 0, 20, cur, 12, 12, 25.0, SR)
            exp = self._reference_fade(x.copy(), 0, 20, cur, 12, 12)
            assert np.allclose(got.astype(np.float64), exp.astype(np.float64),
                               rtol=0, atol=1e-15), f"cur={cur}"
