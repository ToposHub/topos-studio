"""
Routing UI 测试（AM-08 阶段 4.2）。

验证：
1. validate_routing_graph：环检测 + 未知目标检测（UI 实时告警的核心）。
2. UpdateSendCommand：send level/enabled/target 变更的命令 + undo/redo。
3. RoutingDialog：payload 收集、Bus 候选列表、Send slot 增删。
4. 信号转发：TrackMixerWidget.show_routing → panel.track_show_routing。

不依赖真实音频设备，UI 测试用 QT_QPA_PLATFORM=offscreen。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestValidateRoutingGraph:
    """validate_routing_graph：UI 提交前的实时校验。"""

    def test_valid_no_cycle(self):
        """无环无未知目标 → 有效。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = ["busA", "busB"]
        routing = {
            "busA": {"output_bus_id": "busB", "sends": [], "is_bus": True},
            "busB": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        is_valid, errors = validate_routing_graph(bus_ids, routing)
        assert is_valid
        assert errors == []

    def test_detects_cycle(self):
        """Bus→Bus 环 → 报错。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = ["busA", "busB"]
        routing = {
            "busA": {"output_bus_id": "busB", "sends": [], "is_bus": True},
            "busB": {"output_bus_id": "busA", "sends": [], "is_bus": True},  # 环回 busA
        }
        is_valid, errors = validate_routing_graph(bus_ids, routing)
        assert not is_valid
        assert any("环" in e or "cycle" in e for e in errors)

    def test_detects_self_loop(self):
        """Bus→自身 → 环。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = ["busA"]
        routing = {
            "busA": {"output_bus_id": "busA", "sends": [], "is_bus": True},
        }
        is_valid, errors = validate_routing_graph(bus_ids, routing)
        assert not is_valid

    def test_detects_unknown_output_target(self):
        """output_bus_id 指向不存在的 Bus → 报错。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = ["busA"]
        routing = {
            "busA": {"output_bus_id": "ghostBus", "sends": [], "is_bus": True},
        }
        is_valid, errors = validate_routing_graph(bus_ids, routing)
        assert not is_valid
        assert any("不存在" in e for e in errors)

    def test_detects_unknown_send_target(self):
        """send 目标不存在 → 报错。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = ["busA"]
        routing = {
            "t1": {"output_bus_id": None, "sends": [("ghostBus", 0.5)], "is_bus": False},
            "busA": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        is_valid, errors = validate_routing_graph(bus_ids, routing)
        assert not is_valid
        assert any("Send" in e or "不存在" in e for e in errors)

    def test_master_output_is_valid(self):
        """output_bus_id=None（直送 Master）→ 有效。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = ["busA"]
        routing = {
            "t1": {"output_bus_id": None, "sends": [], "is_bus": False},
            "busA": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        is_valid, errors = validate_routing_graph(bus_ids, routing)
        assert is_valid

    def test_multi_layer_chain_valid(self):
        """多层 Bus→Bus→Master 链 → 有效（拓扑序存在）。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = ["bus1", "bus2", "bus3", "bus4"]
        routing = {
            "bus1": {"output_bus_id": "bus2", "sends": [], "is_bus": True},
            "bus2": {"output_bus_id": "bus3", "sends": [], "is_bus": True},
            "bus3": {"output_bus_id": "bus4", "sends": [], "is_bus": True},
            "bus4": {"output_bus_id": None, "sends": [], "is_bus": True},
        }
        is_valid, errors = validate_routing_graph(bus_ids, routing)
        assert is_valid


class TestUpdateSendCommand:
    """UpdateSendCommand：send level/enabled/target 变更。"""

    def _make_sequence_with_send(self):
        """构造含一个 send 的 sequence。"""
        from src.features.edit.core.timeline_v2 import (
            EditTrack, TimelineSequence, Send,
        )
        send = Send(send_id="send1", target_bus_id="busA", level=0.5, enabled=True)
        track = EditTrack(
            track_id="t1", track_type="audio", index=0,
            sends=(send,),
        )
        bus = EditTrack(track_id="busA", track_type="audio", index=1, is_bus=True)
        return TimelineSequence(audio_tracks=(track, bus))

    def test_update_level(self):
        """更新 send level。"""
        from src.features.edit.core.commands import UpdateSendCommand
        seq = self._make_sequence_with_send()
        cmd = UpdateSendCommand.create("t1", "send1", level=0.8)
        new_seq, _ = cmd.apply(seq)
        track = new_seq.get_track_by_id("t1")
        assert abs(track.sends[0].level - 0.8) < 1e-6

    def test_update_enabled(self):
        """更新 send enabled。"""
        from src.features.edit.core.commands import UpdateSendCommand
        seq = self._make_sequence_with_send()
        cmd = UpdateSendCommand.create("t1", "send1", enabled=False)
        new_seq, _ = cmd.apply(seq)
        track = new_seq.get_track_by_id("t1")
        assert track.sends[0].enabled is False

    def test_undo_restores(self):
        """undo 还原 send 原值。"""
        from src.features.edit.core.commands import UpdateSendCommand
        seq = self._make_sequence_with_send()
        cmd = UpdateSendCommand.create("t1", "send1", level=0.9)
        new_seq, updated_cmd = cmd.apply(seq)
        restored = updated_cmd.undo(new_seq)
        track = restored.get_track_by_id("t1")
        assert abs(track.sends[0].level - 0.5) < 1e-6

    def test_serialization_roundtrip(self):
        """序列化往返。"""
        from src.features.edit.core.commands import UpdateSendCommand
        cmd = UpdateSendCommand.create("t1", "send1", level=0.7, enabled=False)
        d = cmd.to_dict()
        assert d["command_type"] == "UpdateSendCommand"
        restored = UpdateSendCommand.from_dict(d)
        assert restored.new_level == 0.7
        assert restored.new_enabled is False

    def test_send_not_found_raises(self):
        """send 不存在抛 ValueError。"""
        from src.features.edit.core.commands import UpdateSendCommand
        seq = self._make_sequence_with_send()
        cmd = UpdateSendCommand.create("t1", "nonexistent", level=0.8)
        with pytest.raises(ValueError):
            cmd.apply(seq)


class TestRoutingDialog:
    """RoutingDialog UI 组件。"""

    def test_dialog_construction(self, qapp):
        """对话框构造。"""
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
        from src.features.edit.core.timeline_v2 import Send

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id="busA",
            sends=(Send(target_bus_id="busB", level=0.5),),
            bus_tracks=[("busA", "Bus A"), ("busB", "Bus B")],
        )
        assert dialog._track_id == "t1"

    def test_collect_payload(self, qapp):
        """收集 payload（output + sends）。"""
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
        from src.features.edit.core.timeline_v2 import Send

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id="busA",
            sends=(Send(target_bus_id="busB", level=0.5),),
            bus_tracks=[("busA", "Bus A"), ("busB", "Bus B")],
        )
        payload = dialog._collect()
        assert payload["output_bus_id"] == "busA"
        assert len(payload["sends"]) == 1
        assert payload["sends"][0].target_bus_id == "busB"

    def test_add_send_slot(self, qapp):
        """添加新 send slot。"""
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id=None,
            sends=(),
            bus_tracks=[("busA", "Bus A")],
        )
        assert len(dialog._send_slots) == 0
        dialog._on_add_send()
        assert len(dialog._send_slots) == 1

    def test_send_slot_collect(self, qapp):
        """SendSlotWidget 收集 Send。"""
        from src.shared.ui.audio_mixer.routing_dialog import _SendSlotWidget
        from src.features.edit.core.timeline_v2 import Send

        send = Send(target_bus_id="busA", level=0.7, enabled=True)
        slot = _SendSlotWidget(send, [("busA", "Bus A"), ("busB", "Bus B")])
        collected = slot.collect()
        assert collected.target_bus_id == "busA"
        assert abs(collected.level - 0.7) < 0.01
        assert collected.enabled is True

    def test_send_slot_remove(self, qapp):
        """SendSlotWidget 标记删除。"""
        from src.shared.ui.audio_mixer.routing_dialog import _SendSlotWidget
        from src.features.edit.core.timeline_v2 import Send

        send = Send(target_bus_id="busA", level=0.7, enabled=True)
        slot = _SendSlotWidget(send, [("busA", "Bus A")])
        slot._on_remove()
        assert slot.is_removed is True
        assert slot.collect() is None

    def test_validate_no_warning_for_valid(self, qapp):
        """有效路由不显示告警。"""
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id="busA",
            sends=(),
            bus_tracks=[("busA", "Bus A")],
        )
        payload = {"output_bus_id": "busA", "sends": []}
        is_valid = dialog._validate_and_warn(payload)
        assert is_valid
        assert dialog._warning_label.isHidden()


class TestTrackMixerWidgetRoutingSignal:
    """TrackMixerWidget.show_routing 信号。"""

    def test_widget_has_show_routing_signal(self, qapp):
        """widget 有 show_routing 信号。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        assert hasattr(TrackMixerWidget, 'show_routing')

    def test_panel_has_routing_signals(self, qapp):
        """panel 有 track_show_routing / track_routing_committed 信号。"""
        from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
        assert hasattr(AudioMixerPanel, 'track_show_routing')
        assert hasattr(AudioMixerPanel, 'track_routing_committed')
