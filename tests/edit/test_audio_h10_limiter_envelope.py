"""H10：Mangler 内部 Limiter 逐采样包络 + 拓扑时延（复审 §5 H10）。

修前复现：标称 50ms release 按"一块一个采样系数"恢复——过载后 1 秒
静音的增益随块长漂移（block=1 → ≈1，64 → 0.6342，256 → 0.5377，
2000 → 0.5050，48000 → 0.5002）。

修后契约（``_MiniLimiter``）：
- 逐采样精确包络 ``E[n] = min(1, min_k t[k]·e^{(n−k)/τ})``（attack 即时、
  release 50ms 指数、声道联动同增益）；
- log 域 running-min 向量化（一次 accumulate），块长无关、逐位一致；
- 拓扑 dry/wet 时延对齐（H07 已交付）复测 1 采样块不崩溃。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_effects.spectral_mangler import (
    _MiniLimiter,
    SpectralManglerEffect,
)
from src.features.edit.core.timeline_v2 import SpectralManglerParams

pytestmark = pytest.mark.edit_core
SR = 48000


class TestSampleClockEnvelope:
    @pytest.mark.parametrize("block", [1, 64, 256, 2000, 48000])
    def test_1s_release_consistent_across_blocks(self, block):
        """过载后 1 秒静音：所有块长的增益恢复一致（=1）。"""
        lim = _MiniLimiter(SR)
        data = np.concatenate([np.ones((2, 1000)) * 2.0,
                               np.zeros((2, SR))], axis=-1)
        pos = 0
        while pos < data.shape[-1]:
            b = min(block, data.shape[-1] - pos)
            lim.process(data[:, pos:pos + b].copy())
            pos += b
        assert float(np.exp(lim._log_e0)) == pytest.approx(1.0, abs=1e-6)

    def test_release_curve_exact_exponential(self):
        """×4 过载 → E=0.25；50ms 静音后 E=0.25·e（逐采样 release）。"""
        lim = _MiniLimiter(SR)
        lim.process(np.ones((2, 10)) * 4.0)
        assert np.exp(lim._log_e0) == pytest.approx(0.25, abs=1e-9)
        lim.process(np.zeros((2, int(0.05 * SR))))
        assert np.exp(lim._log_e0) == pytest.approx(0.25 * np.e, rel=1e-6)

    def test_block_invariance(self):
        lim1 = _MiniLimiter(SR)
        rng = np.random.default_rng(4)
        x = rng.standard_normal((2, SR)) * 1.5
        y1 = lim1.process(x.copy())
        lim2 = _MiniLimiter(SR)
        parts, pos = [], 0
        for b in [255, 257] * 93 + [204]:
            parts.append(lim2.process(x[:, pos:pos + b].copy()))
            pos += b
        np.testing.assert_allclose(y1[:, :pos],
                                   np.concatenate(parts, axis=-1), atol=1e-12)

    def test_channel_linked(self):
        """左过载右安静：两声道同增益（联动），右输出被同步压低。"""
        lim = _MiniLimiter(SR)
        x = np.zeros((2, 100))
        x[0, :50] = 2.0
        x[1, :50] = 0.1
        y = lim.process(x.copy())
        assert np.abs(y[0]).max() <= 1.0 + 1e-9
        assert np.abs(y[1, :10]).max() < 0.1 * 0.51  # 联动压低


class TestTopologyRobustness:
    def test_one_sample_blocks_no_crash(self):
        fx = SpectralManglerEffect(SR)
        fx.set_params(SpectralManglerParams(
            ring_enabled=True, grain_enabled=True, dist_enabled=True,
            output_limiter_enabled=True, routing="grain_ring_dist").to_dict())
        rng = np.random.default_rng(6)
        x = rng.standard_normal((2, 3000)) * 0.3
        outs = [fx.process(x[:, i:i + 1].copy()) for i in range(3000)]
        y = np.concatenate(outs, axis=-1)
        assert np.isfinite(y).all()

    def test_routing_switch_continuous(self):
        """routing 连续切换不崩溃且 wet 有界（淡出保护在 wet 分支）。"""
        fx = SpectralManglerEffect(SR)
        rng = np.random.default_rng(7)
        x = rng.standard_normal((2, 48000)) * 0.3
        routings = ["ring_grain_dist", "grain_ring_dist", "dist_grain_ring",
                    "parallel"] * 6
        pos = 0
        for r in routings:
            fx.set_params(SpectralManglerParams(
                ring_enabled=True, grain_enabled=True, dist_enabled=True,
                routing=r).to_dict())
            seg = x[:, pos:pos + 2000].copy()
            y = fx.process(seg)
            assert np.isfinite(y).all()
            pos += 2000

    def test_reset_and_rate_change_with_freeze(self):
        fx = SpectralManglerEffect(44100.0)
        fx.set_params(SpectralManglerParams(
            grain_enabled=True, grain_freeze=True).to_dict())
        rng = np.random.default_rng(8)
        fx.process(rng.standard_normal((2, 8192)) * 0.3)
        fx.reset_state()
        fx.set_params(SpectralManglerParams(
            grain_enabled=True, grain_freeze=True).to_dict(),
            sample_rate=48000.0)
        y = fx.process(rng.standard_normal((2, 8192)) * 0.3)
        assert np.isfinite(y).all()

    def test_limiter_only_gain_positive(self):
        """LIM-only：is_active（L13）且小信号近直通（>0 增益可解释）。"""
        fx = SpectralManglerEffect(SR)
        fx.set_params(SpectralManglerParams(
            output_limiter_enabled=True).to_dict())
        assert fx.is_active
        x = np.ones((2, 4096)) * 0.2
        y = fx.process(x.copy())
        assert np.abs(y).max() == pytest.approx(0.2, abs=1e-6)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
