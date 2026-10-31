"""
Audio Graph Snapshot 测试（§1.1/1.4）。

验证（计划表 §1.1/§5.1）：
- AudioGraphSnapshot：不可变，含所有声音字段（clip/track/bus/master）。
- stable_hash：覆盖所有声音字段，修改任一字段 hash 变化。
- 编译期校验：环/未知目标/重复 ID 检测，错误明确。
- revision：单调递增。
- SnapshotHistory：update/needs_rebuild。
- diff：字段级差异（调试用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_snapshot import (
    AudioGraphSnapshot, build_audio_graph_snapshot, SnapshotHistory,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack, TimelineSequence, ClipInstance, Send,
)


def _make_clip(track_id: str, start=0, duration=48) -> ClipInstance:
    return ClipInstance(
        clip_id=f"{track_id}_c", asset_id="a1", track_id=track_id,
        source_start=0, source_duration=duration,
        timeline_start=start, timeline_duration=duration,
    )


def _make_sequence():
    t1 = EditTrack(track_id="t1", track_type="audio", index=0, name="Dialogue",
                   clips=(_make_clip("t1"),), volume=0.8, pan=-0.3)
    bus = EditTrack(track_id="busA", track_type="audio", index=1, name="Bus A", is_bus=True)
    return TimelineSequence(audio_tracks=(t1, bus))


class TestSnapshotConstruction:
    """快照构建。"""

    def test_build_from_sequence(self):
        seq = _make_sequence()
        snap = build_audio_graph_snapshot(seq, revision=1)
        assert snap.revision == 1
        # 1 clip（t1）
        assert len(snap.clips) == 1
        # 2 track/bus
        assert len(snap.tracks) == 2

    def test_snapshot_is_immutable(self):
        """快照不可变（frozen dataclass）。"""
        seq = _make_sequence()
        snap = build_audio_graph_snapshot(seq)
        with pytest.raises((AttributeError, Exception)):
            snap.revision = 999  # type: ignore

    def test_contains_all_sound_fields(self):
        """含所有声音字段（§1.1）。"""
        seq = _make_sequence()
        snap = build_audio_graph_snapshot(seq)
        # track 字段
        t = next(t for t in snap.tracks if t.track_id == "t1")
        assert t.volume == 0.8
        assert t.pan == -0.3
        # clip 字段
        c = snap.clips[0]
        assert c.timeline_duration == 48
        # master 字段
        assert hasattr(snap.master, "volume")
        assert hasattr(snap.master, "muted")


class TestStableHash:
    """stable_hash 覆盖所有声音字段。"""

    def test_same_sequence_same_hash(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq, revision=1)
        snap2 = build_audio_graph_snapshot(seq, revision=2)
        # revision 不影响 hash（hash 只看声音字段）
        assert snap1.stable_hash == snap2.stable_hash

    def test_volume_change_changes_hash(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq)
        seq2 = seq.with_track_updated(
            seq.audio_tracks[0].with_volume(1.2)
        )
        snap2 = build_audio_graph_snapshot(seq2)
        assert snap1.stable_hash != snap2.stable_hash

    def test_pan_change_changes_hash(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq)
        seq2 = seq.with_track_updated(
            seq.audio_tracks[0].with_pan(0.5)
        )
        snap2 = build_audio_graph_snapshot(seq2)
        assert snap1.stable_hash != snap2.stable_hash

    def test_master_volume_change_changes_hash(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq)
        seq2 = seq.with_master_volume(1.5)
        snap2 = build_audio_graph_snapshot(seq2)
        assert snap1.stable_hash != snap2.stable_hash

    def test_routing_change_changes_hash(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq)
        # t1 路由到 busA
        seq2 = seq.with_track_updated(
            seq.audio_tracks[0].with_output_bus("busA")
        )
        snap2 = build_audio_graph_snapshot(seq2)
        assert snap1.stable_hash != snap2.stable_hash


class TestCompileValidation:
    """编译期校验（§1.1）。"""

    def test_valid_snapshot_no_errors(self):
        seq = _make_sequence()
        snap = build_audio_graph_snapshot(seq)
        assert snap.is_valid
        assert snap.validation_errors == ()

    def test_cycle_detected(self):
        """环检测。"""
        busA = EditTrack(track_id="busA", track_type="audio", index=0, is_bus=True,
                         output_bus_id="busB")
        busB = EditTrack(track_id="busB", track_type="audio", index=1, is_bus=True,
                         output_bus_id="busA")
        seq = TimelineSequence(audio_tracks=(busA, busB))
        snap = build_audio_graph_snapshot(seq)
        assert not snap.is_valid
        assert any("环" in e or "cycle" in e for e in snap.validation_errors)

    def test_unknown_target_detected(self):
        """未知目标检测。"""
        t1 = EditTrack(track_id="t1", track_type="audio", index=0,
                       output_bus_id="ghostBus")
        seq = TimelineSequence(audio_tracks=(t1,))
        snap = build_audio_graph_snapshot(seq)
        assert not snap.is_valid
        assert any("不存在" in e for e in snap.validation_errors)

    def test_duplicate_id_detected(self):
        """重复 ID 检测。"""
        # 直接构造带重复 ID（绕过 __post_init__ 校验）
        t1 = EditTrack(track_id="t1", track_type="audio", index=0)
        # 手动构建 snapshot 传入重复
        from src.features.edit.core.audio_graph_snapshot import (
            TrackNodeSnapshot, MasterNodeSnapshot, _validate_snapshot,
        )
        dup = TrackNodeSnapshot(
            track_id="t1", name="", is_bus=False, volume=1.0, pan=0.0,
            is_muted=False, is_solo=False, is_enabled=True, output_bus_id=None,
            sends=(), strip_eq_signature=None, effect_chain_signature=None,
            automation_signature=None, automation_mode="off", stem_group="",
        )
        errors = _validate_snapshot([dup, dup])
        assert any("重复" in e for e in errors)


class TestRevisionAndHistory:
    """revision + SnapshotHistory。"""

    def test_revision_monotonic(self):
        seq = _make_sequence()
        history = SnapshotHistory()
        s1 = history.update(seq)
        s2 = history.update(seq)
        assert s2.revision > s1.revision
        assert history.revision == 2

    def test_needs_rebuild_on_change(self):
        seq = _make_sequence()
        history = SnapshotHistory()
        history.update(seq)
        # 改 volume
        seq2 = seq.with_track_updated(
            seq.audio_tracks[0].with_volume(1.5)
        )
        assert history.needs_rebuild(seq2) is True

    def test_no_rebuild_when_unchanged(self):
        seq = _make_sequence()
        history = SnapshotHistory()
        history.update(seq)
        assert history.needs_rebuild(seq) is False

    def test_needs_rebuild_first_time(self):
        seq = _make_sequence()
        history = SnapshotHistory()
        assert history.needs_rebuild(seq) is True


class TestSnapshotDiff:
    """snapshot diff（调试用）。"""

    def test_diff_detects_volume_change(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq)
        seq2 = seq.with_track_updated(
            seq.audio_tracks[0].with_volume(1.5)
        )
        snap2 = build_audio_graph_snapshot(seq2)
        diffs = snap1.diff(snap2)
        assert any("t1" in d for d in diffs)

    def test_diff_detects_track_removal(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq)
        # 移除 busA
        seq2 = TimelineSequence(
            audio_tracks=seq.audio_tracks[:1],  # 只留 t1
        )
        snap2 = build_audio_graph_snapshot(seq2)
        diffs = snap1.diff(snap2)
        assert any("busA" in d and "removed" in d for d in diffs)

    def test_no_diff_when_identical(self):
        seq = _make_sequence()
        snap1 = build_audio_graph_snapshot(seq)
        snap2 = build_audio_graph_snapshot(seq)
        # revision 不同但声音字段同 → diff 应为空（不含 revision）
        diffs = snap1.diff(snap2)
        # diff 不报 revision（只看声音字段）
        sound_diffs = [d for d in diffs if "revision" not in d.lower()]
        assert sound_diffs == []


class TestSnapshotCompleteness:
    """§1.5 同步策略：所有声音字段触发 hash 变化。"""

    def test_all_sound_fields_in_hash(self):
        """参数化验证各字段修改都触发 hash 变化。"""
        seq = _make_sequence()
        base = build_audio_graph_snapshot(seq)

        # 各字段修改
        modifications = [
            ("volume", seq.with_track_updated(seq.audio_tracks[0].with_volume(0.9))),
            ("pan", seq.with_track_updated(seq.audio_tracks[0].with_pan(0.1))),
            ("mute", seq.with_track_updated(seq.audio_tracks[0].with_muted(True))),
            ("solo", seq.with_track_updated(seq.audio_tracks[0].with_solo(True))),
            ("master_volume", seq.with_master_volume(1.3)),
            ("master_muted", seq.with_master_muted(True)),
        ]
        for label, modified_seq in modifications:
            modified_snap = build_audio_graph_snapshot(modified_seq)
            assert base.stable_hash != modified_snap.stable_hash, \
                f"{label} 修改未触发 hash 变化"
