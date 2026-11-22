"""P2 · 频谱变形 Freeze 回卷环与异采样率（FX-22 · 2026-11-21 计划）。

验收：写入超过一圈后捕获/装载，锚点按容量取模恢复相位（旧 clamp
错相位），重放确定性；44.1k 捕获 → 48k 实例按内容时长重采样；
既有 H09 资产合同不回归。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.spectral_mangler import (
    SpectralManglerEffect,
)

PARAMS = {"grain_enabled": True, "grain_density_hz": 30.0,
          "grain_mix_pct": 100.0, "global_mix_pct": 100.0,
          "random_seed": 77, "grain_freeze": False}


class TestWrappedRing:
    def test_over_one_ring_capture_replay(self) -> None:
        """写入 > 一圈：捕获 → 装载 → 重放确定性与内容非静默。"""
        engine = SpectralManglerEffect(48000)
        engine.set_params(dict(PARAMS))
        source = (np.random.default_rng(11).standard_normal(
            (2, int(20 * 48000))) * 0.1).astype(np.float32)
        engine.process(source)
        capture = engine.take_freeze_capture()
        assert capture["anchor"] > engine._grain._cap  # 确实回卷

        def replay(asset):
            e = SpectralManglerEffect(48000)
            e.set_params({**PARAMS, "grain_freeze": True})
            assert e.install_freeze_capture(asset)
            return e.process(np.zeros((2, 24000), np.float32))

        first = replay(capture)
        second = replay(capture)
        np.testing.assert_array_equal(first, second)
        assert float(np.max(np.abs(first))) > 1e-4


class TestCrossRate:
    def test_capture_rate_resampled_on_install(self) -> None:
        """44.1k 捕获 → 48k 实例：历史按内容时长重采样。"""
        engine = SpectralManglerEffect(44100)
        engine.set_params(dict(PARAMS))
        source = (np.random.default_rng(11).standard_normal(
            (2, 44100)) * 0.1).astype(np.float32)
        engine.process(source)
        capture = engine.take_freeze_capture()
        assert capture["sample_rate"] == 44100.0
        target = SpectralManglerEffect(48000)
        target.set_params({**PARAMS, "grain_freeze": True})
        assert target.install_freeze_capture(capture)
        # 重采样后有效内容 ≈ 1 s（44100/44100 → 48000 样本）。
        valid = float(np.max(np.abs(target._grain._hist[:, :48000])))
        assert valid > 1e-3
        out = target.process(np.zeros((2, 24000), np.float32))
        assert float(np.max(np.abs(out))) > 1e-4
