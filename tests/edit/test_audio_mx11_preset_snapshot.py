"""MX-11 / ST-03 回归：预设与快照全字段 capture/apply/undo 一致。

计划验收：
- 完整快照 A→B→A 逐字段一致；
- undo 恢复所有字段（旧实现 undo 漏恢复 strip_eq/FX/Panner/routing）；
- 预设不再"存了效果但应用没恢复"；
- 已知字段往返无损、未知字段不引发异常（ST-03 前向兼容）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands.audio_commands import (
    ApplyChannelStripPresetCommand,
    ApplyMixSnapshotCommand,
    _capture_strip_state,
)
from src.features.edit.core.mixer_preset import ChannelStripPreset, MixSnapshot
from src.features.edit.core.timeline_v2 import (
    CompressorParams,
    DelayParams,
    EditTrack,
    EffectChainParams,
    Send,
    TimelineSequence,
    TrackEffectInstance,
)


def _rich_chain() -> EffectChainParams:
    return EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="delay", params=DelayParams(left_delay=200.0).to_dict(),
            enabled=True, order=0, instance_id="fx-delay-1",
        ),
        TrackEffectInstance(
            effect_type="compressor",
            params=CompressorParams(threshold_db=-18.0).to_dict(),
            enabled=True, order=1, instance_id="fx-comp-1",
        ),
    ))


def _rich_track(track_id="a1") -> EditTrack:
    return EditTrack(
        track_id=track_id, name="A1", track_type="audio",
        volume=0.73, pan=-0.3, is_muted=True,
        effect_chain=_rich_chain(),
        panner={"enabled": True, "spread": 80.0},
        sends=(Send(target_bus_id="bus1", level=0.5, enabled=True),),
    )


def _empty_track(track_id="a1") -> EditTrack:
    return EditTrack(track_id=track_id, name="A1", track_type="audio")


def _timeline(track: EditTrack) -> TimelineSequence:
    return TimelineSequence(audio_tracks=(track,))


class TestMx11ChannelPresetFullFields:
    def test_preset_captures_and_applies_all_fields(self):
        """预设捕获 FX/Panner/routing；应用后逐字段一致；undo 全字段恢复。"""
        preset = ChannelStripPreset.from_track(_rich_track(), name="Rich")
        state = preset.to_dict()
        assert state["effect_chain"] is not None
        assert state["panner"] == {"enabled": True, "spread": 80.0}
        assert len(state["sends"]) == 1

        p2 = ChannelStripPreset.from_dict(state)
        cmd = ApplyChannelStripPresetCommand.create_from_preset("a1", p2)
        t = _timeline(_empty_track())
        t2, applied = cmd.apply(t)
        nt = t2.get_track_by_id("a1")
        assert nt.effect_chain is not None
        assert len(nt.effect_chain.effects) == 2
        assert nt.effect_chain.effects[0].instance_id == "fx-delay-1"
        assert nt.panner == {"enabled": True, "spread": 80.0}
        assert len(nt.sends) == 1
        assert nt.is_muted is True
        assert abs(nt.volume - 0.73) < 1e-6

        # undo 恢复全部字段（旧实现漏 EQ/FX/Panner/routing）
        t3 = applied.undo(t2)
        nt3 = t3.get_track_by_id("a1")
        assert nt3.effect_chain is None
        assert not (nt3.panner or {})
        assert nt3.is_muted is False
        assert not nt3.sends

    def test_command_roundtrip_serialization(self):
        """命令 to_dict/from_dict 往返（undo 会话恢复路径）。"""
        preset = ChannelStripPreset.from_track(_rich_track(), name="Rich")
        cmd = ApplyChannelStripPresetCommand.create_from_preset("a1", preset)
        t = _timeline(_empty_track())
        _, applied = cmd.apply(t)
        d = applied.to_dict()
        cmd2 = ApplyChannelStripPresetCommand.from_dict(d)
        t4 = cmd2.undo(t)
        nt = t4.get_track_by_id("a1")
        assert nt.effect_chain is None
        assert not (nt.panner or {})


class TestMx11MixSnapshotFullFields:
    def test_snapshot_apply_undo_roundtrip(self):
        """快照 A→B→A 逐字段一致；undo 恢复 EQ/FX/Panner/routing。"""
        t_rich = _timeline(_rich_track())
        snap = MixSnapshot.from_timeline(t_rich, name="A")

        t_empty = _timeline(_empty_track())
        assert t_empty.get_track_by_id("a1").effect_chain is None

        cmd = ApplyMixSnapshotCommand.create_from_snapshot(snap)
        t3, applied = cmd.apply(t_empty)
        rt = t3.get_track_by_id("a1")
        assert rt.effect_chain is not None
        assert len(rt.effect_chain.effects) == 2
        assert rt.panner == {"enabled": True, "spread": 80.0}
        assert abs(rt.volume - 0.73) < 0.01
        assert rt.is_muted is True
        assert len(rt.sends) == 1

        t4 = applied.undo(t3)
        rt4 = t4.get_track_by_id("a1")
        assert rt4.effect_chain is None
        assert not (rt4.panner or {})
        assert abs(rt4.volume - 1.0) < 0.01


class TestSt03PresetForwardCompat:
    def test_known_fields_roundtrip_no_loss(self):
        d = ChannelStripPreset.from_track(_rich_track(), name="X").to_dict()
        p = ChannelStripPreset.from_dict(d)
        assert p.panner == {"enabled": True, "spread": 80.0}
        assert p.effect_chain is not None

    def test_unknown_extra_fields_do_not_crash(self):
        """ST-03: 预设文件含未知顶层字段不引发异常（前向兼容底线）。"""
        d = ChannelStripPreset(name="X").to_dict()
        d["future_field"] = {"anything": [1, 2, 3]}
        d["strip_eq"] = {"unknown_band_key": 42}
        p = ChannelStripPreset.from_dict(d)
        assert p.name == "X"

    def test_legacy_preset_without_panner(self):
        """旧预设（无 panner 键）加载 → panner=None（应用时保留目标轨现状）。"""
        d = {"name": "legacy", "volume": 0.5}
        p = ChannelStripPreset.from_dict(d)
        assert p.panner is None
