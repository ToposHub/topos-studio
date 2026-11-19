"""
§4.4 R 按钮 UI 接线测试。

验证 Record Arm R 按钮已启用且接线到 RecordingManager：
- RECORD_ARM_IMPLEMENTED = True（R 按钮启用）
- edit_controller 有 _start_recording / _stop_recording_and_import / _on_take_recorded
- feature flag 翻转后 R 按钮状态正确
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestFeatureFlagEnabled:
    """§4.4: Record Arm 已实现，flag 为 True。"""

    def test_record_arm_implemented_true(self):
        from src.shared.ui.audio_mixer.mixer_feature_flags import RECORD_ARM_IMPLEMENTED
        assert RECORD_ARM_IMPLEMENTED is True


class TestControllerWiring:
    """edit_controller 接线方法存在。"""

    def test_controller_has_recording_methods(self):
        from src.app.controllers.edit_controller import EditControllerMixin
        assert hasattr(EditControllerMixin, '_on_mixer_record_arm_toggled')
        assert hasattr(EditControllerMixin, '_start_recording')
        assert hasattr(EditControllerMixin, '_stop_recording_and_import')
        assert hasattr(EditControllerMixin, '_on_take_recorded')

    def test_record_arm_toggled_routes_to_start_stop(self):
        """_on_mixer_record_arm_toggled 是 R 按钮的入口。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        # 方法可调用（签名正确）
        import inspect
        sig = inspect.signature(EditControllerMixin._on_mixer_record_arm_toggled)
        params = list(sig.parameters.keys())
        assert 'track_id' in params
        assert 'armed' in params


class TestRecordingManagerIntegration:
    """RecordingManager 与 controller 集成（端到端，用 inject_pcm 模拟设备）。"""

    def test_recording_flow_with_inject(self, qapp, tmp_path):
        """完整录音流程：start → inject → stop → take 落盘。

        直接测试 RecordingManager（绕过 Qt 设备），验证 R 按钮接线的底层逻辑。
        """
        from src.features.edit.core.recording_manager import (
            RecordingManager, RecordingConfig,
        )
        import numpy as np

        config = RecordingConfig(
            output_dir=str(tmp_path), scene_name="Test",
            pre_roll_seconds=0,  # 测试无 pre-roll
        )
        mgr = RecordingManager(config)
        assert mgr.start_recording()
        # 注入 PCM（模拟设备输入）
        sig = (0.5 * np.sin(2 * np.pi * 440 * np.arange(4800) / 48000)).astype(np.float32)
        mgr.inject_pcm(sig.reshape(1, -1))
        take = mgr.stop_recording()
        assert take is not None
        assert take.status == "completed"
        assert Path(take.file_path).exists()


class TestRButtonEnabled:
    """R 按钮在 flag=True 时启用。"""

    def test_r_button_enabled(self, qapp):
        from src.shared.ui.audio_mixer import TrackMixerWidget
        from src.shared.ui.audio_mixer.mixer_feature_flags import RECORD_ARM_IMPLEMENTED
        widget = TrackMixerWidget("test-track", "Test", "A1", is_master=False)
        if RECORD_ARM_IMPLEMENTED:
            assert widget._monitor_btn.isEnabled() is True
        else:
            assert widget._monitor_btn.isEnabled() is False

    def test_master_no_r_button(self, qapp):
        """Master 轨无 R 按钮（或无该属性）。"""
        from src.shared.ui.audio_mixer import TrackMixerWidget
        widget = TrackMixerWidget("-1", "Master", "", is_master=True)
        # Master 不创建 _monitor_btn（或为 None）
        assert not hasattr(widget, '_monitor_btn') or widget._monitor_btn is None


def test_studio_patches_inputs_and_arms_tracks_only_in_mixer(qtbot: QtBot) -> None:
    from PyQt6.QtWidgets import QMenu

    from src.features.mixer.ui.studio_console import StudioAudioMixerPanel

    panel = StudioAudioMixerPanel()
    qtbot.addWidget(panel)
    panel.sync_tracks([
        {"track_id": "dialogue", "name": "Dialogue", "tag": "A1"},
        {"track_id": "keys", "name": "Keys", "tag": "A2"},
        {"track_id": "locked", "name": "Locked", "tag": "A3", "is_locked": True},
        {"track_id": "bus", "name": "Bus", "tag": "B1", "is_bus": True},
    ])
    dialogue, keys, locked, bus = (panel._track_mixers[track_id]
                                   for track_id in ("dialogue", "keys", "locked", "bus"))
    panel.show()
    assert not dialogue.recording_input_button.isVisible()
    assert dialogue._monitor_btn.isEnabled()

    panel.set_recording_workflow_active(True)
    assert dialogue.recording_input_button.isVisible()
    assert not dialogue._monitor_btn.isEnabled()
    assert not locked.recording_input_button.isEnabled()
    assert bus.recording_input_button.isHidden() and bus._monitor_btn.isHidden()
    assert panel._master_mixer.recording_input_button.isHidden()
    with qtbot.waitSignal(panel.track_input_requested) as input_requested:
        dialogue.recording_input_button.click()
    assert input_requested.args == ["dialogue"]
    menu = QMenu(panel)
    panel._populate_mixer_menu(menu)
    adr_action = next(action for action in menu.actions() if action.objectName() == "mixer_adr")
    with qtbot.waitSignal(panel.adr_requested):
        adr_action.trigger()

    label = "Line 1 · Interface with a long source name"
    panel.set_recording_inputs({"dialogue": label, "keys": "Line 2"})
    panel.set_narrow_layout(True)
    assert label in dialogue.recording_input_button.toolTip()
    assert dialogue.recording_input_button.text() != label

    armed: set[str] = set()

    def reflect_arm(track_id: str, checked: bool) -> None:
        if checked:
            armed.add(track_id)
        else:
            armed.discard(track_id)
        panel.set_record_arm_state(armed)

    panel.track_record_arm_toggled.connect(reflect_arm)
    dialogue._monitor_btn.click()
    keys._monitor_btn.click()
    assert armed == {"dialogue", "keys"}
    assert dialogue._monitor_btn.isChecked() and keys._monitor_btn.isChecked()

    panel.set_recording_input_level("dialogue", -8.0)
    dialogue.reset_meter()
    dialogue.update_audio_level([-20.0, -22.0])
    assert dialogue.studio_meter.peak_l == -8.0
    assert dialogue.studio_meter.peak_r == -8.0
    assert dialogue.studio_meter.accessibleName() == dialogue.studio_peak.toolTip()
    dialogue._monitor_btn.click()
    assert dialogue.studio_meter.peak_l == -20.0
    assert dialogue.studio_meter.peak_r == -22.0

    panel.set_record_arm_state(armed, busy=True)
    assert not dialogue._monitor_btn.isEnabled()
    assert not keys.recording_input_button.isEnabled()

    panel.set_recording_workflow_active(False)
    assert not dialogue.recording_input_button.isVisible()
    assert dialogue._monitor_btn.isEnabled() and not dialogue._monitor_btn.isChecked()


def test_mixer_timeline_header_controls_arming_and_preserves_recording_status(qtbot: QtBot) -> None:
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.set_audio_only(True)
    view.load_timeline(TimelineSequence(
        "mixer", "Mixer", audio_tracks=(EditTrack("dialogue", "Dialogue", "audio"),)))
    header = view._track_headers["dialogue"]
    assert header._show_record_button
    assert not header._record_button_enabled()

    view.set_track_recording_state("dialogue", "idle")
    assert header._record_button_enabled()
    view.set_track_recording_state("dialogue", "armed")
    assert header._record_button_enabled()
    view.set_mixer_record_arm_busy(True)
    assert header._recording_state == "armed"
    assert not header._record_button_enabled()
    view.set_track_recording_state("dialogue", "recording")
    view.set_mixer_record_arm_busy(False)
    assert not header._record_button_enabled()
