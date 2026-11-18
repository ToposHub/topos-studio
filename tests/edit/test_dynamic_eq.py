"""P1 · Dynamic EQ（2026-11-19 计划第三阶段 #4）。

验收：关闭动态为同版本 EQ（与 StripEQFilter 的 Bell 段一致）；目标
频段压制；块边缘无跳变（分块 == 整段）；旁路一致；参数变化保留状态
（自动化区间可重开）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.dynamic_eq import (
    GAIN_BLOCK,
    DynamicEqEffect,
    rbj_bell_sos,
)

RATE = 48000


def _tone(freq: float, amp: float, seconds: float = 1.5):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make(params: dict | None = None) -> DynamicEqEffect:
    fx = DynamicEqEffect(float(RATE))
    fx.set_params(params or {})
    return fx


def _band_level(sig: np.ndarray, freq: float, bw: float = 150.0) -> float:
    mono = sig[0] if sig.ndim > 1 else sig
    spec = np.abs(np.fft.rfft(mono * np.hanning(mono.shape[-1])))
    freqs = np.fft.rfftfreq(mono.shape[-1], 1.0 / RATE)
    mask = np.abs(freqs - freq) <= bw
    return float(np.max(spec[mask])) if mask.any() else 0.0


class TestDynamicEq:
    def test_dynamic_off_matches_static_strip_eq_bell(self) -> None:
        """关闭动态 = 同版本 EQ：与 StripEQFilter 单 Bell 段一致。"""
        from src.features.edit.core.audio_dsp import StripEQFilter
        from src.features.edit.core.timeline_v2 import (
            StripEQParams,
            default_eq_bands_flat,
        )

        pcm = np.stack([_tone(3000.0, 0.2), _tone(3000.0, 0.2)])
        dyn = _make({"gain_db": -6.0, "dynamic_enabled": False,
                     "frequency": 3000.0, "q": 1.4})
        out_dyn = dyn.process(pcm.copy())

        bands = [dict(b) for b in default_eq_bands_flat()]
        bands[3] = {**bands[3], "filter_type": "Bell", "freq": 3000.0,
                    "gain": -6.0, "q": 1.4, "enabled": True}
        static = StripEQFilter(float(RATE))
        static.set_params(StripEQParams(
            enabled=True, voicing="Clean", master_gain=0.0,  # dB
            bands=tuple(bands)))
        out_static = static.process(pcm.copy())

        # 跳过两滤波器各自的建立瞬态，稳态段一致。
        steady = slice(4 * GAIN_BLOCK, None)
        np.testing.assert_allclose(
            out_dyn[:, steady], out_static[:, steady], atol=5e-3)

    def test_loud_band_pressed_quiet_band_untouched(self) -> None:
        """目标频段被 detector 压制；非目标频段基本不动。"""
        loud_3k = _tone(3000.0, 0.4)
        quiet_300 = _tone(300.0, 0.2)
        pcm = np.stack([loud_3k + quiet_300] * 2)
        fx = _make({"frequency": 3000.0, "q": 2.0, "threshold_dbfs": -25.0,
                    "max_cut_db": 8.0, "slope": 0.6})
        out = fx.process(pcm.copy())
        steady = slice(RATE // 2, None)
        cut = 20.0 * np.log10(
            _band_level(pcm[:, steady], 3000.0)
            / max(_band_level(out[:, steady], 3000.0), 1e-9))
        passive = abs(20.0 * np.log10(
            _band_level(out[:, steady], 300.0)
            / _band_level(pcm[:, steady], 300.0)))
        assert cut > 3.0          # 目标频段压制
        assert passive < 1.0      # 非目标频段 ≤1 dB

    def test_block_consistency_no_boundary_clicks(self) -> None:
        pcm = np.stack([_tone(3000.0, 0.4)] * 2)
        params = {"threshold_dbfs": -25.0, "max_cut_db": 8.0}
        whole = _make(params).process(pcm.copy())
        fx = _make(params)
        chunks = [fx.process(pcm[:, s:s + 977].copy())
                  for s in range(0, pcm.shape[1], 977)]
        stitched = np.concatenate(chunks, axis=1)
        steady = slice(RATE // 3, None)
        np.testing.assert_allclose(
            whole[:, steady], stitched[:, steady], atol=1e-3)

    def test_bypass_passthrough(self) -> None:
        pcm = np.stack([_tone(1000.0, 0.2)] * 2)
        out = _make({"enabled": False}).process(pcm.copy())
        np.testing.assert_array_equal(out, pcm)

    def test_param_change_preserves_state(self) -> None:
        """自动化区间重开：参数变化不清滤波器状态（无跳变）。"""
        pcm = np.stack([_tone(3000.0, 0.4, seconds=2.0)] * 2)
        fx = _make({"threshold_dbfs": -25.0, "max_cut_db": 8.0})
        first = fx.process(pcm[:, :RATE].copy())
        fx.set_params({"max_cut_db": 4.0})   # 微调（不 reset）
        state = fx._sos_state
        assert state is not None and float(np.max(np.abs(state))) > 0
        second = fx.process(pcm[:, RATE:2 * RATE].copy())
        assert second.shape == (2, RATE)
        # 连续性：第二段起点无爆炸（|sample| 有界）。
        assert float(np.max(np.abs(second[:, :64]))) < 1.5

    def test_registered_in_chain(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(RATE))
        chain.set_params([("dynamic_eq", {
            "frequency": 3000.0, "threshold_dbfs": -25.0,
            "max_cut_db": 8.0}, True)])
        pcm = np.stack([_tone(3000.0, 0.4)] * 2)
        out = chain.process(pcm.copy())
        steady = slice(RATE // 2, None)
        cut = 20.0 * np.log10(
            _band_level(pcm[:, steady], 3000.0)
            / max(_band_level(out[:, steady], 3000.0), 1e-9))
        assert cut > 3.0
        assert chain.latency_samples() == 0
