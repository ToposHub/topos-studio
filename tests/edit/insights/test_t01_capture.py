"""T01 · capture_readonly_state 契约测试（深冻结/只读/加载范围/时间基）。"""

from __future__ import annotations

import pytest

from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    LOAD_STATE_UNLOADED,
    SOURCE_MAPPING_CONSTANT,
    SOURCE_MAPPING_IDENTITY,
    SOURCE_MAPPING_UNKNOWN,
)


pytestmark = [pytest.mark.edit_core]


@pytest.fixture()
def captured():
    st = syn.build_synthetic_state()
    snapshot = capture_readonly_state(
        st.state, capture_id="cap-fixed-0001", captured_at=1000.5
    )
    return st, snapshot


def _seq(snapshot, sequence_id):
    matches = [s for s in snapshot.sequences if s.sequence_id == sequence_id]
    assert len(matches) == 1
    return matches[0]


def _track(sequence, track_id):
    matches = [t for t in sequence.tracks if t.track_id == track_id]
    assert len(matches) == 1
    return matches[0]


def _clip(sequence, clip_id):
    for t in sequence.tracks:
        for c in t.clips:
            if c.clip_id == clip_id:
                return c
    raise AssertionError(f"clip {clip_id} not found")


def test_deep_freeze_against_host_mutation(captured):
    """捕获后宿主 dict/metadata 继续被编辑，DTO 与 hash 均不受影响。"""
    st, snapshot = captured
    # EditProject 是 frozen dataclass，但其 dict 字段可变——这正是深冻结
    # 要防的宿主侧写入路径。
    st.project.assets["late-asset"] = st.project.assets[syn.ASSET_A]
    st.project.assets[syn.ASSET_A].metadata["fps"] = 999.0
    st.project.timelines["late-seq"] = st.project.timelines[syn.SEQ_MAIN]

    from src.features.edit.insights.codec import verify_payload_hash, verify_content_hash

    assert len(snapshot.sequences) == 3
    assert len(snapshot.assets) == 3
    assert verify_payload_hash(snapshot)
    assert verify_content_hash(snapshot)


def test_unloaded_sequence_marked_not_empty_timeline(captured):
    _, snapshot = captured
    unloaded = _seq(snapshot, syn.SEQ_UNLOADED)
    assert unloaded.load_state == LOAD_STATE_UNLOADED
    assert unloaded.tracks == ()
    assert unloaded.markers == ()
    assert unloaded.duration_frames is None  # 未收集，不冒充 0
    assert unloaded.fps.num == 25 and unloaded.fps.den == 1

    loaded = _seq(snapshot, syn.SEQ_MAIN)
    assert loaded.load_state == LOAD_STATE_LOADED
    # 手算：V1 末端 460、A1 200、SUB1 150 → 460
    assert loaded.duration_frames == 460


def test_half_open_intervals_and_source_mapping(captured):
    _, snapshot = captured
    main = _seq(snapshot, syn.SEQ_MAIN)
    c1 = _clip(main, syn.CLIP_A1)
    assert (c1.timeline_start, c1.timeline_end) == (0, 120)
    assert (c1.source_start, c1.source_end) == (24, 144)  # 非零入点
    assert c1.source_mapping == SOURCE_MAPPING_IDENTITY

    c3 = _clip(main, syn.CLIP_B1)  # 恒速 2.0
    assert c3.source_mapping == SOURCE_MAPPING_CONSTANT
    assert c3.constant_speed == pytest.approx(2.0)
    assert (c3.timeline_start, c3.timeline_end) == (200, 320)

    c4 = _clip(main, syn.CLIP_B2)  # 冻结 → unknown（M04 未验收映射）
    assert c4.source_mapping == SOURCE_MAPPING_UNKNOWN


def test_fps_rational_and_df_flag(captured):
    _, snapshot = captured
    ntsc = _seq(snapshot, syn.SEQ_NTSC)
    assert ntsc.fps.num == 30000 and ntsc.fps.den == 1001
    assert ntsc.fps.drop_frame is True
    main = _seq(snapshot, syn.SEQ_MAIN)
    assert (main.fps.num, main.fps.den) == (24, 1)
    assert main.fps.drop_frame is False


def test_revisions_captured_per_sequence(captured):
    st, snapshot = captured
    assert snapshot.project.revision == st.project.revision
    assert snapshot.sequence_revision_map == {
        syn.SEQ_MAIN: st.main.revision,
        syn.SEQ_NTSC: st.ntsc.revision,
        syn.SEQ_UNLOADED: 0,
    }


def test_capture_is_readonly_no_revision_or_dict_change(captured):
    st, snapshot = captured
    before_assets = dict(st.project.assets)
    before_seqs = dict(st.sequences)
    capture_readonly_state(st.state)  # 再捕获一次
    assert st.project.revision == st.revision_project
    assert st.main.revision == st.revision_main
    assert st.project.assets.keys() == before_assets.keys()
    assert st.sequences.keys() == before_seqs.keys()


def test_asset_whitelist_and_path_redaction(captured):
    _, snapshot = captured
    by_id = {a.asset_id: a for a in snapshot.assets}
    cam_a = by_id[syn.ASSET_A]
    assert cam_a.file_basename == "CamA_Take3.mov"  # 只有 basename
    assert "/Volumes" not in cam_a.file_basename
    assert cam_a.keywords == ("interview", "hero")
    assert cam_a.rating == 4
    assert by_id[syn.ASSET_SFX].is_offline is True


def test_cues_and_markers_captured(captured):
    _, snapshot = captured
    main = _seq(snapshot, syn.SEQ_MAIN)
    sub = _track(main, syn.TRACK_SUB1)
    zh = [c for c in sub.cues if c.clip_id == syn.CUE_ZH][0]
    assert zh.text == syn.CUE_TEXT_ZH
    assert (zh.timeline_start, zh.timeline_end) == (10, 70)
    marker_ids = {m.marker_id for m in main.markers}
    assert {syn.MARK_TURN, syn.MARK_CHAP} <= marker_ids
    turn = [m for m in main.markers if m.marker_id == syn.MARK_TURN][0]
    assert turn.name == "转折点" and turn.frame == 100


def test_same_asset_two_usages_remain_independent(captured):
    """同一素材两次使用 → 两个独立 ClipEntry（合并归并即违规）。"""
    _, snapshot = captured
    main = _seq(snapshot, syn.SEQ_MAIN)
    c1 = _clip(main, syn.CLIP_A1)
    c2 = _clip(main, syn.CLIP_A2)
    assert c1.asset_id == c2.asset_id == syn.ASSET_A
    assert c1.clip_id != c2.clip_id
    # 跨序列：S2 也用了 CamA
    ntsc = _seq(snapshot, syn.SEQ_NTSC)
    assert _clip(ntsc, syn.CLIP_NTSC1).asset_id == syn.ASSET_A


def test_malformed_state_raises_value_error():
    class NotAState:
        project = None
        sequences = None

    with pytest.raises(ValueError):
        capture_readonly_state(NotAState())
