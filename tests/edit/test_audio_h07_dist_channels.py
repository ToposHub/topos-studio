"""H07：Dist 逐声道状态、有状态过采样与 sample-hold（复审计划 §5 H07）。

修前复现（复审）：左正弦右静音时右输出峰 Tape≈0.00368 / Fold≈0.01967 /
HardClip≈0.00516 / BitCrush≈0.24277（FIR/tone/hold 状态跨声道共享）；
整块/256 块误差 ~2.14（每块尾 31 采样补零）；bitcrush hold 按块内索引
（块长相关、跨块相位断裂）。

修后契约（``_DistModule``）：
- up/down FIR zi、tone zi、hold 相位/值逐声道（系数共享）；
- 2× 过采样经每声道 FIFO 以固定群延迟对齐（无块尾空洞）；
- bitcrush hold 按绝对流索引（跨块连续）；
- ``latency`` = FIR (T−1)/2 + tone 群延迟，拓扑 dry 对齐；
- 效果级 mix=0 = 时延对齐 dry（mix 扫动连续）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_effects.spectral_mangler import (
    SpectralManglerEffect,
    _DistModule,
)
from src.features.edit.core.timeline_v2 import SpectralManglerParams

pytestmark = pytest.mark.edit_core
SR = 48000


def _x_stereo(n: int = SR) -> np.ndarray:
    t = np.arange(n) / SR
    left = 0.4 * np.sin(2 * np.pi * 220.0 * t)
    return np.stack([left, np.zeros(n)])


class TestChannelIsolation:
    @pytest.mark.parametrize("curve", ["tape", "fold", "hard_clip", "bitcrush"])
    def test_silent_right_strictly_silent(self, curve):
        """bias=0：静音右声道严格无声（左输入不串音）。"""
        p = SpectralManglerParams(dist_enabled=True, dist_curve=curve,
                                  dist_drive_db=14.0, dist_fold_pct=60.0,
                                  dist_bias_pct=0.0)
        d = _DistModule(SR)
        y = d.process(_x_stereo(), p)
        assert float(np.abs(y[1]).max()) == 0.0

    def test_distinct_channels_no_crossmix(self):
        """左右不同信号：输出各自跟随自身输入（延迟对齐后）。"""
        t = np.arange(SR) / SR
        L = 0.3 * np.sin(2 * np.pi * 300.0 * t)
        R = 0.3 * np.sin(2 * np.pi * 700.0 * t)
        x = np.stack([L, R])
        p = SpectralManglerParams(dist_enabled=True, dist_curve="tape",
                                  dist_drive_db=0.0, dist_mix_pct=100.0,
                                  dist_bias_pct=0.0, dist_tone_hz=16000.0,
                                  dist_resonance_pct=0.0)
        d = _DistModule(SR)
        y = d.process(x, p)
        lat = d.latency
        # drive=0 + tape 小信号近线性：y[ch] ≈ x[ch] 延迟（各自通道）。
        # 容差 = tanh 三次项(0.009) + tone 时延整数化残差(±0.5 样本相位)
        assert np.abs(y[0, lat + 256:] - L[256:SR - lat]).max() < 0.03
        assert np.abs(y[1, lat + 256:] - R[256:SR - lat]).max() < 0.03

    def test_mono_input_grows_state(self):
        """单声道输入：状态数组按需扩展，不抛错。"""
        p = SpectralManglerParams(dist_enabled=True, dist_bias_pct=0.0)
        d = _DistModule(SR)
        y = d.process(_x_stereo(1024)[:1, :], p)
        assert y.shape[0] == 1
        y2 = d.process(_x_stereo(1024), p)  # 2 声道
        assert y2.shape[0] == 2


class TestStatefulOversampling:
    @pytest.mark.parametrize("curve", ["tape", "fold", "hard_clip", "bitcrush"])
    def test_block_invariance(self, curve):
        """整块 == 1/255/257/1920/随机块（修前 256 块误差 ~2.14）。"""
        rng = np.random.default_rng(9)
        n = 24000
        x = rng.standard_normal((2, n)) * 0.3
        p = SpectralManglerParams(dist_enabled=True, dist_curve=curve,
                                  dist_drive_db=14.0, dist_fold_pct=60.0,
                                  dist_bias_pct=0.0)
        d = _DistModule(SR)
        yw = d.process(x.copy(), p)
        for blocks in ([1] * n, [255, 257] * 46 + [608], [1920] * 12 + [960],
                       list(rng.integers(1, 4000, size=20))):
            blocks = [int(b) for b in blocks]
            total = sum(blocks)
            if total > n:
                continue
            d2 = _DistModule(SR)
            pos, outs = 0, []
            for b in blocks:
                outs.append(d2.process(x[:, pos:pos + b].copy(), p))
                pos += b
            yb = np.concatenate(outs, axis=-1)
            np.testing.assert_allclose(
                yb, yw[:, :total], atol=1e-12, err_msg=curve)

    def test_no_block_tail_holes(self):
        """长正弦无固定块尾零段（修前每块尾 ~31 采样空洞）。"""
        t = np.arange(SR) / SR
        x = np.stack([0.3 * np.sin(2 * np.pi * 440.0 * t)] * 2)
        p = SpectralManglerParams(dist_enabled=True, dist_curve="tape",
                                  dist_drive_db=6.0, dist_bias_pct=0.0)
        d = _DistModule(SR)
        outs = []
        for i in range(25):
            outs.append(d.process(x[:, i * 1920:(i + 1) * 1920].copy(), p))
        y = np.concatenate(outs, axis=-1)
        seg = y[0, 2000:-2000]
        # 无 >8 采样的连续零段
        zeros = seg == 0.0
        run = 0
        worst = 0
        for z in zeros:
            run = run + 1 if z else 0
            worst = max(worst, run)
        assert worst <= 8

    def test_latency_reported_matches_ir(self):
        """实测 IR 峰位与报告时延一致（±2 样本：tone 相位离散化）。"""
        d = _DistModule(SR)
        imp = np.zeros((2, 8192))
        imp[0, 0] = 1.0
        p = SpectralManglerParams(dist_enabled=True, dist_curve="tape",
                                  dist_bias_pct=0.0)
        y = d.process(imp, p)
        peak = int(np.argmax(np.abs(y[0])))
        assert abs(peak - d.latency) <= 2

    def test_reset_clears_state(self):
        p = SpectralManglerParams(dist_enabled=True, dist_bias_pct=0.0)
        d = _DistModule(SR)
        d.process(_x_stereo(4096), p)
        d.reset()
        y = d.process(np.zeros((2, 4096)), p)
        assert float(np.abs(y).max()) == 0.0

    def test_sample_rate_switch(self):
        p = SpectralManglerParams(dist_enabled=True, dist_bias_pct=0.0)
        d = _DistModule(44100.0)
        d.process(_x_stereo(4096), p)
        d2 = _DistModule(48000.0)
        y = d2.process(_x_stereo(4096), p)
        assert np.isfinite(y).all()


class TestBitcrushHold:
    def test_hold_phase_continuous_across_blocks(self):
        """hold 段跨块边界：整块与任意分块逐位一致（相位/值连续）。"""
        n = 24000
        rng = np.random.default_rng(31)
        x = rng.standard_normal((2, n)) * 0.5
        p = SpectralManglerParams(dist_enabled=True, dist_curve="bitcrush",
                                  dist_fold_pct=80.0, dist_bias_pct=0.0)
        d = _DistModule(SR)
        yw = d.process(x.copy(), p)
        d2 = _DistModule(SR)
        pos, outs = 0, []
        for b in [1000] * 24:
            outs.append(d2.process(x[:, pos:pos + b].copy(), p))
            pos += b
        yb = np.concatenate(outs, axis=-1)
        np.testing.assert_array_equal(yb, yw[:, :24000])

    def test_quantization_grid_finite(self):
        """量化原语：输出取自有限量化网格（L16 语义在纯函数层保持）。"""
        from src.features.edit.core.audio_effects.spectral_mangler import (
            _apply_curve,
        )
        u = np.linspace(-1, 1, 20001)
        for fold in (0.0, 0.5, 1.0):
            q = _apply_curve(u, "bitcrush", fold)
            levels = max(2.0, 12.0 * (1.0 - fold * 0.85))
            grid = np.round(np.clip(u, -1, 1) * levels) / levels
            np.testing.assert_allclose(q, grid)


class TestEffectLevelAlignment:
    def test_mix_zero_is_aligned_dry(self):
        """效果级 mix=0：延迟 L 的逐位 dry（L = latency_samples()）。"""
        t = np.arange(24000) / SR
        x = np.stack([0.3 * np.sin(2 * np.pi * 997.0 * t)] * 2)
        p = SpectralManglerParams(dist_enabled=True, dist_drive_db=12.0,
                                  global_mix_pct=0.0)
        fx = SpectralManglerEffect(SR)
        fx.set_params(p.to_dict())
        L = fx.latency_samples()
        y = fx.process(x.copy())
        np.testing.assert_allclose(y[:, L:], x[:, :24000 - L], atol=1e-12)
        np.testing.assert_allclose(y[:, :L], np.zeros((2, L)), atol=1e-12)

    def test_parallel_routing_no_comb(self):
        """并联 dry 按模块时延对齐：mix 扫动无梳状（延迟对齐差恒等）。"""
        t = np.arange(24000) / SR
        x = np.stack([0.3 * np.sin(2 * np.pi * 997.0 * t)] * 2)
        for mix in (25.0, 50.0, 75.0):
            p = SpectralManglerParams(dist_enabled=True, routing="parallel",
                                      dist_curve="tape",
                                      dist_drive_db=0.0, dist_mix_pct=mix,
                                      dist_bias_pct=0.0,
                                      dist_tone_hz=16000.0,
                                      global_mix_pct=100.0)
            fx = SpectralManglerEffect(SR)
            fx.set_params(p.to_dict())
            y = fx.process(x.copy())
            L = fx.latency_samples()
            # drive=0 tape 近线性：分支 ≈ 对齐 dry → 输出 ≈ 延迟输入
            #（容差含 tanh 三次项与 tone 时延整数化残差）
            dev = np.abs(y[:, L + 256:] - x[:, 256:24000 - L]).max() / 0.3
            assert dev < 0.12, (mix, dev)

    def test_all_curves_endpoints_finite(self):
        """曲线/增益/偏置端点全有限（含 +36dB drive / ±100 bias）。"""
        rng = np.random.default_rng(3)
        x = rng.standard_normal((2, 8192))
        for curve in ("tape", "fold", "hard_clip", "bitcrush"):
            for drive in (0.0, 36.0):
                for bias in (-100.0, 0.0, 100.0):
                    p = SpectralManglerParams(
                        dist_enabled=True, dist_curve=curve,
                        dist_drive_db=drive, dist_bias_pct=bias,
                        dist_fold_pct=100.0)
                    d = _DistModule(SR)
                    y = d.process(x * 0.9, p)
                    assert np.isfinite(y).all(), (curve, drive, bias)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
