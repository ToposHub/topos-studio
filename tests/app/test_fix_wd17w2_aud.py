"""WD17-W2 修复验证：audio_mixer 消费端接线（edit_controller 侧）。

覆盖条目：
* AUD-37702 —— AutomationLane.set_keyframe_interp 此前全库无消费者。
  现在 _on_mixer_show_automation 连接该信号 → 新命令类
  SetAutomationKeyframeInterpolationCommand 入撤销栈（仅改插值，值/帧号/
  手柄保留）→ undo 恢复原插值。发射侧契约由
  tests/shared/test_fix_wd17g98_aud.py 锁定，本文件锁消费端。
* AUD-37810 —— _on_mixer_show_routing 此前不传 routing_snapshot（校验
  退化为简化图，Bus↔Bus 既有环不可见）。现在注入全量路由快照 + 当前
  Bus 轨纳入图节点；_on_mixer_routing_committed 增加消费端提交门控：
  校验失败（环/未知目标）→ 实时听感与命令栈都不落。

UI 测试用 QT_QPA_PLATFORM=offscreen，不依赖真实音频设备。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = [pytest.mark.app, pytest.mark.regression]

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.commands import (
    CommandStack,
    SetAutomationKeyframeInterpolationCommand,
)
from src.features.edit.core.timeline_v2 import EditTrack, Send, TimelineSequence
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


# ────────────────────────── 测试替身 ──────────────────────────


class _StatusBar:
    """记录 showMessage 调用（消费端门控的用户可见提示探针）。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, int]] = []

    def showMessage(self, text: str, timeout: int = 0) -> None:
        self.messages.append((text, int(timeout)))


class _AudioManager:
    """记录 set_track_routing 调用（实时听感路径探针）。"""

    def __init__(self) -> None:
        self.routing_calls: list[tuple] = []

    def set_track_routing(self, track_id, output_bus_id, sends,
                          is_bus: bool = False) -> None:
        self.routing_calls.append(
            (track_id, output_bus_id, tuple(sends), is_bus))


class _EditPage:
    def __init__(self, timeline, audio_manager) -> None:
        self._current_timeline = timeline
        self._audio_manager = audio_manager


class _TimelineView:
    """最小 TimelineView 替身：真实 CommandStack + timeline 引用更新。"""

    def __init__(self, timeline) -> None:
        self.timeline = timeline
        self._command_stack = CommandStack()
        self.executed: list = []

    def _execute_command(self, command) -> None:
        self.executed.append(command)
        new_timeline = self._command_stack.execute(command, self.timeline)
        if new_timeline is not None:
            self.timeline = new_timeline

    def undo(self) -> None:
        new_timeline = self._command_stack.undo(self.timeline)
        if new_timeline is not None:
            self.timeline = new_timeline

    @property
    def undo_count(self) -> int:
        return len(self._command_stack._undo_stack)


class _TimelinePanel:
    def __init__(self, tview) -> None:
        self.timeline_view = tview


class _Controller(EditControllerMixin):
    """EditControllerMixin 最小宿主：只提供被测 handler 依赖的属性。"""

    def __init__(self, timeline, audio_manager=None) -> None:
        self.tview = _TimelineView(timeline)
        self.timeline_panel = _TimelinePanel(self.tview)
        self.edit_page = _EditPage(timeline, audio_manager)
        self.status_bar = _StatusBar()


# ────────────────────────── 时间线构造 ──────────────────────────


def _automation_timeline() -> TimelineSequence:
    """一条带 volume automation 的音轨（F0=linear/0.8，F100=hold/1.2）。"""
    track = EditTrack(
        track_id="t1", name="Voice", track_type="audio", index=0,
        automation={
            "volume": KeyframeTrack(
                param_name="volume", min_val=0.0, max_val=2.0,
                keyframes=[
                    Keyframe(
                        frame=0, value=0.8,
                        interpolation=InterpolationType.LINEAR,
                        tangent_mode="flat",
                        handle_left=(0.5, 0.25),
                    ),
                    Keyframe(frame=100, value=1.2,
                             interpolation=InterpolationType.HOLD),
                ],
            ),
        },
    )
    return TimelineSequence(timeline_id="seq", name="T", audio_tracks=(track,))


def _routing_timeline() -> TimelineSequence:
    """vox（普通轨，一条到 B2 的 send + 一条禁用 send）+ B1/B2 两条 Bus。

    B2 已有 output→B1（既有路由）——编辑 B1 使其 output→B2 即构成
    B1→B2→B1 环，只有在注入全量快照的完整图上才可检出。
    """
    vox = EditTrack(
        track_id="vox", name="VOX", track_type="audio", index=0,
        sends=(
            Send(target_bus_id="B2", level=0.5, enabled=True),
            Send(target_bus_id="B1", level=0.3, enabled=False),
        ),
    )
    b1 = EditTrack(track_id="B1", name="Bus 1", track_type="audio", index=1,
                   is_bus=True, output_bus_id=None)
    b2 = EditTrack(track_id="B2", name="Bus 2", track_type="audio", index=2,
                   is_bus=True, output_bus_id="B1")
    return TimelineSequence(timeline_id="seq", name="T",
                            audio_tracks=(vox, b1, b2))


# ────────────────────────── AUD-37702 ──────────────────────────


class TestAud37702InterpolationConsumer:
    """set_keyframe_interp → 命令入栈 → undo 恢复（消费端接线）。"""

    def test_signal_wires_to_command_and_undo_restores(self, qapp) -> None:
        ctl = _Controller(_automation_timeline())
        ctl._on_mixer_show_automation("t1")
        dlg = ctl._automation_lanes["t1"]
        assert dlg is not None and dlg.isVisible()

        # 双击切换插值（发射侧信号）→ 消费端命令恰好 1 条入栈
        dlg.set_keyframe_interp.emit("t1", "volume", 0, "bezier")
        assert ctl.tview.undo_count == 1
        assert isinstance(ctl.tview.executed[-1],
                          SetAutomationKeyframeInterpolationCommand)

        kf = (ctl.tview.timeline.get_track_by_id("t1")
              .automation["volume"].get_keyframe_at(0))
        assert kf.interpolation is InterpolationType.BEZIER
        # 值 / 帧号 / 手柄 / 切线模式保留（仅改插值）
        assert abs(kf.value - 0.8) < 1e-9
        assert kf.frame == 0
        assert kf.tangent_mode == "flat"
        assert kf.handle_left == (0.5, 0.25)
        # 另一个关键帧不受影响
        kf100 = (ctl.tview.timeline.get_track_by_id("t1")
                 .automation["volume"].get_keyframe_at(100))
        assert kf100.interpolation is InterpolationType.HOLD

        # undo：一次撤销恢复原插值（值保持）
        ctl.tview.undo()
        kf = (ctl.tview.timeline.get_track_by_id("t1")
              .automation["volume"].get_keyframe_at(0))
        assert kf.interpolation is InterpolationType.LINEAR
        assert abs(kf.value - 0.8) < 1e-9
        assert kf.tangent_mode == "flat"
        dlg.close()

    def test_missing_keyframe_no_command(self, qapp) -> None:
        """目标帧无关键帧（如已被并发编辑移除）：不提交空命令、不崩溃。"""
        ctl = _Controller(_automation_timeline())
        ctl._on_mixer_automation_set_interp("t1", "volume", 55, "bezier")
        assert ctl.tview.undo_count == 0
        # 时间线未被改动
        kf = (ctl.tview.timeline.get_track_by_id("t1")
              .automation["volume"].get_keyframe_at(0))
        assert kf.interpolation is InterpolationType.LINEAR

    def test_command_class_roundtrip_and_serialization(self) -> None:
        """命令类独立验证：apply/undo/redo + to_dict/from_dict 往返。"""
        timeline = _automation_timeline()
        stack = CommandStack()
        cmd = SetAutomationKeyframeInterpolationCommand.create(
            "t1", "volume", 100, "bezier")
        tl2 = stack.execute(cmd, timeline)
        kf = tl2.get_track_by_id("t1").automation["volume"].get_keyframe_at(100)
        assert kf.interpolation is InterpolationType.BEZIER
        assert abs(kf.value - 1.2) < 1e-9

        tl3 = stack.undo(tl2)
        kf = tl3.get_track_by_id("t1").automation["volume"].get_keyframe_at(100)
        assert kf.interpolation is InterpolationType.HOLD

        tl4 = stack.redo(tl3)
        kf = tl4.get_track_by_id("t1").automation["volume"].get_keyframe_at(100)
        assert kf.interpolation is InterpolationType.BEZIER

        # 序列化往返（撤销栈中已带快照的 updated command）
        executed_cmd = stack._undo_stack[-1]
        restored = SetAutomationKeyframeInterpolationCommand.from_dict(
            executed_cmd.to_dict())
        tl5 = restored.undo(tl4)
        kf = tl5.get_track_by_id("t1").automation["volume"].get_keyframe_at(100)
        assert kf.interpolation is InterpolationType.HOLD


# ────────────────────────── AUD-37810 ──────────────────────────


class TestAud37810RoutingSnapshotAndGate:
    """routing_snapshot 注入 + 消费端提交门控。"""

    def test_show_routing_passes_full_snapshot(self, qapp) -> None:
        """对话框收到全量路由快照；被编辑的 Bus 轨本身纳入图节点。"""
        ctl = _Controller(_routing_timeline())
        ctl._on_mixer_show_routing("B1")
        dlg = ctl._routing_dialog
        assert dlg is not None

        snap = dlg._routing_snapshot
        # 其他轨道的真实路由被注入（B2→B1 既有边对校验可见）
        assert snap["B2"]["output_bus_id"] == "B1"
        assert snap["B2"]["is_bus"] is True
        # enabled send 计入，disabled send 不计
        assert snap["vox"]["sends"] == [("B2", 0.5)]
        assert snap["vox"]["is_bus"] is False
        # 当前 Bus 轨（B1）纳入图节点，否则经它的环不可检出
        bus_ids = {bid for bid, _ in dlg._bus_tracks}
        assert "B1" in bus_ids and "B2" in bus_ids
        dlg.close()

    def test_build_routing_validation_graph_overrides_current(self) -> None:
        """校验图以全量 tracks 为基础，当前轨拟定值最后写入覆盖。"""
        ctl = _Controller(_routing_timeline())
        timeline = ctl.tview.timeline
        bus_ids, routing = ctl._build_routing_validation_graph(
            timeline, "vox", "B1",
            [Send(target_bus_id="B2", level=0.8)])
        assert bus_ids == ["B1", "B2"]
        assert routing["vox"]["output_bus_id"] == "B1"
        assert routing["vox"]["sends"] == [("B2", 0.8)]
        assert routing["vox"]["is_bus"] is False
        assert routing["B2"]["output_bus_id"] == "B1"
        assert routing["B1"]["is_bus"] is True

    def test_cyclic_commit_blocked_at_consumer(self, qapp) -> None:
        """B1→B2 与既有 B2→B1 成环：消费端门控阻止提交（听感/命令栈/时间线均不变）。"""
        mgr = _AudioManager()
        ctl = _Controller(_routing_timeline(), mgr)
        ctl._on_mixer_routing_committed(
            "B1", {"output_bus_id": "B2", "sends": []})
        assert ctl.tview.undo_count == 0            # 无命令入栈
        assert mgr.routing_calls == []              # 实时听感未落
        assert ctl.status_bar.messages              # 用户可见提示
        # 时间线未被改动
        assert ctl.tview.timeline.get_track_by_id("B1").output_bus_id is None

    def test_unknown_target_commit_blocked_at_consumer(self, qapp) -> None:
        """output 指向不存在的 Bus：同样被门控阻止。"""
        mgr = _AudioManager()
        ctl = _Controller(_routing_timeline(), mgr)
        ctl._on_mixer_routing_committed(
            "vox", {"output_bus_id": "ghost", "sends": []})
        assert ctl.tview.undo_count == 0
        assert mgr.routing_calls == []
        assert ctl.tview.timeline.get_track_by_id("vox").output_bus_id is None

    def test_valid_commit_passes_and_undo_restores(self, qapp) -> None:
        """合法路由照常提交（命令入栈 + 实时听感），undo 恢复。"""
        mgr = _AudioManager()
        ctl = _Controller(_routing_timeline(), mgr)
        # sends 保持既有状态（整体替换语义下无 send diff → 恰好 1 条命令）
        ctl._on_mixer_routing_committed("vox", {
            "output_bus_id": "B1",
            "sends": [
                Send(target_bus_id="B2", level=0.5, enabled=True),
                Send(target_bus_id="B1", level=0.3, enabled=False),
            ],
        })
        assert ctl.tview.undo_count == 1
        # 实时听感：enabled send 才进引擎
        assert mgr.routing_calls == [("vox", "B1", (("B2", 0.5),), False)]
        assert (ctl.tview.timeline.get_track_by_id("vox").output_bus_id
                == "B1")
        ctl.tview.undo()
        assert (ctl.tview.timeline.get_track_by_id("vox").output_bus_id
                is None)

    def test_dialog_cycle_blocked_with_injected_snapshot(self, qapp) -> None:
        """端到端：经控制器创建的对话框（已带快照）里把 B1 输出设为 B2
        —— 与既有 B2→B1 成环，routing_committed 不发射、命令不入栈、
        告警可见；改回 Master（无环）后放行。"""
        ctl = _Controller(_routing_timeline())
        ctl._on_mixer_show_routing("B1")
        dlg = ctl._routing_dialog
        emitted: list = []
        dlg.routing_committed.connect(
            lambda tid, payload: emitted.append((tid, payload)))

        idx_b2 = dlg._output_combo.findData("B2")
        assert idx_b2 >= 0
        dlg._output_combo.setCurrentIndex(idx_b2)
        # 环 → 发射被门控（告警已显示，用户修正后再提交）
        assert emitted == []
        assert ctl.tview.undo_count == 0
        assert dlg._warning_label.isVisibleTo(dlg)
        assert ctl.tview.timeline.get_track_by_id("B1").output_bus_id is None

        # 反例：回 Master（None，无环）→ 放行发射
        idx_master = dlg._output_combo.findData(None)
        dlg._output_combo.setCurrentIndex(idx_master)
        assert len(emitted) == 1
        assert emitted[0][0] == "B1"
        assert emitted[0][1]["output_bus_id"] is None
        dlg.close()
