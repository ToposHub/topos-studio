"""P1 · 对白剪辑工具核心算法（2026-11-19 计划第二阶段）。

验收（模拟场景的最小闭环）：Room Tone 填补无缝循环+边缘交叉淡化；
J/L cut 分割点与画面切点偏移；批量交叉淡化按容差配对；ADR take 与
同期参考的互相关对齐（整样本 + 子帧）。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.dialogue_tools import (
    estimate_alignment_offset,
    fill_room_tone,
    plan_batch_crossfades,
    plan_jl_cut,
)

RATE = 48000


class TestRoomToneFill:
    def _clip(self) -> np.ndarray:
        """对白贯穿整段，但 [1000, 13000) 是纯 Room Tone 采集窗。"""
        t = np.arange(RATE) / RATE
        dialogue = 0.3 * np.sin(2 * np.pi * 300.0 * t)
        dialogue[1000:13000] = 0.0
        noise = 0.01 * np.random.default_rng(7).standard_normal(RATE)
        sig = (dialogue + noise).astype(np.float32)
        return np.stack([sig, sig])

    def test_fill_replaces_selection_with_noise_floor(self) -> None:
        pcm = self._clip()
        filled = fill_room_tone(
            pcm, RATE, 24000, 48000,
            source_start=1000, source_end=13000)
        interior = filled[:, 25000:47000]
        # 中段是噪声底：RMS 与采集窗一致；频谱无相干音调
        # （对白 300 Hz 会以 >>10× 中位数突出，白噪声峰值 ~3-4×）。
        spec = np.abs(np.fft.rfft(interior[0] * np.hanning(interior.shape[1])))
        freqs = np.fft.rfftfreq(interior.shape[1], 1.0 / RATE)
        dialogue_band = spec[(freqs > 250) & (freqs < 350)].max()
        floor_level = float(np.median(spec))
        assert dialogue_band < 10.0 * floor_level
        src_rms = float(np.sqrt(np.mean(pcm[:, 1000:13000] ** 2)))
        out_rms = float(np.sqrt(np.mean(interior ** 2)))
        assert out_rms == pytest.approx(src_rms, rel=0.3)
        assert 0.002 < float(np.max(np.abs(interior))) < 0.2
        # 选区外逐样本不变。
        np.testing.assert_array_equal(filled[:, :23900], pcm[:, :23900])
        np.testing.assert_array_equal(filled[:, 48100:], pcm[:, 48100:])
        # 不修改输入。
        np.testing.assert_array_equal(pcm, self._clip())

    def test_short_source_rejected(self) -> None:
        pcm = self._clip()
        with pytest.raises(ValueError):
            fill_room_tone(pcm, RATE, 0, 48000,
                           source_start=0, source_end=100)

    def test_loop_energy_matches_source_rms(self) -> None:
        rng = np.random.default_rng(3)
        noise = (0.02 * rng.standard_normal((2, RATE))).astype(np.float32)
        filled = fill_room_tone(
            noise, RATE, 0, RATE,
            source_start=0, source_end=RATE // 2, crossfade_ms=4.0)
        body = filled[:, 2400:-2400]
        src_rms = float(np.sqrt(np.mean(noise[:, :RATE // 2] ** 2)))
        out_rms = float(np.sqrt(np.mean(body ** 2)))
        assert out_rms == pytest.approx(src_rms, rel=0.35)


class TestJLCut:
    def _clip(self, start=0, duration=100):
        return SimpleNamespace(timeline_start=start,
                               timeline_duration=duration)

    def test_j_cut_splits_before_video_cut(self) -> None:
        plan = plan_jl_cut(self._clip(start=10, duration=100), 60, -8)
        assert plan.valid and plan.kind == "J"
        assert plan.audio_split_frame == 52

    def test_l_cut_splits_after(self) -> None:
        plan = plan_jl_cut(self._clip(start=10, duration=100), 60, 12)
        assert plan.valid and plan.kind == "L"
        assert plan.audio_split_frame == 72

    def test_out_of_clip_invalid(self) -> None:
        plan = plan_jl_cut(self._clip(start=10, duration=20), 60, 60)
        assert not plan.valid and plan.reason


class TestBatchCrossfades:
    def _track(self, starts_durations):
        clips = [
            SimpleNamespace(clip_id=f"c{i}",
                            timeline_start=s, timeline_duration=d)
            for i, (s, d) in enumerate(starts_durations)]
        return SimpleNamespace(clips=clips)

    def test_pairs_within_tolerance(self) -> None:
        track = self._track([(0, 100), (101, 100), (250, 100)])
        plan = plan_batch_crossfades(track, max_gap_frames=2)
        assert [(l, r) for l, r, _ in plan.pairs] == [("c0", "c1")]
        assert plan.skipped and plan.skipped[0][2] == 49

    def test_zero_gap_pairs(self) -> None:
        track = self._track([(0, 100), (100, 100)])
        plan = plan_batch_crossfades(track, max_gap_frames=0)
        assert len(plan.pairs) == 1


class TestAlignmentEstimate:
    def _dialogue(self, offset_samples: int):
        rng = np.random.default_rng(11)
        t = np.arange(int(0.6 * RATE)) / RATE
        envelope = np.exp(-((t - 0.3) ** 2) / (2 * 0.05 ** 2))
        voice = 0.4 * np.sin(2 * np.pi * 220.0 * t) * envelope
        voice = voice + 0.005 * rng.standard_normal(voice.shape[0])
        ref = np.concatenate([
            voice, np.zeros(int(1.2 * RATE) - voice.shape[0])])
        # take = voice 从 offset 开始（ref 的 cue 出现在 offset 处）。
        take = np.concatenate([
            np.zeros(offset_samples), voice,
            np.zeros(max(0, int(0.05 * RATE)))])
        return ref.astype(np.float32), take.astype(np.float32)

    def test_integer_offset_found(self) -> None:
        offset = 7200
        ref, take = self._dialogue(offset)
        est = estimate_alignment_offset(ref, take, RATE)
        assert est is not None
        assert est.confidence > 0.3
        # take 的 cue 在 +offset 处；对齐到参考的 0 → 移动 −offset。
        assert est.offset_samples == pytest.approx(-offset, abs=8)

    def test_fractional_refinement(self) -> None:
        # 半样本错位：子帧插值应给出 ≈±0.5 的残差方向。
        ref, take = self._dialogue(7200)
        shifted = np.concatenate([take[1:], take[:1]])  # 左移 1 样本
        est = estimate_alignment_offset(ref, shifted, RATE)
        assert est is not None
        assert est.offset_samples == pytest.approx(-7200 + 1, abs=8)

    def test_silent_returns_none(self) -> None:
        est = estimate_alignment_offset(
            np.zeros(int(1.0 * RATE), dtype=np.float32),
            np.zeros(int(0.3 * RATE), dtype=np.float32), RATE)
        assert est is None
