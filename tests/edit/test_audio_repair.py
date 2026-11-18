"""§5 频谱修复：掩码、attenuate/heal DSP 与 spectral_repair 步骤。

验收来自 docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §5 画笔：
- 440 Hz 信号 + 仅持续 80 ms 的 2 kHz 干扰；遮罩后 2 kHz 能量下降 ≥12 dB、
  500 Hz 以下信号变化 ≤1 dB；选区外逐样本相同；
- 画笔载荷沿 (sample, hz) 持久化 + 时间半径 + log2 带宽，笔迹间隙填补；
- heal 从前后未遮罩帧插值对数幅值并按相邻帧相位推进重建。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_denoise import estimate_tone_amplitude
from src.features.edit.core.audio_edit import (
    append_step,
    make_step,
    new_audio_edit,
    render_pre_render_steps,
    validate_audio_edit,
)
from src.features.edit.core.audio_repair import (
    build_repair_mask,
    normalize_strokes,
    repair_spectral,
)

RATE = 48000


def _fixture_pcm() -> np.ndarray:
    """1 s：440 Hz + 1 kHz 常驻；2 kHz 干扰仅在 [0.40, 0.48) s。"""
    t = np.arange(RATE) / RATE
    sig = (0.2 * np.sin(2 * np.pi * 440.0 * t)
           + 0.05 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
    pcm = np.stack([sig, sig])
    lo, hi = int(0.40 * RATE), int(0.48 * RATE)
    interfer = (0.3 * np.sin(2 * np.pi * 2000.0 * t[lo:hi])).astype(np.float32)
    pcm[:, lo:hi] += interfer[None, :]
    return pcm


def _strokes(sample: int = int(0.44 * RATE), hz: float = 2000.0,
             radius: int = 6000, band: float = 0.5) -> list[dict]:
    return [{"sample": sample, "hz": hz, "time_radius": radius,
             "bandwidth_oct": band}]


class TestMask:
    def test_stroke_gap_filling(self) -> None:
        strokes = _strokes(sample=48000) + _strokes(sample=96000, hz=1000.0)
        filled = normalize_strokes(strokes, RATE)
        assert len(filled) > 2
        assert filled == sorted(filled, key=lambda s: s["sample"])
        gaps = [b["sample"] - a["sample"] for a, b in zip(filled, filled[1:])]
        assert max(gaps) <= 6000 * 0.5 + 1

    def test_mask_bounds_and_soft_edge(self) -> None:
        mask = build_repair_mask(RATE, _strokes(), RATE)
        assert mask.shape[0] == 2048 // 2 + 1
        assert float(mask.min()) >= 0.0 and float(mask.max()) <= 1.0
        assert float(mask.max()) == pytest.approx(1.0, abs=1e-5)
        # DC 行（0 Hz）不参与修复。
        assert float(mask[0].max()) == 0.0
        # 远离笔迹的时间位置不受影响。
        assert float(mask[:, 0].max()) == 0.0

    def test_empty_and_invalid_strokes(self) -> None:
        assert build_repair_mask(RATE, [], RATE).size == 0
        cleaned = normalize_strokes(
            [{"sample": 10, "hz": 100.0, "time_radius": 0, "bandwidth_oct": 1.0}],
            RATE)
        assert cleaned == []


class TestAttenuate:
    def test_acceptance_two_khz_dropped_twelve_db(self) -> None:
        pcm = _fixture_pcm()
        out = repair_spectral(
            pcm, RATE, _strokes(), (int(0.3 * RATE), int(0.6 * RATE)),
            amount_db=24.0, mode="attenuate")
        lo, hi = int(0.41 * RATE), int(0.47 * RATE)
        before = estimate_tone_amplitude(pcm, RATE, 2000.0, lo, hi)
        after = estimate_tone_amplitude(out, RATE, 2000.0, lo, hi)
        assert 20 * np.log10(after / before) <= -12.0
        # 440 Hz / 1 kHz（含 500 Hz 以下与选区内邻带）变化 ≤1 dB。
        for freq in (440.0, 1000.0):
            delta = 20 * np.log10(
                estimate_tone_amplitude(out, RATE, freq, lo, hi)
                / estimate_tone_amplitude(pcm, RATE, freq, lo, hi))
            assert abs(delta) <= 1.0
        # 选区外逐样本相同。
        np.testing.assert_array_equal(pcm[:, :int(0.3 * RATE)],
                                      out[:, :int(0.3 * RATE)])
        np.testing.assert_array_equal(pcm[:, int(0.6 * RATE):],
                                      out[:, int(0.6 * RATE):])

    def test_zero_amount_is_bypass(self) -> None:
        pcm = _fixture_pcm()
        out = repair_spectral(
            pcm, RATE, _strokes(), (int(0.3 * RATE), int(0.6 * RATE)),
            amount_db=0.0, mode="attenuate")
        np.testing.assert_array_equal(out, pcm)


class TestHeal:
    def test_heal_restores_notched_region(self) -> None:
        # 440 Hz 连续信号中段被挖掉（时间-频率局部缺失）→ heal 重建幅值。
        t = np.arange(RATE) / RATE
        sig = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        pcm = np.stack([sig, sig]).copy()
        lo, hi = int(0.45 * RATE), int(0.50 * RATE)
        pcm[:, lo:hi] = 0.0
        healed = repair_spectral(
            pcm, RATE, _strokes(sample=(lo + hi) // 2, hz=440.0,
                                radius=4000, band=0.6),
            (int(0.40 * RATE), int(0.55 * RATE)),
            amount_db=48.0, mode="heal")
        mid = slice(int(0.455 * RATE), int(0.495 * RATE))
        before = estimate_tone_amplitude(pcm, RATE, 440.0,
                                         mid.start, mid.stop)
        after = estimate_tone_amplitude(healed, RATE, 440.0,
                                        mid.start, mid.stop)
        # 能量恢复：挖空段 RMS 从 0 恢复到原信号 RMS 的 ≥35%（相位推进
        # 无展开时的保守下限——重建是能量级的，音色相干性靠衰减兜底）。
        target_rms = 0.2 / np.sqrt(2.0)
        healed_rms = float(np.sqrt(np.mean(np.square(healed[0][mid]))))
        assert healed_rms >= 0.35 * target_rms
        assert after > before * 3.0


class TestStep:
    def _repair_edit(self, **param_overrides) -> dict:
        params = dict(
            strokes=_strokes(), amount_db=24.0, mode="attenuate", nfft=2048)
        params.update(param_overrides)
        return new_audio_edit([make_step(
            "spectral_repair", int(0.3 * RATE), int(0.6 * RATE), (0, 1),
            **params)])

    def test_validation(self) -> None:
        assert validate_audio_edit(self._repair_edit(), clip_output_samples=RATE) == []
        for bad in (
            self._repair_edit(amount_db=99.0),
            self._repair_edit(mode="repaint"),
            self._repair_edit(nfft=123),
            self._repair_edit(strokes=[]),
            self._repair_edit(strokes=[{"sample": 10 ** 9, "hz": 100.0,
                                        "time_radius": 100, "bandwidth_oct": 1.0}]),
            self._repair_edit(strokes=[{"sample": 10, "hz": RATE,
                                        "time_radius": 100, "bandwidth_oct": 1.0}]),
            self._repair_edit(strokes=[{"sample": 10, "hz": 100.0,
                                        "time_radius": 0, "bandwidth_oct": 1.0}]),
        ):
            assert validate_audio_edit(bad, clip_output_samples=RATE), bad

    def test_render_matches_direct_call(self) -> None:
        pcm = _fixture_pcm()
        edit = self._repair_edit()
        rendered = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        direct = repair_spectral(
            pcm, RATE, _strokes(), (int(0.3 * RATE), int(0.6 * RATE)),
            amount_db=24.0, mode="attenuate")
        np.testing.assert_array_equal(rendered, direct)
        # 降噪 + 修复按栈序工作（先降噪后修复）。
        combo = append_step(new_audio_edit([make_step(
            "spectral_denoise", int(0.3 * RATE), int(0.6 * RATE), (0, 1),
            noise_start=0, noise_end=int(0.3 * RATE), reduction_db=6.0)]),
            edit["steps"][0])
        combo_out = render_pre_render_steps(pcm, combo, sample_rate=RATE)
        assert combo_out.shape == pcm.shape
        assert not np.allclose(combo_out, rendered)

    def test_requires_scipy(self, monkeypatch) -> None:
        import src.features.edit.core.audio_repair as repair_mod
        monkeypatch.setattr(repair_mod, "_SCIPY_AVAILABLE", False)
        with pytest.raises(RuntimeError):
            repair_spectral(
                _fixture_pcm(), RATE, _strokes(),
                (int(0.3 * RATE), int(0.6 * RATE)))
