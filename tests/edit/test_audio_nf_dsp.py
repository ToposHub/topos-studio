"""NF-01/VC/SM：两款新复合 FX 的 DSP 回归（计划 §6–§8，2026-11-15）。

Voice Changer（VC-01..04）：
- Pitch 比例（±12st 内 FFT 峰 ≤25 cents——双读头 Doppler 型的交叉边带
  与 FFT bin 量化为已知误差源，写入算法契约；±24 极值 ≤60 cents。
  WSOLA/相位声码器升级为后续质量路径）。
- 时长保持（每块 n 进 n 出）；中性恒等（延迟对齐后 ≤2e-5）。
- Formant：中性恒等；+6st 包络上移且 F0 保持。
- Vocoder：零 modulator 输出按 release 衰减至静默。
- jitter/seed：同 seed 不同块长输出一致（绝对采样时间驱动）。

Spectral Mangler（SM-01..04）：
- Ring：1kHz×200Hz sine 载波 → 800/1200Hz 边带；mix=0 恒等；
  spread=0 左右一致。
- 并联：全恒等 / 全 mix0 严格 unity；串行顺序差异；模块旁路。
- Grain：同 seed 块长无关；freeze 停写。
- Dist：tape +6dB 小信号 ×1.995；非零 bias 去 DC；bitcrush 量化。
- 全局 mix=0 恒等；限幅器削波钳制。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_effects.voice_changer import _estimate_f0
from src.features.edit.core.effect_registry import (
    _get_audio_default_params,
    get_effect_registry_health,
)
from src.features.edit.core.timeline_v2 import (
    SpectralManglerParams,
    VoiceChangerParams,
)

pytestmark = pytest.mark.edit_core
SR = 48000.0


def _sine(n: int, freq: float, amp: float = 0.4) -> np.ndarray:
    t = np.arange(n) / SR
    return np.stack([amp * np.sin(2 * np.pi * freq * t)] * 2).astype(np.float32)


def _fft_peak(y: np.ndarray, lo: float, hi: float) -> float:
    seg = y[int(SR):] if len(y) > int(SR) else y
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    fr = np.fft.rfftfreq(len(seg), 1 / SR)
    m = (fr >= lo) & (fr <= hi)
    return float(fr[np.argmax(sp * m)])


def _run_vc(p: VoiceChangerParams, x: np.ndarray) -> np.ndarray:
    chain = EffectChain(SR)
    chain.set_params([("voice_changer", p.to_dict(), True, "V", 4)],
                     sample_rate=SR)
    return np.asarray(chain.process(x))


def _run_sm(p: SpectralManglerParams, x: np.ndarray) -> np.ndarray:
    chain = EffectChain(SR)
    chain.set_params([("spectral_mangler", p.to_dict(), True, "M", 4)],
                     sample_rate=SR)
    return np.asarray(chain.process(x))


class TestNF01Registration:
    def test_registry_healthy_with_new_types(self):
        h = get_effect_registry_health()
        assert not h.registry_error
        by = {e.effect_type: e for e in h.effects}
        for t in ("voice_changer", "spectral_mangler"):
            assert t in by and by[t].registered and by[t].has_backend
        defaults = _get_audio_default_params()
        assert len(defaults["voice_changer"]) == 27
        assert len(defaults["spectral_mangler"]) == 33

    def test_params_roundtrip_and_enum_guards(self):
        vc = VoiceChangerParams.default()
        assert VoiceChangerParams.from_dict(vc.to_dict()) == vc
        sm = SpectralManglerParams.default()
        assert SpectralManglerParams.from_dict(sm.to_dict()) == sm
        with pytest.raises(ValueError):
            SpectralManglerParams(routing="bogus")
        with pytest.raises(ValueError):
            VoiceChangerParams(vocoder_carrier="wobble")
        # 容错 from_dict：未知枚举回默认、bands 钳位
        bad = VoiceChangerParams.from_dict(
            {"formant_mode": "x", "vocoder_bands": 99})
        assert bad.formant_mode == "natural" and bad.vocoder_bands == 32


class TestVCPitch:
    @pytest.mark.parametrize("st,expect", [
        (12.0, 880.0), (-12.0, 220.0), (7.0, 659.3)])
    def test_pitch_ratio_fft_peak(self, st, expect):
        x = _sine(int(SR) * 2, 440.0)
        out = _run_vc(VoiceChangerParams(pitch_enabled=True,
                                          pitch_semitones=st), x)
        pk = _fft_peak(out[0], expect * 0.95, expect * 1.05)
        cents = 1200 * np.log2(pk / expect)
        assert abs(cents) <= 25.0, f"{st}st → {pk}Hz ({cents:+.1f}c)"

    def test_extreme_pitch_bounded(self):
        x = _sine(int(SR) * 2, 440.0)
        out = _run_vc(VoiceChangerParams(pitch_enabled=True,
                                          pitch_semitones=-24.0), x)
        pk = _fft_peak(out[0], 100.0, 125.0)
        assert abs(1200 * np.log2(pk / 110.0)) <= 60.0

    def test_duration_preserved_and_finite(self):
        x = _sine(int(SR), 440.0)
        p = VoiceChangerParams(pitch_enabled=True, pitch_semitones=5.0)
        out = _run_vc(p, x)
        assert out.shape == x.shape
        assert np.isfinite(out).all()

    def test_jitter_same_seed_block_invariant(self):
        """同 seed + 同绝对区间：整块与分块输出一致（jitter 由绝对采样
        时间驱动，不依赖块边界）。"""
        n = int(SR)
        x = _sine(n, 440.0)
        p = VoiceChangerParams(pitch_enabled=True, pitch_semitones=3.0,
                               pitch_jitter_pct=60.0, random_seed=1234)
        whole = _run_vc(p, x)
        # 分块（chain 实例状态跨块）
        chain = EffectChain(SR)
        chain.set_params([("voice_changer", p.to_dict(), True, "V", 4)],
                         sample_rate=SR)
        parts = []
        pos = 0
        rng = np.random.default_rng(7)
        while pos < n:
            m = min(int(rng.integers(256, 4000)), n - pos)
            parts.append(np.asarray(chain.process(x[:, pos:pos + m])))
            pos += m
        chunked = np.concatenate(parts, axis=1)
        np.testing.assert_allclose(whole, chunked, atol=1e-5)


class TestVCFormantAndNeutral:
    def test_neutral_identity_after_latency(self):
        n = int(SR) * 2
        x = _sine(n, 440.0)
        out = _run_vc(VoiceChangerParams.default(), x)
        lag = 1024  # formant WOLA 时延（win）
        # 恒等区：q ≥ win（1024）起；out[q] == x[q - lag]
        lo, hi = 1024 + lag, n - 1024
        err = float(np.max(np.abs(out[:, lo:hi]
                                  - x[:, lo - lag:hi - lag])))
        assert err < 2e-4, f"neutral identity err {err}"

    def test_formant_shift_moves_envelope_keeps_f0(self):
        """合成 source-filter 语音（谐波 + 共振峰峰），Formant +6st：
        F0 保持 ±30 cents；谱峰上移 ≈ 2^(6/12)。"""
        n = int(SR) * 2
        t = np.arange(n) / SR
        f0 = 150.0
        voice = np.zeros(n)
        for k in range(1, 25):
            amp = np.exp(-((k * f0 - 500.0) / 600.0) ** 2) \
                + 0.5 * np.exp(-((k * f0 - 1500.0) / 500.0) ** 2)
            voice += amp * np.sin(2 * np.pi * f0 * k * t)
        voice = (0.4 * voice / np.max(np.abs(voice))).astype(np.float32)
        x = np.stack([voice, voice])
        out = _run_vc(VoiceChangerParams(formant_shift_st=6.0), x)
        f0_out, conf = _estimate_f0(out[0, int(SR):].astype(np.float64), SR,
                                    lo=80, hi=400)
        assert f0_out is not None and abs(1200 * np.log2(f0_out / f0)) <= 30.0
        # 包络峰上移（与谐波精细结构无关的度量）：对输出做同款倒谱
        # 包络，峰位置应接近 ρ×输入包络峰
        def envelope_peak(y):
            seg = y[int(SR):] * np.hanning(int(SR))
            X = np.abs(np.fft.rfft(seg))
            cep = np.fft.irfft(np.log(X + 1e-10))
            cep[29:] = 0.0
            E = np.exp(np.clip(np.fft.rfft(cep).real, -30, 30))
            fr = np.fft.rfftfreq(int(SR), 1 / SR)
            m = fr > 200.0
            return float(fr[m][np.argmax(E[m])])
        rho = 2.0 ** (6.0 / 12.0)
        p_in = envelope_peak(voice)
        p_out = envelope_peak(out[0])
        assert abs(p_out / (p_in * rho) - 1.0) < 0.2, (
            f"envelope peak {p_in}→{p_out} (expect ×{rho:.3f})")

    def test_output_gain(self):
        x = _sine(int(SR), 440.0)
        out = _run_vc(VoiceChangerParams(output_gain_db=-6.0), x)
        lag = 768 + 1024
        seg_out = out[0, lag + 1024:-1024]
        seg_in = x[0, 1024:-(lag + 2048)]
        ratio = float(np.sqrt(np.mean(seg_out ** 2))
                      / max(1e-9, np.sqrt(np.mean(seg_in ** 2))))
        assert abs(ratio - 10 ** (-6 / 20)) < 0.02


class TestVCVocoder:
    def test_zero_modulator_decays_to_silence(self):
        """零 modulator：包络按 release 衰减，末尾静默（载波不泄漏）。"""
        n = int(SR)
        x = np.zeros((2, n), np.float32)
        x[:, :4800] = _sine(4800, 440.0)[:, :4800] * 0.8
        out = _run_vc(VoiceChangerParams(vocoder_enabled=True,
                                         vocoder_release_ms=50.0), x)
        tail = out[:, -int(0.2 * SR):]
        assert float(np.max(np.abs(tail))) < 1e-3, "carrier leaked"

    def test_vocoder_carrier_audible(self):
        n = int(SR)
        x = _sine(n, 440.0)
        out = _run_vc(VoiceChangerParams(vocoder_enabled=True), x)
        assert float(np.max(np.abs(out))) > 1e-3

    def test_telemetry_fields(self):
        x = _sine(int(SR), 440.0)
        chain = EffectChain(SR)
        chain.set_params([("voice_changer",
                           VoiceChangerParams(pitch_enabled=True,
                                              pitch_semitones=4.0).to_dict(),
                           True, "VC1", 4)], sample_rate=SR)
        chain.process(x)
        taps = chain.tap_levels()
        assert "VC1" in taps and taps["VC1"]["effect_type"] == "voice_changer"


class TestSMRing:
    def test_sine_carrier_sidebands(self):
        x = _sine(int(SR) * 2, 1000.0, 0.5)
        p = SpectralManglerParams(ring_enabled=True, ring_mode="sine",
                                  ring_carrier_hz=200.0,
                                  ring_lfo_depth_pct=0.0,
                                  ring_mix_pct=100.0, global_mix_pct=100.0)
        out = _run_sm(p, x)
        seg = out[0, int(SR):]
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        fr = np.fft.rfftfreq(len(seg), 1 / SR)
        for target in (800.0, 1200.0):
            m = (fr > target - 15) & (fr < target + 15)
            near = np.max(sp[m])
            noise_floor = np.median(sp)
            assert near > noise_floor * 50, f"sideband {target}Hz missing"
        # 载频与原频抑制（mix=100 纯湿）
        m_1000 = (fr > 995) & (fr < 1005)
        assert np.max(sp[m_1000]) < np.max(sp[(fr > 785) & (fr < 815)])

    def test_ring_mix_zero_identity(self):
        x = _sine(int(SR), 440.0, 0.3)
        p = SpectralManglerParams(ring_enabled=True, ring_mix_pct=0.0,
                                  global_mix_pct=100.0)
        np.testing.assert_allclose(_run_sm(p, x), x, atol=1e-6)

    def test_spread_zero_channels_identical(self):
        x = _sine(int(SR), 440.0, 0.3)
        p = SpectralManglerParams(ring_enabled=True, ring_mode="sine",
                                  ring_spread_pct=0.0, ring_mix_pct=100.0)
        out = _run_sm(p, x)
        np.testing.assert_allclose(out[0], out[1], atol=1e-6)


class TestSMTopology:
    def _en(self, m, mix=100.0):
        return SpectralManglerParams(routing="parallel", global_mix_pct=100.0,
                                     **{f"{m}_enabled": True,
                                        f"{m}_mix_pct": mix})

    def test_parallel_all_identity_modules_strict_unity(self):
        """全部启用但模块恒等（mix=0）→ 严格 unity（计划 §10.2 oracle）。"""
        x = _sine(int(SR), 440.0, 0.3)
        p = SpectralManglerParams(routing="parallel",
                                  ring_enabled=True, ring_mix_pct=0.0,
                                  grain_enabled=True, grain_mix_pct=0.0,
                                  dist_enabled=True, dist_mix_pct=0.0,
                                  global_mix_pct=100.0)
        np.testing.assert_allclose(_run_sm(p, x), x, atol=1e-5)

    def test_serial_order_matters(self):
        """非交换模块组合：dist→grain 与 grain→dist 输出不同。"""
        n = int(SR)
        rng = np.random.default_rng(3)
        x = (rng.standard_normal((2, n)) * 0.2).astype(np.float32)
        p1 = SpectralManglerParams(routing="dist_grain_ring",
                                   dist_enabled=True, grain_enabled=True,
                                   grain_density_hz=30.0)
        p2 = SpectralManglerParams(routing="ring_grain_dist",
                                   dist_enabled=True, grain_enabled=True,
                                   grain_density_hz=30.0)
        o1 = _run_sm(p1, x)
        o2 = _run_sm(p2, x)
        assert float(np.max(np.abs(o1 - o2))) > 1e-3

    def test_module_bypass_only_that_module(self):
        x = _sine(int(SR), 440.0, 0.3)
        base = SpectralManglerParams(ring_enabled=True, ring_mix_pct=100.0)
        off = SpectralManglerParams(ring_enabled=False)
        np.testing.assert_allclose(_run_sm(off, x), x, atol=1e-6)

    def test_global_mix_zero_identity(self):
        x = _sine(int(SR), 440.0, 0.3)
        p = SpectralManglerParams(ring_enabled=True, dist_enabled=True,
                                  grain_enabled=True, global_mix_pct=0.0)
        np.testing.assert_allclose(_run_sm(p, x), x, atol=1e-6)


class TestSMGrain:
    def test_same_seed_block_invariant(self):
        """同 seed + 同绝对区间：分块一致（密度绝对相位 + 触发序随机）。"""
        n = int(SR)
        rng = np.random.default_rng(5)
        x = (rng.standard_normal((2, n)) * 0.25).astype(np.float32)
        p = SpectralManglerParams(grain_enabled=True, grain_mix_pct=100.0,
                                  grain_feedback_pct=0.0,
                                  grain_pitch_jitter_pct=80.0,
                                  grain_spray_pct=90.0,
                                  grain_density_hz=40.0, random_seed=777,
                                  global_mix_pct=100.0)

        def run_split(splits):
            chain = EffectChain(SR)
            chain.set_params([("spectral_mangler", p.to_dict(), True,
                               "G", 4)], sample_rate=SR)
            out = np.zeros((2, n), np.float32)
            pos = 0
            for m in splits:
                m = min(m, n - pos)
                out[:, pos:pos + m] = chain.process(x[:, pos:pos + m])
                pos += m
            return out

        whole = run_split([n])
        splits = []
        pos = 0
        r2 = np.random.default_rng(11)
        while pos < n:
            m = min(int(r2.integers(512, 6000)), n - pos)
            splits.append(m)
            pos += m
        chunked = run_split(splits)
        np.testing.assert_allclose(whole, chunked, atol=1e-5)

    def test_different_seed_different_output(self):
        n = int(SR)
        rng = np.random.default_rng(9)
        x = (rng.standard_normal((2, n)) * 0.25).astype(np.float32)
        outs = []
        for seed in (1, 2):
            p = SpectralManglerParams(grain_enabled=True,
                                      grain_pitch_jitter_pct=90.0,
                                      grain_spray_pct=90.0,
                                      random_seed=seed, global_mix_pct=100.0)
            outs.append(_run_sm(p, x))
        assert float(np.max(np.abs(outs[0] - outs[1]))) > 1e-3

    def test_freeze_holds_history(self):
        """freeze 后：输入变化不再进入输出（历史停写）。两条独立链走
        相同预冻历史，冻结段分别喂静音/任意输入 → 输出一致。"""
        n = int(SR)
        half = n // 2
        t = np.arange(n) / SR
        voice = (0.4 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32)
        x = np.stack([voice, voice])
        p_on = SpectralManglerParams(grain_enabled=True, grain_freeze=False,
                                     grain_mix_pct=100.0,
                                     grain_feedback_pct=0.0,
                                     grain_density_hz=60.0,
                                     grain_pitch_st=0.0, random_seed=42,
                                     global_mix_pct=100.0)
        p_frz = SpectralManglerParams(**{**p_on.to_dict(),
                                         "grain_freeze": True})
        outs = []
        for second_input in (np.zeros((2, half), np.float32), x[:, half:]):
            chain = EffectChain(SR)
            chain.set_params([("spectral_mangler", p_on.to_dict(), True,
                               "F", 4)], sample_rate=SR)
            chain.process(x[:, :half])
            chain.set_params([("spectral_mangler", p_frz.to_dict(), True,
                               "F", 4)], sample_rate=SR)
            outs.append(np.asarray(chain.process(second_input)))
        np.testing.assert_allclose(outs[0], outs[1], atol=1e-6)


class TestSMDist:
    def test_tape_drive_small_signal_linear(self):
        """+6dB drive：小信号 ≈ ×1.995（tanh 小信号增益 1）。"""
        x = _sine(int(SR), 440.0, 0.01)
        p = SpectralManglerParams(dist_enabled=True, dist_curve="tape",
                                  dist_drive_db=6.0, dist_fold_pct=0.0,
                                  dist_bias_pct=0.0, dist_resonance_pct=0.0,
                                  dist_mix_pct=100.0, global_mix_pct=100.0)
        out = _run_sm(p, x)
        gain = float(np.max(np.abs(out)) / 0.01)
        assert abs(gain - 10 ** (6 / 20)) < 0.05

    def test_bias_dc_removed(self):
        """非零 bias：对称输入去 DC 后均值接近 0。"""
        n = int(SR * 1.5)
        t = np.arange(n) / SR
        x = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        xb = np.stack([x, x])
        p = SpectralManglerParams(dist_enabled=True, dist_curve="tape",
                                  dist_drive_db=12.0, dist_bias_pct=80.0,
                                  dist_mix_pct=100.0, global_mix_pct=100.0)
        out = _run_sm(p, xb)
        assert abs(float(np.mean(out[0, int(SR) // 2:]))) < 5e-3

    def test_bitcrush_quantizes(self):
        t = np.arange(SR) / SR
        x = (0.5 * np.sin(2 * np.pi * 997.0 * t)).astype(np.float32)
        xb = np.stack([x, x])
        p = SpectralManglerParams(dist_enabled=True, dist_curve="bitcrush",
                                  dist_fold_pct=90.0, dist_drive_db=6.0,
                                  dist_mix_pct=100.0, global_mix_pct=100.0)
        out = _run_sm(p, xb)
        levels = np.unique(np.round(out[0, int(SR):], decimals=4))
        assert len(levels) <= 8, f"quantization missing: {len(levels)} levels"

    def test_limiter_clips(self):
        rng = np.random.default_rng(2)
        x = ((rng.standard_normal((2, int(SR))) * 0.8).astype(np.float32))
        p = SpectralManglerParams(dist_enabled=True, dist_drive_db=30.0,
                                  dist_mix_pct=100.0,
                                  output_gain_db=12.0,
                                  output_limiter_enabled=True,
                                  global_mix_pct=100.0)
        out = _run_sm(p, x)
        assert float(np.max(np.abs(out))) <= 1.0 + 1e-6


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
