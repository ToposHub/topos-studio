"""T15 · 快照保存/读取与变化对照测试。"""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.diff import (
    compare_snapshots,
    export_diff_json,
    export_diff_markdown,
    load_snapshot_file,
    save_snapshot_file,
)


pytestmark = [pytest.mark.edit_core]


@pytest.fixture(scope="module")
def base_snap():
    st = syn.build_synthetic_state()
    return capture_readonly_state(st.state, capture_id="cap-old", captured_at=1.0)


def test_save_load_roundtrip_identical(base_snap, tmp_path):
    path = tmp_path / "snap.json"
    save_snapshot_file(base_snap, path)
    result = load_snapshot_file(path)
    assert result.ok, result.error
    assert result.snapshot == base_snap  # 同次捕获序列化→重读恒等


def test_load_rejects_tampered_file(base_snap, tmp_path):
    path = tmp_path / "snap.json"
    save_snapshot_file(base_snap, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sequences"][0]["name"] = "被篡改"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = load_snapshot_file(path)
    assert not result.ok and result.error.code == "HASH_MISMATCH"


def test_load_rejects_unknown_schema(base_snap, tmp_path):
    path = tmp_path / "snap.json"
    save_snapshot_file(base_snap, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = "99"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = load_snapshot_file(path)
    assert not result.ok and result.error.code == "UNSUPPORTED_SCHEMA"


def test_revision_only_change_is_empty_diff(base_snap):
    """revision/捕获元数据变化 → 内容级 diff 为空（不造成所有行变化）。"""
    st = syn.build_synthetic_state()
    bumped = syn.build_bumped_state(st)
    new_snap = capture_readonly_state(
        bumped.state, capture_id="cap-new", captured_at=999.0
    )
    diff = compare_snapshots(base_snap, new_snap)
    assert not diff.has_changes
    assert diff.same_project is True


def test_cue_text_change_located_precisely(base_snap):
    st = syn.build_synthetic_state()
    changed_main = dc_replace(
        st.main,
        subtitle_tracks=(
            dc_replace(
                st.main.subtitle_tracks[0],
                subtitle_clips=(
                    type(st.main.subtitle_tracks[0].subtitle_clips[0])(
                        clip_id=syn.CUE_ZH, track_id=syn.TRACK_SUB1,
                        timeline_start=10, timeline_duration=60, text="改后的台词",
                    ),
                    st.main.subtitle_tracks[0].subtitle_clips[1],
                ),
            ),
        ),
    )
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    sequences = dict(st.sequences)
    sequences[syn.SEQ_MAIN] = changed_main
    new_snap = capture_readonly_state(
        ProjectSequences(project=st.project, sequences=sequences),
        capture_id="cap-new", captured_at=2.0,
    )
    diff = compare_snapshots(base_snap, new_snap)
    assert diff.has_changes
    # 精确：只有这一条 cue changed，且定位到正确序列
    assert len(diff.cue_changes) == 1
    change = diff.cue_changes[0]
    assert change.sequence_id == syn.SEQ_MAIN and change.entity_id == syn.CUE_ZH
    assert change.kind == "changed"
    assert change.changes == (
        change.changes[0].__class__(field="text", old=syn.CUE_TEXT_ZH, new="改后的台词"),
    )
    assert diff.clip_changes == () and diff.marker_changes == ()


def test_clip_duration_change_and_stats_delta(base_snap):
    st = syn.build_synthetic_state()
    v1 = st.main.video_tracks[0]
    new_clips = list(v1.clips)
    # 手算：A2 [120,200) → [120,240)，相邻 B1 起点挪到 240 且时长不变
    new_clips[1] = dc_replace(new_clips[1], timeline_start=120, timeline_duration=120)
    new_clips[2] = dc_replace(new_clips[2], timeline_start=240, timeline_duration=120)
    new_v1 = dc_replace(v1, clips=tuple(new_clips))
    changed_main = dc_replace(
        st.main, video_tracks=(new_v1,) + st.main.video_tracks[1:]
    )
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    sequences = dict(st.sequences)
    sequences[syn.SEQ_MAIN] = changed_main
    new_snap = capture_readonly_state(
        ProjectSequences(project=st.project, sequences=sequences),
        capture_id="cap-new", captured_at=2.0,
    )
    diff = compare_snapshots(base_snap, new_snap)
    changed_ids = {
        (c.sequence_id, c.entity_id) for c in diff.clip_changes if c.kind == "changed"
    }
    assert changed_ids == {(syn.SEQ_MAIN, syn.CLIP_A2), (syn.SEQ_MAIN, syn.CLIP_B1)}
    # 统计差值：S1 覆盖并集 380 → 420
    # （改后 V1 = [0,120)[120,240)[240,360) → [0,360) ∪ [400,460) = 360+60）
    delta = {s.sequence_id: s for s in diff.stats_delta}[syn.SEQ_MAIN]
    assert delta.coverage_union_old == 380 and delta.coverage_union_new == 420
    assert delta.clip_count_old == 5 and delta.clip_count_new == 5


def test_unloaded_not_misjudged_as_mass_removal(base_snap):
    """一侧未加载的序列进 not_compared，不报成大量删除。"""
    unloaded_main = dc_replace(
        base_snap.sequences[0], load_state="unloaded",
        duration_frames=None, tracks=(), markers=(),
    )
    new_snap = dc_replace(
        base_snap, capture_id="cap-new",
        sequences=(unloaded_main,) + base_snap.sequences[1:],
    )
    diff = compare_snapshots(base_snap, new_snap)
    assert syn.SEQ_MAIN in diff.not_compared
    removed_in_main = [
        c for c in diff.clip_changes
        if c.sequence_id == syn.SEQ_MAIN and c.kind == "removed"
    ]
    assert removed_in_main == []  # 完整性范围不同 ≠ 删除


def test_duplicate_labels_paired_by_id(base_snap):
    """A1/A2 同素材同名：只改其中一个的源入点，另一个不动（按 ID 配对）。"""
    st = syn.build_synthetic_state()
    v1 = st.main.video_tracks[0]
    new_clips = list(v1.clips)
    new_clips[0] = dc_replace(new_clips[0], source_start=25)  # A1 源入点 24→25
    new_v1 = dc_replace(v1, clips=tuple(new_clips))
    changed_main = dc_replace(st.main, video_tracks=(new_v1,) + st.main.video_tracks[1:])
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences

    sequences = dict(st.sequences)
    sequences[syn.SEQ_MAIN] = changed_main
    new_snap = capture_readonly_state(
        ProjectSequences(project=st.project, sequences=sequences),
        capture_id="cap-new", captured_at=2.0,
    )
    diff = compare_snapshots(base_snap, new_snap)
    assert [c.entity_id for c in diff.clip_changes] == [syn.CLIP_A1]


def test_rebuilt_project_flagged(base_snap):
    new_snap = dc_replace(
        base_snap, capture_id="cap-new",
        project=dc_replace(base_snap.project, project_id="other-project"),
    )
    diff = compare_snapshots(base_snap, new_snap)
    assert diff.same_project is False  # 提示不可直接一一对应


def test_exports_readonly(base_snap, tmp_path):
    st = syn.build_synthetic_state()
    new_snap = capture_readonly_state(
        st.state, capture_id="cap-new", captured_at=2.0
    )
    diff = compare_snapshots(base_snap, new_snap)
    export_diff_json(diff, tmp_path / "d.json")
    export_diff_markdown(diff, tmp_path / "d.md")
    payload = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "insights-snapshot-diff/1"
    assert "no project restore/merge" in payload["note"]
    md = (tmp_path / "d.md").read_text(encoding="utf-8")
    assert "编辑内容无变化" in md
