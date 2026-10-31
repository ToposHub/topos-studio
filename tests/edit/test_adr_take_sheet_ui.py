"""P1-10 ADR Take Sheet 面板与控制器接线测试。

验收（计划表 P1-10）：
- R/停止只产生一个 asset 和一次 Undo（命令栈导入 + 撤销）
- 多 take 可比较（take sheet 列表/试听解码）与替换（UpdateClipSourceCommand
  按时间码对齐 source_start）
- 最佳 take 标记（同场次互斥，MarkBestTakeCommand 可撤销）
- loop record：take 完成后同打点自动续录
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.edit_ui

from src.features.edit.core.recording_manager import (
    RecordingConfig,
    RecordingManager,
    TakeInfo,
    write_take_wav,
)

SR = 48000


def _sine(amp: float, freq: float, duration: float) -> np.ndarray:
    n = int(SR * duration)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32).reshape(1, -1)


def _completed_take(tmp_path, number: int, scene: str = "ADR",
                    in_point: int = 250) -> TakeInfo:
    take = TakeInfo(
        take_number=number, name=f"{scene}_Take{number:02d}",
        scene_name=scene, status="completed",
        duration_seconds=2.0, peak_dbfs=-6.5,
        start_timecode="00:00:10:00", time_reference_samples=480000,
        in_point_frame=in_point, start_timestamp="2026-08-27T10:00:00",
    )
    take.file_path = str(tmp_path / f"{take.name}.wav")
    assert write_take_wav(take.file_path, _sine(0.5, 440, 0.3), SR, 24, take)
    return take


# ─────────────────────────────────────────────────────────────────────────────
# 面板
# ─────────────────────────────────────────────────────────────────────────────


class TestAdrPanelBasics:

    def test_defaults_and_settings(self, qapp):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        settings = panel.adr_settings()
        assert settings["channels"] == 1
        assert settings["pre_roll_seconds"] == 2.0
        assert settings["post_roll_seconds"] == 1.0
        assert settings["loop"] is False
        assert settings["monitor"] is False

    def test_state_label_updates(self, qapp):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        panel.set_recording_state("recording")
        assert "REC" in panel._state_label.text()
        panel.set_recording_state("pre_roll")
        assert "Pre-roll" in panel._state_label.text()

    def test_calibration_display(self, qapp):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        from src.features.edit.core.recording_manager import calibrate_latency
        panel = AdrTakeSheetPanel()
        panel.set_calibration(calibrate_latency(16.0))
        assert "8.0" in panel._calib_label.text()
        panel.set_calibration(None)
        assert "calibrat" in panel._calib_label.text().lower()

    def test_settings_changed_signal(self, qapp):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        fired = []
        panel.settings_changed.connect(lambda: fired.append(1))
        panel._pre_roll_spin.setValue(3.5)
        panel._loop_check.setChecked(True)
        assert len(fired) >= 2
        assert panel.adr_settings()["loop"] is True

    def test_add_take_entry_and_current(self, qapp, tmp_path):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        take = _completed_take(tmp_path, 1)
        panel.add_take_entry(take, asset_id="a1", track_id="trk")
        assert panel._table.rowCount() == 1
        assert panel.current_take_id() == take.take_id
        # TC 列显示
        assert panel._table.item(0, 4).text() == "00:00:10:00"
        # 幂等：同一 take_id 再加入不重复
        panel.add_take_entry(take, asset_id="a1")
        assert panel._table.rowCount() == 1

    def test_mark_best_button_emits_signal(self, qapp, tmp_path):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        take = _completed_take(tmp_path, 1)
        panel.add_take_entry(take)
        received = []
        panel.mark_best_requested.connect(received.append)
        panel._mark_best_btn.click()
        assert received == [take.take_id]

    def test_replace_button_emits_signal(self, qapp, tmp_path):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        take = _completed_take(tmp_path, 2)
        panel.add_take_entry(take)
        received = []
        panel.replace_clip_requested.connect(received.append)
        panel._replace_btn.click()
        assert received == [take.take_id]

    def test_audition_decode(self, qapp, tmp_path):
        """试听解码器能读回 take 文件（planar + 元数据）。"""
        from src.features.edit.ui.adr_take_sheet_panel import TakeAuditionPlayer
        take = _completed_take(tmp_path, 1)
        samples, sample_rate, channels = TakeAuditionPlayer._decode(take.file_path)
        assert samples is not None
        assert sample_rate == SR
        assert channels == 1
        assert samples.shape[0] == 1
        assert samples.shape[1] == int(0.3 * SR)

    def test_monitor_sink_lifecycle(self, qapp):
        from src.features.edit.ui.adr_take_sheet_panel import InputMonitorSink
        sink = InputMonitorSink()
        started = sink.start(SR, 1)
        if started:  # 有真实输出设备时
            block = _sine(0.2, 440, 0.05)
            sink.write_block(block)  # 不抛异常
            assert sink.active
            sink.stop()
            assert not sink.active
        else:
            assert not sink.active


# ─────────────────────────────────────────────────────────────────────────────
# 控制器接线（一次 Undo / take sheet / loop / 替换）
# ─────────────────────────────────────────────────────────────────────────────


class _FakeMediaPoolWithStack:
    """带真实 ProjectCommandStack 的 media_pool 替身。"""

    def __init__(self, project):
        self._project = project
        from src.features.edit.core.commands import ProjectCommandStack
        self.stack = ProjectCommandStack()
        self.set_project_calls = []

    def get_command_stack(self):
        return self.stack

    def set_project(self, project):
        self.set_project_calls.append(project)


def _make_controller(project, qapp, panel=None):
    from src.app.controllers.edit_controller import EditControllerMixin
    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl._current_edit_project = project
    ctrl.media_pool_panel = _FakeMediaPoolWithStack(project)
    tview = MagicMock()
    tview.set_project = MagicMock()
    tview.project_changed = MagicMock()
    tpanel = MagicMock()
    tpanel.timeline_view = tview
    ctrl.timeline_panel = tpanel
    ctrl.timeline_preview_renderer = MagicMock()
    ctrl.edit_page = None
    if panel is not None:
        ctrl.adr_take_sheet_panel = panel
    return ctrl


def _make_project():
    from src.features.edit.core.project import EditProject
    return EditProject.create(name="adr-ui")


class TestTakeImportSingleUndo:

    def test_import_uses_command_stack_and_single_undo(self, qapp, tmp_path):
        """验收：R/停止 → 一个 asset；一次 Undo → asset 移除。"""
        panel = None
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)

        take = _completed_take(tmp_path, 1)
        ctrl._on_take_recorded("trk", take)

        # take sheet 面板出现条目
        assert panel._table.rowCount() == 1
        entry = panel.take_sheet().entries[0]
        assert entry.asset_id
        assert entry.start_timecode == "00:00:10:00"

        # 一个 asset；一次 Undo 移除
        new_project = ctrl._current_edit_project
        assert len(new_project.assets) == 1
        undone = ctrl.media_pool_panel.get_command_stack().undo(new_project)
        assert len(undone.assets) == 0
        assert undone.get_asset_by_path(take.file_path) is None

    def test_panel_take_sheet_rebuilt_from_project(self, qapp, tmp_path):
        """_apply_project_change 后面板 take sheet 与工程一致（重开恢复）。"""
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)
        take = _completed_take(tmp_path, 3, in_point=100)
        ctrl._on_take_recorded("trk", take)
        # 重建路径（_apply_project_change 内 build_take_sheet_from_project）
        assert panel._table.rowCount() == 1
        rebuilt = panel.take_sheet().entries[0]
        assert rebuilt.take_number == 3
        assert rebuilt.in_point_frame == 100


class TestMarkBestWiring:

    def test_mark_best_updates_project_and_panel(self, qapp, tmp_path):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)

        t1 = _completed_take(tmp_path, 1)
        t2 = _completed_take(tmp_path, 2)
        ctrl._on_take_recorded("trk", t1)
        ctrl._on_take_recorded("trk", t2)

        ctrl._on_adr_mark_best(t1.take_id)
        entry1 = panel.take_sheet().entry_for_take(t1.take_id)
        assert ctrl._current_edit_project.assets[
            entry1.asset_id].metadata["is_best_take"] is True
        # 面板 ★ 列
        assert panel._table.item(0, 6).text() == "★"

        # 换最佳 → 互斥
        ctrl._on_adr_mark_best(t2.take_id)
        project = ctrl._current_edit_project
        entry2 = panel.take_sheet().entry_for_take(t2.take_id)
        assert project.assets[entry2.asset_id].metadata["is_best_take"] is True
        assert "is_best_take" not in project.assets[entry1.asset_id].metadata

    def test_mark_best_without_asset_is_noop(self, qapp, tmp_path):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)
        take = _completed_take(tmp_path, 1)
        panel.add_take_entry(take)  # 无 asset_id
        ctrl._on_adr_mark_best(take.take_id)  # 不抛异常
        assert ctrl._current_edit_project is project


class TestReplaceClipWiring:

    def test_replace_selected_clip_aligned(self, qapp, tmp_path):
        """take 替换所选片段：source_start 按时间码对齐。"""
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        from src.features.edit.core.timeline_v2 import EditTrack, ClipInstance

        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)
        take = _completed_take(tmp_path, 1, in_point=250)
        ctrl._on_take_recorded("trk", take)
        entry = panel.take_sheet().entry_for_take(take.take_id)

        # 最小时间线：clip 在 300 帧；take 首样本 = 480000/48000*25 = 250 帧
        clip = ClipInstance(
            clip_id="c1", asset_id="old-asset", track_id="A1",
            timeline_start=300, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        track = EditTrack(track_id="A1", track_type="audio", name="ADR")
        track = track.with_clip_added(clip)
        timeline = MagicMock()
        timeline.fps = 25.0
        timeline.all_tracks = (track,)

        executed = []

        class _FakeSelection:
            selected_clip_ids = ["c1"]

        tview = MagicMock()
        tview._selection = _FakeSelection()
        tview._execute_command = executed.append
        ctrl.timeline_panel.timeline_view = tview
        ctrl.edit_page = MagicMock()
        ctrl.edit_page._current_timeline = timeline

        ctrl._on_adr_replace_clip(take.take_id)
        assert len(executed) == 1
        cmd = executed[0]
        from src.features.edit.core.commands import UpdateClipSourceCommand
        assert isinstance(cmd, UpdateClipSourceCommand)
        assert cmd.new_asset_id == entry.asset_id
        # source_start = clip.timeline_start(300) − first_sample_frame(250) = 50
        assert cmd.new_source_start == 50

    def test_replace_without_selection_is_noop(self, qapp, tmp_path):
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)
        take = _completed_take(tmp_path, 1)
        ctrl._on_take_recorded("trk", take)

        timeline = MagicMock()
        timeline.fps = 25.0
        timeline.all_tracks = ()
        tview = MagicMock()
        tview._selection = MagicMock()
        tview._selection.selected_clip_ids = []
        executed = []
        tview._execute_command = executed.append
        ctrl.timeline_panel.timeline_view = tview
        ctrl.edit_page = MagicMock()
        ctrl.edit_page._current_timeline = timeline

        ctrl._on_adr_replace_clip(take.take_id)
        assert executed == []


class TestLoopRecordWiring:

    def _arm_loop_state(self, ctrl, track_id, in_point):
        """模拟 arm 阶段登记的按轨 loop 状态。"""
        ctrl._adr_loop_tracks = {track_id}
        ctrl._adr_in_points = {track_id: in_point}

    def test_loop_restarts_same_manager_and_in_point(self, qapp, tmp_path):
        """loop：In/Out 截止的 take 完成回调后同打点续录（manager 复用）。"""
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        panel._loop_check.setChecked(True)
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)

        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        starts = []
        original_start = mgr.start_recording

        def _spy_start(take_number=None, in_point_frame=None):
            starts.append(in_point_frame)
            return original_start(take_number=take_number,
                                  in_point_frame=in_point_frame)

        mgr.start_recording = _spy_start
        mgr.on_take_completed = (
            lambda take: ctrl._on_take_recorded("trk", take, None))
        ctrl._recording_managers = {"trk": mgr}
        self._arm_loop_state(ctrl, "trk", 125)

        mgr.start_recording(in_point_frame=125)
        mgr.inject_pcm(_sine(0.5, 440, 0.1))
        mgr.stop_recording()  # 模拟 loop 截止定时（不 pop manager）

        assert len(starts) >= 2  # 首录 + 回调续录
        assert starts[-1] == 125  # 同一打点
        assert "trk" in ctrl._recording_managers  # manager 留缓存

    def test_disarm_stops_loop(self, qapp, tmp_path):
        """disarm（pop manager + 清该轨 loop）后 loop 不再续录。"""
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)

        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        mgr.on_take_completed = (
            lambda take: ctrl._on_take_recorded("trk", take, None))
        ctrl._recording_managers = {"trk": mgr}
        self._arm_loop_state(ctrl, "trk", 50)

        mgr.start_recording(in_point_frame=50)
        mgr.inject_pcm(_sine(0.5, 440, 0.1))
        ctrl._stop_recording_and_import("trk")
        assert "trk" not in ctrl._recording_managers
        assert "trk" not in ctrl._adr_loop_tracks  # 该轨 loop 已清

    def test_loop_restart_skips_replaced_manager(self, qapp, tmp_path):
        """审计修复：post-roll 内 disarm→再 arm 后，旧 manager 回调不误启新 manager。"""
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        old_mgr = RecordingManager(cfg)
        old_mgr.on_take_completed = (
            lambda take: ctrl._on_take_recorded("trk", take, None))
        new_mgr = RecordingManager(cfg)
        new_mgr._state = RecordingManager.STATE_RECORDING  # 模拟已开始录音
        ctrl._recording_managers = {"trk": new_mgr}  # 已被替换
        self._arm_loop_state(ctrl, "trk", 50)
        old_mgr.start_recording(in_point_frame=50)
        old_mgr.inject_pcm(_sine(0.5, 440, 0.1))
        old_mgr.stop_recording()  # 旧 manager 的迟到回调
        # 新 manager 未被重启（is_recording 仍为 True 且未被 start 覆盖）
        assert new_mgr.is_recording is True

    def test_take_recorded_skips_foreign_project(self, qapp, tmp_path):
        """审计修复：post-roll 内切工程 → take 不导入别的工程。"""
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        project_a = _make_project()
        ctrl = _make_controller(project_a, qapp, panel=panel)
        take = _completed_take(tmp_path, 1)
        # take 属于工程 A（project_id 不同），当前会话工程 B
        project_b = _make_project()
        ctrl._current_edit_project = project_b
        ctrl._on_take_recorded("trk", take, recorded_project_id="proj-a")
        # 未导入（B 工程无该 asset；面板也无条目）
        assert project_b.get_asset_by_path(take.file_path) is None
        assert panel._table.rowCount() == 0


class TestArmWiringWithPanel:

    def test_arm_reads_panel_settings(self, qapp, tmp_path, monkeypatch):
        """arm 使用面板 input patch（设备/声道/pre-roll）。"""
        from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel
        panel = AdrTakeSheetPanel()
        panel._pre_roll_spin.setValue(0.0)  # 测试即时开始
        panel._channels_combo.setCurrentIndex(0)
        project = _make_project()
        ctrl = _make_controller(project, qapp, panel=panel)

        created = {}

        import src.app.controllers.edit_controller as ec

        class _FakeManager:
            def __init__(self, config):
                self.config = config
                self.on_take_completed = None
                self.on_error = None
                self.on_level = None
                self.on_state_changed = None
                self.on_monitor_block = None
                self.enable_live_post_roll = False

            @staticmethod
            def list_input_devices():
                # AUD-19403 后设备枚举延迟到事件循环期执行，面板在测试
                # 事件循环里可能扫到本替身 —— 与真实类接口保持一致。
                return []

            def start_recording(self, take_number=None, in_point_frame=None):
                created["in_point"] = in_point_frame
                return True

            def stop_recording(self):
                return None

            @property
            def is_recording(self):
                return False

        real_manager = ec.RecordingManager if hasattr(ec, "RecordingManager") else None
        import src.features.edit.core.recording_manager as rm
        monkeypatch.setattr(rm, "RecordingManager", _FakeManager)
        # edit_controller 内部是局部 import → patch 源模块即可生效

        ctrl._start_recording("trk")
        assert "trk" in ctrl._recording_managers
        mgr = ctrl._recording_managers["trk"]
        assert mgr.config.pre_roll_seconds == 0.0
        assert mgr.enable_live_post_roll is True
        assert mgr.on_state_changed.__func__ is panel.set_recording_state.__func__
        assert created.get("in_point") is None  # edit_page=None → 无打点

        ctrl._stop_recording_and_import("trk")
        assert "trk" not in ctrl._recording_managers
