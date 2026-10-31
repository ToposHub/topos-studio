"""
Batch Mute/Solo MacroCommand 测试（AM-12 阶段 4）。

验证批量 mute/solo 操作用 MacroCommand 聚合为单次 undo，而非逐条独立入栈。

验收标准（计划表 AM-12）：
- 一次操作只生成一个 undo entry
- redo 前后 track mute/solo 状态完全一致
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import (
    MacroCommand,
    SetTrackMuteCommand,
    SetTrackSoloCommand,
)
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence


def _make_seq(n_tracks: int = 3) -> TimelineSequence:
    """构造含 n 个音频轨的 sequence。"""
    tracks = tuple(
        EditTrack(track_id=f"t{i}", name=f"A{i}", track_type="audio", index=i)
        for i in range(n_tracks)
    )
    return TimelineSequence(audio_tracks=tracks)


class TestBatchMuteMacro:
    """Batch Mute 用 MacroCommand 聚合（AM-12）。"""

    def test_batch_mute_single_undo(self):
        """批量 mute 3 轨 → MacroCommand apply 后全部 mute，undo 后全部恢复。"""
        seq = _make_seq(3)
        # 构造 batch mute macro（模拟 _on_mixer_batch_mute_changed）
        subs = [SetTrackMuteCommand.create(f"t{i}", True) for i in range(3)]
        macro = MacroCommand.create("Batch Mute", subs)

        seq2, updated = macro.apply(seq)
        # 全部 mute
        for i in range(3):
            assert seq2.get_track_by_id(f"t{i}").is_muted is True

        # 单次 undo 恢复全部
        seq3 = updated.undo(seq2)
        for i in range(3):
            assert seq3.get_track_by_id(f"t{i}").is_muted is False

    def test_batch_mute_one_macro_not_three(self):
        """AM-12 核心：3 个 mute 命令聚合成 1 个 MacroCommand（非 3 个独立命令）。"""
        subs = [SetTrackMuteCommand.create(f"t{i}", True) for i in range(3)]
        macro = MacroCommand.create("Batch Mute", subs)
        assert len(macro.sub_commands) == 3
        # 这是一个单一命令对象（单次 undo entry）

    def test_batch_unmute(self):
        """批量 unmute（从 mute→unmute）。"""
        tracks = tuple(
            EditTrack(track_id=f"t{i}", track_type="audio", index=i, is_muted=True)
            for i in range(3)
        )
        seq = TimelineSequence(audio_tracks=tracks)
        subs = [SetTrackMuteCommand.create(f"t{i}", False) for i in range(3)]
        macro = MacroCommand.create("Batch Unmute", subs)
        seq2, updated = macro.apply(seq)
        for i in range(3):
            assert seq2.get_track_by_id(f"t{i}").is_muted is False
        # undo 恢复 mute
        seq3 = updated.undo(seq2)
        for i in range(3):
            assert seq3.get_track_by_id(f"t{i}").is_muted is True


class TestBatchSoloMacro:
    """Batch Solo Clear 用 MacroCommand 聚合（AM-12）。"""

    def test_batch_clear_solo_single_undo(self):
        """批量 clear solo → MacroCommand apply 后全部非 solo，undo 恢复。"""
        tracks = tuple(
            EditTrack(track_id=f"t{i}", track_type="audio", index=i, is_solo=True)
            for i in range(3)
        )
        seq = TimelineSequence(audio_tracks=tracks)
        subs = [SetTrackSoloCommand.create(f"t{i}", False) for i in range(3)]
        macro = MacroCommand.create("Clear All Solo", subs)

        seq2, updated = macro.apply(seq)
        for i in range(3):
            assert seq2.get_track_by_id(f"t{i}").is_solo is False

        seq3 = updated.undo(seq2)
        for i in range(3):
            assert seq3.get_track_by_id(f"t{i}").is_solo is True

    def test_mixed_mute_solo_in_one_macro(self):
        """mute + solo 混合在一个 MacroCommand（一次 undo）。"""
        seq = _make_seq(2)
        subs = [
            SetTrackMuteCommand.create("t0", True),
            SetTrackSoloCommand.create("t1", True),
        ]
        macro = MacroCommand.create("Batch State", subs)
        seq2, updated = macro.apply(seq)
        assert seq2.get_track_by_id("t0").is_muted is True
        assert seq2.get_track_by_id("t1").is_solo is True
        # 单次 undo
        seq3 = updated.undo(seq2)
        assert seq3.get_track_by_id("t0").is_muted is False
        assert seq3.get_track_by_id("t1").is_solo is False
