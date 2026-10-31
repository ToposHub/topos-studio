"""
Mixer Preset 完整性测试（AM-13 阶段 4）。

验证 ChannelStripPreset 覆盖完整 strip 状态（含 effect_chain + routing），
序列化往返保留所有字段。此前预设只覆盖 volume/pan/mute/solo/strip_eq，
丢失 FX 链与路由。

验收标准（计划表 AM-13）：
- 预设覆盖完整 FX 链和 routing
- 保存/加载/跨项目套用均有快照测试
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.mixer_preset import ChannelStripPreset
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    EffectChainParams,
    Send,
    StripEQParams,
    TrackEffectInstance,
)


def _track_with_full_state() -> EditTrack:
    """构造带 EQ + FX + routing 的完整 strip 轨道。"""
    eq = StripEQParams.default("3-band")
    chain = EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="strip_eq", params=eq.to_dict(),
            enabled=True, order=0, panel="eq",
        ),
    ))
    return EditTrack(
        track_id="t1", name="A1", track_type="audio", index=0,
        volume=0.8, pan=0.3, is_muted=False, is_solo=True,
        strip_eq=eq, effect_chain=chain,
        output_bus_id="bus1",
        sends=(Send(target_bus_id="bus2", level=0.5),),
    )


class TestPresetCompleteness:
    """AM-13: 预设覆盖完整 strip 状态。"""

    def test_from_track_captures_effect_chain(self):
        """from_track 捕获 effect_chain（此前缺失）。"""
        t = _track_with_full_state()
        preset = ChannelStripPreset.from_track(t)
        assert preset.effect_chain is not None
        assert len(preset.effect_chain.effects) == 1

    def test_from_track_captures_routing(self):
        """from_track 捕获 routing（output_bus_id/sends/is_bus，此前缺失）。"""
        t = _track_with_full_state()
        preset = ChannelStripPreset.from_track(t)
        assert preset.output_bus_id == "bus1"
        assert len(preset.sends) == 1
        assert preset.sends[0].target_bus_id == "bus2"

    def test_from_track_captures_basic_fields(self):
        """基础字段仍正确（volume/pan/mute/solo/EQ）。"""
        t = _track_with_full_state()
        preset = ChannelStripPreset.from_track(t)
        assert preset.volume == 0.8
        assert preset.pan == 0.3
        assert preset.is_solo is True
        assert preset.strip_eq is not None


class TestPresetSerialization:
    """AM-13: 预设序列化往返保留完整状态。"""

    def test_roundtrip_preserves_effect_chain(self):
        """effect_chain 序列化往返保留。"""
        t = _track_with_full_state()
        preset = ChannelStripPreset.from_track(t)
        data = preset.to_dict()
        restored = ChannelStripPreset.from_dict(data)
        assert restored.effect_chain is not None
        assert len(restored.effect_chain.effects) == 1
        # FX 链内容一致
        assert (restored.effect_chain.to_dict()
                == preset.effect_chain.to_dict())

    def test_roundtrip_preserves_routing(self):
        """routing 序列化往返保留。"""
        t = _track_with_full_state()
        preset = ChannelStripPreset.from_track(t)
        restored = ChannelStripPreset.from_dict(preset.to_dict())
        assert restored.output_bus_id == "bus1"
        assert len(restored.sends) == 1
        assert restored.sends[0].target_bus_id == "bus2"
        assert abs(restored.sends[0].level - 0.5) < 1e-6

    def test_roundtrip_full_equality(self):
        """完整预设往返后 to_dict 相等。"""
        t = _track_with_full_state()
        preset = ChannelStripPreset.from_track(t)
        restored = ChannelStripPreset.from_dict(preset.to_dict())
        assert restored.to_dict() == preset.to_dict()

    def test_backward_compat_old_preset_without_fx_routing(self):
        """旧预设（无 effect_chain/routing）仍能加载（向后兼容）。"""
        old_data = {
            "preset_id": "old1",
            "name": "Old Preset",
            "volume": 0.9,
            "pan": 0.0,
            "is_muted": False,
            "is_solo": False,
            "strip_eq": None,
            "created_at": "2025-01-01",
            # 无 effect_chain / output_bus_id / sends / is_bus
        }
        preset = ChannelStripPreset.from_dict(old_data)
        assert preset.name == "Old Preset"
        assert preset.volume == 0.9
        assert preset.effect_chain is None
        assert preset.output_bus_id is None
        assert preset.sends == ()
        assert preset.is_bus is False


class TestPresetApplyToTrack:
    """AM-13: 预设可应用到轨道（完整状态恢复）。"""

    def test_preset_dict_can_reconstruct_track_state(self):
        """预设的 to_dict 含足够信息重建等价轨道状态。"""
        t = _track_with_full_state()
        preset = ChannelStripPreset.from_track(t)
        # 预设字段与轨道字段对应
        assert preset.volume == t.volume
        assert preset.pan == t.pan
        assert preset.is_muted == t.is_muted
        assert preset.is_solo == t.is_solo
        assert preset.output_bus_id == t.output_bus_id
        assert len(preset.sends) == len(t.sends)
        assert preset.is_bus == t.is_bus
