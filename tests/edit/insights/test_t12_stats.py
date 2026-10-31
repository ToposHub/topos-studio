"""T12 · 素材使用与剪辑节奏统计测试（对照 expected.py + 独立算法交叉验证）。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.edit.insights import expected as exp
from tests.edit.insights import synthetic as syn

from src.features.edit.core.asset_usage_index import build_asset_usage_index
from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.stats import (
    compute_coverage,
    compute_rhythm,
    compute_usage_rhythm,
    merge_intervals,
)


pytestmark = [pytest.mark.edit_core]


@pytest.fixture(scope="module")
def report():
    st = syn.build_synthetic_state()
    snapshot = capture_readonly_state(st.state, capture_id="cap-t12", captured_at=0.0)
    return compute_usage_rhythm(snapshot)


def _seq(report, sequence_id):
    matches = [s for s in report.sequences if s.sequence_id == sequence_id]
    assert len(matches) == 1
    return matches[0]


# ── 覆盖并集（独立算法交叉验证）──────────────────────────────────


def test_merge_intervals_boundaries():
    assert merge_intervals([(0, 120), (120, 200)]) == ((0, 200),)   # 相接合并
    assert merge_intervals([(200, 320), (0, 120)]) == ((0, 120), (200, 320))
    assert merge_intervals([(0, 200), (50, 100)]) == ((0, 200),)    # 包含
    assert merge_intervals([(5, 5), (0, 10)]) == ((0, 10),)         # 空区间丢弃


def test_coverage_matches_hand_computed(report):
    main = _seq(report, syn.SEQ_MAIN)
    v1 = [t for t in main.tracks if t.track_id == syn.TRACK_V1][0]
    cov = v1.coverage
    assert cov.union_frames == exp.S1_V1_COVERAGE_FRAMES == 380
    assert cov.gap_frames == exp.S1_V1_GAP_FRAMES == 80
    assert cov.span_frames == 460
    assert v1.sum_of_durations == 380  # 本例 V1 行和恰等于并集（无叠轨）


def test_stacked_sum_differs_from_union(report):
    """A1 音频与 V1 时间重叠：行和 580 ≠ 全轨并集（必须分列）。"""
    main = _seq(report, syn.SEQ_MAIN)
    all_sum = sum(t.sum_of_durations for t in main.tracks)
    assert all_sum == 580  # 手算 120+80+120+60+200
    assert main.all_tracks_union.union_frames == 380  # A1 ⊂ V1 张成内
    assert main.all_tracks_union.union_frames != all_sum


def test_cue_coverage_hand_computed(report):
    main = _seq(report, syn.SEQ_MAIN)
    assert main.cue_coverage.union_frames == exp.S1_CUE_COVERAGE_FRAMES == 130


# ── 节奏 ─────────────────────────────────────────────────────────


def test_rhythm_hand_computed(report):
    main = _seq(report, syn.SEQ_MAIN)
    rhythm = main.video_shot_rhythm
    assert rhythm.shot_count == 4
    assert rhythm.lengths == tuple(sorted(exp.S1_V1_SHOT_LENGTHS))  # 分布升序
    assert rhythm.mean_length == exp.S1_V1_MEAN_SHOT
    assert rhythm.median_length == exp.S1_V1_MEDIAN_SHOT
    assert rhythm.min_length == 60 and rhythm.max_length == 120
    # cuts/min 手算：cut=3，时长 460 帧 @24fps → 3*24*60/460 = 4320/460
    assert rhythm.cuts_per_minute == pytest.approx(4320 / 460)
    assert rhythm.denominator_frames == 460  # 分母显式携带


def test_rhythm_zero_duration_is_none():
    rhythm = compute_rhythm([], duration_frames=0, fps_num=24, fps_den=1)
    assert rhythm.mean_length is None
    assert rhythm.cuts_per_minute is None  # 分母不可得 → None 不冒充 0


# ── 素材使用（与既有 asset_usage_index 语义交叉验证）────────────


def test_asset_usage_counts_and_positions(report):
    by_id = {a.asset_id: a for a in report.assets}
    cam_a = by_id[syn.ASSET_A]
    assert cam_a.occurrence_count == 3                       # 手算：S1×2 + NTSC×1
    assert set(cam_a.sequence_ids) == {syn.SEQ_MAIN, syn.SEQ_NTSC}
    positions = {(o.clip_id, o.timeline_start, o.timeline_end) for o in cam_a.occurrences}
    assert positions == {
        (syn.CLIP_A1, 0, 120), (syn.CLIP_A2, 120, 200), (syn.CLIP_NTSC1, 0, 100),
    }
    assert by_id[syn.ASSET_B].occurrence_count == 2
    assert by_id[syn.ASSET_SFX].occurrence_count == 1
    assert report.unused_asset_ids == ()                     # 三个素材全被使用


def test_semantics_match_existing_asset_usage_index():
    """同一宿主状态：本包直接使用统计 == 既有 build_asset_usage_index。"""
    st = syn.build_synthetic_state()
    snapshot = capture_readonly_state(st.state, capture_id="cap-t12-x", captured_at=0.0)
    mine = compute_usage_rhythm(snapshot)
    existing = build_asset_usage_index(
        st.sequences.values(), project=st.project
    )
    for asset_id, usage in ((a.asset_id, a) for a in mine.assets):
        reference = existing.locations_for(asset_id)
        assert usage.occurrence_count == len(reference)
    assert set(mine.unused_asset_ids) == set(existing.unused_asset_ids)


# ── 禁用轨口径 ───────────────────────────────────────────────────


def test_disabled_track_scope_visible():
    from src.features.edit.insights.capture import capture_readonly_state

    st = syn.build_synthetic_state()
    snapshot = capture_readonly_state(st.state, capture_id="cap-t12-d", captured_at=0.0)
    # DTO 层把 V1 置为禁用（快照深冻结，replace 出新快照变体）
    disabled_seq = None
    new_sequences = []
    for seq in snapshot.sequences:
        if seq.sequence_id != syn.SEQ_MAIN:
            new_sequences.append(seq)
            continue
        new_tracks = []
        for track in seq.tracks:
            if track.track_id == syn.TRACK_V1:
                track = replace(track, is_enabled=False)
            new_tracks.append(track)
        disabled_seq = replace(seq, tracks=tuple(new_tracks))
        new_sequences.append(disabled_seq)
    variant = replace(snapshot, sequences=tuple(new_sequences))

    included = compute_usage_rhythm(variant, include_disabled_tracks=True)
    excluded = compute_usage_rhythm(variant, include_disabled_tracks=False)
    main_in = _seq(included, syn.SEQ_MAIN)
    main_ex = _seq(excluded, syn.SEQ_MAIN)
    assert main_in.clip_count == 5        # V1(4)+A1(1)
    assert main_ex.clip_count == 1        # 只剩 A1
    assert main_ex.video_shot_rhythm.shot_count == 0
    # 口径声明随参数变化且进入报告
    assert "disabled tracks included" in included.scope_notes
    assert "disabled tracks excluded" in excluded.scope_notes


def test_unloaded_marks_incomplete():
    st = syn.build_synthetic_state()
    snapshot = capture_readonly_state(st.state, capture_id="cap-t12-u", captured_at=0.0)
    report = compute_usage_rhythm(snapshot)
    assert report.complete is False      # S3 未加载
    unloaded = _seq(report, syn.SEQ_UNLOADED)
    assert unloaded.video_shot_rhythm is None
    assert unloaded.all_tracks_union is None


def test_scope_notes_declare_direct_usage_only(report):
    joined = " ".join(report.scope_notes)
    assert "direct clip references only" in joined
    assert "not expanded" in joined      # 嵌套不展开声明
