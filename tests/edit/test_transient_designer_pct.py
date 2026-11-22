"""P1 · 瞬态塑形百分比前端（FX-17 · 2026-11-21 计划）。

验收：``db = 0.12 × pct``（±100% → ±12 dB）；新版支路逐声道包络 +
反相安全聚合；分块逐位一致；旧 dB 音色冻结（位精确）；mix 干湿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.modulation_tools import (
    PCT_TO_DB,
    TransientDesignerEffect,
)
from src.features.edit.core.timeline_v2 import TransientDesignerParams

RATE = 48000


def _impulse_fixture() -> np.ndarray:
    """短促起振 + 稳态持续的双声道信号。"""
    t = np.arange(RATE) / RATE
    sig = (0.15 * np.sin(2 * np.pi * 200.0 * t))[None, :].astype(np.float32)
    sig = np.repeat(sig, 2, axis=0)
    sig[:, :100] = 0.0
    return sig


class TestPercentMapping:
    def test_mapping_constant(self) -> None:
        assert PCT_TO_DB == 0.12

    def test_model_round_trip(self) -> None:
        params = TransientDesignerParams.from_dict(
            {"attack_pct": 50.0, "sustain_pct": -25.0, "mix": 0.5,
             "output_gain_db": -3.0})
        assert params.attack_pct == 50.0
        restored = TransientDesignerParams.from_dict(params.to_dict())
        assert restored == params
        # 未给 pct 键 → None（旧 dB 路径）
        legacy = TransientDesignerParams.from_dict({"attack_db": 6.0})
        assert legacy.attack_pct is None and legacy.sustain_pct is None

    @pytest.mark.parametrize("pct,expected_db", [(50.0, 6.0), (-50.0, -6.0),
                                                 (100.0, 12.0)])
    def test_percent_to_db_attack(self, pct, expected_db) -> None:
        sig = _impulse_fixture()
        fx = TransientDesignerEffect(RATE)
        fx.set_params({"attack_pct": pct, "sustain_pct": 0.0})
        out = fx.process(sig.copy())
        ratio = float(np.max(out[0, 100:110])) / max(
            float(np.max(sig[0, 100:110])), 1e-9)
        assert 20 * np.log10(ratio) == pytest.approx(expected_db, abs=0.3)


class TestNewPath:
    def test_anti_phase_stereo_triggers(self) -> None:
        """逐声道包络取最大：反相立体声不抵消检测。"""
        sig = _impulse_fixture()
        anti = np.stack([sig[0], -sig[0]])
        fx = TransientDesignerEffect(RATE)
        fx.set_params({"attack_pct": 50.0})
        out = fx.process(anti.copy())
        ratio = float(np.max(out[0, 100:110])) / max(
            float(np.max(sig[0, 100:110])), 1e-9)
        assert ratio == pytest.approx(2.0, abs=0.05)

    @pytest.mark.parametrize("step", [513, 1024, 4001])
    def test_block_consistency_bit_exact(self, step) -> None:
        sig = _impulse_fixture()
        params = {"attack_pct": 50.0, "sustain_pct": -25.0}
        fx = TransientDesignerEffect(RATE)
        fx.set_params(dict(params))
        whole = fx.process(sig.copy())
        fxb = TransientDesignerEffect(RATE)
        fxb.set_params(dict(params))
        stitched = np.concatenate(
            [fxb.process(sig[:, s:s + step])
             for s in range(0, sig.shape[1], step)], axis=1)
        np.testing.assert_array_equal(whole, stitched)

    def test_mix_zero_passthrough(self) -> None:
        sig = _impulse_fixture()
        fx = TransientDesignerEffect(RATE)
        fx.set_params({"attack_pct": 50.0, "mix": 0.0})
        np.testing.assert_array_equal(fx.process(sig.copy()), sig)

    def test_sustain_negative_attenuates_tail(self) -> None:
        sig = _impulse_fixture()
        fx = TransientDesignerEffect(RATE)
        fx.set_params({"attack_pct": 0.0, "sustain_pct": -50.0})
        out = fx.process(sig.copy())
        # 稳态段按 −6 dB 衰减（weight → 0 → sustain_gain）。
        tail_ratio = float(np.max(np.abs(out[0, RATE // 2:]))) / max(
            float(np.max(np.abs(sig[0, RATE // 2:]))), 1e-9)
        assert 20 * np.log10(tail_ratio) == pytest.approx(-6.0, abs=0.3)


class TestLegacyFrozen:
    def test_legacy_db_path_bit_exact(self) -> None:
        """旧 attack_db/sustain_db 路径与旧实现逐样本一致。"""
        sig = _impulse_fixture()
        fx = TransientDesignerEffect(RATE)
        fx.set_params({"attack_db": 6.0, "sustain_db": 0.0})
        out = fx.process(sig.copy())
        k_fast = np.exp(-1.0 / (8.0 * RATE / 1000.0))
        k_slow = np.exp(-1.0 / (80.0 * RATE / 1000.0))
        mono = np.abs(sig.mean(axis=0)).astype(np.float64)
        f_env = s_env = 0.0
        gains = np.empty(RATE, dtype=np.float32)
        for i, value in enumerate(mono):
            f_env = k_fast * f_env + (1.0 - k_fast) * value
            s_env = k_slow * s_env + (1.0 - k_slow) * value
            ratio_db = 20.0 * np.log10(max(f_env, 1e-9) / max(s_env, 1e-9))
            weight = float(np.clip((ratio_db - 3.0) / 6.0, 0.0, 1.0))
            gains[i] = (1.0 * (1.0 - weight) + 10.0 ** (6.0 / 20.0) * weight)
        reference = sig * gains[None, :]
        np.testing.assert_array_equal(out, reference.astype(np.float32))
