"""P1-1 · 频谱类处理的声道域（2026-11-21 计划第三步）。

验收来自 docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md：

- 保长降噪/频谱修复先解析合法声道，计算时仅写目标通道——左右相同
  的 2 kHz 输入只修左声道时，右声道逐样本不变（此前预渲染分支不消
  费 channels，右声道被连带改写，峰值差约 0.234）；
- 无合法目标声道（全越界）时恒等返回；
- 全部声道（None）行为与旧版一致。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

RATE = 48000


def _tone(freq: float, seconds: float = 2.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, sig])


def _band_energy(pcm: np.ndarray, channel: int, freq: float,
                 lo: int, hi: int) -> float:
    """Goertzel 式单频幅度（与既有修复测试同一测量方式）。"""
    seg = pcm[channel, lo:hi].astype(np.float64)
    if seg.size == 0:
        return 0.0
    t = np.arange(seg.shape[-1]) / RATE
    cosine = np.cos(2.0 * np.pi * freq * t)
    sine = np.sin(2.0 * np.pi * freq * t)
    return math.hypot(float(np.mean(seg * cosine)) * 2.0,
                      float(np.mean(seg * sine)) * 2.0)


class TestSpectralRepairChannelScope:
    def _strokes(self, center: int) -> list[dict]:
        return [{
            "sample": center,
            "hz": 2000.0,
            "time_radius": 6000,
            "bandwidth_oct": 0.5,
        }]

    def test_left_only_repair_leaves_right_bit_identical(self) -> None:
        """复现案：仅选左声道的 spectral_repair 不再改写右声道。"""
        pytest.importorskip("src.features.edit.core.audio_repair")
        from src.features.edit.core.audio_repair import repair_spectral

        pcm = _tone(2000.0)
        lo, hi = 24000, 72000
        center = (lo + hi) // 2
        out = repair_spectral(
            pcm, RATE, self._strokes(center), (lo, hi),
            amount_db=24.0, mode="attenuate", channels=(0,))
        # 右声道逐样本不变。
        np.testing.assert_array_equal(out[1], pcm[1])
        # 左声道笔迹邻域的目标频带被衰减（≥6 dB）。
        probe_lo, probe_hi = center - 2400, center + 2400
        before = _band_energy(pcm, 0, 2000.0, probe_lo, probe_hi)
        after = _band_energy(out, 0, 2000.0, probe_lo, probe_hi)
        assert 20 * math.log10(after / before) <= -6.0

    def test_all_channels_matches_legacy(self) -> None:
        pytest.importorskip("src.features.edit.core.audio_repair")
        from src.features.edit.core.audio_repair import repair_spectral

        pcm = _tone(2000.0)
        lo, hi = 24000, 72000
        legacy = repair_spectral(
            pcm, RATE, self._strokes((lo + hi) // 2), (lo, hi),
            amount_db=18.0, mode="attenuate")
        scoped = repair_spectral(
            pcm, RATE, self._strokes((lo + hi) // 2), (lo, hi),
            amount_db=18.0, mode="attenuate", channels=None)
        np.testing.assert_array_equal(legacy, scoped)

    def test_invalid_channel_set_is_identity(self) -> None:
        pytest.importorskip("src.features.edit.core.audio_repair")
        from src.features.edit.core.audio_repair import repair_spectral

        pcm = _tone(2000.0)
        out = repair_spectral(
            pcm, RATE, self._strokes(48000), (24000, 72000),
            channels=(7, -1))  # 全越界/非法
        np.testing.assert_array_equal(out, pcm)

    def test_stack_step_channels_consumed_by_render(self) -> None:
        """预渲染路径消费 step 的 channels：右声道不被连带改写。"""
        from src.features.edit.core.audio_edit import (
            make_step, new_audio_edit, render_pre_render_steps,
        )

        pcm = _tone(2000.0)
        edit = new_audio_edit([make_step(
            "spectral_repair", 24000, 72000, (0,),
            mode="attenuate", amount_db=24.0,
            strokes=self._strokes(48000))])
        out = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        np.testing.assert_array_equal(out[1], pcm[1])
        assert _band_energy(out, 0, 2000.0, 45600, 50400) < \
            _band_energy(pcm, 0, 2000.0, 45600, 50400) * 0.5


class TestDenoiseChannelScope:
    def _noise_and_target(self):
        rng = np.random.default_rng(7)
        t = np.arange(2 * RATE) / RATE
        signal = (0.3 * np.sin(2 * np.pi * 2000.0 * t)).astype(np.float32)
        noise = (0.05 * rng.standard_normal(2 * RATE)).astype(np.float32)
        mixed = (signal + noise).astype(np.float32)
        pcm = np.stack([mixed, mixed])
        return pcm

    def test_left_only_denoise_leaves_right_untouched(self) -> None:
        pytest.importorskip("src.features.edit.core.audio_denoise")
        from src.features.edit.core.audio_denoise import (
            denoise_spectral_subtraction,
        )

        pcm = self._noise_and_target()
        out = denoise_spectral_subtraction(
            pcm, RATE, (0, 24000), (48000, 96000), 12.0,
            channels=(0,))
        np.testing.assert_array_equal(out[1], pcm[1])
        # 左声道噪声被压低（RMS 下降）。
        rms_before = float(np.sqrt(np.mean(pcm[0, 48000:96000] ** 2)))
        rms_after = float(np.sqrt(np.mean(out[0, 48000:96000] ** 2)))
        assert rms_after < rms_before * 0.95

    def test_denoise_step_channels_consumed_by_render(self) -> None:
        from src.features.edit.core.audio_edit import (
            make_step, new_audio_edit, render_pre_render_steps,
        )

        pcm = self._noise_and_target()
        edit = new_audio_edit([make_step(
            "spectral_denoise", 48000, 96000, (1,),
            reduction_db=12.0, noise_start=0, noise_end=24000)])
        out = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        np.testing.assert_array_equal(out[0], pcm[0])
        rms_before = float(np.sqrt(np.mean(pcm[1, 48000:96000] ** 2)))
        rms_after = float(np.sqrt(np.mean(out[1, 48000:96000] ** 2)))
        assert rms_after < rms_before * 0.95
