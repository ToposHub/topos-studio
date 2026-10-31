"""E05/E16: 音频自动化录制与 Touch release。

审计实测：``_record_fader_sample`` 读取 ``EditPage._is_playing``，该属性
从未定义 → Write/Touch/Latch 推子样本永不采集。E16：Touch 松手后不回归
原曲线（AUD-15611 声明的边界）。

回归锁定：
- 权威播放状态来自 TimelinePanel 的 playback controller（_edit_playback_state）；
- 播放中采集、停止后 stop() 产出单个覆盖命令（单次 undo）；
- Touch 松手写入 release 斜坡，终点回到 release_provider 给出的原值；
- 未提供 provider 时保持旧行为（松手即停写）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.automation_recorder import AutomationRecorder


class TestAuthoritativePlaybackState:
    def test_playing_state_from_playback_controller(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        controller = SimpleNamespace(is_playing=True, current_frame=42)
        ctrl.timeline_panel = SimpleNamespace(
            get_playback_controller=lambda: controller)
        is_playing, frame = ctrl._edit_playback_state()
        assert is_playing is True
        assert frame == 42

    def test_stopped_when_no_panel(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        ctrl.timeline_panel = None
        assert ctrl._edit_playback_state() == (False, 0)

    def test_record_fader_sample_requires_playing(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        controller = SimpleNamespace(is_playing=False, current_frame=0)
        ctrl.timeline_panel = SimpleNamespace(
            get_playback_controller=lambda: controller)
        ctrl._automation_mode = "Write"
        ctrl._record_fader_sample("t1", "volume", 1.0)
        assert not getattr(ctrl, "_automation_recorders", {})


class TestTouchReleaseRamp:
    def test_release_ramp_returns_to_original_curve(self) -> None:
        rec = AutomationRecorder()
        rec.start(
            "volume", "touch", "t1",
            release_provider=lambda frame: 0.4,  # 底层曲线原值
            release_frames=12,
        )
        rec.record(100, 0.8, fader_down=True)
        rec.record(101, 0.9, fader_down=True)
        rec.record(102, 0.9, fader_down=False)  # 松手

        cmd = rec.stop()
        assert cmd is not None
        frames = sorted(kf[0] for kf in cmd.keyframes)
        values = {kf[0]: kf[1] for kf in cmd.keyframes}
        # 斜坡终点 = 松手帧 + release_frames，值回到原曲线
        assert frames[-1] == 102 + 12
        assert abs(values[frames[-1]] - 0.4) < 1e-6
        # 起点仍是触碰值
        assert abs(values[frames[0]] - 0.8) < 1e-6

    def test_no_provider_keeps_old_behavior(self) -> None:
        rec = AutomationRecorder()
        rec.start("volume", "touch", "t1")
        rec.record(100, 0.8, fader_down=True)
        rec.record(101, 0.9, fader_down=False)
        cmd = rec.stop()
        assert cmd is not None
        frames = sorted(kf[0] for kf in cmd.keyframes)
        assert frames[-1] == 100  # 无 release 斜坡（松手事件本身不是采样）

    def test_single_command_per_recording(self) -> None:
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.5)
        rec.record(50, 1.0)
        cmd = rec.stop()
        assert cmd is not None  # 单命令、单次 undo


class TestStopCommitsRecordings:
    def test_commit_executes_commands_via_timeline_view(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        executed: list = []
        tview = SimpleNamespace(_execute_command=executed.append)
        ctrl.timeline_panel = SimpleNamespace(timeline_view=tview)
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.5)
        rec.record(10, 0.7)
        ctrl._automation_recorders = {("t1", "volume"): rec}
        ctrl._commit_automation_recordings()
        assert len(executed) == 1
        assert ctrl._automation_recorders == {}
