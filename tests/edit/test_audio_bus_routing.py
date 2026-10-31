"""
Audio Bus Routing 测试（W24-W27）。

覆盖：
1. Send / EditTrack 路由字段（output_bus_id + sends）
2. Bus 命令（AddBus/RemoveBus/SetOutputBus/AddSend/RemoveSend）
3. 引擎两层拓扑（Track → Bus → Master 信号流）
4. RemoveBus 自动重路由
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence, Send
from src.features.edit.core.commands import (
    AddBusCommand, RemoveBusCommand, SetTrackOutputBusCommand,
    AddSendCommand, RemoveSendCommand,
)


# =============================================================================
# 1. Send / 路由字段
# =============================================================================

class TestSendDataModel:
    """Send 数据类与 EditTrack 路由字段。"""

    def test_send_validates_target(self):
        with pytest.raises(ValueError):
            Send(target_bus_id="")

    def test_send_validates_level_range(self):
        with pytest.raises(ValueError):
            Send(target_bus_id="b1", level=1.5)
        with pytest.raises(ValueError):
            Send(target_bus_id="b1", level=-0.1)

    def test_with_output_bus(self):
        t = EditTrack(track_type="audio")
        t2 = t.with_output_bus("bus1")
        assert t2.output_bus_id == "bus1"
        assert t.output_bus_id is None  # immutable

    def test_with_send_added_removed(self):
        t = EditTrack(track_type="audio")
        s = Send(target_bus_id="bus1", level=0.5)
        t2 = t.with_send_added(s)
        assert len(t2.sends) == 1
        t3 = t2.with_send_removed(s.send_id)
        assert len(t3.sends) == 0

    def test_with_send_updated(self):
        t = EditTrack(track_type="audio")
        s = Send(target_bus_id="bus1", level=0.5)
        t = t.with_send_added(s)
        s2 = Send(send_id=s.send_id, target_bus_id="bus1", level=0.8)
        t2 = t.with_send_updated(s2)
        assert t2.sends[0].level == 0.8

    def test_serialization_roundtrip(self):
        t = EditTrack(track_type="audio", output_bus_id="bus1")
        t = t.with_send_added(Send(target_bus_id="bus2", level=0.3))
        seq = TimelineSequence().with_track_added(t)
        seq2 = TimelineSequence.from_dict(seq.to_dict())
        t2 = seq2.audio_tracks[0]
        assert t2.output_bus_id == "bus1"
        assert len(t2.sends) == 1
        assert t2.sends[0].level == 0.3


# =============================================================================
# 2. Bus 命令
# =============================================================================

class TestBusCommands:
    """AddBus/RemoveBus/SetOutputBus/AddSend/RemoveSend apply/undo。"""

    def test_add_bus(self):
        seq = TimelineSequence()
        cmd = AddBusCommand.create("Drum Bus")
        seq2, upd = cmd.apply(seq)
        assert any(t.is_bus for t in seq2.audio_tracks)
        assert upd._new_bus_id is not None
        # undo
        seq3 = upd.undo(seq2)
        assert not any(t.is_bus for t in seq3.audio_tracks)

    def test_set_output_bus_apply_undo(self):
        t = EditTrack(track_type="audio", name="Kick")
        seq = TimelineSequence().with_track_added(t)
        cmd = SetTrackOutputBusCommand.create(t.track_id, "bus1")
        seq2, upd = cmd.apply(seq)
        assert seq2.get_track_by_id(t.track_id).output_bus_id == "bus1"
        seq3 = upd.undo(seq2)
        assert seq3.get_track_by_id(t.track_id).output_bus_id is None

    def test_add_send_apply_undo(self):
        t = EditTrack(track_type="audio", name="Kick")
        seq = TimelineSequence().with_track_added(t)
        cmd = AddSendCommand.create(t.track_id, "bus1", level=0.5)
        seq2, upd = cmd.apply(seq)
        got = seq2.get_track_by_id(t.track_id)
        assert len(got.sends) == 1
        assert got.sends[0].level == 0.5
        # undo
        seq3 = upd.undo(seq2)
        assert len(seq3.get_track_by_id(t.track_id).sends) == 0

    def test_remove_send_apply_undo(self):
        t = EditTrack(track_type="audio", name="Kick")
        seq = TimelineSequence().with_track_added(t)
        cmd1 = AddSendCommand.create(t.track_id, "bus1", level=0.5)
        seq2, upd1 = cmd1.apply(seq)
        send_id = upd1._new_send_id
        cmd2 = RemoveSendCommand.create(t.track_id, send_id)
        seq3, _ = cmd2.apply(seq2)
        assert len(seq3.get_track_by_id(t.track_id).sends) == 0

    def test_remove_bus_reroutes_to_master(self):
        """删除 Bus 时，路由到该 Bus 的轨道自动重路由到 Master。"""
        seq = TimelineSequence()
        # 加 Bus
        cmd_bus = AddBusCommand.create("Bus1")
        seq, upd_bus = cmd_bus.apply(seq)
        bus_id = upd_bus._new_bus_id
        # 加普通轨并路由到 Bus
        t = EditTrack(track_type="audio", name="Kick", output_bus_id=bus_id)
        seq = seq.with_track_added(t)
        # 删除 Bus
        cmd_rem = RemoveBusCommand.create(bus_id)
        seq2, upd_rem = cmd_rem.apply(seq)
        # Bus 被删
        assert seq2.get_track_by_id(bus_id) is None
        # track 重路由到 Master
        assert seq2.get_track_by_id(t.track_id).output_bus_id is None
        # undo 恢复
        seq3 = upd_rem.undo(seq2)
        assert seq3.get_track_by_id(bus_id) is not None
        assert seq3.get_track_by_id(t.track_id).output_bus_id == bus_id


# =============================================================================
# 3. 引擎两层拓扑
# =============================================================================

class TestEngineTwoLayerTopology:
    """mix_samples 两层信号流：Track → Bus → Master。"""

    def _sine(self, amp, freq, sr, n):
        t = np.arange(n) / sr
        s = (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)
        return np.stack([s, s])

    def test_track_to_bus_to_master(self):
        """两条轨 → Bus(volume=0.5) → Master。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        sig = self._sine(0.5, 1000, sr, n)

        dec1 = Mock(spec=AudioDecoder)
        dec1.decode_samples_for_frame = Mock(return_value=sig.copy())
        dec2 = Mock(spec=AudioDecoder)
        dec2.decode_samples_for_frame = Mock(return_value=sig.copy())

        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec1, AudioTrackState(track_id="t1", volume=1.0))
        m.add_track("t2", dec2, AudioTrackState(track_id="t2", volume=1.0))
        m._tracks["bus1"] = AudioTrackState(track_id="bus1", volume=0.5)
        m.set_track_routing("t1", "bus1", [], is_bus=False)
        m.set_track_routing("t2", "bus1", [], is_bus=False)
        m.set_track_routing("bus1", None, [], is_bus=True)

        out, peaks = m.mix_samples(0, 24.0, n)
        master_peak = float(np.max(np.abs(out[0])))
        # AM-08: 两轨 0.5 正弦叠加 → bus → master，经 equal-power pan 衰减。
        # 核心：Bus 不丢声（AM-02），master 有显著信号。
        assert master_peak > 0.2, f"Bus 路由后 master 应有信号, got {master_peak}"
        assert master_peak < 0.6, f"master 不应过响, got {master_peak}"
        assert "bus1" in peaks  # bus 电平上报

    def test_send_to_bus_parallel(self):
        """Track 直送 Master + send 0.5 到 Bus1 → Master = direct + bus(send)。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        sig = self._sine(0.5, 1000, sr, n)

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig.copy())
        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        m._tracks["bus1"] = AudioTrackState(track_id="bus1", volume=1.0)
        m.set_track_routing("t1", None, [("bus1", 0.5)], is_bus=False)
        m.set_track_routing("bus1", None, [], is_bus=True)

        out, _ = m.mix_samples(0, 24.0, n)
        peak = float(np.max(np.abs(out[0])))
        # AM-08: direct + send 经 equal-power pan。核心：send 贡献存在（>仅 direct）。
        # 仅 direct（无 send）的基线对比：
        m2 = AudioMixer()
        m2.set_sample_rate(float(sr))
        dec2 = Mock(spec=AudioDecoder)
        dec2.decode_samples_for_frame = Mock(return_value=sig.copy())
        m2.add_track("t1", dec2, AudioTrackState(track_id="t1", volume=1.0))
        m2.set_track_routing("t1", None, [], is_bus=False)
        out2, _ = m2.mix_samples(0, 24.0, n)
        direct_peak = float(np.max(np.abs(out2[0])))
        assert peak > direct_peak + 0.05, f"send 应增加 master 信号, direct={direct_peak} with_send={peak}"

    def test_bus_mute_excludes_from_master(self):
        """Bus 静音时，路由到它的信号不输出到 Master。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        sig = self._sine(0.5, 1000, sr, n)

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig.copy())
        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        m._tracks["bus1"] = AudioTrackState(track_id="bus1", volume=1.0, muted=True)
        m.set_track_routing("t1", "bus1", [], is_bus=False)
        m.set_track_routing("bus1", None, [], is_bus=True)

        out, _ = m.mix_samples(0, 24.0, n)
        assert float(np.max(np.abs(out[0]))) < 0.01  # Master 静音


# =============================================================================
# 4. AM-14: Bus DAG 拓扑排序 + 环检测 + 多层 Bus→Bus
# =============================================================================

class TestBusTopologicalSort:
    """topological_sort_buses 纯函数单元测试（AM-14）。"""

    def test_single_bus_to_master(self):
        """单 Bus 输出到 Master：无 Bus→Bus 边，排序含该 Bus。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {"bus1": {"output_bus_id": None, "is_bus": True}}
        sorted_ids, cycle = topological_sort_buses(["bus1"], routing)
        assert sorted_ids == ["bus1"]
        assert cycle == []

    def test_two_layer_bus_chain(self):
        """bus1 → bus2 → Master：拓扑序 bus1 在 bus2 前。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {
            "bus1": {"output_bus_id": "bus2", "is_bus": True},
            "bus2": {"output_bus_id": None, "is_bus": True},
        }
        sorted_ids, cycle = topological_sort_buses(["bus1", "bus2"], routing)
        assert sorted_ids == ["bus1", "bus2"]
        assert cycle == []

    def test_four_layer_bus_chain(self):
        """4 层链：b1→b2→b3→b4→Master（计划表 1.3 验收：至少 4 层）。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {
            "b1": {"output_bus_id": "b2"},
            "b2": {"output_bus_id": "b3"},
            "b3": {"output_bus_id": "b4"},
            "b4": {"output_bus_id": None},
        }
        sorted_ids, cycle = topological_sort_buses(["b1", "b2", "b3", "b4"], routing)
        assert sorted_ids == ["b1", "b2", "b3", "b4"]
        assert cycle == []

    def test_cycle_detected(self):
        """环：bus1→bus2→bus1，两者都应进 cycle。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {
            "bus1": {"output_bus_id": "bus2"},
            "bus2": {"output_bus_id": "bus1"},
        }
        sorted_ids, cycle = topological_sort_buses(["bus1", "bus2"], routing)
        assert sorted_ids == []
        assert set(cycle) == {"bus1", "bus2"}

    def test_bus_send_cycle_detected(self):
        """Bus send 也属于同一 block 的信号边，必须参与环检测。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {
            "bus1": {"output_bus_id": None, "sends": [("bus2", 0.5)], "is_bus": True},
            "bus2": {"output_bus_id": None, "sends": [("bus1", 0.5)], "is_bus": True},
        }
        _, cycle = topological_sort_buses(["bus1", "bus2"], routing)
        assert set(cycle) == {"bus1", "bus2"}

    def test_self_loop_detected(self):
        """自环：bus→自身。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {"bus1": {"output_bus_id": "bus1"}}
        sorted_ids, cycle = topological_sort_buses(["bus1"], routing)
        assert sorted_ids == []
        assert cycle == ["bus1"]

    def test_unknown_target_treated_as_master(self):
        """output_bus_id 指向不存在的 Bus：视为 Master，不构成边。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {"bus1": {"output_bus_id": "ghost"}}
        sorted_ids, cycle = topological_sort_buses(["bus1"], routing)
        assert sorted_ids == ["bus1"]
        assert cycle == []

    def test_partial_cycle(self):
        """部分环：b1→b2→b3→b2（b1 不在环，b2/b3 在环）。"""
        from src.features.edit.core.audio_playback import topological_sort_buses
        routing = {
            "b1": {"output_bus_id": "b2"},
            "b2": {"output_bus_id": "b3"},
            "b3": {"output_bus_id": "b2"},  # 回到 b2 形成环
        }
        sorted_ids, cycle = topological_sort_buses(["b1", "b2", "b3"], routing)
        assert "b1" in sorted_ids
        assert set(cycle) == {"b2", "b3"}


class TestMultiLayerBusRouting:
    """多层 Bus→Bus 路由的引擎集成测试（AM-14）。"""

    def _dc_decoder(self, amp):
        """DC 假 decoder。"""
        class _DC:
            def decode_samples_for_frame(self, f, fps, n):
                s = np.full(n, amp, dtype=np.float32)
                return np.stack([s, s])
            def close(self): pass
        return _DC()

    def test_three_layer_bus_to_master(self):
        """t1→bus1→bus2→Master：信号穿透三层 Bus。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
        from src.features.edit.core.audio_dsp import balance_gains
        pan_c = balance_gains(0.0)[0]
        m = AudioMixer()
        m.add_track("t1", self._dc_decoder(0.3), AudioTrackState(track_id="t1"))
        m.set_track_routing("t1", "bus1", [], is_bus=False)
        m.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=1.0))
        m.set_track_routing("bus1", "bus2", [], is_bus=True)
        m.register_bus_track("bus2", AudioTrackState(track_id="bus2", volume=1.0))
        m.set_track_routing("bus2", None, [], is_bus=True)
        out, _ = m.mix_samples(0, 24.0, 480, 2)
        # AM-08: 三级 pan=0 各衰减 PAN_CENTER，合计 PAN_CENTER³
        expected = 0.3 * pan_c ** 3
        assert np.allclose(out[0], expected, atol=1e-5), \
            f"三层 Bus 应透传 {expected}，实际 {out[0][0]}"

    def test_bus_chain_with_volume(self):
        """多层 Bus 每层 volume 累乘。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
        from src.features.edit.core.audio_dsp import balance_gains
        pan_c = balance_gains(0.0)[0]
        m = AudioMixer()
        m.add_track("t1", self._dc_decoder(0.4), AudioTrackState(track_id="t1"))
        m.set_track_routing("t1", "bus1", [], is_bus=False)
        m.register_bus_track("bus1", AudioTrackState(track_id="bus1", volume=0.5))
        m.set_track_routing("bus1", "bus2", [], is_bus=True)
        m.register_bus_track("bus2", AudioTrackState(track_id="bus2", volume=0.5))
        m.set_track_routing("bus2", None, [], is_bus=True)
        out, _ = m.mix_samples(0, 24.0, 480, 2)
        # 0.4 × PAN_CENTER³ × 0.5(bus1) × 0.5(bus2)
        expected = 0.4 * pan_c ** 3 * 0.5 * 0.5
        assert np.allclose(out[0], expected, atol=1e-5)

    def test_cycle_does_not_crash(self):
        """环路由不崩溃，环中 Bus 被跳过（输出不含环中信号）。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
        m = AudioMixer()
        m.add_track("t1", self._dc_decoder(0.5), AudioTrackState(track_id="t1"))
        m.set_track_routing("t1", "bus3", [], is_bus=False)
        m.register_bus_track("bus3", AudioTrackState(track_id="bus3"))
        m.set_track_routing("bus3", "bus4", [], is_bus=True)
        m.register_bus_track("bus4", AudioTrackState(track_id="bus4"))
        m.set_track_routing("bus4", "bus3", [], is_bus=True)  # 环
        # 不应抛异常
        out, _ = m.mix_samples(0, 24.0, 480, 2)
        # 环中 Bus 被跳过，master 应为静音
        assert np.allclose(out, 0.0, atol=1e-6)
