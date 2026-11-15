"""R05：状态 DTO、空值清除、Master 快照与全域自动化回归（计划 §5 R05）。

复审确认的问题（probe_state / probe_bus_automation 转为 pytest）：
- 干净轨 ChannelStripPreset 应用到富状态轨后旧 FX/Panner/Bus/Sends 保留
  （None 同时表达"没存"和"清空"）——v2 字段存在性区分两者。
- MixSnapshot 只存 master volume/mute——Master EQ/FX 不进快照。
- Bus fx_sampler 建而不消费；Master 无 FX 自动化通路
  （fx/D/level_db=-12 探针实时/离线均输出 0.2，正确约 0.050238）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState


class _ConstantDecoder:
    """恒定电平测试解码器。"""

    _channels = 2

    def decode_samples_for_frame(self, frame, fps, num_samples):
        return np.full((2, num_samples), 0.2, np.float32)

    def close(self):
        pass
from src.features.edit.core.commands.audio_commands import (
    ApplyChannelStripPresetCommand,
    ApplyMixSnapshotCommand,
)
from src.features.edit.core.mixer_preset import ChannelStripPreset, MixSnapshot
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    EffectChainParams,
    Send,
    StripEQParams,
    TimelineSequence,
    TrackEffectInstance,
    build_track_fx_sampler,
)
from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational

SR = 48000


def delay_chain(iid: str = "D", wet: float = 100.0) -> EffectChainParams:
    return EffectChainParams(effects=(TrackEffectInstance(
        effect_type="delay", instance_id=iid, params={
            "enabled": True, "left_delay": 200.0, "right_delay": 200.0,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "dry_wet": wet, "level_db": 0.0, "low_cut": 20.0,
            "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
        }),))


class TestBusAutomation:
    def test_bus_fx_level_automation_realtime_and_offline(self):
        """fx/D/level_db=-12 常数曲线：Bus Delay level 被自动化压低。
        输入 0.2 → 预期 0.2 × 10^(-12/20) ≈ 0.050238（实时与离线一致）。"""
        curve = KeyframeTrack(param_name="fx/D/level_db",
                              min_val=-24.0, max_val=6.0)
        curve.add_keyframe(Keyframe(frame=0, value=-12.0))
        bus = EditTrack("B", "Bus", "audio", 1, is_bus=True,
                        effect_chain=delay_chain(wet=0.0),
                        automation_mode="read",
                        automation={"fx/D/level_db": curve})
        # 实时
        mixer = AudioMixer()
        mixer.add_track("T", _ConstantDecoder(), AudioTrackState(track_id="T"))
        mixer.register_bus_track("B")
        mixer.set_track_routing("T", "B", [])
        mixer.set_track_routing("B", None, [], is_bus=True)
        mixer.set_track_effect_chain("B", bus.effect_chain,
                                     animated_sampler=build_track_fx_sampler(bus))
        realtime = mixer.mix_samples(0, 50.0, 960)[0]
        # 离线
        renderer = AudioGraphRenderer()
        renderer.configure_from_sequence(TimelineSequence(audio_tracks=(bus,)))
        offline = np.zeros((2, 960), np.float32)
        renderer.render_bus_stage({"B": np.full((2, 960), 0.2, np.float32)},
                                  offline, has_solo=False)
        expected = 0.2 * 10.0 ** (-12.0 / 20.0)
        assert abs(float(np.max(np.abs(realtime))) - expected) < 1e-4
        assert abs(float(np.max(np.abs(offline))) - expected) < 1e-4

    def test_master_fx_level_automation_offline(self):
        """Master 链 fx 自动化：master_automation 的 fx/{iid}/{param} 键
        在 apply_master 块边界消费（R05：Master 同等 FX 通路）。"""
        curve = KeyframeTrack(param_name="fx/D/level_db",
                              min_val=-24.0, max_val=6.0)
        curve.add_keyframe(Keyframe(frame=0, value=-12.0))
        seq = TimelineSequence(
            audio_tracks=(),
            master_effect_chain=delay_chain(wet=0.0),
            master_automation={"fx/D/level_db": curve},
            master_automation_mode="read",
        )
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        assert renderer._master_fx_sampler is not None, "master sampler not built"
        out = renderer.apply_master(np.full((2, 960), 0.2, np.float32))
        expected = 0.2 * 10.0 ** (-12.0 / 20.0)
        assert abs(float(np.max(np.abs(out))) - expected) < 1e-4

    def test_master_fx_automation_off_keeps_static(self):
        """automation_mode=off：不消费曲线，静态参数生效。"""
        curve = KeyframeTrack(param_name="fx/D/level_db",
                              min_val=-24.0, max_val=6.0)
        curve.add_keyframe(Keyframe(frame=0, value=-12.0))
        seq = TimelineSequence(
            audio_tracks=(),
            master_effect_chain=delay_chain(wet=0.0),
            master_automation={"fx/D/level_db": curve},
            master_automation_mode="off",
        )
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        assert renderer._master_fx_sampler is None
        out = renderer.apply_master(np.full((2, 960), 0.2, np.float32))
        assert abs(float(np.max(np.abs(out))) - 0.2) < 1e-5


class TestExplicitClearSemantics:
    def test_from_track_preset_clears_rich_track_state(self):
        """全量捕获的干净预设应用到富状态轨：FX/Panner/Bus/Sends 清空。"""
        clean = ChannelStripPreset.from_dict(ChannelStripPreset.from_track(
            EditTrack("T", "Clean", "audio", 0)).to_dict())
        rich = EditTrack("T", "Rich", "audio", 0, effect_chain=delay_chain(),
                         panner={"enabled": True, "spread": 80.0},
                         output_bus_id="B",
                         sends=(Send(target_bus_id="B", level=0.5),))
        changed, _ = ApplyChannelStripPresetCommand.create_from_preset(
            "T", clean).apply(TimelineSequence(audio_tracks=(rich,)))
        actual = changed.audio_tracks[0]
        assert actual.effect_chain is None
        assert not actual.panner
        assert actual.output_bus_id is None
        assert len(actual.sends) == 0

    def test_legacy_five_field_preset_keeps_routing(self):
        """旧版预设文件（五字段 + 无 explicit_fields）：应用时保留目标轨
        的 FX/Panner/路由/Sends（兼容语义不变）。"""
        legacy_raw = {
            "preset_id": "p0", "name": "legacy",
            "algorithm_version": 2,
            "volume": 0.8, "pan": 0.0, "is_muted": False, "is_solo": False,
            "strip_eq": None, "effect_chain": None,
            "panner": None, "output_bus_id": None, "sends": [],
            "is_bus": False, "created_at": "",
        }
        assert "explicit_fields" not in legacy_raw
        legacy = ChannelStripPreset.from_dict(legacy_raw)
        assert legacy.explicit_fields == frozenset()
        rich = EditTrack("T", "Rich", "audio", 0, effect_chain=delay_chain(),
                         panner={"enabled": True, "spread": 80.0},
                         output_bus_id="B",
                         sends=(Send(target_bus_id="B", level=0.5),))
        changed, _ = ApplyChannelStripPresetCommand.create_from_preset(
            "T", legacy).apply(TimelineSequence(audio_tracks=(rich,)))
        actual = changed.audio_tracks[0]
        assert actual.effect_chain is not None
        assert actual.panner
        assert actual.output_bus_id == "B"
        assert len(actual.sends) == 1
        assert actual.volume == 0.8

    def test_explicit_fields_roundtrip(self):
        track = EditTrack("T", "T", "audio", 0, effect_chain=delay_chain())
        preset = ChannelStripPreset.from_track(track, "cap")
        d = preset.to_dict()
        assert set(d["explicit_fields"]) == {
            "strip_eq", "effect_chain", "panner", "output_bus_id", "sends"}
        rt = ChannelStripPreset.from_dict(d)
        assert rt.explicit_fields == preset.explicit_fields


class TestMixSnapshotMasterStrip:
    def test_snapshot_restores_master_eq_and_fx(self):
        """A→改 Master EQ/FX→应用 A：Master 条带完整恢复（含 undo）。"""
        base = TimelineSequence(
            master_strip_eq=StripEQParams(master_gain=3.0),
            master_effect_chain=delay_chain("MD"))
        snap = MixSnapshot.from_timeline(base, "A")
        d = snap.to_dict()
        rt = MixSnapshot.from_dict(d)
        assert rt.master_strip_eq != "_unset"
        assert rt.master_effect_chain != "_unset"

        changed = TimelineSequence(
            master_strip_eq=StripEQParams(master_gain=9.0))
        cmd = ApplyMixSnapshotCommand.create_from_snapshot(rt)
        restored, updated = cmd.apply(changed)
        assert restored.master_strip_eq is not None
        assert abs(restored.master_strip_eq.master_gain - 3.0) < 1e-9
        assert restored.master_effect_chain is not None
        # undo 恢复 apply 前状态（gain 9，无链）——用 updated 命令
        # （apply 把 _old_snapshot 填进返回的命令，CommandStack 同语义）
        undone = updated.undo(restored)
        assert abs(undone.master_strip_eq.master_gain - 9.0) < 1e-9
        assert undone.master_effect_chain is None

    def test_legacy_snapshot_without_master_strip_keeps_current(self):
        """旧格式快照（无 master_strip_eq/master_effect_chain 键）：应用时
        保留目标 Master 现状（缺字段=保留，不清除）。"""
        legacy_snap = MixSnapshot.from_dict({
            "snapshot_id": "s", "name": "old",
            "track_states": {},
            "master_volume": 1.0, "master_muted": False,
        })
        assert legacy_snap.master_strip_eq == "_unset"
        target = TimelineSequence(
            master_strip_eq=StripEQParams(master_gain=9.0))
        cmd = ApplyMixSnapshotCommand.create_from_snapshot(legacy_snap)
        restored, _ = cmd.apply(target)
        assert restored.master_strip_eq is not None
        assert abs(restored.master_strip_eq.master_gain - 9.0) < 1e-9

    def test_snapshot_capture_empty_master_clears_on_apply(self):
        """捕获时 Master 无 EQ/FX：应用快照到有 EQ/FX 的工程 → 清除。"""
        empty = TimelineSequence()
        snap = MixSnapshot.from_timeline(empty, "A")
        target = TimelineSequence(
            master_strip_eq=StripEQParams(master_gain=9.0),
            master_effect_chain=delay_chain("MD"))
        cmd = ApplyMixSnapshotCommand.create_from_snapshot(snap)
        restored, _ = cmd.apply(target)
        assert restored.master_strip_eq is None
        assert restored.master_effect_chain is None


class TestMasterChainSamplerRegistration:
    def test_set_master_effect_chain_accepts_sampler(self):
        mixer = AudioMixer()
        curve = KeyframeTrack(param_name="fx/D/level_db",
                              min_val=-24.0, max_val=6.0)
        curve.add_keyframe(Keyframe(frame=0, value=-12.0))
        from src.features.edit.core.audio_graph_renderer import _MasterFxShim
        shim = _MasterFxShim("read", delay_chain(wet=0.0),
                             {"fx/D/level_db": curve})
        sampler = build_track_fx_sampler(shim)
        mixer.set_master_effect_chain(delay_chain(wet=0.0),
                                      animated_sampler=sampler)
        assert "-1" in mixer._track_effect_samplers
        mixer.set_master_effect_chain(None)
        assert "-1" not in mixer._track_effect_samplers


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
