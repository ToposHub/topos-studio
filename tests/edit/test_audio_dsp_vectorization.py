"""DSP 向量化/状态连续性回归（CW-A1 重构版）。

背景：audio_dsp.py 的全部效果器都按块向量化（lfilter/sosfilt/np 分段），
并用跨块状态（IIR zi / 延迟线 / 包络标量）保持连续。本文件对每个效果器
验证「整块处理 == 分块处理」（块边界不产生可闻差异），并对 EffectChain
验证参数变化时的实例复用（DSP 状态不丢）。

旧版（graphic EQ / Schroeder reverb / ping-pong delay / NoiseGate 的
标量参照实现）已随 CW-A1 参数化重写退役；等价性断言由本文件的块分割
等价测试 + test_audio_dsp.py 的跨块连续性测试共同承担。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    StripEQFilter,
    LimiterEffect,
    CompressorEffect,
    ReverbEffect,
    DelayEffect,
    SaturationEffect,
    ImagerEffect,
    ExpanderEffect,
    EffectChain,
)
from src.features.edit.core.timeline_v2 import default_eq_bands_flat

SR = 48000.0


def _sine(freq: float, n: int, amp: float = 0.3) -> np.ndarray:
    t = np.arange(n) / SR
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, sig])


def _noise(n: int, amp: float = 0.3, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((2, n)) * amp).astype(np.float32)


def _assert_block_equivalence(effect_factory, params: dict, samples: np.ndarray,
                              splits=(2, 4), tol=1e-4) -> None:
    """整块处理 vs 分块处理等价（跳过首块启动瞬态段）。"""
    eff_full = effect_factory()
    eff_full.set_params(params, sample_rate=SR)
    full = eff_full.process(samples.copy())

    for k in splits:
        eff_split = effect_factory()
        eff_split.set_params(params, sample_rate=SR)
        chunks = []
        step = samples.shape[1] // k
        for i in range(k):
            lo = i * step
            hi = (i + 1) * step if i < k - 1 else samples.shape[1]
            chunks.append(eff_split.process(samples[:, lo:hi].copy()))
        split = np.concatenate(chunks, axis=1)
        # 跳过第一块（IIR 启动瞬态在两条路径上一致，但保守起见比较后续块）
        diff = np.max(np.abs(full[:, step:] - split[:, step:]))
        assert diff < tol, f"{k}-split discontinuity: {diff:.6f}"


class TestStripEQBlockEquivalence:
    def test_parametric_eq_block_equivalence(self) -> None:
        bands = [dict(b) for b in default_eq_bands_flat()]
        bands[1] = {**bands[1], "filter_type": "Low Shelf", "freq": 150.0,
                    "gain": 3.5, "q": 1.4, "enabled": True}
        bands[3] = {**bands[3], "filter_type": "Bell", "freq": 2500.0,
                    "gain": -4.0, "q": 1.8, "enabled": True}
        bands[4] = {**bands[4], "filter_type": "High Shelf", "freq": 8000.0,
                    "gain": 2.0, "q": 1.2, "enabled": True}
        params = {"enabled": True, "voicing": "British", "master_gain": 1.5,
                  "bands": bands}
        _assert_block_equivalence(StripEQFilter, params, _noise(9600))

    def test_hpf_lpf_block_equivalence(self) -> None:
        bands = [dict(b) for b in default_eq_bands_flat()]
        bands[0] = {**bands[0], "filter_type": "HPF", "freq": 80.0,
                    "slope": 18, "enabled": True}
        bands[5] = {**bands[5], "filter_type": "LPF", "freq": 16000.0,
                    "slope": 12, "enabled": True}
        params = {"enabled": True, "bands": bands}
        _assert_block_equivalence(StripEQFilter, params, _noise(9600))


class TestDynamicsBlockEquivalence:
    def test_compressor_block_equivalence(self) -> None:
        params = {"enabled": True, "threshold_db": -18.0, "ratio": 4.0,
                  "attack_ms": 2.0, "hold_ms": 0.0, "release_ms": 150.0,
                  "knee": 20.0, "mix": 80.0, "makeup_gain": 0.0}
        _assert_block_equivalence(CompressorEffect, params,
                                  _noise(19200, amp=0.5), tol=1e-4)

    def test_compressor_hold_block_equivalence(self) -> None:
        params = {"enabled": True, "threshold_db": -18.0, "ratio": 6.0,
                  "attack_ms": 1.0, "hold_ms": 50.0, "release_ms": 200.0,
                  "knee": 0.0, "mix": 100.0}
        _assert_block_equivalence(CompressorEffect, params,
                                  _noise(19200, amp=0.5), tol=5e-3)

    def test_limiter_block_equivalence(self) -> None:
        params = {"enabled": True, "threshold_db": -6.0, "attack_ms": 0.71,
                  "hold_ms": 0.0, "release_ms": 93.0}
        _assert_block_equivalence(LimiterEffect, params,
                                  _noise(19200, amp=0.8), tol=1e-4)

    def test_expander_block_equivalence(self) -> None:
        # 响-静-响动态信号（触发开关门转换）
        n = 4800
        sig = np.concatenate([
            0.4 * np.sin(2 * np.pi * 440.0 * np.arange(n) / SR),
            0.002 * np.sin(2 * np.pi * 440.0 * np.arange(n) / SR),
            0.4 * np.sin(2 * np.pi * 440.0 * np.arange(n) / SR),
            0.002 * np.sin(2 * np.pi * 440.0 * np.arange(n) / SR),
        ]).astype(np.float32)
        samples = np.stack([sig, sig])
        params = {"enabled": True, "mode": "Expander", "threshold_db": -25.0,
                  "range_db": 20.0, "ratio": 2.0, "attack_ms": 1.0,
                  "hold_ms": 0.0, "release_ms": 150.0}
        _assert_block_equivalence(ExpanderEffect, params, samples,
                                  splits=(2, 4), tol=1e-4)


class TestReverbBlockEquivalence:
    def test_reverb_block_equivalence(self) -> None:
        params = {"enabled": True, "dry_wet": 40.0, "direct_db": 0.0,
                  "er_db": -2.0, "reverb_db": -3.0, "pre_delay": 20.0,
                  "distance": 5.0, "reverb_time": 1200.0,
                  "brightness": 60.0, "modulation": 5.0}
        _assert_block_equivalence(ReverbEffect, params, _sine(440.0, 19200),
                                  tol=1e-4)

    def test_reverb_tone_block_equivalence(self) -> None:
        params = {"enabled": True, "dry_wet": 60.0, "direct_db": 0.0,
                  "er_db": 0.0, "reverb_db": 0.0, "pre_delay": 10.0,
                  "distance": 2.0, "reverb_time": 800.0,
                  "er_lo_freq": 250.0, "er_hi_freq": 9000.0,
                  "er_lo_gain": -6.0, "er_hi_gain": 3.0,
                  "rev_lo_freq": 300.0, "rev_hi_freq": 10000.0,
                  "rev_lo_gain": -4.0, "rev_hi_gain": -2.0}
        _assert_block_equivalence(ReverbEffect, params, _noise(19200, amp=0.3),
                                  tol=1e-4)


class TestDelayBlockEquivalence:
    @pytest.mark.parametrize("blend", [0.0, 35.0, 100.0])
    def test_delay_block_equivalence(self, blend: float) -> None:
        params = {"enabled": True, "left_delay": 137.0, "right_delay": 211.0,
                  "left_feedback": 38.0, "right_feedback": -28.0,
                  "stereo_blend": blend, "dry_wet": 35.0, "level_db": -1.0,
                  "high_ratio": 45.0, "low_cut": 120.0, "high_cut": 11000.0}
        _assert_block_equivalence(DelayEffect, params, _noise(19200),
                                  tol=1e-4)

    def test_delay_mono_format_block_equivalence(self) -> None:
        params = {"enabled": True, "left_delay": 90.0, "right_delay": 250.0,
                  "left_feedback": 30.0, "right_feedback": 30.0,
                  "stereo_blend": 20.0, "dry_wet": 40.0, "level_db": 0.0,
                  "high_ratio": 30.0, "low_cut": 100.0, "high_cut": 12000.0,
                  "input_format": "Mono"}
        _assert_block_equivalence(DelayEffect, params, _noise(19200),
                                  tol=1e-4)


class TestStatelessBlockEquivalence:
    def test_saturation_block_equivalence(self) -> None:
        params = {"enabled": True, "drive": 45.0}
        _assert_block_equivalence(SaturationEffect, params, _noise(9600))

    def test_imager_block_equivalence(self) -> None:
        params = {"enabled": True, "width": 60.0, "pan": -20.0}
        _assert_block_equivalence(ImagerEffect, params, _noise(9600))


class TestChainStateReuse:
    """EffectChain 参数变化复用实例（DSP 状态连续）。"""

    def test_param_change_reuses_instance(self) -> None:
        chain = EffectChain(SR)
        chain.set_params([("delay", {"enabled": True, "left_delay": 200.0,
                                     "right_delay": 200.0}, True)])
        old = chain._effects[0]
        chain.set_params([("delay", {"enabled": True, "left_delay": 250.0,
                                     "right_delay": 250.0}, True)])
        assert chain._effects[0] is old

    def test_reordered_entries_keep_state(self) -> None:
        """顺序调整（EQ ↔ 延迟互换位置）时同类型实例仍被复用。"""
        eq_params = {"enabled": True, "bands": [dict(b) for b in default_eq_bands_flat()]}
        dl_params = {"enabled": True, "left_delay": 150.0, "right_delay": 150.0}
        chain = EffectChain(SR)
        chain.set_params([("strip_eq", eq_params, True),
                          ("delay", dl_params, True)])
        eq_before = chain._effects[0]
        dl_before = chain._effects[1]
        chain.set_params([("delay", dl_params, True),
                          ("strip_eq", eq_params, True)])
        # 实例按类型复用（位置换，对象保留）
        assert {id(e) for e in chain._effects} == {id(eq_before), id(dl_before)}

    def test_chain_block_equivalence(self) -> None:
        """整链（EQ → Expander → Compressor → Delay → Reverb → Limiter）
        分块 == 整块。"""
        eq_params = {"enabled": True,
                     "bands": [dict(b) for b in default_eq_bands_flat()]}
        ex_params = {"enabled": True, "mode": "Expander", "threshold_db": -30.0,
                     "range_db": 15.0, "release_ms": 150.0}
        cp_params = {"enabled": True, "threshold_db": -16.0, "ratio": 3.0,
                     "attack_ms": 2.0, "release_ms": 120.0, "knee": 20.0}
        dl_params = {"enabled": True, "left_delay": 120.0, "right_delay": 180.0,
                     "left_feedback": 20.0, "right_feedback": 20.0,
                     "dry_wet": 20.0, "high_ratio": 50.0,
                     "low_cut": 120.0, "high_cut": 12000.0}
        rv_params = {"enabled": True, "dry_wet": 25.0, "pre_delay": 12.0,
                     "distance": 3.0, "reverb_time": 900.0}
        lim_params = {"enabled": True, "threshold_db": -3.0, "release_ms": 93.0}
        entries = [
            ("strip_eq", eq_params, True),
            ("expander", ex_params, True),
            ("compressor", cp_params, True),
            ("delay", dl_params, True),
            ("reverb", rv_params, True),
            ("limiter", lim_params, True),
        ]
        samples = _noise(19200, amp=0.4)
        chain_full = EffectChain(SR)
        chain_full.set_params(entries)
        full = chain_full.process(samples.copy())

        chain_split = EffectChain(SR)
        chain_split.set_params(entries)
        step = 4800
        chunks = []
        for i in range(samples.shape[1] // step):
            lo, hi = i * step, (i + 1) * step
            chunks.append(chain_split.process(samples[:, lo:hi].copy()))
        split = np.concatenate(chunks, axis=1)
        diff = np.max(np.abs(full[:, step:] - split[:, step:]))
        assert diff < 2e-3, f"chain block discontinuity: {diff:.6f}"


class TestDegenerateInputs:
    """极端输入的鲁棒性（向量化路径不崩、不产生 NaN/inf）。"""

    @pytest.mark.parametrize("factory,params", [
        (StripEQFilter, {"enabled": True}),
        (LimiterEffect, {"enabled": True, "threshold_db": -6.0}),
        (CompressorEffect, {"enabled": True}),
        (ReverbEffect, {"enabled": True, "dry_wet": 100.0, "direct_db": -60.0}),
        (DelayEffect, {"enabled": True}),
        (SaturationEffect, {"enabled": True, "drive": 80.0}),
        (ImagerEffect, {"enabled": True, "width": 150.0}),
        (ExpanderEffect, {"enabled": True, "mode": "Gate"}),
    ])
    def test_tiny_and_huge_blocks(self, factory, params) -> None:
        eff = factory(SR)
        eff.set_params(params, sample_rate=SR)
        out1 = eff.process(np.zeros((2, 1), dtype=np.float32))
        assert out1.shape == (2, 1)
        assert np.all(np.isfinite(out1))
        # 单样本 × 多块（延迟线最小分段路径）
        acc = []
        for _ in range(64):
            acc.append(eff.process(np.zeros((2, 1), dtype=np.float32)))
        merged = np.concatenate(acc, axis=1)
        assert np.all(np.isfinite(merged))

    def test_all_zero_input_stays_finite(self) -> None:
        rv = ReverbEffect(SR)
        rv.set_params({"enabled": True})
        out = rv.process(np.zeros((2, 9600), dtype=np.float32))
        assert np.all(np.isfinite(out))
