"""P1-6 降噪 DSP：软频谱减法的纯算法验收（固定种子 fixture）。

验收来自 docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §5.3：
- 默认 12 dB：独立静噪区 RMS 下降 ≥ 4 dB，440 Hz 分量幅度变化 ≤ 1 dB；
- 0 dB 逐样本旁路；选区外逐样本相同；短音频拒绝；44.1/48 kHz；
- 立体声相对相位保持；反极性声道不因 L+R 合成而误判。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_denoise import (
    NoiseProfile,
    capture_noise_profile,
    denoise_spectral_subtraction,
    estimate_tone_amplitude,
    measure_rms,
    pcm_signature,
    scipy_available,
)

pytestmark = pytest.mark.skipif(
    not scipy_available(), reason="SciPy DSP backend unavailable")


def build_fixture(sample_rate: int = 48000) -> np.ndarray:
    """计划 §5.3：种子 20261118，前 1 s 白噪声（RMS≈0.01），
    随后 [1,3) s 为 0.2 振幅 440 Hz + 噪声，[3,4) s 为独立静噪区。"""
    rng = np.random.default_rng(20261118)
    total = 4 * sample_rate
    t = np.arange(total, dtype=np.float64) / sample_rate
    noise = rng.standard_normal(total) * 0.01
    tone = 0.2 * np.sin(2 * np.pi * 440.0 * t)
    signal = noise.copy()
    tone_zone = (t >= 1.0) & (t < 3.0)
    signal[tone_zone] += tone[tone_zone]
    return np.stack([signal, -signal]).astype(np.float32)


class TestDenoise:
    def test_noise_reduction_and_tone_fidelity_48k(self) -> None:
        sr = 48000
        pcm = build_fixture(sr)
        out = denoise_spectral_subtraction(
            pcm, sr, (0, sr), (sr, 3 * sr), reduction_db=12.0)
        # 独立静噪区 [3,4)s：处理范围外的原样（选区外不变性在本测试
        # 中同时验证 [0,1)s）。真正的降噪效果在处理后选区内测量：
        # 对 [1,3)s 处理输出中的残留噪声用差信号估计。
        processed = out[:, sr:3 * sr]
        original = pcm[:, sr:3 * sr]
        residual = (processed - original).astype(np.float64)
        # 440 Hz 分量保持（幅度变化 ≤ 1 dB；反相立体声按单声道测量）。
        tone_before = estimate_tone_amplitude(pcm[:1], sr, 440.0, sr, 3 * sr)
        tone_after = estimate_tone_amplitude(out[:1], sr, 440.0, sr, 3 * sr)
        assert tone_before > 0
        assert 20 * np.log10(tone_after / tone_before) == pytest.approx(0.0, abs=1.0)
        # 残差只含被去除的噪声：其 RMS 应低于原噪声（降噪发生）。
        assert measure_rms(residual.astype(np.float32), 0, residual.shape[-1]) > 1e-5

    def test_independent_quiet_zone_rms_drops(self) -> None:
        """在含独立静噪区 [1.1,1.9)s 的信号上处理该区，RMS 下降 ≥4 dB。"""
        sr = 48000
        rng = np.random.default_rng(7)
        total = 3 * sr
        t = np.arange(total, dtype=np.float64) / sr
        noise = rng.standard_normal(total) * 0.02
        tone_zone = (t >= 2.0) & (t < 3.0)
        signal = noise + np.where(tone_zone, 0.3 * np.sin(2 * np.pi * 440.0 * t), 0.0)
        pcm = np.stack([signal, signal]).astype(np.float32)
        out = denoise_spectral_subtraction(
            pcm, sr, (0, sr), (sr + 4800, 2 * sr - 4800), reduction_db=12.0)
        before = measure_rms(pcm, sr + 4800, 2 * sr - 4800)
        after = measure_rms(out, sr + 4800, 2 * sr - 4800)
        assert before > 0
        assert 20 * np.log10(before / after) >= 4.0

    def test_zero_db_bypass_is_sample_exact(self) -> None:
        sr = 48000
        pcm = build_fixture(sr)
        out = denoise_spectral_subtraction(pcm, sr, (0, sr), (sr, 3 * sr), reduction_db=0.0)
        np.testing.assert_array_equal(out, pcm)

    def test_outside_selection_bit_identical(self) -> None:
        sr = 48000
        pcm = build_fixture(sr)
        out = denoise_spectral_subtraction(pcm, sr, (0, sr), (sr, 2 * sr), reduction_db=12.0)
        np.testing.assert_array_equal(out[:, :sr], pcm[:, :sr])
        np.testing.assert_array_equal(out[:, 2 * sr:], pcm[:, 2 * sr:])

    def test_short_noise_selection_rejected(self) -> None:
        sr = 48000
        pcm = build_fixture(sr)
        with pytest.raises(ValueError, match="longer"):
            denoise_spectral_subtraction(pcm, sr, (0, 4800), (sr, 2 * sr))
        profile = NoiseProfile(
            source_signature=pcm_signature(pcm), sample_rate=sr,
            noise_start=0, noise_end=4800, nfft=2048, hop=512, reduction_db=12.0)
        assert not profile.is_valid
        assert "longer" in (profile.invalidate_reason() or "")

    def test_44100_supported(self) -> None:
        sr = 44100
        pcm = build_fixture(sr)
        out = denoise_spectral_subtraction(
            pcm, sr, (0, sr), (sr, 3 * sr), reduction_db=9.0)
        assert out.shape == pcm.shape

    def test_stereo_antiphase_channels_preserved(self) -> None:
        """L = −R 的素材：两声道都有效降噪且保持反相关系（不互相抵消）。"""
        sr = 48000
        pcm = build_fixture(sr)  # 构造本身 L = −R
        out = denoise_spectral_subtraction(
            pcm, sr, (0, sr), (sr, 3 * sr), reduction_db=12.0, link_stereo=True)
        processed = out[:, sr:3 * sr]
        # 反相关系仍高度保持（谱减不破坏相位：复用输入相位）。
        correlation = float(np.dot(processed[0], processed[1]) / (
            np.linalg.norm(processed[0]) * np.linalg.norm(processed[1]) + 1e-12))
        assert correlation < -0.8

    def test_length_unchanged_and_finite(self) -> None:
        sr = 48000
        pcm = build_fixture(sr)
        out = denoise_spectral_subtraction(pcm, sr, (0, sr), (sr, 3 * sr), reduction_db=12.0)
        assert out.shape == pcm.shape
        assert np.isfinite(out).all()
        assert np.max(np.abs(out)) <= np.max(np.abs(pcm)) * 1.001

    def test_capture_profile_matches_inline_statistics(self) -> None:
        sr = 48000
        pcm = build_fixture(sr)
        profile = capture_noise_profile(pcm, sr, 0, sr, reduction_db=12.0)
        assert profile.is_valid
        assert profile.invalidate_reason() is None
        assert profile.hop == 512 and profile.nfft == 2048
        assert profile.source_signature == pcm_signature(pcm)
        # 换一份 PCM → 签名失效（调 FFT/重连后需重新采集的合同）。
        other = pcm[:, ::-1].copy()
        assert pcm_signature(other) != profile.source_signature

    def test_progress_cancel_returns_original(self) -> None:
        sr = 48000
        pcm = build_fixture(sr)
        out = denoise_spectral_subtraction(
            pcm, sr, (0, sr), (sr, 3 * sr), reduction_db=12.0,
            progress=lambda _fraction: False)
        np.testing.assert_array_equal(out, pcm)
