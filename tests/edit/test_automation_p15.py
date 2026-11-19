"""P1-5 四个子项验收：Trim Automation、写保护、自动化预览捕获、时间线参数 lane。

验收（docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md P1-5 方法列
的未落地子项，status 报告边界 6）：

- Trim Automation：模型字段 + 求值（曲线值/静态默认同乘）+ 引擎逐样本
  ramp 同语义 + 命令入栈可撤销 + 序列化；
- 写保护：模型字段 + 录制入口/写模式切换守卫 + 命令 + 序列化；
- 预览捕获：AutomationPreviewCapture 不建命令；引擎预览层优先于正式
  曲线（预听）；commit → OverwriteAutomationCommand 单次 undo；
- 时间线参数 lane：TimelinePanel 条带（轨道选择 + AutomationLanePanel
  复用 + 命令接线）+ AutomationLane 对话框形态保持兼容。
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

from src.features.edit.core.commands import CommandStack
from src.features.edit.core.commands.audio_commands import (
    SetTrackAutomationTrimCommand,
    SetTrackAutomationWriteProtectCommand,
)
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core
FPS = 25


def _kf_track(points: list[tuple[int, float]], param: str = "volume") -> KeyframeTrack:
    track = KeyframeTrack(param_name=param, min_val=0.0, max_val=2.0)
    for frame, value in points:
        track.add_keyframe(Keyframe(frame=float(frame), value=float(value)))
    return track


class TestTrimAutomation:
    def test_value_evaluation_applies_trim(self):
        track = (EditTrack("T", "T", "audio", 0)
                 .with_automation_trim("volume", 0.5))
        track = track._rebuild(automation={"volume": _kf_track([(0, 1.0), (48, 1.0)])})
        assert track.get_automation_value("volume", 24, 1.0) == pytest.approx(0.5)
        # 无曲线参数：静态默认同乘
        assert track.get_automation_value("pan", 0, 1.0) == pytest.approx(1.0)
        no_trim = EditTrack("T", "T", "audio", 0)._rebuild(
            automation={"volume": _kf_track([(0, 1.0), (48, 1.0)])})
        assert no_trim.get_automation_value("volume", 24, 1.0) == pytest.approx(1.0)

    def test_command_roundtrip_and_undo(self):
        seq = TimelineSequence(audio_tracks=(EditTrack("T", "T", "audio", 0),))
        stack = CommandStack()
        applied = stack.execute(
            SetTrackAutomationTrimCommand.create("T", "volume", 2.0), seq)
        assert applied.audio_tracks[0].automation_trim == {"volume": 2.0}
        restored = stack.undo(applied)
        assert restored.audio_tracks[0].automation_trim == {}
        data = SetTrackAutomationTrimCommand.create("T", "volume", 0.5).to_dict()
        assert data["command_type"] == "SetTrackAutomationTrimCommand"
        restored_cmd = SetTrackAutomationTrimCommand.from_dict(data)
        assert restored_cmd.factor == 0.5

    def test_serialization(self):
        track = EditTrack("T", "T", "audio", 0).with_automation_trim("volume", 0.5)
        rt = TimelineSequence.from_dict(
            TimelineSequence(audio_tracks=(track,)).to_dict()).audio_tracks[0]
        assert rt.automation_trim == {"volume": 0.5}
        assert rt.automation_trim_factor("volume") == 0.5

    def test_engine_ramp_applies_trim(self):
        """引擎逐样本 ramp 同语义：trim 倍率乘在曲线与静态默认上。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
        mixer = AudioMixer()
        mixer.add_track("T", SimpleNamespace(
            _channels=2,
            decode_samples_for_frame=lambda frame, fps, n: np.ones(
                (2, n), dtype=np.float32) * 0.5,
        ), AudioTrackState(track_id="T"))
        mixer.set_track_automation("T", {"volume": _kf_track([(0, 1.0), (48, 1.0)])})
        mixer.set_track_automation_mode("T", "read")
        mixer.set_track_automation_trim("T", {"volume": 0.5})
        chunk, peaks = mixer.mix_samples(0, float(FPS), 48)
        # 曲线恒 1.0 × trim 0.5 → 块内样本 ≈ 0.5 × 0.5(源) = 0.25
        assert float(np.max(np.abs(chunk))) == pytest.approx(0.25, abs=1e-5)
        # 清除 trim → 恢复 0.5
        mixer.set_track_automation_trim("T", {})
        chunk, _peaks = mixer.mix_samples(0, float(FPS), 48)
        assert float(np.max(np.abs(chunk))) == pytest.approx(0.5, abs=1e-5)


class TestWriteProtect:
    def test_command_and_serialization(self):
        seq = TimelineSequence(audio_tracks=(EditTrack("T", "T", "audio", 0),))
        stack = CommandStack()
        applied = stack.execute(
            SetTrackAutomationWriteProtectCommand.create("T", True), seq)
        assert applied.audio_tracks[0].automation_write_protected is True
        restored = stack.undo(applied)
        assert restored.audio_tracks[0].automation_write_protected is False
        data = SetTrackAutomationWriteProtectCommand.create("T", True).to_dict()
        assert data["command_type"] == "SetTrackAutomationWriteProtectCommand"

    def test_serialization_roundtrip(self):
        track = EditTrack("T", "T", "audio", 0).with_automation_write_protected(True)
        rt = TimelineSequence.from_dict(
            TimelineSequence(audio_tracks=(track,)).to_dict()).audio_tracks[0]
        assert rt.automation_write_protected is True

    def test_recording_guard_blocks_protected_track(self, qapp):
        """写保护轨的 fader 采集被拒绝（不产生 recorder）。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=TimelineSequence(
                audio_tracks=(EditTrack("T", "T", "audio", 0)
                             .with_automation_write_protected(True),))),
            _automation_mode_for_track=(
                lambda tid: "write"),
            status_bar=None,
        )
        EditControllerMixin._record_fader_sample(host, "T", "volume", 1.5)
        assert not getattr(host, "_automation_recorders", {})
        # 非保护轨正常进入录制器创建路径
        host2 = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=TimelineSequence(
                audio_tracks=(EditTrack("T2", "T2", "audio", 0),))),
            _automation_mode_for_track=(lambda tid: "write"),
            _edit_playback_state=lambda: (True, 12),
            status_bar=None,
        )
        host2._get_automation_recorder = (
            EditControllerMixin._get_automation_recorder.__get__(host2))
        host2._make_automation_release_provider = (
            EditControllerMixin._make_automation_release_provider.__get__(host2))
        EditControllerMixin._record_fader_sample(host2, "T2", "volume", 1.5)
        assert ("T2", "volume") in EditControllerMixin._get_automation_recorder(host2)


class TestPreviewCapture:
    def test_capture_finish_returns_keyframes_without_command(self):
        from src.features.edit.core.automation_recorder import (
            AutomationPreviewCapture,
        )
        capture = AutomationPreviewCapture("volume", "write", "T")
        capture.start()
        for frame, value in ((0, 1.0), (24, 1.4), (48, 1.4), (72, 0.8)):
            capture.record(frame, value)
        keyframes = capture.finish()
        assert keyframes[0][0] == 0 and keyframes[-1][0] == 72

    def test_engine_preview_layer_precedence(self):
        """预览层存在时优先于正式曲线（预听；与模式无关）。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
        mixer = AudioMixer()
        mixer.add_track("T", SimpleNamespace(
            _channels=2,
            decode_samples_for_frame=lambda frame, fps, n: np.ones(
                (2, n), dtype=np.float32) * 0.5,
        ), AudioTrackState(track_id="T"))
        # 引擎模式 off：正式曲线不读 → 基线 0.5×1.0
        chunk, _p = mixer.mix_samples(0, float(FPS), 48)
        assert float(np.max(np.abs(chunk))) == pytest.approx(0.5, abs=1e-5)
        # 挂预览层（off 模式也读预览）→ 曲线 1.6 × 0.5
        mixer.set_track_automation_preview("T", {"volume": _kf_track([(0, 1.6), (48, 1.6)])})
        chunk, _p = mixer.mix_samples(0, float(FPS), 48)
        assert float(np.max(np.abs(chunk))) == pytest.approx(0.8, abs=1e-5)
        # 丢弃预览层 → 回基线
        mixer.set_track_automation_preview("T", {})
        chunk, _p = mixer.mix_samples(0, float(FPS), 48)
        assert float(np.max(np.abs(chunk))) == pytest.approx(0.5, abs=1e-5)

    def test_commit_builds_single_undo_command(self, qapp):
        """控制器 commit：预览层 → OverwriteAutomationCommand（单次 undo）。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.automation_recorder import (
            OverwriteAutomationCommand,
        )
        committed: list = []
        track = EditTrack("T", "T", "audio", 0)
        mgr = SimpleNamespace(
            _mixer=SimpleNamespace(_automation_preview={
                "T": {"volume": _kf_track([(0, 1.2), (48, 1.2)])},
            }),
            set_track_automation_preview=lambda _tid, _a: None,
        )
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=TimelineSequence(
                audio_tracks=(track,))),
            _get_mixer_audio_manager=lambda: mgr,
            timeline_panel=SimpleNamespace(timeline_view=SimpleNamespace(
                _execute_command=committed.append)),
        )
        host.discard_automation_preview = (
            EditControllerMixin.discard_automation_preview.__get__(host))
        host._get_automation_preview_captures = (
            EditControllerMixin._get_automation_preview_captures.__get__(host))
        assert EditControllerMixin.commit_automation_preview(host, "T", "volume") is True
        assert len(committed) == 1
        assert isinstance(committed[0], OverwriteAutomationCommand)


class TestTimelineParamLane:
    def test_strip_toggle_and_sync(self, qapp, qtbot):
        from src.features.edit.ui.timeline_panel import TimelinePanel
        panel = TimelinePanel()
        qtbot.addWidget(panel)
        assert not panel.automation_lane_visible()
        panel.toggle_automation_lane(True)
        assert panel.automation_lane_visible()
        assert panel._automation_lane is not None
        # 轨道选择器同步
        panel.timeline_view._timeline = TimelineSequence(
            fps=Rational(FPS, 1),
            audio_tracks=(EditTrack("T", "Dialogue", "audio", 0),))
        panel.sync_automation_lane_tracks()
        combo = panel._automation_lane_track_combo
        assert combo.count() == 1 and combo.itemData(0) == "T"
        # 换轨 → 面板绑定 track_id 并同步数据
        panel._on_automation_lane_track_changed(0)
        assert panel._automation_lane._track_id == "T"
        panel.toggle_automation_lane(False)
        assert not panel.automation_lane_visible()

    def test_dialog_wrapper_delegates(self, qapp):
        from src.shared.ui.audio_mixer.automation_lane import AutomationLane
        lane = AutomationLane("track-1", "Voice", total_frames=1000)
        track = _kf_track([(100, 1.0)])
        lane.update_track_data({"volume": track})
        assert lane._track_id == "track-1"
        assert lane._curve is not None
        received: list[tuple] = []
        lane.set_keyframe_interp.connect(lambda *a: received.append(a))
        from PyQt6.QtCore import QEvent, QPointF
        pos = QPointF(lane._curve._frame_to_x(100), lane._curve._value_to_y(1.0))
        from tests.shared.test_fix_wd17g98_aud import _mouse_event
        lane._curve.mouseDoubleClickEvent(
            _mouse_event(QEvent.Type.MouseButtonDblClick, pos))
        assert received and received[0][1] == "volume"
