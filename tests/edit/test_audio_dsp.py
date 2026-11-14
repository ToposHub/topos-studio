"""
Audio DSP 单元测试（CW-A1 重构版）

覆盖：
1. AudioEffect 基类契约（子类必须实现接口）
2. StripEQFilter 参数化 EQ 回归（旁路/启用/shape/频段判别/跨块连续）
3. LimiterEffect 限幅器（阈值/attack/释放/包络跟随/声道联动）
4. CompressorEffect 压缩器（阈值/比率/makeup/并行 mix/声道联动）
5. ReverbEffect 声学房间混响（dry 直通/尾音/RT60/跨块）
6. DelayEffect 立体声延迟（L/R 时间/负反馈/交叉/跨块）
7. Saturation / Imager（未变动，原样回归）
8. ExpanderEffect（Expander/Gate，接替 NoiseGate）
9. EffectChain 链串联（顺序/复用/重置/is_active）

深度行为测试见 test_audio_dsp_pro.py（CW-A1 新增）。
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
    ExpanderEffect,
    EffectChain,
    _LIMITER_DEFAULT_THRESHOLD_DB,
    _LIMITER_DEFAULT_RELEASE_MS,
)
from src.features.edit.core.timeline_v2 import (
    StripEQParams, CompressorParams, ReverbParams, DelayParams,
    SaturationParams, ImagerParams, ExpanderParams,
    default_eq_bands_flat,
)


def _bands(**overrides) -> list:
    """构造参数化 EQ 频段列表（平直 + 指定覆盖）。"""
    bands = [dict(b) for b in default_eq_bands_flat()]
    for key, kv in overrides.items():
        i = int(key)
        bands[i] = {**bands[i], **kv}
    return bands


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
        assert ExpanderEffect.effect_type == "expander"


# =============================================================================
# 2. StripEQFilter 继承回归
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
        flt.set_params({"bands": _bands(), "enabled": False})
        samples = np.array([[0.5, -0.3, 0.8]], dtype=np.float32)
        out = flt.process(samples)
        assert out is samples

    def test_enabled_processes_and_preserves_shape(self):
        """启用时处理样本，shape 与 dtype 保持一致。"""
        flt = StripEQFilter(48000.0)
        flt.set_params({"bands": _bands(**{"2": {"gain": -3.0}})})
        samples = np.random.randn(2, 256).astype(np.float32) * 0.5
        out = flt.process(samples)
        assert out.shape == samples.shape
        assert out.dtype == samples.dtype

    def test_is_active_reflects_state(self):
        flt = StripEQFilter(48000.0)
        assert not flt.is_active
        flt.set_params({"bands": _bands()})
        assert flt.is_active
        flt.set_params({"bands": _bands(), "enabled": False})
        assert not flt.is_active


# =============================================================================
# 2b. StripEQFilter 参数化 DSP
# =============================================================================

class TestStripEQFilterMultiBand:
    """参数化 EQ 的 DSP 正确性。"""

    def test_flat_eq_is_near_identity(self):
        """平直 EQ（所有频段 0 dB）应接近直通。"""
        sr = 48000.0
        samples = np.random.randn(2, 1024).astype(np.float32) * 0.3
        flt = StripEQFilter(sr)
        flt.set_params({"bands": _bands()})
        out = flt.process(samples)
        assert np.allclose(out, samples, atol=1e-3)

    def test_lowshelf_boosts_low_frequencies(self):
        """Band 2 Low Shelf @250Hz +6dB 应在 100Hz 提升约 +6dB。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 100.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params({"bands": _bands(**{"1": {"filter_type": "Low Shelf",
                                                "freq": 250.0, "gain": 6.0,
                                                "q": 1.4, "enabled": True}})})
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert 4.0 < gain_db < 7.5, f"gain {gain_db:.2f} dB not in [4.0, 7.5]"

    def test_highshelf_boosts_high_frequencies(self):
        """Band 5 High Shelf @8.2kHz +6dB 应在 10kHz 明显提升。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 10000.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params({"bands": _bands(**{"4": {"filter_type": "High Shelf",
                                                 "freq": 8200.0, "gain": 6.0,
                                                 "q": 1.2, "enabled": True}})})
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert gain_db > 3.0, f"gain {gain_db:.2f} dB should be > 3.0"

    def test_peaking_at_center_frequency(self):
        """Band 4 Bell @2.4kHz +6dB 应在中心频接近 +6dB。"""
        sr = 48000.0
        n = 8192
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 2400.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params({"bands": _bands(**{"3": {"filter_type": "Bell",
                                                 "freq": 2400.0, "gain": 6.0,
                                                 "q": 1.4, "enabled": True}})})
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 400:])))
        out_peak = float(np.max(np.abs(out[0, 400:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert abs(gain_db - 6.0) < 1.0, f"peaking @2.4kHz: {gain_db:.2f} dB"

    def test_hpf_attenuates_low_frequencies(self):
        """Band 1 HPF @200Hz 24dB/oct 应大幅衰减 50Hz 信号。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 50.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params({"bands": _bands(**{"0": {"filter_type": "HPF",
                                                 "freq": 200.0, "slope": 24,
                                                 "enabled": True}})})
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert gain_db < -10.0, f"HPF should attenuate, got {gain_db:.2f} dB"

    def test_lpf_attenuates_high_frequencies(self):
        """Band 6 LPF @2000Hz 应衰减 10kHz 信号。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 10000.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        flt = StripEQFilter(sr)
        flt.set_params({"bands": _bands(**{"5": {"filter_type": "LPF",
                                                 "freq": 2000.0, "slope": 12,
                                                 "enabled": True}})})
        out = flt.process(samples)
        in_peak = float(np.max(np.abs(samples[0, 200:])))
        out_peak = float(np.max(np.abs(out[0, 200:])))
        gain_db = 20 * np.log10(out_peak / in_peak)
        assert gain_db < -10.0, f"LPF should attenuate, got {gain_db:.2f} dB"

    def test_master_gain_applies_uniformly(self):
        """master_gain +6dB 应使所有样本提升约 2x。"""
        sr = 48000.0
        samples = np.random.randn(2, 1024).astype(np.float32) * 0.3
        flt = StripEQFilter(sr)
        flt.set_params({"bands": _bands(), "master_gain": 6.0})
        out = flt.process(samples)
        ratio = float(np.mean(np.abs(out)) / np.mean(np.abs(samples)))
        assert abs(ratio - 2.0) < 0.05, f"gain ratio {ratio:.3f} (expected ~2.0)"

    def test_shape_switch_resets_state(self):
        """切换频段形状不崩，且状态正确重建。"""
        sr = 48000.0
        flt = StripEQFilter(sr)
        samples = np.random.randn(2, 512).astype(np.float32) * 0.3
        flt.set_params({"bands": _bands()})
        flt.process(samples)
        flt.set_params({"bands": _bands(**{"3": {"filter_type": "Notch",
                                                 "freq": 1000.0, "enabled": True}})})
        out = flt.process(samples)
        assert out.shape == samples.shape

    def test_cross_block_continuity(self):
        """分两块处理 vs 整块处理，结果应一致（IIR 状态跨块连续）。"""
        sr = 48000.0
        samples = np.random.randn(2, 2048).astype(np.float32) * 0.3
        params = {"bands": _bands(**{
            "1": {"filter_type": "Low Shelf", "freq": 110.0, "gain": 3.0, "enabled": True},
            "3": {"filter_type": "Bell", "freq": 2400.0, "gain": 3.0, "enabled": True},
            "4": {"filter_type": "Bell", "freq": 6000.0, "gain": -3.0, "enabled": True},
        })}
        flt_full = StripEQFilter(sr)
        flt_full.set_params(params)
        full = flt_full.process(samples.copy())
        flt_split = StripEQFilter(sr)
        flt_split.set_params(params)
        h1 = flt_split.process(samples[:, :1024].copy())
        h2 = flt_split.process(samples[:, 1024:].copy())
        split = np.concatenate([h1, h2], axis=1)
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

        lim.set_params({"threshold_db": -6.0, "enabled": False})
        assert lim.process(samples) is samples

    def test_rejects_non_2d(self):
        """1D 输入原样返回（shape 契约保护）。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -6.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9, 0.3], dtype=np.float32)
        assert lim.process(samples_1d) is samples_1d

    def test_limits_above_threshold(self):
        """超过阈值的峰值被压到阈值附近。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -6.0, "attack_ms": 0.71,
                        "release_ms": 93.0, "enabled": True})
        samples = np.ones((1, 1024), dtype=np.float32) * 0.9
        out = lim.process(samples)
        threshold_lin = 10.0 ** (-6.0 / 20.0)
        peak_out = float(np.max(np.abs(out)))
        assert peak_out <= threshold_lin * 1.02, f"峰值 {peak_out} 超过阈值 {threshold_lin}"

    def test_preserves_below_threshold(self):
        """远低于阈值的信号基本不受影响。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -3.0, "enabled": True})
        samples = np.ones((1, 512), dtype=np.float32) * 0.1
        out = lim.process(samples)
        assert np.allclose(out, samples, atol=1e-5)

    def test_link_channels(self):
        """多声道联动：所有声道应用相同 gain（保持声像）。

        用足够长的块让 attack 增益收敛（块尾比较，避开启动段与硬保护
        逐样本钳制——钳制只作用于超阈值声道，不破坏增益联动不变量）。
        """
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -6.0, "release_ms": 93.0, "enabled": True})
        n = 2048
        samples = np.stack([
            np.full(n, 0.9, dtype=np.float32),
            np.full(n, 0.1, dtype=np.float32),
        ])
        out = lim.process(samples)
        ratio_left = out[0, -1] / samples[0, -1]
        ratio_right = out[1, -1] / samples[1, -1]
        assert abs(ratio_left - ratio_right) < 1e-3, \
            f"声道 gain 不一致：左 {ratio_left} 右 {ratio_right}"

    def test_envelope_continues_across_blocks(self):
        """包络状态跨块连续（reset_state 后清零）。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -6.0, "release_ms": 93.0, "enabled": True})
        block1 = np.ones((1, 256), dtype=np.float32) * 0.9
        lim.process(block1)
        assert lim._envelope > 0.5
        block2 = np.zeros((1, 256), dtype=np.float32)
        lim.process(block2)
        # release 93ms，256 样本远不够衰减完
        assert lim._envelope > 0.01

        lim.reset_state()
        assert lim._envelope == 0.0

    def test_release_speed(self):
        """release 越小衰减越快（_release_coeff 越小）。"""
        lim_fast = LimiterEffect(48000.0)
        lim_fast.set_params({"threshold_db": -6.0, "release_ms": 50.0, "enabled": True})
        lim_slow = LimiterEffect(48000.0)
        lim_slow.set_params({"threshold_db": -6.0, "release_ms": 4000.0, "enabled": True})
        assert lim_fast._release_coeff < lim_slow._release_coeff

    def test_default_params(self):
        """构造后默认参数与常量一致。"""
        lim = LimiterEffect(48000.0)
        assert lim._threshold_db == _LIMITER_DEFAULT_THRESHOLD_DB
        assert lim._release_ms == _LIMITER_DEFAULT_RELEASE_MS
        assert lim._enabled is False

    def test_is_active_reflects_enabled(self):
        lim = LimiterEffect(48000.0)
        assert not lim.is_active
        lim.set_params({"threshold_db": -6.0, "enabled": True})
        assert lim.is_active

    def test_gr_telemetry_exposed(self):
        """CW-A1: last_gr_db 遥测暴露。"""
        lim = LimiterEffect(48000.0)
        lim.set_params({"threshold_db": -6.0, "enabled": True})
        lim.process(np.ones((1, 512), dtype=np.float32) * 0.9)
        assert lim.last_gr_db > 1.0


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
        assert comp.process(samples) is samples

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
            release_ms=100.0, knee=0.0,
        ).to_dict())
        out = comp.process(samples)

        in_rms = float(np.sqrt(np.mean(samples[0, 500:] ** 2)))
        out_rms = float(np.sqrt(np.mean(out[0, 500:] ** 2)))
        reduction_db = 20 * np.log10(out_rms / in_rms)
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
            threshold_db=-12.0, ratio=4.0, knee=0.0,
        ).to_dict())
        out = comp.process(samples)

        in_rms = float(np.sqrt(np.mean(samples[0, 500:] ** 2)))
        out_rms = float(np.sqrt(np.mean(out[0, 500:] ** 2)))
        ratio = out_rms / in_rms
        assert abs(ratio - 1.0) < 0.05, f"below-threshold ratio {ratio:.4f}"

    def test_makeup_gain_boosts_output(self):
        """makeup_gain 提升整体输出电平。"""
        sr = 48000.0
        n = 8192
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])

        comp_no_makeup = CompressorEffect(sr)
        comp_no_makeup.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, makeup_gain=0.0, knee=0.0,
        ).to_dict())
        out_no = comp_no_makeup.process(samples.copy())

        comp_makeup = CompressorEffect(sr)
        comp_makeup.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, makeup_gain=6.0, knee=0.0,
        ).to_dict())
        out_yes = comp_makeup.process(samples.copy())

        no_rms = float(np.sqrt(np.mean(out_no[0, 500:] ** 2)))
        yes_rms = float(np.sqrt(np.mean(out_yes[0, 500:] ** 2)))
        assert yes_rms > no_rms * 1.5, "+6dB makeup should boost >1.5x"

    def test_parallel_mix_zero_passthrough(self):
        """CW-A1: mix=0（纯干）时直通。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(
            threshold_db=-12.0, ratio=10.0, knee=0.0, mix=0.0,
        ).to_dict())
        out = comp.process(samples)
        assert np.allclose(out, samples, atol=1e-5)

    def test_link_channels(self):
        """声道联动：两声道应用相同 gain（声像不漂移）。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        left = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        right = (0.1 * np.sin(2 * np.pi * 880.0 * t)).astype(np.float32)
        samples = np.stack([left, right])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, knee=0.0,
        ).to_dict())
        out = comp.process(samples)

        nonzero = np.abs(samples[0]) > 0.1
        gain_l = out[0, nonzero] / samples[0, nonzero]
        nonzero_r = np.abs(samples[1]) > 0.01
        gain_r = out[1, nonzero_r] / samples[1, nonzero_r]
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
        """soft-knee（knee>0）不崩。"""
        sr = 48000.0
        n = 4096
        t = np.arange(n) / sr
        sig = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        samples = np.stack([sig, sig])

        comp = CompressorEffect(sr)
        comp.set_params(CompressorParams(
            threshold_db=-12.0, ratio=4.0, knee=60.0,
        ).to_dict())
        out = comp.process(samples)
        assert out.shape == samples.shape

    def test_default_params(self):
        """构造后默认参数与常量一致。"""
        from src.features.edit.core.audio_dsp import (
            _COMP_DEFAULT_THRESHOLD_DB, _COMP_DEFAULT_RATIO,
            _COMP_DEFAULT_ATTACK_MS, _COMP_DEFAULT_RELEASE_MS,
            _COMP_DEFAULT_KNEE, _COMP_DEFAULT_MAKEUP_DB,
        )
        comp = CompressorEffect(48000.0)
        assert comp._threshold_db == _COMP_DEFAULT_THRESHOLD_DB
        assert comp._ratio == _COMP_DEFAULT_RATIO
        assert comp._attack_ms == _COMP_DEFAULT_ATTACK_MS
        assert comp._release_ms == _COMP_DEFAULT_RELEASE_MS
        assert comp._knee == _COMP_DEFAULT_KNEE
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
        assert comp._env_db > -100.0
        comp.reset_state()
        assert comp._env_db < -150.0

    def test_sidechain_listen_broadcasts_detector(self):
        """CW-A1: sc_listen 输出检源广播。"""
        comp = CompressorEffect(48000.0)
        comp.set_params({"threshold_db": -20.0, "sc_listen": True, "enabled": True})
        quiet = np.zeros((2, 256), dtype=np.float32) + 0.02
        det = np.zeros((2, 256), dtype=np.float32) + 0.6
        out = comp.process(quiet, sidechain=det)
        assert np.allclose(out[0], np.max(np.abs(det), axis=0))


# =============================================================================
# 3c. ReverbEffect 声学房间混响 DSP
# =============================================================================

class TestReverbEffect:
    """ReverbEffect（声学房间模型）的 DSP 正确性。"""

    def _wet_params(self, **overrides) -> dict:
        """全湿直达 -inf（便于观察湿声时间结构）。"""
        p = {"enabled": True, "dry_wet": 100.0, "direct_db": -60.0,
             "er_db": 0.0, "reverb_db": 0.0}
        p.update(overrides)
        return p

    def test_is_audio_effect_subclass(self):
        assert issubclass(ReverbEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        rv = ReverbEffect(48000.0)
        samples = np.array([[0.5, -0.3]], dtype=np.float32)
        assert rv.process(samples) is samples

    def test_rejects_non_2d(self):
        rv = ReverbEffect(48000.0)
        rv.set_params({"enabled": True})
        samples_1d = np.array([0.5, 0.9], dtype=np.float32)
        assert rv.process(samples_1d) is samples_1d

    def test_dry_wet_zero_is_passthrough(self):
        """dry_wet=0 + direct 0dB → 直通。"""
        sr = 48000.0
        n = 4800
        t = np.arange(n) / sr
        sig = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        rv = ReverbEffect(sr)
        rv.set_params({"enabled": True, "dry_wet": 0.0, "direct_db": 0.0})
        out = rv.process(samples)
        assert np.allclose(out, samples, atol=1e-3)

    def test_produces_tail_after_impulse(self):
        """脉冲后产生混响尾音。"""
        sr = 48000.0
        imp = np.zeros((2, int(sr)))
        imp[:, 100] = 1.0
        rv = ReverbEffect(sr)
        rv.set_params(self._wet_params(pre_delay=10.0, reverb_time=800.0, distance=2.0))
        out = rv.process(imp)
        w = out.sum(axis=0)
        # 尾音区（ER 跨距之后）应有能量
        tail = np.abs(w[int(sr * 0.15):int(sr * 0.4)])
        assert float(np.max(tail)) > 1e-4, "impulse should produce reverb tail"

    def test_longer_rt60_has_longer_tail(self):
        """更长 RT60 的尾音衰减更慢。"""
        sr = 48000.0
        imp = np.zeros((2, int(sr * 3)))
        imp[:, 100] = 1.0

        def _tail_energy(rt60: float) -> float:
            rv = ReverbEffect(sr)
            rv.set_params(self._wet_params(pre_delay=10.0, distance=2.0, reverb_time=rt60))
            w = np.abs(rv.process(imp).sum(axis=0))
            return float(np.sum(w[int(sr * 1.0):int(sr * 2.0)]))

        e_short = _tail_energy(300.0)
        e_long = _tail_energy(3000.0)
        assert e_long > e_short * 2.0, \
            f"RT60 3s tail {e_long:.4f} should >> RT60 0.3s tail {e_short:.4f}"

    def test_cross_block_continuity(self):
        """延迟线状态跨块连续。"""
        sr = 48000.0
        n = 9600
        t = np.arange(n) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        params = self._wet_params(pre_delay=20.0, distance=5.0, reverb_time=1000.0,
                                  dry_wet=50.0, direct_db=0.0)

        rv_full = ReverbEffect(sr)
        rv_full.set_params(params)
        full = rv_full.process(samples.copy())

        rv_split = ReverbEffect(sr)
        rv_split.set_params(params)
        h1 = rv_split.process(samples[:, :4800].copy())
        h2 = rv_split.process(samples[:, 4800:].copy())
        split = np.concatenate([h1, h2], axis=1)

        diff = np.max(np.abs(full[:, 4800:] - split[:, 4800:]))
        assert diff < 1e-4, f"cross-block discontinuity: {diff:.6f}"

    def test_stereo_symmetric_input_symmetric_output(self):
        """对称立体声输入 → 对称输出（L/R 去相关网络对称构造）。"""
        sr = 48000.0
        n = 9600
        t = np.arange(n) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        rv = ReverbEffect(sr)
        rv.set_params(self._wet_params())
        out = rv.process(samples)
        # L/R 网络独立去相关，能量应同量级（结构对称性）
        e_l = float(np.sqrt(np.mean(out[0] ** 2)))
        e_r = float(np.sqrt(np.mean(out[1] ** 2)))
        assert abs(e_l - e_r) / max(e_l, e_r, 1e-9) < 0.5

    def test_reset_state_clears_tail(self):
        rv = ReverbEffect(48000.0)
        rv.set_params(self._wet_params())
        imp = np.zeros((2, 4800))
        imp[:, 100] = 1.0
        rv.process(imp)
        rv.reset_state()
        assert float(np.max(np.abs(rv._ring))) == 0.0

    def test_default_params(self):
        rv = ReverbEffect(48000.0)
        assert rv._params is not None
        assert rv._params["reverb_time"] == 2730.0
        assert rv._params["dry_wet"] == 100.0

    def test_is_active_reflects_enabled(self):
        rv = ReverbEffect(48000.0)
        assert not rv.is_active
        rv.set_params({"enabled": True})
        assert rv.is_active


# =============================================================================
# 3d. DelayEffect 立体声延迟 DSP
# =============================================================================

class TestDelayEffect:
    """DelayEffect（立体声双线）的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(DelayEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        dl = DelayEffect(48000.0)
        samples = np.array([[0.5, -0.3]], dtype=np.float32)
        assert dl.process(samples) is samples

    def test_rejects_non_2d(self):
        dl = DelayEffect(48000.0)
        dl.set_params({"enabled": True})
        samples_1d = np.array([0.5, 0.9], dtype=np.float32)
        assert dl.process(samples_1d) is samples_1d

    def test_delay_time_accurate(self):
        """左右独立延迟时间准确。"""
        sr = 48000.0
        dl = DelayEffect(sr)
        dl.set_params({"enabled": True, "left_delay": 100.0, "right_delay": 300.0,
                       "left_feedback": 0.0, "right_feedback": 0.0,
                       "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                       "high_ratio": 0.0, "low_cut": 20.0, "high_cut": 20000.0})
        imp = np.zeros((2, int(sr)))
        imp[0, 10] = 1.0
        imp[1, 10] = 1.0
        out = dl.process(imp)
        pk_l = np.argmax(np.abs(out[0])) / sr * 1000.0
        pk_r = np.argmax(np.abs(out[1])) / sr * 1000.0
        assert 95.0 <= pk_l <= 106.0
        assert 295.0 <= pk_r <= 306.0

    def test_dry_wet_zero_is_dry(self):
        """dry_wet=0 → 纯干声。"""
        sr = 48000.0
        n = 4800
        t = np.arange(n) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        dl = DelayEffect(sr)
        dl.set_params({"enabled": True, "left_delay": 100.0, "right_delay": 100.0,
                       "dry_wet": 0.0, "level_db": 0.0})
        out = dl.process(samples)
        assert np.allclose(out, samples, atol=1e-4)

    def test_negative_feedback_alternates(self):
        """负反馈产生相位反转的交替回声。"""
        sr = 48000.0
        dl = DelayEffect(sr)
        dl.set_params({"enabled": True, "left_delay": 50.0, "right_delay": 50.0,
                       "left_feedback": -80.0, "right_feedback": 0.0,
                       "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                       "high_ratio": 0.0, "low_cut": 20.0, "high_cut": 20000.0})
        imp = np.zeros((2, int(sr)))
        imp[0, 5] = 1.0
        imp[1, 5] = 1.0
        out = dl.process(imp)
        e1 = out[0, 5 + int(0.05 * sr)]
        e2 = out[0, 5 + int(0.10 * sr)]
        assert e1 * e2 < 0.0, "negative feedback echoes should alternate sign"

    def test_cross_block_continuity(self):
        """延迟线跨块连续。"""
        sr = 48000.0
        n = 9600
        t = np.arange(n) / sr
        sig = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        params = {"enabled": True, "left_delay": 120.0, "right_delay": 180.0,
                  "left_feedback": 30.0, "right_feedback": 30.0,
                  "stereo_blend": 20.0, "dry_wet": 40.0, "level_db": 0.0,
                  "high_ratio": 40.0, "low_cut": 100.0, "high_cut": 12000.0}

        dl_full = DelayEffect(sr)
        dl_full.set_params(params)
        full = dl_full.process(samples.copy())

        dl_split = DelayEffect(sr)
        dl_split.set_params(params)
        h1 = dl_split.process(samples[:, :4800].copy())
        h2 = dl_split.process(samples[:, 4800:].copy())
        split = np.concatenate([h1, h2], axis=1)

        diff = np.max(np.abs(full[:, 4800:] - split[:, 4800:]))
        assert diff < 1e-4, f"cross-block discontinuity: {diff:.6f}"

    def test_higher_feedback_sustains_longer(self):
        """更高反馈的回声尾更长。"""
        sr = 48000.0

        def _tail_energy(fb: float) -> float:
            dl = DelayEffect(sr)
            dl.set_params({"enabled": True, "left_delay": 60.0, "right_delay": 60.0,
                           "left_feedback": fb, "right_feedback": fb,
                           "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
                           "high_ratio": 0.0, "low_cut": 20.0, "high_cut": 20000.0})
            imp = np.zeros((2, int(sr)))
            imp[0, 5] = 1.0
            imp[1, 5] = 1.0
            out = dl.process(imp)
            return float(np.sum(np.abs(out[0, int(sr * 0.4):int(sr * 0.9)])))

        e_low = _tail_energy(20.0)
        e_high = _tail_energy(80.0)
        assert e_high > e_low * 2.0

    def test_reset_state_clears_buffer(self):
        dl = DelayEffect(48000.0)
        dl.set_params({"enabled": True, "left_delay": 100.0, "right_delay": 100.0})
        imp = np.zeros((2, 4800))
        imp[0, 5] = 1.0
        imp[1, 5] = 1.0
        dl.process(imp)
        dl.reset_state()
        assert float(np.max(np.abs(dl._buf_l))) == 0.0
        assert float(np.max(np.abs(dl._buf_r))) == 0.0

    def test_default_params(self):
        dl = DelayEffect(48000.0)
        assert dl._left_delay == 121.0
        assert dl._right_delay == 135.0
        assert dl._dry_wet == 30.0
        assert dl._enabled is False

    def test_is_active_reflects_enabled(self):
        dl = DelayEffect(48000.0)
        assert not dl.is_active
        dl.set_params({"enabled": True})
        assert dl.is_active


# =============================================================================
# 3e. SaturationEffect 谐波失真（未变动，原样回归）
# =============================================================================

class TestSaturationEffect:
    """SaturationEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(SaturationEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        sat = SaturationEffect(48000.0)
        samples = np.array([[0.5, -0.3]], dtype=np.float32)
        assert sat.process(samples) is samples

    def test_rejects_non_2d(self):
        sat = SaturationEffect(48000.0)
        sat.set_params({"drive": 50.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9], dtype=np.float32)
        assert sat.process(samples_1d) is samples_1d

    def test_drive_zero_is_passthrough(self):
        sat = SaturationEffect(48000.0)
        sat.set_params({"drive": 0.0, "enabled": True})
        samples = np.random.randn(2, 512).astype(np.float32) * 0.4
        out = sat.process(samples)
        assert np.allclose(out, samples, atol=1e-6)

    def test_drive_positive_changes_signal(self):
        sat = SaturationEffect(48000.0)
        sat.set_params({"drive": 60.0, "enabled": True})
        samples = (np.linspace(-0.9, 0.9, 512)[np.newaxis, :].repeat(2, axis=0)).astype(np.float32)
        out = sat.process(samples)
        assert not np.allclose(out, samples, atol=1e-3)

    def test_overload_peak_is_soft_clipped(self):
        sat = SaturationEffect(48000.0)
        sat.set_params({"drive": 100.0, "enabled": True})
        samples = np.array([[2.0, -2.0, 1.5, -1.5]], dtype=np.float32)
        out = sat.process(samples)
        assert float(np.max(np.abs(out))) < 1.35  # (1+k)/(1+k*0.9) < 1.35

    def test_preserves_shape_and_dtype(self):
        sat = SaturationEffect(48000.0)
        sat.set_params({"drive": 50.0, "enabled": True})
        samples = np.random.randn(2, 256).astype(np.float32) * 0.5
        out = sat.process(samples)
        assert out.shape == samples.shape
        assert out.dtype == samples.dtype

    def test_default_params(self):
        sat = SaturationEffect(48000.0)
        assert sat._drive == 0.0
        assert sat._enabled is False

    def test_is_active_reflects_enabled_and_drive(self):
        sat = SaturationEffect(48000.0)
        assert not sat.is_active
        sat.set_params({"drive": 0.0, "enabled": True})
        assert not sat.is_active  # drive=0 无实际效果
        sat.set_params({"drive": 50.0, "enabled": True})
        assert sat.is_active


# =============================================================================
# 3f. ImagerEffect 立体声成像（未变动，原样回归）
# =============================================================================

class TestImagerEffect:
    """ImagerEffect 的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(ImagerEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        img = ImagerEffect(48000.0)
        samples = np.array([[0.5, -0.3]], dtype=np.float32)
        assert img.process(samples) is samples

    def test_rejects_non_2d(self):
        img = ImagerEffect(48000.0)
        img.set_params({"width": 100.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9], dtype=np.float32)
        assert img.process(samples_1d) is samples_1d

    def test_default_is_near_identity(self):
        img = ImagerEffect(48000.0)
        img.set_params(ImagerParams().to_dict())
        samples = np.random.randn(2, 512).astype(np.float32) * 0.3
        out = img.process(samples)
        assert np.allclose(out, samples, atol=1e-4)

    def test_width_zero_produces_mono(self):
        img = ImagerEffect(48000.0)
        img.set_params({"width": 0.0, "enabled": True})
        left = np.random.randn(512).astype(np.float32) * 0.3
        right = np.random.randn(512).astype(np.float32) * 0.3
        samples = np.stack([left, right])
        out = img.process(samples)
        assert np.allclose(out[0], out[1], atol=1e-6)

    def test_pan_left_silences_right(self):
        img = ImagerEffect(48000.0)
        img.set_params({"pan": -100.0, "width": 100.0, "enabled": True})
        left = np.ones(256, dtype=np.float32) * 0.3
        right = np.ones(256, dtype=np.float32) * 0.3
        samples = np.stack([left, right])
        out = img.process(samples)
        assert float(np.max(np.abs(out[1]))) < 1e-6

    def test_mono_input_width_no_effect(self):
        img = ImagerEffect(48000.0)
        img.set_params({"width": 0.0, "pan": 0.0, "enabled": True})
        samples = (np.random.randn(1, 256) * 0.3).astype(np.float32)
        out = img.process(samples)
        assert np.allclose(out, samples, atol=1e-6)

    def test_default_params(self):
        img = ImagerEffect(48000.0)
        assert img._pan == 0.0
        assert img._width == 100.0
        assert img._enabled is False

    def test_is_active_only_when_effective(self):
        img = ImagerEffect(48000.0)
        assert not img.is_active
        img.set_params({"pan": 0.0, "width": 100.0, "enabled": True})
        assert not img.is_active  # 中性参数无实际效果
        img.set_params({"pan": 30.0, "width": 100.0, "enabled": True})
        assert img.is_active


# =============================================================================
# 3g. ExpanderEffect（CW-A1: 接替 NoiseGateEffect）
# =============================================================================

class TestExpanderEffect:
    """ExpanderEffect（Expander/Gate 双模式）的 DSP 正确性。"""

    def test_is_audio_effect_subclass(self):
        assert issubclass(ExpanderEffect, AudioEffect)

    def test_bypass_returns_input_unchanged(self):
        ex = ExpanderEffect(48000.0)
        samples = np.array([[0.5, -0.3]], dtype=np.float32)
        assert ex.process(samples) is samples

    def test_rejects_non_2d(self):
        ex = ExpanderEffect(48000.0)
        ex.set_params({"threshold_db": -30.0, "enabled": True})
        samples_1d = np.array([0.5, 0.9], dtype=np.float32)
        assert ex.process(samples_1d) is samples_1d

    def test_loud_signal_passes(self):
        """高于阈值的响信号直通。"""
        sr = 48000.0
        n = 9600
        t = np.arange(n) / sr
        sig = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        samples = np.stack([sig, sig])
        ex = ExpanderEffect(sr)
        ex.set_params(ExpanderParams(
            mode="Expander", threshold_db=-25.0,
            attack_ms=1.0, release_ms=100.0,
        ).to_dict())
        out = ex.process(samples)
        assert np.allclose(out, samples, atol=1e-3)

    def test_quiet_signal_attenuated(self):
        """低于阈值的静信号被下扩（包络开门初始化，测 release 收敛后的尾段）。"""
        sr = 48000.0
        n = 48000  # 1s = 10×release，包络充分收敛
        rng = np.random.default_rng(7)
        samples = (rng.standard_normal((2, n)) * 0.002).astype(np.float32)
        ex = ExpanderEffect(sr)
        ex.set_params(ExpanderParams(
            mode="Expander", threshold_db=-25.0, range_db=30.0,
            attack_ms=1.0, release_ms=100.0,
        ).to_dict())
        out = ex.process(samples)
        tail = slice(n // 2, None)
        reduction_db = 20 * np.log10(float(np.std(out[:, tail])) / float(np.std(samples[:, tail])))
        assert reduction_db < -5.0, f"quiet attenuation {reduction_db:.2f} dB"

    def test_gate_mode_deeper_than_expander(self):
        """Gate 模式（高等效比率）衰减比 Expander 深。"""
        sr = 48000.0
        n = 48000
        rng = np.random.default_rng(7)
        samples = (rng.standard_normal((2, n)) * 0.002).astype(np.float32)
        tail = slice(n // 2, None)

        def _red(mode: str) -> float:
            ex = ExpanderEffect(sr)
            ex.set_params(ExpanderParams(
                mode=mode, threshold_db=-25.0, range_db=40.0,
                attack_ms=1.0, release_ms=100.0,
            ).to_dict())
            out = ex.process(samples)
            return 20 * np.log10(float(np.std(samples[:, tail])) / float(np.std(out[:, tail])))

        assert _red("Gate") > _red("Expander") + 3.0

    def test_range_caps_attenuation(self):
        """range_db 封顶最大衰减。"""
        sr = 48000.0
        n = 48000
        rng = np.random.default_rng(7)
        samples = (rng.standard_normal((2, n)) * 0.001).astype(np.float32)
        ex = ExpanderEffect(sr)
        ex.set_params(ExpanderParams(
            mode="Gate", threshold_db=-25.0, range_db=6.0,
            attack_ms=1.0, release_ms=100.0,
        ).to_dict())
        out = ex.process(samples)
        tail = slice(n // 2, None)
        reduction_db = 20 * np.log10(float(np.std(samples[:, tail])) / float(np.std(out[:, tail])))
        assert reduction_db <= 7.5, f"range cap violated: {reduction_db:.2f} dB"

    def test_cross_block_continuity(self):
        """包络跨块连续。"""
        sr = 48000.0
        n = 9600
        t = np.arange(n) / sr
        sig = np.concatenate([
            0.4 * np.sin(2 * np.pi * 440.0 * t[:n // 2]),
            0.002 * np.sin(2 * np.pi * 440.0 * t[n // 2:]),
        ]).astype(np.float32)
        samples = np.stack([sig, sig])
        params = ExpanderParams(
            mode="Expander", threshold_db=-25.0, range_db=20.0,
            attack_ms=1.0, release_ms=200.0,
        ).to_dict()

        ex_full = ExpanderEffect(sr)
        ex_full.set_params(params)
        full = ex_full.process(samples.copy())

        ex_split = ExpanderEffect(sr)
        ex_split.set_params(params)
        h1 = ex_split.process(samples[:, :n // 2].copy())
        h2 = ex_split.process(samples[:, n // 2:].copy())
        split = np.concatenate([h1, h2], axis=1)

        diff = np.max(np.abs(full[:, n // 2:] - split[:, n // 2:]))
        assert diff < 1e-4, f"cross-block discontinuity: {diff:.6f}"

    def test_link_channels(self):
        """声道联动。"""
        sr = 48000.0
        n = 4800
        t = np.arange(n) / sr
        left = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        right = (0.001 * np.sin(2 * np.pi * 880.0 * t)).astype(np.float32)
        samples = np.stack([left, right])
        ex = ExpanderEffect(sr)
        ex.set_params(ExpanderParams(
            mode="Expander", threshold_db=-25.0, range_db=20.0,
        ).to_dict())
        out = ex.process(samples)
        # 右声道虽低于阈值，但与左声道联动（同 gain）
        # 左声道直通 → gain=1 → 右声道也直通
        assert np.allclose(out[1], samples[1], atol=1e-4)

    def test_legacy_noise_gate_params_map_to_gate(self):
        """旧 noise_gate 键无损承接为 Gate 模式。"""
        ex = ExpanderEffect(48000.0)
        ex.set_params({"threshold_db": -45.0, "attack_ms": 2.0,
                       "release_ms": 150.0, "enabled": True})
        assert ex._mode == "Gate"
        assert ex._threshold_db == -45.0

    def test_reset_state(self):
        ex = ExpanderEffect(48000.0)
        ex.set_params(ExpanderParams().to_dict())
        ex.process(np.ones((2, 256), dtype=np.float32) * 0.5)
        assert ex._env_lin > 0.0
        ex.reset_state()
        assert ex._env_lin == 1.0  # 开门初始化

    def test_default_params(self):
        ex = ExpanderEffect(48000.0)
        assert ex._threshold_db == -35.0
        assert ex._mode == "Expander"
        assert ex._enabled is False

    def test_is_active_reflects_enabled(self):
        ex = ExpanderEffect(48000.0)
        assert not ex.is_active
        ex.set_params({"enabled": True})
        assert ex.is_active

    def test_gr_telemetry_exposed(self):
        """CW-A1: last_gr_db 遥测。"""
        ex = ExpanderEffect(48000.0)
        ex.set_params({"mode": "Gate", "threshold_db": -25.0, "range_db": 40.0,
                       "attack_ms": 1.0, "release_ms": 100.0, "enabled": True})
        n = 48000
        ex.process((np.random.default_rng(3).standard_normal((2, n)) * 0.002).astype(np.float32))
        assert ex.last_gr_db > 8.0


# =============================================================================
# 4. EffectChain 链串联
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
        chain.set_params([("limiter", {"threshold_db": -6.0, "enabled": True}, True)])
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
        eq_params = {"bands": _bands(**{"1": {"filter_type": "Low Shelf",
                                              "freq": 110.0, "gain": 9.0,
                                              "q": 1.4, "enabled": True}})}
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
        chain.set_params([("limiter", {"threshold_db": -6.0, "enabled": True}, True)])
        old_inst = chain._effects[0]
        chain.set_params([("limiter", {"threshold_db": -12.0, "enabled": True}, True)])
        new_inst = chain._effects[0]
        assert new_inst is old_inst

    def test_bypass_flag_disables_effect(self):
        """enabled=False 的条目被跳过。"""
        chain = EffectChain(48000.0)
        chain.set_params([("limiter", {"threshold_db": -6.0, "enabled": True}, False)])
        samples = np.ones((1, 256), dtype=np.float32) * 0.9
        out = chain.process(samples)
        assert float(np.max(np.abs(out))) >= 0.85
        assert not chain.is_active

    def test_reset_state(self):
        """reset_state 传播到所有效果器。"""
        chain = EffectChain(48000.0)
        chain.set_params([
            ("strip_eq", {"bands": _bands()}, True),
            ("limiter", {"threshold_db": -6.0, "enabled": True}, True),
        ])
        chain.process(np.ones((2, 256), dtype=np.float32) * 0.9)
        assert chain._effects[1]._envelope > 0.0
        chain.reset_state()
        assert chain._effects[1]._envelope == 0.0

    def test_unknown_effect_type_skipped(self):
        """未知 effect_type 的条目被跳过（不崩）。"""
        chain = EffectChain(48000.0)
        chain.set_params([
            ("unknown_fx", {"foo": 1}, True),
            ("limiter", {"threshold_db": -6.0, "enabled": True}, True),
        ])
        assert len(chain._effects) == 1
        assert isinstance(chain._effects[0], LimiterEffect)

    def test_sample_rate_propagates(self):
        """set_params 时 sample_rate 传播到效果器。"""
        chain = EffectChain(48000.0)
        chain.set_params([("limiter", {"threshold_db": -6.0, "enabled": True}, True)])
        assert chain._effects[0]._sample_rate == 48000.0
        chain.set_params([("limiter", {"threshold_db": -6.0, "enabled": True}, True)], sample_rate=96000.0)
        assert chain._effects[0]._sample_rate == 96000.0

    def test_four_tuple_entries_with_instance_ids(self):
        """CW-A1: 4 元组（含 instance_id）条目正常工作。"""
        chain = EffectChain(48000.0)
        chain.set_params([
            ("limiter", {"threshold_db": -6.0, "enabled": True}, True, "lim-1"),
        ])
        assert chain._instance_ids == ["lim-1"]
        assert chain.collect_gain_reduction().get("lim-1", 0.0) >= 0.0
