"""T04 · 黄金数据验证：capture 必须对上独立手算预期（expected.py）。

这里的断言值全部来自 expected.py 的字面量/独立算法，不调用任何被测
实现来"生成答案"。
"""

from __future__ import annotations

import os
import time

import pytest

from tests.edit.insights import expected as exp
from tests.edit.insights import scale_fixture
from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state


pytestmark = [pytest.mark.edit_core]


@pytest.fixture(scope="module")
def snap():
    st = syn.build_synthetic_state()
    return capture_readonly_state(
        st.state, capture_id="cap-t04", captured_at=0.0
    )


def _sequence(snapshot, sequence_id):
    matches = [s for s in snapshot.sequences if s.sequence_id == sequence_id]
    assert len(matches) == 1
    return matches[0]


# ── 黄金工程：区间/并集/计数 ─────────────────────────────────────


def test_v1_intervals_match_hand_computed(snap):
    main = _sequence(snap, syn.SEQ_MAIN)
    v1 = [t for t in main.tracks if t.track_id == syn.TRACK_V1][0]
    intervals = [(c.timeline_start, c.timeline_end) for c in v1.clips]
    # 独立暴力并集（逐帧集合）必须等于手算 380；实现无关。
    assert exp.brute_force_union_frames(intervals) == exp.S1_V1_COVERAGE_FRAMES
    # 间隙 = 460 - 320（手算）
    assert exp.S1_V1_GAP_FRAMES == 80


def test_shot_lengths_mean_median_hand_computed(snap):
    main = _sequence(snap, syn.SEQ_MAIN)
    v1 = [t for t in main.tracks if t.track_id == syn.TRACK_V1][0]
    lengths = tuple(c.timeline_end - c.timeline_start for c in v1.clips)
    assert lengths == exp.S1_V1_SHOT_LENGTHS
    assert sum(lengths) / len(lengths) == exp.S1_V1_MEAN_SHOT
    sorted_lengths = sorted(lengths)
    median = (sorted_lengths[1] + sorted_lengths[2]) / 2
    assert median == exp.S1_V1_MEDIAN_SHOT


def test_cue_coverage_hand_computed(snap):
    main = _sequence(snap, syn.SEQ_MAIN)
    sub = [t for t in main.tracks if t.track_id == syn.TRACK_SUB1][0]
    intervals = [(c.timeline_start, c.timeline_end) for c in sub.cues]
    assert exp.brute_force_union_frames(intervals) == exp.S1_CUE_COVERAGE_FRAMES


def test_clip_and_entity_counts(snap):
    main = _sequence(snap, syn.SEQ_MAIN)
    ntsc = _sequence(snap, syn.SEQ_NTSC)
    main_clips = [c for t in main.tracks for c in t.clips]
    ntsc_clips = [c for t in ntsc.tracks for c in t.clips]
    assert len(main_clips) == exp.CLIP_COUNTS["main"]
    assert len(ntsc_clips) == exp.CLIP_COUNTS["ntsc"]
    assert len(main_clips) + len(ntsc_clips) == exp.TOTAL_CLIP_OCCURRENCES

    used_assets = {c.asset_id for c in main_clips + ntsc_clips}
    assert len(used_assets) == exp.UNIQUE_USED_ASSETS

    main_cues = [c for t in main.tracks for c in t.cues]
    ntsc_cues = [c for t in ntsc.tracks for c in t.cues]
    assert len(main_cues) == exp.CUE_COUNTS["main"]
    assert len(ntsc_cues) == exp.CUE_COUNTS["ntsc"]
    assert len(main.markers) == exp.MARKER_COUNTS["main"]
    assert len(ntsc.markers) == exp.MARKER_COUNTS["ntsc"]


def test_duration_and_timecode_expectations(snap):
    main = _sequence(snap, syn.SEQ_MAIN)
    assert main.duration_frames == exp.S1_DURATION
    # 显示时间码（独立手算值；此处只验证既有 Timecode 公式与手算一致，
    # 报表侧禁止另造公式）
    from src.shared.media.metadata import Rational
    from src.shared.media.timecode import Timecode, TimecodeType

    tc24 = Timecode.from_frame_number(100, Rational(24, 1), TimecodeType.NON_DROP_FRAME)
    assert tc24.to_string() == exp.TC_S1_FRAME100
    tc_df = Timecode.from_frame_number(
        100, Rational(30000, 1001), TimecodeType.DROP_FRAME
    )
    assert tc_df.to_string() == exp.TC_S2_FRAME100


# ── 边界清单覆盖自检（计划 T04 要求的每个边界都在 fixture 里）───


def test_fixture_covers_required_boundaries(snap):
    """两序列/重复源/叠轨/间隙/marker/中文多行/三种fps/非零入点/变速/
    冻结/离线/未加载——逐一存在性断言（缺任一边界=fixture 失效）。"""
    st = syn.build_synthetic_state()
    loaded_ids = [s.sequence_id for s in snap.sequences if s.load_state == "loaded"]
    assert len(loaded_ids) == 2                       # 两序列（第三条 unloaded）
    assert any(s.load_state == "unloaded" for s in snap.sequences)  # 未加载 ref
    main = _sequence(snap, syn.SEQ_MAIN)
    v1 = [t for t in main.tracks if t.track_id == syn.TRACK_V1][0]
    asset_refs = [c.asset_id for c in v1.clips]
    assert asset_refs.count(syn.ASSET_A) == 2          # 同素材两次使用（重复源）
    assert v1.clips[2].source_start == 0 and v1.clips[0].source_start == 24  # 非零入点
    assert v1.clips[2].constant_speed == 2.0           # 速度变化
    assert v1.clips[3].source_mapping == "unknown"     # 冻结
    # 叠轨：A1 音频 [0,200) 与 V1 视频 [0,120) 时间重叠
    a1 = [t for t in main.tracks if t.track_id == syn.TRACK_A1][0]
    assert a1.clips[0].timeline_start < v1.clips[0].timeline_end
    # 中文多行引号
    sub = [t for t in main.tracks if t.track_id == syn.TRACK_SUB1][0]
    assert any('\n' in c.text and '"' in c.text for c in sub.cues)
    # 离线素材
    assert any(a.is_offline for a in snap.assets)
    # 三种 fps：24/1、30000/1001 DF、25(unloaded ref)
    fps_set = {(s.fps.num, s.fps.den) for s in snap.sequences}
    assert (24, 1) in fps_set and (30000, 1001) in fps_set and (25, 1) in fps_set


# ── 规模 fixtures ────────────────────────────────────────────────


@pytest.mark.parametrize("n", [100, 10_000])
def test_scale_fixture_independent_arithmetic(n):
    sc = scale_fixture.build_scale_state(n)
    t0 = time.perf_counter()
    snapshot = capture_readonly_state(sc.state, capture_id=f"cap-scale-{n}")
    elapsed = time.perf_counter() - t0
    # 独立手算：duration = 24N；asset 数 = ceil(N/3)；cue/marker = N//100
    (seq,) = snapshot.sequences
    assert seq.duration_frames == scale_fixture.CLIP_FRAMES * n
    assert len(snapshot.assets) == (n + 2) // 3
    v1 = [t for t in seq.tracks if t.track_type == "video"][0]
    assert len(v1.clips) == n
    assert len(seq.markers) == n // 100
    # 捕获是纯内存白名单复制——10k 实体应在秒级内（宽松上界，只防回归性劣化）
    assert elapsed < 30.0, f"capture of {n} clips took {elapsed:.1f}s"


def test_scale_100k_behind_env_flag():
    """10 万规模按内存条件选择：默认跳过，INSIGHTS_SCALE_100K=1 才跑。"""
    if os.environ.get("INSIGHTS_SCALE_100K") != "1":
        pytest.skip("10万规模按条件开启：INSIGHTS_SCALE_100K=1")
    sc = scale_fixture.build_scale_state(100_000)
    snapshot = capture_readonly_state(sc.state, capture_id="cap-scale-100k")
    (seq,) = snapshot.sequences
    assert seq.duration_frames == scale_fixture.CLIP_FRAMES * 100_000
