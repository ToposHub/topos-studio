"""
Audio DSP 单元测试

覆盖：
1. AudioEffect 基类契约（子类必须实现接口）
2. StripEQFilter 继承基类后的行为回归（旁路/启用/shape）
3. LimiterEffect 限幅器 DSP 正确性（阈值/释放/包络跟随/声道联动）
4. EffectChain 链串联（顺序/复用实例/重置/is_active）
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    AudioEffect,
    StripEQFilter,
    LimiterEffect,
    CompressorEffect,
    ReverbEffect,
    DelayEffect,
    SaturationEffect,
    ImagerEffect,
    NoiseGateEffect,
    EffectChain,
    _LIMITER_DEFAULT_THRESHOLD_DB,
    _LIMITER_DEFAULT_RELEASE_MS,
)
from src.features.edit.core.timeline_v2 import (
    StripEQParams, CompressorParams, ReverbParams, DelayParams,
    SaturationParams, ImagerParams, NoiseGateParams,
)


# =============================================================================
# 1. AudioEffect 基类契约
# =============================================================================

class TestAudioEffectBase:
    """基类定义的接口契约。"""

    def test_base_methods_raise_not_implemented(self):
        """基类的 set_params/process/reset_state/is_active 必须由子类实现。"""
        base = AudioEffect()
        with pytest.raises(NotImplementedError):
            base.set_params(None)
        with pytest.raises(NotImplementedError):
            base.process(np.zeros((2, 8)))
        with pytest.raises(NotImplementedError):
            base.reset_state()
        with pytest.raises(NotImplementedError):
            _ = base.is_active

    def test_effect_type_default_empty(self):
        assert AudioEffect.effect_type == ""

    def test_subclasses_register_effect_type(self):
        """具体效果器子类必须覆盖 effect_type。"""
        assert StripEQFilter.effect_type == "strip_eq"
        assert LimiterEffect.effect_type == "limiter"
        assert CompressorEffect.effect_type == "compressor"
        assert ReverbEffect.effect_type == "reverb"
        assert DelayEffect.effect_type == "delay"
        assert SaturationEffect.effect_type == "saturation"
        assert ImagerEffect.effect_type == "imager"
        assert NoiseGateEffect.effect_type == "noise_gate"


# =============================================================================
# 2. StripEQFilter 继承回归（确保改造基类后行为不变）
# =============================================================================

class TestStripEQFilterInheritance:
    """StripEQFilter 改继承 AudioEffect 后的回归测试。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(StripEQFilter, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """params=None 时旁路，原样返回输入。"""
        flt = StripEQFilter(48000.0)
        samples = np.array([[0.5, -0.3, 0.8, -0.1]], dtype=np.float32)
        out = flt.process(samples)
        assert out is samples  # 旁路应原样返回同一对象

    def test_disabled_returns_input_unchanged(self):
        """enabled=False 时旁路。"""
        flt = StripEQFilter(48000.0)
        flt.set_params(StripEQParams(mode="3-band", bands=(6.0, 0.0, 0.0), enabled=False))
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        out = flt.process(samples)
        assert out is samples

    def test_enabled_processes_and_preserves_shape(self):
        """启用时处理样本，shape 与 dtype 保持一致。"""
        flt = StripEQFilter(48000.0)
        flt.set_params(StripEQParams(mode="3-band", bands=(6.0, 0.0, -3.0)))
        samples = np.random.randn(2, 256).astype(np.float32) * 0.5
        out = flt.process(samples)
        assert out.shape == samples.shape
        assert out.dtype == samples.dtype
        # 不原地修改
        assert not np.array_equal(out, samples)

    def test_is_active_reflects_state(self):
        flt = StripEQFilter(48000.0)
        assert not flt.is_active
        flt.set_params(StripEQParams(mode="3-band", bands=(3.0, 0.0, 0.0)))
        assert flt.is_active
        flt.set_params(StripEQParams(mode="3-band", bands=(3.0, 0.0, 0.0), enabled=False))
        assert not flt.is_active


# =============================================================================
# 2b. StripEQFilter 多频段 DSP（Graphic EQ）
# =============================================================================

class TestStripEQFilterMultiBand:
    """多频段 Graphic EQ 的 DSP 正确性。"""

    def test_flat_eq_is_near_identity(self):
        """平直 EQ（所有频段 0 dB）应接近直通。"""
        sr = 48000.0
        samples = np.random.randn(2, 1024).astype(np.float32) * 0.3
        for mode in ("3-band", "7-band", "10-band", "15-band"):
            flt = StripEQFilter(sr)
            flt.set_params(StripEQParams.default(mode))
            out = flt.process(samples)
            assert np.allclose(out, samples, atol=1e-3), \
                f"flat EQ ({mode}) changed signal by {np.max(np.abs(out - samples)):.4f}"

    def test_lowshelf_boosts_low_frequencies(self):
        """3-band 第一段 lowshelf @320Hz +6dB 应在 100Hz 提升约 +6dB。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 100.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params(StripEQParams(mode="3-band", bands=(6.0, 0.0, 0.0)))
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        # 3-band lowshelf @320Hz，100Hz 在通带内，应接近 +6dB
        assert 4.0 < gain_db < 7.0, f"gain {gain_db:.2f} dB not in [4.0, 7.0]"

    def test_highshelf_boosts_high_frequencies(self):
        """3-band highshelf @3200Hz +6dB 应在 10kHz 提升约 +5~6dB。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 10000.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params(StripEQParams(mode="3-band", bands=(0.0, 0.0, 6.0)))
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert gain_db > 3.0, f"gain {gain_db:.2f} dB should be > 3.0"

    def test_peaking_at_center_frequency(self):
        """10-band peaking @500Hz +6dB 应在 500Hz 接近 +6dB。"""
        sr = 48000.0
        n = 8192
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 500.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        # 第 5 段是 500Hz peaking Q=1.4
        bands = [0.0] * 10
        bands[4] = 6.0
        flt.set_params(StripEQParams(mode="10-band", bands=tuple(bands)))
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 400:])))
        out_peak = float(np.max(np.abs(out[0, 400:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert abs(gain_db - 6.0) < 0.5, f"peaking @500Hz: {gain_db:.2f} dB (expected ~+6)"

    def test_low_cut_attenuates_low_frequencies(self):
        """Low-Cut @200Hz 应大幅衰减 50Hz 信号。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 50.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params(StripEQParams(
            mode="3-band", bands=(0.0, 0.0, 0.0),
            low_cut=200.0, low_cut_enable=True,
        ))
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert gain_db < -10.0, f"low-cut should attenuate, got {gain_db:.2f} dB"

    def test_high_cut_attenuates_high_frequencies(self):
        """High-Cut @2000Hz 应衰减 10kHz 信号。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 10000.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params(StripEQParams(
            mode="3-band", bands=(0.0, 0.0, 0.0),
            high_cut=2000.0, high_cut_enable=True,
        ))
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert gain_db < -10.0, f"high-cut should attenuate, got {gain_db:.2f} dB"

    def test_output_gain_applies_uniformly(self):
        """整体增益 +6dB 应使所有样本提升约 2x。"""
        sr = 48000.0
        samples = np.random.randn(2, 1024).astype(np.float32) * 0.3
        flt = StripEQFilter(sr)
        flt.set_params(StripEQParams(mode="3-band", bands=(0.0, 0.0, 0.0), gain=6.0))
        out = flt.process(samples)
        # 平直 bands + +6dB gain ≈ 2x 线性
        ratio = float(np.mean(np.abs(out)) / np.mean(np.abs(samples)))
        assert abs(ratio - 2.0) < 0.05, f"gain ratio {ratio:.3f} (expected ~2.0)"

    def test_mode_switch_resets_state(self):
        """切换 mode（频段数变化）不崩，且状态正确重建。"""
        sr = 48000.0
        flt = StripEQFilter(sr)
        samples = np.random.randn(2, 512).astype(np.float32) * 0.3
        flt.set_params(StripEQParams.default("3-band"))
        flt.process(samples)
        # 切到 15-band
        flt.set_params(StripEQParams.default("15-band"))
        out = flt.process(samples)
        assert out.shape == samples.shape
        assert np.allclose(out, samples, atol=1e-3)

    def test_cross_block_continuity(self):
        """分两块处理 vs 整块处理，结果应一致（IIR 状态跨块连续）。"""
        sr = 48000.0
        samples = np.random.randn(2, 2048).astype(np.float32) * 0.3
        params = StripEQParams(mode="10-band",
                               bands=(3.0, 0, 0, 0, 3.0, 0, 0, 0, 3.0, 0))
        # 整块
        flt_full = StripEQFilter(sr)
        flt_full.set_params(params)
        full = flt_full.process(samples.copy())
        # 分两块
        flt_split = StripEQFilter(sr)
        flt_split.set_params(params)
        h1 = flt_split.process(samples[:, :1024].copy())
        h2 = flt_split.process(samples[:, 1024:].copy())
        split = np.concatenate([h1, h2], axis=1)
        # 比较后半段（前段可能有启动瞬态）
        diff = np.max(np.abs(full[:, 1024:] - split[:, 1024:]))
        assert diff < 1e-4, f"cross-block discontinuity: {diff:.6f}"


# =============================================================================
# 3. LimiterEffect 限幅器 DSP
# =============================================================================

class TestLimiterEffect:
    """LimiterEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(LimiterEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """未调用 set_params 或 enabled=False 时旁路。"""
        lim = LimiterEffect(48000.0)
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        assert lim.process(samples) is samples  # 默认 _enabled=False

        lim.set_params({"threshold_db": -3.0, "enabled": False})
        assert lim.process(samples) is samples

    def test_rejects_non_2d(self):
        """1D 输入原样返回（shape 契约保护）。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -3.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert lim.process(samples_1d) is samples_1d

    def test_limits_above_threshold(self):
        """超过阈值的峰值被压到阈值附近。"""
        lim = LimiterEffect(48000.0)
        # 阈值 -6 dB ≈ 0.501 linear
        lim.set_params({"threshold_db": -6.0, "release_ms": 100.0, "enabled": True})
        # 单声道，恒定大幅信号（远超阈值）
        samples = np.ones((1, 1024), dtype=np.float32) * 0.9
        out = lim.process(samples)
        # 处理后峰值应 <= 阈值（留 1% 容差给包络跟随的瞬时）
        threshold_lin = 10.0 ** (-6.0 / 20.0)
        peak_out = float(np.max(np.abs(out)))
        assert peak_out <= threshold_lin * 1.02, f"峰值 {peak_out} 超过阈值 {threshold_lin}"

    def test_preserves_below_threshold(self):
        """远低于阈值的信号基本不受影响。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -3.0, "release_ms": 100.0, "enabled": True})
        # 阈值 -3dB ≈ 0.708，输入 0.1 远低于
        samples = np.ones((1, 512), dtype=np.float32) * 0.1
        out = lim.process(samples)
        # gain 应为 1.0，信号不变
        assert np.allclose(out, samples, atol=1e-5)

    def test_link_channels(self):
        """多声道联动：所有声道应用相同 gain（保持声像）。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -6.0, "release_ms": 100.0, "enabled": True})
        # 左声道满幅，右声道小信号
        samples = np.array([
            [0.9, 0.9, 0.9, 0.9],
            [0.1, 0.1, 0.1, 0.1],
        ], dtype=np.float32)
        out = lim.process(samples)
        # 左右声道应被同一 gain 缩放（左被压，右也被压相同比例）
        ratio_left = out[0, 0] / samples[0, 0]
        ratio_right = out[1, 0] / samples[1, 0]
        assert abs(ratio_left - ratio_right) < 1e-4, \
            f"声道 gain 不一致：左 {ratio_left} 右 {ratio_right}"

    def test_envelope_continues_across_blocks(self):
        """包络状态跨块连续（reset_state 后清零）。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -6.0, "release_ms": 50.0, "enabled": True})
        # 第一块：大幅信号拉高包络
        block1 = np.ones((1, 256), dtype=np.float32) * 0.9
        lim.process(block1)
        assert lim._envelope > 0.5  # 包络已被拉高
        # 第二块：静音，包络应从高位衰减（非清零）
        block2 = np.zeros((1, 256), dtype=np.float32)
        lim.process(block2)
        # 衰减后仍应有残留（release 50ms，256 样本远不够衰减完）
        assert lim._envelope > 0.01

        # reset_state 清零
        lim.reset_state()
        assert lim._envelope == 0.0

    def test_release_speed(self):
        """release 越小衰减越快（_release_coeff 越小）。"""
        lim_fast = LimiterEffect(48000.0)
        lim_fast.set_params({"threshold_db": -6.0, "release_ms": 10.0, "enabled": True})
        lim_slow = LimiterEffect(48000.0)
        lim_slow.set_params({"threshold_db": -6.0, "release_ms": 500.0, "enabled": True})
        assert lim_fast._release_coeff < lim_slow._release_coeff

    def test_default_params(self):
        """构造后默认参数与常量一致。"""
        lim = LimiterEffect(48000.0)
        assert lim._threshold_db == _LIMITER_DEFAULT_THRESHOLD_DB
        assert lim._release_ms == _LIMITER_DEFAULT_RELEASE_MS
        assert lim._enabled is False  # 默认未启用

    def test_is_active_reflects_enabled(self):
        lim = LimiterEffect(48000.0)
        assert not lim.is_active
        lim.set_params({"threshold_db": -3.0, "enabled": True})
        assert lim.is_active


# =============================================================================
# 3b. CompressorEffect 压缩器 DSP
# =============================================================================

class TestCompressorEffect:
    """CompressorEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(CompressorEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """未调用 set_params 或 enabled=False 时旁路。"""
        comp = CompressorEffect(48000.0)
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        assert comp.process(samples) is samples  # 默认 _enabled=False

    def test_rejects_non_2d(self):
        """1D 输入原样返回。"""
        comp = CompressorEffect(48000.0)
        comp.set_params({"threshold_db": -12.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert comp.process(samples_1d) is samples_1d

    def test_compresses_above_threshold(self):
        """高于阈值的信号被压缩（RMS 下降）。"""
        sr = 48000.0
        n = 8192
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)  # 0 dBFS peak
        samples = np.stack([sig, sig])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, attack_ms=1.0,
            release_ms=100.0, knee_db=0.0,
        ).to_dict())
        out = comp.process(samples)

        in_rms = float(np.sqrt(np.mean(samples[0, 500:] ** 2)))
        out_rms = float(np.sqrt(np.mean(out[0, 500:] ** 2)))
        reduction_db = 20 * np.log10(out_rms / in_rms)
        # 0 dBFS sine (RMS -3 dB) 远超阈值 -12，应明显压缩
        assert reduction_db < -4.0, f"reduction {reduction_db:.2f} dB should be < -4"

    def test_preserves_below_threshold(self):
        """低于阈值的信号几乎不压缩。"""
        sr = 48000.0
        n = 8192
        t = np.arange(n) / sr
        sig = (0.05 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)  # RMS ~ -29 dB
        samples = np.stack([sig, sig])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, knee_db=0.0,
        ).to_dict())
        out = comp.process(samples)

        in_rms = float(np.sqrt(np.mean(samples[0, 500:] ** 2)))
        out_rms = float(np.sqrt(np.mean(out[0, 500:] ** 2)))
        ratio = out_rms / in_rms
        assert abs(ratio - 1.0) < 0.05, f"below-threshold ratio {ratio:.4f} should be ~1.0"

    def test_makeup_gain_boosts_output(self):
        """makeup_gain 提升整体输出电平。"""
        sr = 48000.0
        n = 8192
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])

        comp_no_makeup = CompressorEffect(sr)
        comp_no_makeup.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, makeup_gain=0.0, knee_db=0.0,
        ).to_dict())
        out_no = comp_no_makeup.process(samples.copy())

        comp_makeup = CompressorEffect(sr)
        comp_makeup.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, makeup_gain=6.0, knee_db=0.0,
        ).to_dict())
        out_yes = comp_makeup.process(samples.copy())

        no_rms = float(np.sqrt(np.mean(out_no[0, 500:] ** 2)))
        yes_rms = float(np.sqrt(np.mean(out_yes[0, 500:] ** 2)))
        assert yes_rms > no_rms * 1.5, "+6dB makeup should boost >1.5x"

    def test_link_channels(self):
        """声道联动：两声道应用相同 gain（声像不漂移）。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        # 左声道大幅、右声道小幅（不同内容）
        left = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        right = (0.1 * np.sin(2 * np.pi * 880.0 * t)).astype(np.float32)
        samples = np.stack([left, right])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, knee_db=0.0,
        ).to_dict())
        out = comp.process(samples)

        # 两声道每采样的 gain 应一致（link channels）
        # gain = out / input（在 input 非零处）
        nonzero = np.abs(samples[0]) > 0.1
        gain_l = out[0, nonzero] / samples[0, nonzero]
        nonzero_r = np.abs(samples[1]) > 0.01
        gain_r = out[1, nonzero_r] / samples[1, nonzero_r]
        # 取各自稳态均值（attack 后）
        gl_steady = float(np.mean(gain_l[-200:]))
        gr_steady = float(np.mean(gain_r[-200:]))
        assert abs(gl_steady - gr_steady) < 0.05, \
            f"linked gains differ: L={gl_steady:.4f} R={gr_steady:.4f}"

    def test_cross_block_continuity(self):
        """包络跨块连续。"""
        sr = 48000.0
        n = 8192
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])

        params = CompressorParams(
            threshold_db=-12.0, ratio=4.0,
            attack_ms=5.0, release_ms=200.0,
        ).to_dict()

        comp_full = CompressorEffect(sr)
        comp_full.set_params(params)
        full = comp_full.process(samples.copy())

        comp_split = CompressorEffect(sr)
        comp_split.set_params(params)
        h1 = comp_split.process(samples[:, :4096].copy())
        h2 = comp_split.process(samples[:, 4096:].copy())
        split = np.concatenate([h1, h2], axis=1)

        diff = np.max(np.abs(full[:, 4096:] - split[:, 4096:]))
        assert diff < 1e-4, f"cross-block discontinuity: {diff:.6f}"

    def test_soft_knee_does_not_crash(self):
        """soft-knee（knee>0）不崩，且产生平滑过渡。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, knee_db=20.0,
        ).to_dict())
        out = comp.process(samples)
        assert out.shape == samples.shape

    def test_default_params(self):
        """构造后默认参数与常量一致。"""
        from src.features.edit.core.audio_dsp import (
            _COMP_DEFAULT_THRESHOLD_DB, _COMP_DEFAULT_RATIO,
            _COMP_DEFAULT_ATTACK_MS, _COMP_DEFAULT_RELEASE_MS,
            _COMP_DEFAULT_KNEE_DB, _COMP_DEFAULT_MAKEUP_DB,
        )
        comp = CompressorEffect(48000.0)
        assert comp._threshold_db == _COMP_DEFAULT_THRESHOLD_DB
        assert comp._ratio == _COMP_DEFAULT_RATIO
        assert comp._attack_ms == _COMP_DEFAULT_ATTACK_MS
        assert comp._release_ms == _COMP_DEFAULT_RELEASE_MS
        assert comp._knee_db == _COMP_DEFAULT_KNEE_DB
        assert comp._makeup_db == _COMP_DEFAULT_MAKEUP_DB
        assert comp._enabled is False

    def test_is_active_reflects_enabled(self):
        comp = CompressorEffect(48000.0)
        assert not comp.is_active
        comp.set_params({"threshold_db": -12.0, "enabled": True})
        assert comp.is_active

    def test_reset_state(self):
        """reset_state 重置包络。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(threshold_db=-12.0, ratio=4.0).to_dict())
        comp.process(samples)
        assert comp._env_db > -100.0  # 拉起了包络
        comp.reset_state()
        assert comp._env_db < -150.0  # 重置回静默


# =============================================================================
# 3c. ReverbEffect 混响器 DSP（Schroeder FDN）
# =============================================================================

class TestReverbEffect:
    """ReverbEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(ReverbEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """未调用 set_params 或 enabled=False 时旁路。"""
        rev = ReverbEffect(48000.0)
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        assert rev.process(samples) is samples  # 默认 _enabled=False

    def test_rejects_non_2d(self):
        """1D 输入原样返回。"""
        rev = ReverbEffect(48000.0)
        rev.set_params({"mix": 30.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert rev.process(samples_1d) is samples_1d

    def test_mix_zero_is_dry(self):
        """mix=0 时完全干信号（直通）。"""
        sr = 48000.0
        t = np.arange(2048) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])

        rev = ReverbEffect(sr)
        rev.set_params(ReverbParams(mix=0.0).to_dict())
        out = rev.process(samples)
        assert np.allclose(out, samples, atol=1e-6)

    def test_produces_tail_after_impulse(self):
        """脉冲后湿信号持续多个样本（混响拖尾）。"""
        sr = 48000.0
        impulse = np.zeros(8192, dtype=np.float32)
        impulse[100:200] = 1.0  # 短脉冲
        samples = np.stack([impulse, impulse])

        rev = ReverbEffect(sr)
        rev.set_params(ReverbParams(mix=100.0, decay=2.0).to_dict())  # 100% wet
        out = rev.process(samples)

        # 脉冲后 2000-4000 样本处应有能量（输入是 0，输出非 0 = 拖尾）
        tail_energy = float(np.sqrt(np.mean(out[0, 2000:4000] ** 2)))
        assert tail_energy > 0.001, f"tail energy {tail_energy:.6f} should be > 0.001"

    def test_longer_decay_has_longer_tail(self):
        """decay 越大拖尾越长（远处能量更大）。"""
        sr = 48000.0
        impulse = np.zeros(8192, dtype=np.float32)
        impulse[100:200] = 1.0
        samples = np.stack([impulse, impulse])

        rev_long = ReverbEffect(sr)
        rev_long.set_params(ReverbParams(mix=100.0, decay=5.0).to_dict())
        out_long = rev_long.process(samples)

        rev_short = ReverbEffect(sr)
        rev_short.set_params(ReverbParams(mix=100.0, decay=0.5).to_dict())
        out_short = rev_short.process(samples)

        # 远处（7000+）长 decay 能量应更大
        long_e = float(np.sqrt(np.mean(out_long[0, 7000:] ** 2)))
        short_e = float(np.sqrt(np.mean(out_short[0, 7000:] ** 2)))
        assert long_e > short_e, f"long {long_e:.6f} should be > short {short_e:.6f}"

    def test_cross_block_continuity(self):
        """延迟线状态跨块连续。"""
        sr = 48000.0
        t = np.arange(8192) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        params = ReverbParams(mix=50.0, decay=2.0).to_dict()

        rev_full = ReverbEffect(sr)
        rev_full.set_params(params)
        full = rev_full.process(samples.copy())

        rev_split = ReverbEffect(sr)
        rev_split.set_params(params)
        h1 = rev_split.process(samples[:, :4096].copy())
        h2 = rev_split.process(samples[:, 4096:].copy())
        split = np.concatenate([h1, h2], axis=1)

        diff = np.max(np.abs(full[:, 4096:] - split[:, 4096:]))
        assert diff < 1e-5, f"cross-block discontinuity: {diff:.8f}"

    def test_stereo_symmetric_input_symmetric_output(self):
        """L==R 输入时输出也 L==R（Freeverb 单声道混响均分到两声道）。"""
        sr = 48000.0
        t = np.arange(4096) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])

        rev = ReverbEffect(sr)
        rev.set_params(ReverbParams(mix=50.0).to_dict())
        out = rev.process(samples)
        assert np.allclose(out[0], out[1], atol=1e-10)

    def test_reset_state_clears_tail(self):
        """reset_state 后无拖尾残留。"""
        sr = 48000.0
        impulse = np.zeros(4096, dtype=np.float32)
        impulse[100:200] = 1.0
        samples = np.stack([impulse, impulse])

        rev = ReverbEffect(sr)
        rev.set_params(ReverbParams(mix=100.0, decay=3.0).to_dict())
        rev.process(samples)
        # 重置后处理静音，应无拖尾
        rev.reset_state()
        silent = np.zeros((2, 1024), dtype=np.float32)
        out = rev.process(silent)
        # 头几百样本可能极少残留（缓冲清零），整体应接近 0
        assert float(np.max(np.abs(out))) < 1e-6

    def test_default_params(self):
        """构造后默认参数与常量一致。"""
        from src.features.edit.core.audio_dsp import (
            _REVERB_DEFAULT_MIX, _REVERB_DEFAULT_DECAY,
            _REVERB_DEFAULT_PRE_DELAY_MS, _REVERB_DEFAULT_DAMPING_HZ,
        )
        rev = ReverbEffect(48000.0)
        assert rev._mix == _REVERB_DEFAULT_MIX
        assert rev._decay == _REVERB_DEFAULT_DECAY
        assert rev._pre_delay_ms == _REVERB_DEFAULT_PRE_DELAY_MS
        assert rev._damping_hz == _REVERB_DEFAULT_DAMPING_HZ
        assert rev._enabled is False

    def test_is_active_reflects_enabled(self):
        rev = ReverbEffect(48000.0)
        assert not rev.is_active
        rev.set_params({"mix": 30.0, "enabled": True})
        assert rev.is_active


# =============================================================================
# 3d. DelayEffect 延迟器 DSP
# =============================================================================

class TestDelayEffect:
    """DelayEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(DelayEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """未调用 set_params 时旁路。"""
        dly = DelayEffect(48000.0)
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        assert dly.process(samples) is samples

    def test_rejects_non_2d(self):
        """1D 输入原样返回。"""
        dly = DelayEffect(48000.0)
        dly.set_params({"time_ms": 100.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert dly.process(samples_1d) is samples_1d

    def test_delay_time_accurate(self):
        """延迟时间准确：脉冲在 N+delay_samples 处出现。"""
        sr = 48000.0
        delay_ms = 100.0
        delay_samples = int(round(delay_ms * 0.001 * sr))  # 4800
        n = delay_samples + 200
        impulse = np.zeros(n, dtype=np.float32)
        impulse[100] = 1.0
        samples = np.stack([impulse, np.zeros(n, dtype=np.float32)])

        dly = DelayEffect(sr)
        dly.set_params(DelayParams(
            time_ms=delay_ms, feedback=0.0, mix=100.0, ping_pong=False,
        ).to_dict())
        out = dly.process(samples)

        # 100% wet：干信号被压制（L[100] ≈ 0），延迟脉冲在 L[4900]
        assert abs(out[0, 100]) < 0.01, f"dry should be suppressed: {out[0,100]}"
        assert out[0, 4900] > 0.5, f"delayed pulse should appear: {out[0,4900]}"

    def test_mix_zero_is_dry(self):
        """mix=0 时完全干信号。"""
        sr = 48000.0
        t = np.arange(2048) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])

        dly = DelayEffect(sr)
        dly.set_params(DelayParams(mix=0.0).to_dict())
        out = dly.process(samples)
        assert np.allclose(out, samples, atol=1e-6)

    def test_ping_pong_alternates_channels(self):
        """ping-pong 模式下回声在 L/R 之间交替。"""
        sr = 48000.0
        delay_ms = 50.0
        delay_samples = int(round(delay_ms * 0.001 * sr))  # 2400
        n = delay_samples * 4 + 200
        impulse = np.zeros(n, dtype=np.float32)
        impulse[100] = 1.0
        samples = np.stack([impulse, np.zeros(n, dtype=np.float32)])

        dly = DelayEffect(sr)
        dly.set_params(DelayParams(
            time_ms=delay_ms, feedback=60.0, mix=100.0, ping_pong=True,
        ).to_dict())
        out = dly.process(samples)

        # echo1 (2500) in L, echo2 (4900) in R, echo3 (7300) in L
        assert out[0, 2500] > 0.5, f"echo1 in L: {out[0,2500]}"
        assert out[1, 4900] > 0.3, f"echo2 in R: {out[1,4900]}"
        assert out[0, 7300] > 0.1, f"echo3 back in L: {out[0,7300]}"

    def test_no_pingpong_keeps_echo_in_same_channel(self):
        """非 ping-pong 模式回声留在原声道。"""
        sr = 48000.0
        delay_ms = 50.0
        delay_samples = int(round(delay_ms * 0.001 * sr))
        n = delay_samples * 2 + 200
        impulse = np.zeros(n, dtype=np.float32)
        impulse[100] = 1.0
        samples = np.stack([impulse, np.zeros(n, dtype=np.float32)])

        dly = DelayEffect(sr)
        dly.set_params(DelayParams(
            time_ms=delay_ms, feedback=50.0, mix=100.0, ping_pong=False,
        ).to_dict())
        out = dly.process(samples)

        # echo1 在 L，echo2 也在 L（同声道反馈）
        assert out[0, 2500] > 0.5
        assert out[0, 4900] > 0.2
        # R 声道应几乎无信号
        assert abs(out[1, 2500]) < 0.05

    def test_cross_block_continuity(self):
        """延迟线状态跨块连续。"""
        sr = 48000.0
        t = np.arange(8192) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        params = DelayParams(time_ms=50.0, feedback=40.0, mix=50.0).to_dict()

        dly_full = DelayEffect(sr)
        dly_full.set_params(params)
        full = dly_full.process(samples.copy())

        dly_split = DelayEffect(sr)
        dly_split.set_params(params)
        h1 = dly_split.process(samples[:, :4096].copy())
        h2 = dly_split.process(samples[:, 4096:].copy())
        split = np.concatenate([h1, h2], axis=1)

        diff = np.max(np.abs(full[:, 4096:] - split[:, 4096:]))
        assert diff < 1e-5, f"cross-block discontinuity: {diff:.8f}"

    def test_higher_feedback_sustains_longer(self):
        """feedback 越大回声持续越长。"""
        sr = 48000.0
        delay_ms = 50.0
        delay_samples = int(round(delay_ms * 0.001 * sr))
        n = delay_samples * 5 + 200
        impulse = np.zeros(n, dtype=np.float32)
        impulse[100] = 1.0
        samples = np.stack([impulse, np.zeros(n, dtype=np.float32)])

        dly_high = DelayEffect(sr)
        dly_high.set_params(DelayParams(time_ms=delay_ms, feedback=80.0, mix=100.0).to_dict())
        out_high = dly_high.process(samples)

        dly_low = DelayEffect(sr)
        dly_low.set_params(DelayParams(time_ms=delay_ms, feedback=20.0, mix=100.0).to_dict())
        out_low = dly_low.process(samples)

        # 远处（第 4 次回声）高 feedback 应更大
        high_e = float(np.max(np.abs(out_high[0, 4 * delay_samples + 100:])))
        low_e = float(np.max(np.abs(out_low[0, 4 * delay_samples + 100:])))
        assert high_e > low_e

    def test_reset_state_clears_buffer(self):
        """reset_state 后延迟线清空。"""
        sr = 48000.0
        delay_ms = 50.0
        delay_samples = int(round(delay_ms * 0.001 * sr))
        impulse = np.zeros(delay_samples + 200, dtype=np.float32)
        impulse[100] = 1.0
        samples = np.stack([impulse, np.zeros(delay_samples + 200, dtype=np.float32)])

        dly = DelayEffect(sr)
        dly.set_params(DelayParams(time_ms=delay_ms, feedback=60.0, mix=100.0).to_dict())
        dly.process(samples)
        dly.reset_state()
        # 处理静音，应无残留回声
        silent = np.zeros((2, delay_samples + 200), dtype=np.float32)
        out = dly.process(silent)
        assert float(np.max(np.abs(out))) < 1e-6

    def test_default_params(self):
        """构造后默认参数与常量一致。"""
        from src.features.edit.core.audio_dsp import (
            _DELAY_DEFAULT_TIME_MS, _DELAY_DEFAULT_FEEDBACK, _DELAY_DEFAULT_MIX,
            _DELAY_DEFAULT_PINGPONG, _DELAY_DEFAULT_LOWCUT_HZ,
        )
        dly = DelayEffect(48000.0)
        assert dly._time_ms == _DELAY_DEFAULT_TIME_MS
        assert dly._feedback == _DELAY_DEFAULT_FEEDBACK
        assert dly._mix == _DELAY_DEFAULT_MIX
        assert dly._ping_pong == _DELAY_DEFAULT_PINGPONG
        assert dly._low_cut_hz == _DELAY_DEFAULT_LOWCUT_HZ
        assert dly._enabled is False

    def test_is_active_reflects_enabled(self):
        dly = DelayEffect(48000.0)
        assert not dly.is_active
        dly.set_params({"time_ms": 100.0, "enabled": True})
        assert dly.is_active


# =============================================================================
# 3e. SaturationEffect 谐波失真 DSP
# =============================================================================

class TestSaturationEffect:
    """SaturationEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(SaturationEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """未调用 set_params 时旁路。"""
        sat = SaturationEffect(48000.0)
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        assert sat.process(samples) is samples

    def test_rejects_non_2d(self):
        """1D 输入原样返回。"""
        sat = SaturationEffect(48000.0)
        sat.set_params({"drive": 50.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert sat.process(samples_1d) is samples_1d

    def test_drive_zero_is_passthrough(self):
        """drive=0 时 k=0，完全直通。"""
        sr = 48000.0
        t = np.arange(2048) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])
        sat = SaturationEffect(sr)
        sat.set_params(SaturationParams(drive=0.0, enabled=True).to_dict())
        out = sat.process(samples)
        assert np.allclose(out, samples, atol=1e-6)

    def test_drive_positive_changes_signal(self):
        """drive>0 应改变信号（sigmoid 非线性映射）。"""
        sr = 48000.0
        t = np.arange(4096) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])
        sat = SaturationEffect(sr)
        sat.set_params(SaturationParams(drive=50.0, enabled=True).to_dict())
        out = sat.process(samples)
        assert not np.allclose(out, samples, atol=1e-3), "drive=50 should change signal"

    def test_overload_peak_is_soft_clipped(self):
        """过载信号（峰值 > 1.0）被软限幅（峰值下降）。"""
        sr = 48000.0
        t = np.arange(4096) / sr
        sig = (1.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)  # 峰值 1.5
        samples = np.stack([sig, sig])

        sat = SaturationEffect(sr)
        sat.set_params(SaturationParams(drive=50.0, enabled=True).to_dict())
        out = sat.process(samples)

        in_pk = float(np.max(np.abs(samples)))
        out_pk = float(np.max(np.abs(out)))
        assert out_pk < in_pk, f"overload soft-clip: {out_pk:.3f} should be < {in_pk:.3f}"

    def test_mid_amplitude_is_boosted(self):
        """中等幅度（0.5 附近）被提升（动态范围压缩，sigmoid 曲线特性）。"""
        sr = 48000.0
        t = np.arange(4096) / sr
        sig = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)  # 峰值 0.5
        samples = np.stack([sig, sig])

        sat = SaturationEffect(sr)
        sat.set_params(SaturationParams(drive=50.0, enabled=True).to_dict())
        out = sat.process(samples)

        in_pk = float(np.max(np.abs(samples)))
        out_pk = float(np.max(np.abs(out)))
        # 0.5 处 sigmoid 输出 > 0.5（中段提升）
        assert out_pk > in_pk, f"mid-amp boost: {out_pk:.3f} should be > {in_pk:.3f}"

    def test_low_amplitude_is_linearly_boosted(self):
        """低电平信号（<<1）被线性放大（sigmoid 在小输入处接近 (1+k)*x）。

        这是 audio_pro sigmoid 曲线的特性：y = (1+k)*x / (1+k*|x|)，
        当 |x| 很小时分母≈1，故 y ≈ (1+k)*x（线性放大，无削波）。
        """
        sr = 48000.0
        t = np.arange(2048) / sr
        sig = (0.01 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        sat = SaturationEffect(sr)
        sat.set_params(SaturationParams(drive=50.0, enabled=True).to_dict())
        out = sat.process(samples)
        # drive=50 → k=25 → 低电平应被放大约 (1+25)=26 倍
        # 用中值（避开 0）比较比例
        nonzero = np.abs(samples[0]) > 1e-6
        ratio = float(np.median(np.abs(out[0, nonzero]) / np.abs(samples[0, nonzero])))
        assert 20.0 < ratio < 30.0, f"low-amp boost ratio {ratio:.2f} should be ~26 (1+k)"

    def test_preserves_shape_and_dtype(self):
        """处理保持 shape 与 dtype。"""
        sr = 48000.0
        samples = np.random.randn(2, 512).astype(np.float32) * 0.5
        sat = SaturationEffect(sr)
        sat.set_params(SaturationParams(drive=80.0, enabled=True).to_dict())
        out = sat.process(samples)
        assert out.shape == samples.shape
        assert out.dtype == samples.dtype

    def test_no_state_to_reset(self):
        """reset_state 是空操作（无状态效果），不崩。"""
        sat = SaturationEffect(48000.0)
        sat.set_params(SaturationParams(drive=50.0, enabled=True).to_dict())
        sat.reset_state()  # 应无异常

    def test_default_params(self):
        from src.features.edit.core.audio_dsp import _SAT_DEFAULT_DRIVE
        sat = SaturationEffect(48000.0)
        assert sat._drive == _SAT_DEFAULT_DRIVE
        assert sat._enabled is False

    def test_is_active_reflects_enabled_and_drive(self):
        sat = SaturationEffect(48000.0)
        assert not sat.is_active
        sat.set_params({"drive": 50.0, "enabled": True})
        assert sat.is_active
        # drive=0 + enabled → 不活跃（无效果）
        sat.set_params({"drive": 0.0, "enabled": True})
        assert not sat.is_active


# =============================================================================
# 3f. ImagerEffect 立体声成像 DSP
# =============================================================================

class TestImagerEffect:
    """ImagerEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(ImagerEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """未调用 set_params 时旁路。"""
        img = ImagerEffect(48000.0)
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        assert img.process(samples) is samples

    def test_rejects_non_2d(self):
        """1D 输入原样返回。"""
        img = ImagerEffect(48000.0)
        img.set_params({"width": 150.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert img.process(samples_1d) is samples_1d

    def test_default_is_near_identity(self):
        """width=100 pan=0 应接近直通。"""
        sr = 48000.0
        samples = np.random.randn(2, 2048).astype(np.float32) * 0.5
        img = ImagerEffect(sr)
        img.set_params(ImagerParams(width=100.0, pan=0.0, enabled=True).to_dict())
        out = img.process(samples)
        assert np.allclose(out, samples, atol=1e-6)

    def test_width_zero_produces_mono(self):
        """width=0 应输出 mono（L==R）。"""
        sr = 48000.0
        samples = np.random.randn(2, 2048).astype(np.float32) * 0.5
        img = ImagerEffect(sr)
        img.set_params(ImagerParams(width=0.0, pan=0.0, enabled=True).to_dict())
        out = img.process(samples)
        # width=0 → Side gain=0 → L=M, R=M（mono）
        assert np.allclose(out[0], out[1], atol=1e-6), "width=0 should produce mono (L==R)"

    def test_width_200_doubles_side(self):
        """width=200 应使 Side 信号加倍（超宽）。"""
        # 用纯 Side 信号测试：L=1, R=-1（M=0, S=1）
        side_sig = np.array([[1.0, 1.0, 1.0], [-1.0, -1.0, -1.0]], dtype=np.float32)
        img = ImagerEffect(48000.0)
        img.set_params(ImagerParams(width=200.0, pan=0.0, enabled=True).to_dict())
        out = img.process(side_sig)
        # M=0, S=1, width=200 → S*2=2 → L=0+2=2, R=0-2=-2
        assert abs(out[0, 0] - 2.0) < 1e-5
        assert abs(out[1, 0] - (-2.0)) < 1e-5

    def test_pan_left_silences_right(self):
        """pan=-100 应使 R 声道静音。"""
        sr = 48000.0
        samples = np.random.randn(2, 2048).astype(np.float32) * 0.5
        img = ImagerEffect(sr)
        img.set_params(ImagerParams(width=100.0, pan=-100.0, enabled=True).to_dict())
        out = img.process(samples)
        assert np.allclose(out[1], 0.0, atol=1e-6), "pan=-100 should silence R"

    def test_pan_right_silences_left(self):
        """pan=+100 应使 L 声道静音。"""
        sr = 48000.0
        samples = np.random.randn(2, 2048).astype(np.float32) * 0.5
        img = ImagerEffect(sr)
        img.set_params(ImagerParams(width=100.0, pan=100.0, enabled=True).to_dict())
        out = img.process(samples)
        assert np.allclose(out[0], 0.0, atol=1e-6), "pan=100 should silence L"

    def test_mono_input_width_no_effect(self):
        """单声道输入 width 无效（无 Side 信号），仅 pan 平均生效。"""
        sr = 48000.0
        mono = np.random.randn(1, 2048).astype(np.float32) * 0.5
        img = ImagerEffect(sr)
        img.set_params(ImagerParams(width=200.0, pan=0.0, enabled=True).to_dict())
        out = img.process(mono)
        # 单声道 pan=0 → 平均增益=1.0，直通
        assert np.allclose(out, mono, atol=1e-6)

    def test_preserves_shape_and_dtype(self):
        """处理保持 shape 与 dtype。"""
        sr = 48000.0
        samples = np.random.randn(2, 512).astype(np.float32) * 0.5
        img = ImagerEffect(sr)
        img.set_params(ImagerParams(width=150.0, pan=20.0, enabled=True).to_dict())
        out = img.process(samples)
        assert out.shape == samples.shape
        assert out.dtype == samples.dtype

    def test_default_params(self):
        from src.features.edit.core.audio_dsp import _IMAGER_DEFAULT_PAN, _IMAGER_DEFAULT_WIDTH
        img = ImagerEffect(48000.0)
        assert img._pan == _IMAGER_DEFAULT_PAN
        assert img._width == _IMAGER_DEFAULT_WIDTH
        assert img._enabled is False

    def test_is_active_only_when_effective(self):
        """is_active 仅在 pan≠0 或 width≠100 时为 True。"""
        img = ImagerEffect(48000.0)
        assert not img.is_active
        # width=100 pan=0 → 无效果
        img.set_params({"width": 100.0, "pan": 0.0, "enabled": True})
        assert not img.is_active
        # width≠100 → 有效果
        img.set_params({"width": 150.0, "pan": 0.0, "enabled": True})
        assert img.is_active
        # pan≠0 → 有效果
        img.set_params({"width": 100.0, "pan": 30.0, "enabled": True})
        assert img.is_active


# =============================================================================
# 3g. NoiseGateEffect 噪声门 DSP
# =============================================================================

class TestNoiseGateEffect:
    """NoiseGateEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(NoiseGateEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        """未调用 set_params 时旁路。"""
        ng = NoiseGateEffect(48000.0)
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        assert ng.process(samples) is samples

    def test_rejects_non_2d(self):
        """1D 输入原样返回。"""
        ng = NoiseGateEffect(48000.0)
        ng.set_params({"threshold_db": -30.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert ng.process(samples_1d) is samples_1d

    def test_loud_signal_passes(self):
        """高于阈值的信号几乎直通（门开）。"""
        sr = 48000.0
        t = np.arange(8192) / sr
        sig = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)  # ~-6 dBFS
        samples = np.stack([sig, sig])
        ng = NoiseGateEffect(sr)
        ng.set_params(NoiseGateParams(
            threshold_db=-20.0, attack_ms=1.0, release_ms=50.0,
        ).to_dict())
        out = ng.process(samples)
        # 信号远高于 -20dB，门常开，应几乎直通（稳态段）
        assert np.allclose(out[:, 500:], samples[:, 500:], atol=1e-2)

    def test_quiet_signal_attenuated(self):
        """低于阈值的信号被衰减到接近 0（门关）。"""
        sr = 48000.0
        t = np.arange(16384) / sr  # 长缓冲让门完全关闭
        sig = (0.001 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)  # ~-60 dBFS
        samples = np.stack([sig, sig])
        ng = NoiseGateEffect(sr)
        ng.set_params(NoiseGateParams(
            threshold_db=-30.0, attack_ms=1.0, release_ms=50.0,
        ).to_dict())
        out = ng.process(samples)
        # 稳态段（最后 2000 样本，门已完全关闭）
        in_rms = float(np.sqrt(np.mean(samples[0, -2000:] ** 2)))
        out_rms = float(np.sqrt(np.mean(out[0, -2000:] ** 2)))
        assert out_rms < in_rms * 0.1, f"quiet should be attenuated: {out_rms}/{in_rms}"

    def test_dynamic_signal_gates_correctly(self):
        """动态信号：前半段高（开门），后半段低（关门）。"""
        sr = 48000.0
        t = np.arange(8192) / sr
        sig_loud = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        sig_quiet = (0.001 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        dynamic = np.concatenate([sig_loud[:4096], sig_quiet[4096:]])
        samples = np.stack([dynamic, dynamic])

        ng = NoiseGateEffect(sr)
        ng.set_params(NoiseGateParams(
            threshold_db=-25.0, attack_ms=1.0, release_ms=100.0,
        ).to_dict())
        out = ng.process(samples)

        # 高信号段（2000-4000）应通过
        loud_part = float(np.max(np.abs(out[0, 2000:4000])))
        assert loud_part > 0.3, f"loud part should pass: {loud_part}"
        # 低信号段（7000-8000，门已关）应被衰减
        quiet_part = float(np.max(np.abs(out[0, 7000:8000])))
        assert quiet_part < 0.05, f"quiet part should be gated: {quiet_part}"

    def test_cross_block_continuity(self):
        """门增益状态跨块连续。"""
        sr = 48000.0
        t = np.arange(8192) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        params = NoiseGateParams(
            threshold_db=-25.0, attack_ms=5.0, release_ms=200.0,
        ).to_dict()

        ng_full = NoiseGateEffect(sr)
        ng_full.set_params(params)
        full = ng_full.process(samples.copy())

        ng_split = NoiseGateEffect(sr)
        ng_split.set_params(params)
        h1 = ng_split.process(samples[:, :4096].copy())
        h2 = ng_split.process(samples[:, 4096:].copy())
        split = np.concatenate([h1, h2], axis=1)

        diff = np.max(np.abs(full[:, 4096:] - split[:, 4096:]))
        assert diff < 1e-5, f"cross-block discontinuity: {diff:.8f}"

    def test_link_channels(self):
        """声道联动：两声道应用相同门增益。"""
        sr = 48000.0
        t = np.arange(8192) / sr
        # 左声道高、右声道低
        left = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        right = (0.001 * np.sin(2 * np.pi * 880.0 * t)).astype(np.float32)
        samples = np.stack([left, right])

        ng = NoiseGateEffect(sr)
        ng.set_params(NoiseGateParams(
            threshold_db=-25.0, attack_ms=1.0, release_ms=100.0,
        ).to_dict())
        out = ng.process(samples)

        # 左声道高信号驱动门常开，右声道应跟随（不被衰减，尽管它本身很安静）
        # 比较 R 声道稳态是否被衰减
        r_in = float(np.sqrt(np.mean(samples[1, 2000:4000] ** 2)))
        r_out = float(np.sqrt(np.mean(out[1, 2000:4000] ** 2)))
        # 因为 L 高信号让门常开，R 应几乎直通（不被关门）
        assert r_out > r_in * 0.5, "linked channels: R should pass when L drives gate open"

    def test_reset_state(self):
        """reset_state 重置门增益到 1.0（开）。"""
        sr = 48000.0
        t = np.arange(4096) / sr
        sig = (0.001 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        ng = NoiseGateEffect(sr)
        ng.set_params(NoiseGateParams(threshold_db=-30.0).to_dict())
        ng.process(samples)
        # 门应该关了一些
        assert ng._gate_gain < 0.5
        ng.reset_state()
        assert ng._gate_gain == 1.0

    def test_default_params(self):
        from src.features.edit.core.audio_dsp import (
            _GATE_DEFAULT_THRESHOLD_DB, _GATE_DEFAULT_ATTACK_MS,
            _GATE_DEFAULT_RELEASE_MS,
        )
        ng = NoiseGateEffect(48000.0)
        assert ng._threshold_db == _GATE_DEFAULT_THRESHOLD_DB
        assert ng._attack_ms == _GATE_DEFAULT_ATTACK_MS
        assert ng._release_ms == _GATE_DEFAULT_RELEASE_MS
        assert ng._enabled is False

    def test_is_active_reflects_enabled(self):
        ng = NoiseGateEffect(48000.0)
        assert not ng.is_active
        ng.set_params({"threshold_db": -30.0, "enabled": True})
        assert ng.is_active


# =============================================================================
# 4. EffectChain 效果器链
# =============================================================================

class TestEffectChain:
    """EffectChain 链串联行为。"""

    def test_empty_chain_passthrough(self):
        """空链原样返回输入。"""
        chain = EffectChain(48000.0)
        samples = np.array([[0.5, -0.3]], dtype=np.float32)
        assert chain.process(samples) is samples
        assert not chain.is_active

    def test_none_entries_clears_chain(self):
        chain = EffectChain(48000.0)
        chain.set_params([("limiter", {"threshold_db": -3.0, "enabled": True}, True)])
        assert chain.is_active
        chain.set_params(None)
        assert not chain.is_active
        assert chain.process(np.zeros((1, 4))) is not None  # 不崩

    def test_single_limiter_applied(self):
        """链中单个限幅器生效。"""
        chain = EffectChain(48000.0)
        chain.set_params([("limiter", {"threshold_db": -6.0, "release_ms": 50.0, "enabled": True}, True)])
        samples = np.ones((1, 512), dtype=np.float32) * 0.9
        out = chain.process(samples)
        threshold_lin = 10.0 ** (-6.0 / 20.0)
        assert float(np.max(np.abs(out))) <= threshold_lin * 1.02

    def test_chain_order_matters(self):
        """EQ → Limiter 与 Limiter → EQ 结果不同（顺序生效）。"""
        eq_params = StripEQParams(mode="3-band", bands=(12.0, 0.0, 0.0)).to_dict()
        lim_params = {"threshold_db": -3.0, "release_ms": 50.0, "enabled": True}

        chain_eq_first = EffectChain(48000.0)
        chain_eq_first.set_params([
            ("strip_eq", eq_params, True),
            ("limiter", lim_params, True),
        ])
        chain_lim_first = EffectChain(48000.0)
        chain_lim_first.set_params([
            ("limiter", lim_params, True),
            ("strip_eq", eq_params, True),
        ])

        samples = np.random.randn(2, 512).astype(np.float32) * 0.3
        out1 = chain_eq_first.process(samples.copy())
        out2 = chain_lim_first.process(samples.copy())
        assert not np.allclose(out1, out2, atol=1e-4)

    def test_instance_reuse_on_param_change(self):
        """参数变化时复用同类型实例（保持 DSP 状态连续）。"""
        chain = EffectChain(48000.0)
        chain.set_params([("limiter", {"threshold_db": -3.0, "enabled": True}, True)])
        old_inst = chain._effects[0]
        # 改参数不重建实例
        chain.set_params([("limiter", {"threshold_db": -6.0, "enabled": True}, True)])
        new_inst = chain._effects[0]
        assert new_inst is old_inst

    def test_bypass_flag_disables_effect(self):
        """enabled=False 的条目被跳过。"""
        chain = EffectChain(48000.0)
        chain.set_params([("limiter", {"threshold_db": -6.0, "enabled": True}, False)])  # 链条 enabled=False
        samples = np.ones((1, 256), dtype=np.float32) * 0.9
        out = chain.process(samples)
        # 条目被 bypass，原样返回（未被限幅）
        assert float(np.max(np.abs(out))) >= 0.85
        assert not chain.is_active

    def test_reset_state(self):
        """reset_state 传播到所有效果器。"""
        chain = EffectChain(48000.0)
        chain.set_params([
            ("strip_eq", StripEQParams(mode="3-band", bands=(3.0, 0.0, 0.0)).to_dict(), True),
            ("limiter", {"threshold_db": -3.0, "enabled": True}, True),
        ])
        # 先处理拉高状态
        chain.process(np.ones((2, 256), dtype=np.float32) * 0.9)
        assert chain._effects[1]._envelope > 0.0
        chain.reset_state()
        assert chain._effects[1]._envelope == 0.0

    def test_unknown_effect_type_skipped(self):
        """未知 effect_type 的条目被跳过（不崩）。"""
        chain = EffectChain(48000.0)
        chain.set_params([
            ("unknown_fx", {"foo": 1}, True),
            ("limiter", {"threshold_db": -3.0, "enabled": True}, True),
        ])
        assert len(chain._effects) == 1
        assert isinstance(chain._effects[0], LimiterEffect)

    def test_sample_rate_propagates(self):
        """set_params 时 sample_rate 传播到效果器。"""
        chain = EffectChain(48000.0)
        chain.set_params([("limiter", {"threshold_db": -3.0, "enabled": True}, True)])
        assert chain._effects[0]._sample_rate == 48000.0
        chain.set_params([("limiter", {"threshold_db": -3.0, "enabled": True}, True)], sample_rate=96000.0)
        assert chain._effects[0]._sample_rate == 96000.0
