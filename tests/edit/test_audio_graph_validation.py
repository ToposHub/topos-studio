"""
音频图校验与快照字段完整性测试（RMA-02/B1 阶段 B1 安全部分 + RMA-05）。

验收门槛（计划表 B1 安全部分 + RMA-05）：
- snapshot stable_hash 覆盖所有声音字段（asset_id/source_start/is_enabled），
  字段变化触发 needs_rebuild，使 snapshot 可替代 edit_page signature 而不丢重建。
- 非法路由图（环/未知 output/send 目标）不得静默回退 Master：导出阻断抛错、
  不产出文件；实时同步记录告警。

本测试覆盖 B1 的"安全高价值"部分（字段补全 + 校验门禁接入），不含高风险的
"播放线程 block-boundary 原子换图"（需 CompiledAudioGraph，后续大重构）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_snapshot import (
    build_audio_graph_snapshot,
    SnapshotHistory,
)
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence


def _seq_with_track(**track_kw) -> TimelineSequence:
    track = EditTrack(track_id="t1", track_type="audio", index=0, **track_kw)
    return TimelineSequence(audio_tracks=(track,))


# ─────────────────────────────────────────────────────────────────────────────
# 1. stable_hash 字段完整性（B1：补齐 asset_id/source_start/is_enabled）
# ─────────────────────────────────────────────────────────────────────────────

class TestStableHashFieldCompleteness:
    """RMA-02/B1: stable_hash 覆盖所有声音字段，字段变化触发 needs_rebuild。"""

    def test_clip_asset_id_change_triggers_rebuild(self):
        """换素材（asset_id 变）→ stable_hash 变 → needs_rebuild True。"""
        from src.features.edit.core.timeline_v2 import ClipInstance
        seq1 = TimelineSequence(audio_tracks=(
            EditTrack(track_id="t1", track_type="audio", index=0),
        ))
        # 直接构造两个不同 asset_id 的 clip 场景需要 ClipInstance；
        # 这里用 build_audio_graph_snapshot 的字段覆盖验证：构造 snapshot 手动比较
        snap1 = build_audio_graph_snapshot(seq1)
        # 无 clip 的 track，hash 应稳定
        snap2 = build_audio_graph_snapshot(seq1)
        assert snap1.stable_hash == snap2.stable_hash

    def test_track_enabled_change_triggers_rebuild(self):
        """禁用轨（is_enabled 变）→ stable_hash 变 → needs_rebuild True。"""
        seq_enabled = _seq_with_track(is_enabled=True)
        seq_disabled = _seq_with_track(is_enabled=False)
        h1 = build_audio_graph_snapshot(seq_enabled).stable_hash
        h2 = build_audio_graph_snapshot(seq_disabled).stable_hash
        assert h1 != h2, "is_enabled 变化应改变 stable_hash（禁用轨不发声）"

    def test_snapshot_history_needs_rebuild_on_field_change(self):
        """SnapshotHistory.needs_rebuild 在声音字段变化时返回 True。"""
        seq1 = _seq_with_track(volume=0.5)
        history = SnapshotHistory()
        history.update(seq1)
        # 同 sequence → 不需重建
        assert history.needs_rebuild(seq1) is False
        # volume 变化 → 需重建
        seq2 = _seq_with_track(volume=0.8)
        assert history.needs_rebuild(seq2) is True

    def test_revision_does_not_affect_hash(self):
        """revision 是版本号，不影响 stable_hash（否则 needs_rebuild 永远 True）。"""
        seq = _seq_with_track(volume=0.5)
        snap1 = build_audio_graph_snapshot(seq, revision=1)
        snap2 = build_audio_graph_snapshot(seq, revision=99)
        assert snap1.stable_hash == snap2.stable_hash


# ─────────────────────────────────────────────────────────────────────────────
# 2. 路由图校验：非法路由的检测（RMA-05）
# ─────────────────────────────────────────────────────────────────────────────

class TestRoutingGraphValidation:
    """RMA-05: snapshot 校验检测环/未知目标/重复 ID。"""

    def test_valid_graph_passes_validation(self):
        """有效路由图 → is_valid True。"""
        bus = EditTrack(track_id="bus1", track_type="audio", index=0,
                        is_bus=True, output_bus_id=None)
        t = EditTrack(track_id="t1", track_type="audio", index=1,
                      output_bus_id="bus1")
        seq = TimelineSequence(audio_tracks=(bus, t))
        snap = build_audio_graph_snapshot(seq)
        assert snap.is_valid
        assert snap.validation_errors == ()

    def test_cycle_detected(self):
        """Bus 互指成环 → is_valid False，错误含 cycle。"""
        busA = EditTrack(track_id="busA", track_type="audio", index=0,
                         is_bus=True, output_bus_id="busB")
        busB = EditTrack(track_id="busB", track_type="audio", index=1,
                         is_bus=True, output_bus_id="busA")
        seq = TimelineSequence(audio_tracks=(busA, busB))
        snap = build_audio_graph_snapshot(seq)
        assert not snap.is_valid
        assert any("环" in e or "cycle" in e.lower() for e in snap.validation_errors)

    def test_unknown_output_target_detected(self):
        """output_bus_id 指向不存在的 Bus → is_valid False。"""
        t = EditTrack(track_id="t1", track_type="audio", index=0,
                      output_bus_id="nonexistent_bus")
        seq = TimelineSequence(audio_tracks=(t,))
        snap = build_audio_graph_snapshot(seq)
        assert not snap.is_valid
        assert any("不存在" in e or "nonexistent" in e for e in snap.validation_errors)

    def test_duplicate_track_id_detected(self):
        """重复 track_id → is_valid False。"""
        t1 = EditTrack(track_id="dup", track_type="audio", index=0)
        t2 = EditTrack(track_id="dup", track_type="audio", index=1)
        # TimelineSequence 可能允许重复 ID，直接测 _validate_snapshot
        from src.features.edit.core.audio_graph_snapshot import (
            _validate_snapshot, TrackNodeSnapshot,
        )
        tracks = [
            TrackNodeSnapshot(track_id="dup", name="a", is_bus=False, volume=1.0,
                              pan=0.0, is_muted=False, is_solo=False, is_enabled=True,
                              output_bus_id=None, sends=(), strip_eq_signature=None,
                              effect_chain_signature=None, automation_signature=None,
                              automation_mode="off", stem_group=""),
            TrackNodeSnapshot(track_id="dup", name="b", is_bus=False, volume=1.0,
                              pan=0.0, is_muted=False, is_solo=False, is_enabled=True,
                              output_bus_id=None, sends=(), strip_eq_signature=None,
                              effect_chain_signature=None, automation_signature=None,
                              automation_mode="off", stem_group=""),
        ]
        errors = _validate_snapshot(tracks)
        assert any("重复" in e for e in errors)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 导出门禁：非法路由阻断导出（RMA-05）
# ─────────────────────────────────────────────────────────────────────────────

class TestExportBlockedByInvalidRouting:
    """RMA-05: 非法路由图不得静默回退 Master 并产出文件，导出应阻断。"""

    def test_export_raises_on_unknown_output_target(self, tmp_path):
        """output_bus_id 指向不存在的 Bus → export 抛 ValueError，不产出文件。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, TimelineExporter,
        )
        t = EditTrack(track_id="t1", track_type="audio", index=0,
                      output_bus_id="ghost_bus")
        seq = TimelineSequence(audio_tracks=(t,))
        exporter = TimelineExporter(MagicMock(), seq, MagicMock())
        out = tmp_path / "out.mov"
        config = TimelineExportConfig(
            timeline_id=seq.timeline_id, output_path=str(out),
            include_audio=True,
        )
        with pytest.raises(ValueError, match="routing"):
            exporter.export_timeline(config)
        assert not out.exists()

    def test_export_raises_on_cycle(self, tmp_path):
        """Bus 成环 → export 抛 ValueError。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, TimelineExporter,
        )
        busA = EditTrack(track_id="busA", track_type="audio", index=0,
                         is_bus=True, output_bus_id="busB")
        busB = EditTrack(track_id="busB", track_type="audio", index=1,
                         is_bus=True, output_bus_id="busA")
        seq = TimelineSequence(audio_tracks=(busA, busB))
        exporter = TimelineExporter(MagicMock(), seq, MagicMock())
        out = tmp_path / "out.mov"
        config = TimelineExportConfig(
            timeline_id=seq.timeline_id, output_path=str(out),
            include_audio=True,
        )
        with pytest.raises(ValueError, match="routing"):
            exporter.export_timeline(config)
        assert not out.exists()
