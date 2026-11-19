"""P1-4 对白剪辑工具 UI/命令层：切空不动同步 + 同场对话编辑组。

- SetDialogueGroupCommand：批量归组/退出一步撤销 + 序列化往返 +
  宏注册表完备（未登记会被既有完备性守卫拦下）；
- TimelineView 处理器：静音间隙宏按**降序**应用（原 clip_id 始终覆盖
  剩余左段）、下游片段不被移动（同步不动）；对话组按素材场次归组，
  多选一并处理。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.commands import MacroCommand
from src.features.edit.core.commands import SplitClipCommand
from src.features.edit.core.commands.clip_props_commands import (
    SetDialogueGroupCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


class TestSetDialogueGroupCommand:
    def _sequence(self) -> TimelineSequence:
        clips = (ClipInstance("c1", "a", "T", 0, 48, 0, 48),
                 ClipInstance("c2", "a", "T", 48, 48, 0, 48))
        return TimelineSequence(audio_tracks=(EditTrack("T", "T", "audio", 0,
                                                        clips=clips),))

    def test_assign_and_undo_restores_groups(self):
        from src.features.edit.core.commands import CommandStack
        seq = self._sequence()
        stack = CommandStack()
        applied = stack.execute(SetDialogueGroupCommand.create(("c1", "c2"), "SC04"), seq)
        assert all(clip.dialogue_group_id == "SC04"
                   for track in applied.audio_tracks for clip in track.clips)
        undone = stack.undo(applied)
        assert all(clip.dialogue_group_id is None
                   for track in undone.audio_tracks for clip in track.clips)

    def test_clear_is_explicit_none(self):
        seq = self._sequence()
        assigned = SetDialogueGroupCommand.create(("c1",), "SC04").apply(seq)
        cleared = SetDialogueGroupCommand.create(("c1",), None).apply(assigned)
        clip = cleared.audio_tracks[0].clips[0]
        assert clip.dialogue_group_id is None

    def test_serialization_roundtrip(self):
        command = SetDialogueGroupCommand.create(("c1", "c2"), "SC04")
        applied = command.apply(self._sequence())
        data = command.to_dict()
        assert data["command_type"] == "SetDialogueGroupCommand"
        restored = SetDialogueGroupCommand.from_dict(data)
        undone = restored.undo(applied)
        assert all(clip.dialogue_group_id is None
                   for track in undone.audio_tracks for clip in track.clips)

    def test_macro_registry_covers_command(self):
        from src.features.edit.core.commands import _MACRO_SUBCOMMAND_TYPES
        assert "SetDialogueGroupCommand" in _MACRO_SUBCOMMAND_TYPES


class TestTimelineDialogueHandlers:
    """TimelineView 处理器：切空宏（降序切分）与对话组归组。"""

    SR = 48000
    FPS = 25

    def _view_with_clip(self, qtbot):
        from src.features.edit.ui.timeline_view import TimelineView
        view = TimelineView()
        qtbot.addWidget(view)
        # source_in/out 是样本域：50 帧 @25fps = 2 s = 96000 样本
        clips = (ClipInstance("c1", "asset-1", "T", 0, 50, 0, 96000),
                 ClipInstance("c2", "asset-2", "T", 60, 40, 0, 76800))
        # 直接注入模型（不 load_timeline——避免波形解码 worker 干扰离屏测试）
        view._timeline = TimelineSequence(
            fps=Rational(self.FPS, 1),
            audio_tracks=(EditTrack("T", "T", "audio", 0, clips=clips),))
        view._project = SimpleNamespace(assets={
            "asset-1": SimpleNamespace(path="/tmp/x.wav", scene="SC04"),
            "asset-2": SimpleNamespace(path="/tmp/y.wav", scene=""),
        })
        executed: list = []
        view._execute_command = executed.append  # type: ignore[method-assign]
        return view, executed

    def _patch_decode(self, monkeypatch, pcm):
        import src.features.edit.core.clip_pcm as clip_pcm
        monkeypatch.setattr(clip_pcm, "decode_audio_range",
                            lambda *a, **k: pcm)
        import src.features.edit.ui.timeline_view as tv
        # 处理器经 clip_pcm.decode_audio_range 惰性导入 → 同一模块属性
        monkeypatch.setattr(tv, "decode_audio_range", lambda *a, **k: pcm,
                            raising=False)

    def test_gap_split_macro_applies_descending_and_keeps_sync(self, qtbot, monkeypatch):
        view, executed = self._view_with_clip(qtbot)
        sr = self.SR
        # 2 s PCM = clip 50 帧 @25fps：0.6s 音频 + 0.3s 静音 ×2 + 0.2s 音频
        sections = [np.full((2, int(sr * 0.6)), 0.2, dtype=np.float32),
                    np.zeros((2, int(sr * 0.3)), dtype=np.float32),
                    np.full((2, int(sr * 0.6)), 0.2, dtype=np.float32),
                    np.zeros((2, int(sr * 0.3)), dtype=np.float32),
                    np.full((2, int(sr * 0.2)), 0.2, dtype=np.float32)]
        self._patch_decode(monkeypatch, np.concatenate(sections, axis=1))
        view._on_clip_split_at_silence_requested("c1", "T")
        assert len(executed) == 1
        macro = executed[0]
        assert isinstance(macro, MacroCommand)
        frames = [command.split_frame for command in macro.sub_commands]
        assert frames == sorted(frames, reverse=True), "必须降序应用"
        # 40s 时间线上切点都落在 c1 [0, 50) 内部，c2 起点不受影响
        assert all(0 < frame < 50 for frame in frames)
        assert frames[-1] < 60

    def test_gap_split_skips_retimed_and_missing_asset(self, qtbot, monkeypatch):
        view, executed = self._view_with_clip(qtbot)
        from src.features.edit.core.timeline_v2 import SpeedParams
        retimed = view._timeline.get_clip_by_id("c1")
        view._timeline = view._timeline.with_track_updated(
            view._timeline.audio_tracks[0]._rebuild(clips=(
                __import__("dataclasses").replace(retimed,
                                                  speed=SpeedParams(speed=2.0)),)))
        self._patch_decode(monkeypatch, np.zeros((2, 100), dtype=np.float32))
        view._on_clip_split_at_silence_requested("c1", "T")
        assert executed == []
        view._on_clip_split_at_silence_requested("c2", "T")  # asset 无 scene/path 也无内容
        assert executed == []

    def test_dialogue_group_assign_uses_scene_and_selection(self, qtbot, monkeypatch):
        view, executed = self._view_with_clip(qtbot)
        from src.features.edit.core.selection import SelectionItem, SelectionType, TimelineSelection
        view._selection = TimelineSelection(items=(
            SelectionItem(SelectionType.CLIP, "c2"),))
        view._on_clip_dialogue_group_requested("c1", "T", {"kind": "assign"})
        assert len(executed) == 1
        assert isinstance(executed[0], SetDialogueGroupCommand)
        assert executed[0].clip_ids == ("c1", "c2")
        assert executed[0].group_id == "scene:SC04"
        # 无场次元数据 → no-op
        view._on_clip_dialogue_group_requested("c2", "T", {"kind": "assign"})
        assert len(executed) == 1
        # 清除 → group_id None
        view._on_clip_dialogue_group_requested("c1", "T", {"kind": "clear"})
        assert executed[1].group_id is None
