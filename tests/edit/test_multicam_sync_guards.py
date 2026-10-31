"""F2.6 · 多机位同步防错匹配 / TC 同步 / 多段漂移判别。

验收锁定：
- 静音（低置信）/反相/周期信号歧义/出界 → 拒绝写回（带原因，非静默）。
- 时间码同步：TC 差换算正确；缺 TC/坏 TC 按错误呈现不写。
- 多段（首/中/末）：一致 → 中位偏移可写；发散（速率漂移）→ 诊断拒绝
  保留原片（不伪称已同步）；部分段失败 → inconsistent。
- filter_writable_offsets：error/rejection 全过滤 + 原因清单。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.multicam_sync import (
    MAX_SYNC_AMBIGUITY,
    MIN_SYNC_CONFIDENCE,
    SyncAlignment,
    alignment_rejection,
    best_lag_diagnostics,
    combine_segment_alignments,
    compute_sync_alignments,
    compute_timecode_alignments,
    filter_writable_offsets,
)

pytestmark = pytest.mark.edit_core

SR = 8000


def _noise(seconds: float = 3.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * 0.2).astype(np.float32)


class TestMismatchGuards:
    def test_uncorrelated_noise_low_confidence_rejected(self) -> None:
        """不相关噪声（静音/无共同音频）不写错误偏移。"""
        ref = _noise(seed=1)
        other = _noise(seed=2)
        lag, conf, _amb = best_lag_diagnostics(ref, other, SR * 5)
        assert abs(conf) < MIN_SYNC_CONFIDENCE
        assert alignment_rejection(SyncAlignment(
            "a", lag_seconds=lag / SR, confidence=conf)) == "low_confidence"

    def test_inverted_polarity_detected(self) -> None:
        """反相接错线：峰值相关为负且显著——拒绝并给反相原因。"""
        ref = _noise(seed=3)
        lag, conf, _amb = best_lag_diagnostics(ref, -ref, SR * 5)
        assert conf < 0 and abs(conf) >= MIN_SYNC_CONFIDENCE
        assert alignment_rejection(SyncAlignment(
            "a", confidence=conf)) == "inverted_polarity"

    def test_periodic_signal_ambiguity_rejected(self) -> None:
        """重复音乐床（周期冲击串）在周期倍数处近等高峰——歧义拒绝。"""
        period = SR // 2  # 0.5s 周期
        t = np.zeros(SR * 4, dtype=np.float32)
        pulse = _noise(0.05, seed=5)
        for start in range(0, t.size - pulse.size, period):
            t[start:start + pulse.size] += pulse
        ref = t
        other = np.roll(t, SR // 4)  # 周期内移位
        _lag, _conf, amb = best_lag_diagnostics(ref, other, SR * 5)
        assert amb > MAX_SYNC_AMBIGUITY, f"周期信号应有高歧义，got {amb}"
        assert alignment_rejection(SyncAlignment(
            "a", confidence=0.9, ambiguity=amb)) == "ambiguous_peak"

    def test_out_of_range_rejected(self) -> None:
        assert alignment_rejection(
            SyncAlignment("a", confidence=0.9), out_of_range=True) \
            == "offset_out_of_range"

    def test_reliable_alignment_passes(self) -> None:
        ref = _noise(seed=7)
        shifted = np.concatenate([_noise(0.5, seed=8), ref])[:ref.size]
        signals = {"ref": ref, "a": shifted}
        result = compute_sync_alignments(
            signals, sample_rate=SR, reference_id="ref",
            fps_by_asset={"ref": 24.0, "a": 24.0})
        assert result["a"].rejection == ""
        assert result["a"].lag_seconds == pytest.approx(0.5, abs=0.01)

    def test_silence_segment_rejected_in_compute(self) -> None:
        signals = {"ref": _noise(seed=9), "a": np.zeros(SR * 3, np.float32)}
        result = compute_sync_alignments(
            signals, sample_rate=SR, reference_id="ref",
            fps_by_asset={"ref": 24.0, "a": 24.0})
        assert result["a"].rejection == "low_confidence"


class TestTimecodeSync:
    def _meta(self, tc: str) -> dict:
        return {"timecode": tc}

    def test_tc_difference_math(self) -> None:
        """角度 TC 晚 2 秒 → lag +2s（该角度开录更早，与波形语义一致）。"""
        result = compute_timecode_alignments(
            {"ref": self._meta("01:00:00:00"), "a": self._meta("01:00:02:00")},
            "ref", {"ref": 24.0, "a": 24.0})
        assert result["a"].lag_seconds == pytest.approx(2.0)
        assert result["a"].offset_frames == 48
        assert result["a"].rejection == "" and not result["a"].error

    def test_missing_tc_is_error_not_written(self) -> None:
        result = compute_timecode_alignments(
            {"ref": self._meta("01:00:00:00"), "a": {}},
            "ref", {"ref": 24.0, "a": 24.0})
        assert result["a"].error == "no_timecode"

    def test_bad_tc_is_error(self) -> None:
        result = compute_timecode_alignments(
            {"ref": self._meta("01:00:00:00"), "a": self._meta("garbage")},
            "ref", {"ref": 24.0, "a": 24.0})
        assert result["a"].error == "bad_timecode"

    def test_missing_reference_tc_errors_all(self) -> None:
        result = compute_timecode_alignments(
            {"ref": {}, "a": self._meta("01:00:00:00")},
            "ref", {"ref": 24.0, "a": 24.0})
        assert result["a"].error == "reference_no_timecode"


class TestMultiSegmentDrift:
    def _align(self, lag: float, conf: float = 0.9) -> SyncAlignment:
        return SyncAlignment(
            "a", lag_seconds=lag, offset_frames=int(lag * 24),
            confidence=conf)

    def test_consistent_segments_use_median(self) -> None:
        combined = combine_segment_alignments(
            [self._align(0.42), self._align(0.43), self._align(0.42)], fps=24.0)
        assert combined.rejection == ""
        assert combined.lag_seconds == pytest.approx(0.42)
        assert combined.offset_frames == round(0.42 * 24)

    def test_linear_drift_diagnosed_not_written(self) -> None:
        """三段偏移线性发散（速率漂移）——诊断拒绝，保留原片。"""
        combined = combine_segment_alignments(
            [self._align(0.0), self._align(0.5), self._align(1.0)], fps=24.0)
        assert combined.rejection == "rate_drift_detected"

    def test_two_segment_inconsistency_rejected(self) -> None:
        combined = combine_segment_alignments(
            [self._align(0.0), self._align(1.0)], fps=24.0)
        assert combined.rejection == "inconsistent_segments"

    def test_all_rejected_propagates_reason(self) -> None:
        combined = combine_segment_alignments(
            [SyncAlignment("a", confidence=0.1, rejection="low_confidence")])
        assert combined.rejection == "low_confidence"


class TestWritableFilter:
    def test_error_and_rejection_excluded_with_reasons(self) -> None:
        payload = {
            "ok": {"offset_frames": 12, "confidence": 0.9},
            "bad": {"offset_frames": 99, "error": "no_audio_stream"},
            "risky": {"offset_frames": 55, "rejection": "ambiguous_peak"},
        }
        offsets, skipped = filter_writable_offsets(payload)
        assert offsets == {"ok": 12}
        assert len(skipped) == 2
        assert any("no_audio_stream" in s for s in skipped)
        assert any("ambiguous_peak" in s for s in skipped)
