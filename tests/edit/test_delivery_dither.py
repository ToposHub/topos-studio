"""交付层 Dither（FX-23 · 2026-11-21 计划第 5 项）。

验收：固定 seed、目标字长 LSB 标尺、无削顶溢出、浮点路径关闭、
静音空白策略、dither 改善量化噪声（无 dither 时小信号退化为方波）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.delivery_dither import (
    DitherSettings,
    apply_dither,
    quantize_to_bit_depth,
)


class TestQuantize:
    def test_lsb_grid_24bit(self) -> None:
        lsb = 2.0 ** -23
        pcm = np.array([[0.5, 0.5 * lsb, -0.5 * lsb, 1.0, -1.0]],
                       dtype=np.float32)
        q = quantize_to_bit_depth(pcm, 24)
        # 全部落到 LSB 网格（步数整数）。
        steps = q / lsb
        np.testing.assert_allclose(steps, np.round(steps), atol=1e-6)
        # 饱和到 [-1, 1-LSB]。
        assert float(np.max(q)) <= 1.0 - lsb + 1e-9
        assert float(np.min(q)) >= -1.0
        # half-away-from-zero：0.5 LSB 进位。
        assert q[0, 1] == pytest.approx(lsb)
        assert q[0, 2] == pytest.approx(-lsb)

    def test_float_bit_depth_passthrough(self) -> None:
        pcm = np.array([[0.123456789, 0.5]], dtype=np.float32)
        np.testing.assert_array_equal(
            quantize_to_bit_depth(pcm, 32), pcm)


class TestDither:
    def test_fixed_seed_reproducible(self) -> None:
        rng = np.random.default_rng(7)
        pcm = (rng.uniform(-0.3, 0.3, (2, 4096))).astype(np.float32)
        a = apply_dither(pcm, 24, seed=123)
        b = apply_dither(pcm, 24, seed=123)
        np.testing.assert_array_equal(a, b)
        # 不同 seed 不同实现（dither 不是恒等操作）。
        assert np.max(np.abs(a - pcm)) > 0.0
        assert not np.array_equal(a, apply_dither(pcm, 24, seed=124))

    def test_no_clipping_overflow(self) -> None:
        rng = np.random.default_rng(3)
        pcm = np.full((2, 4096), 1.0 - 2.0 ** -24, dtype=np.float32)
        for depth in (16, 24):
            out = apply_dither(pcm, depth, seed=5)
            lsb = 2.0 ** -(depth - 1)
            assert float(np.max(out)) <= 1.0 - lsb + 1e-9
            assert float(np.min(out)) >= -1.0

    def test_silence_kept_black_by_default(self) -> None:
        pcm = np.zeros((2, 512), dtype=np.float32)
        out = apply_dither(pcm, 24, seed=1, dither_silence=False)
        np.testing.assert_array_equal(out, pcm)
        # dither_silence=True 时空白也加噪（TPDF 峰值 ≤ 1 LSB）。
        noisy = apply_dither(pcm, 24, seed=1, dither_silence=True)
        assert float(np.max(np.abs(noisy))) <= 2.0 ** -23 + 1e-12

    def test_float_path_disabled(self) -> None:
        pcm = np.array([[0.25]], dtype=np.float32)
        np.testing.assert_array_equal(apply_dither(pcm, 32, seed=1), pcm)

    def test_shaped_shape_rejected_until_validated(self) -> None:
        pcm = np.zeros((1, 8), dtype=np.float32)
        with pytest.raises(ValueError):
            apply_dither(pcm, 24, seed=1, shape="shaped")

    def test_settings_round_trip_and_apply(self) -> None:
        settings = DitherSettings(enabled=True, bit_depth=24, seed=99)
        restored = DitherSettings.from_dict(settings.to_dict())
        assert restored.enabled and restored.seed == 99
        rng = np.random.default_rng(11)
        pcm = rng.uniform(-0.5, 0.5, (2, 2048)).astype(np.float32)
        out = restored.apply(pcm)
        lsb = 2.0 ** -23
        steps = out / lsb
        np.testing.assert_allclose(steps, np.round(steps), atol=1e-6)
        # 默认关闭：仅量化。
        off = DitherSettings(enabled=False, bit_depth=24).apply(pcm)
        np.testing.assert_array_equal(
            off, quantize_to_bit_depth(pcm, 24))

    def test_dither_reduces_quantization_distortion(self) -> None:
        """小信号正弦：无 dither 的量化误差与信号相关（谐波突出）；
        dither 将误差解相关为平坦噪声（谐波峰/噪声底比值显著下降）。"""
        rate = 48000
        t = np.arange(rate) / rate
        amp = 3.0 * 2.0 ** -23  # ≈3 LSB 的极小信号
        pcm = (amp * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(np.float32)
        off = quantize_to_bit_depth(pcm, 24)
        on = DitherSettings(enabled=True, bit_depth=24, seed=42).apply(pcm)

        def harmonic_to_floor(x: np.ndarray) -> float:
            windowed = x[0, rate // 4: 3 * rate // 4] * np.hanning(rate // 2)
            spec = np.abs(np.fft.rfft(windowed))
            bin_hz = rate / (rate // 2)

            def band_peak(freq: float) -> float:
                center = int(round(freq / bin_hz))
                return float(spec[center - 3:center + 4].max())

            harmonics = max(band_peak(440.0), band_peak(660.0),
                            band_peak(880.0))
            floor = float(np.median(spec[300:3000]))
            return harmonics / max(floor, 1e-30)

        # 无 dither：失真谐波显著高于本底；dither：误差解相关 → 比值低。
        assert harmonic_to_floor(on) < harmonic_to_floor(off)
