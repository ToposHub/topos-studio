"""P1-10 ADR 录音链路核心测试。

验收（计划表 P1-10）：
- rolling pre-roll：文件包含 pre-roll 音频，首样本时间基准前移。
- post-roll：设备在采时停止 → 先收尾再落盘（单一完成事件源不变）。
- 延迟补偿：校准偏移写进 bext TimeReference，音视频对齐 ≤10ms。
- 时间码：take.start_timecode 与 TimeReference 一致（SMPTE DF/NDF）。
- take sheet：多 take 列表 / 最佳 take 互斥 / 工程重建。
- 一次 Undo：ImportMediaAssetCommand / MarkBestTakeCommand 可撤销。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.recording_manager import (
    RecordingConfig,
    RecordingManager,
    TakeInfo,
    calibrate_latency,
    compute_time_reference,
    default_click_template,
    estimate_click_offset_ms,
    is_drop_frame_rate,
    write_take_wav,
)
from src.features.edit.core.take_sheet import (
    TakeSheet,
    build_take_sheet_from_project,
    take_metadata_for_asset,
)

SR = 48000


def _sine(amp: float, freq: float, duration: float) -> np.ndarray:
    n = int(SR * duration)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32).reshape(1, -1)


# ─────────────────────────────────────────────────────────────────────────────
# 时间基准 / 延迟补偿（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeTimeReference:
    """compute_time_reference：in-point → TimeReference + SMPTE TC。"""

    def test_no_preroll_no_latency(self):
        samples, tc = compute_time_reference(100, 25.0, SR)
        assert samples == round(100 / 25.0 * SR)  # 192000
        assert tc == "00:00:04:00"

    def test_preroll_and_latency_shift_first_sample(self):
        # in-point=100 帧 @25fps；pre-roll 2s + 延迟 20ms → 前移 50.5 帧
        samples, tc = compute_time_reference(
            100, 25.0, SR, pre_roll_captured_seconds=2.0, latency_ms=20.0)
        assert samples == round(49.5 / 25.0 * SR)  # 95040
        assert tc == "00:00:02:00"  # round(49.5)=50

    def test_sequence_start_offset(self):
        samples, tc = compute_time_reference(
            0, 25.0, SR, sequence_start_frame=3600 * 25)  # 01:00:00:00 起
        assert tc == "01:00:00:00"
        assert samples == 3600 * SR

    def test_clamps_negative_to_zero(self):
        samples, tc = compute_time_reference(
            10, 25.0, SR, pre_roll_captured_seconds=5.0)
        assert samples == 0
        assert tc == "00:00:00:00"

    def test_invalid_fps_returns_zero(self):
        assert compute_time_reference(100, None, SR) == (0, "")
        assert compute_time_reference(100, 0, SR) == (0, "")

    def test_rational_fps_and_drop_frame(self):
        from fractions import Fraction
        fps = Fraction(30000, 1001)
        assert is_drop_frame_rate(fps) is True
        assert is_drop_frame_rate(25.0) is False
        # 一小时 29.97DF ≈ 107892 帧（drop 编号），TC 显示 00:59:59;xx 附近合法
        samples, tc = compute_time_reference(0, fps, SR)
        assert tc == "00:00:00;00"
        assert ";" in tc

    def test_take_metadata_written_at_start(self, tmp_path):
        """start_recording(in_point_frame=...) 直接填好 take 时间字段。"""
        cfg = RecordingConfig(
            output_dir=str(tmp_path), pre_roll_seconds=0, fps=25.0,
        )
        mgr = RecordingManager(cfg)
        mgr.start_recording(in_point_frame=125)
        take = mgr._current_take
        assert take.in_point_frame == 125
        assert take.time_reference_samples == round(125 / 25.0 * SR)
        assert take.start_timecode == "00:00:05:00"
        mgr.cancel_recording()

    def test_calibration_overrides_config_latency(self, tmp_path):
        """set_calibration 的校准结果优先于 config.latency_ms。"""
        cfg = RecordingConfig(
            output_dir=str(tmp_path), pre_roll_seconds=0, fps=25.0,
            latency_ms=50.0,
        )
        mgr = RecordingManager(cfg)
        mgr.set_calibration(calibrate_latency(20.0))  # input=10ms
        mgr.start_recording(in_point_frame=1000)
        take = mgr._current_take
        # 前移 10ms → 0.25 帧 → first≈999.75 → samples=round(999.75/25*SR)
        assert take.time_reference_samples == round(999.75 / 25.0 * SR)
        mgr.cancel_recording()

    def test_bwf_time_reference_written_to_file(self, tmp_path):
        """TimeReference 落进 bext（外部 DAW 按时间码对齐）。"""
        from src.features.edit.core.bwf import read_bext_chunk
        cfg = RecordingConfig(
            output_dir=str(tmp_path), pre_roll_seconds=0, fps=25.0)
        mgr = RecordingManager(cfg)
        mgr.start_recording(in_point_frame=250)
        take = mgr._current_take
        expected_ref = take.time_reference_samples
        mgr.inject_pcm(_sine(0.5, 440, 0.1))
        completed = mgr.stop_recording()
        assert completed.status == "completed"
        bext = read_bext_chunk(completed.file_path)
        assert bext is not None
        assert bext.time_reference_samples == expected_ref == round(10.0 * SR)


# ─────────────────────────────────────────────────────────────────────────────
# Rolling pre-roll：倒计时期间采集并入 take
# ─────────────────────────────────────────────────────────────────────────────


class TestRollingPreRoll:

    def test_preroll_audio_included_in_take(self, tmp_path):
        """pre-roll 期间注入的音频最终写进文件（专业 ADR pre-roll）。"""
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=2.0)
        mgr = RecordingManager(cfg)
        mgr.start_recording(in_point_frame=None)
        assert mgr.state == "pre_roll"
        mgr.inject_pcm(_sine(0.4, 440, 1.5))  # pre-roll 内容进 rolling
        assert mgr._rolling_samples == int(1.5 * SR)
        mgr._begin_capture()  # 倒计时到点
        assert mgr.state == "recording"
        assert mgr._pcm_buffer is not None
        assert mgr._pcm_buffer.shape[1] == int(1.5 * SR)
        mgr.inject_pcm(_sine(0.4, 880, 0.5))  # 正式录制内容
        take = mgr.stop_recording()
        assert take.status == "completed"
        # 文件包含 pre-roll + 正文 = 2.0s
        assert abs(take.duration_seconds - 2.0) < 0.01

    def test_rolling_keeps_only_last_window(self, tmp_path):
        """rolling 超窗数据在并入 take 时裁剪到最近 pre_roll_seconds。"""
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=2.0)
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.3, 440, 3.5))  # 超过 2s 窗口
        assert mgr._rolling_samples == int(3.5 * SR)  # 整块淘汰，未到最后一块
        mgr._begin_capture()  # splice 时裁剪到最后 2s
        assert mgr._pcm_buffer.shape[1] == int(2.0 * SR)
        mgr.cancel_recording()

    def test_no_preroll_config_unchanged(self, tmp_path):
        """pre_roll=0 时行为与旧版一致（无 rolling）。"""
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.3))
        take = mgr.stop_recording()
        assert abs(take.duration_seconds - 0.3) < 0.01

    def test_stop_during_preroll_finalizes_immediately(self, tmp_path):
        """pre-roll 中停止：立即落盘（不等待），空 take 归档。"""
        cfg = RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=2.0)
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        take = mgr.stop_recording()
        assert take is not None
        assert mgr.state == "idle"


# ─────────────────────────────────────────────────────────────────────────────
# Post-roll：停止后继续采集尾巴
# ─────────────────────────────────────────────────────────────────────────────


class TestPostRoll:

    def test_postroll_delays_finalize_and_captures_tail(self, tmp_path, qapp):
        """enable_live_post_roll 时停止 → stopping 采集尾巴 → 定时到落盘。"""
        cfg = RecordingConfig(
            output_dir=str(tmp_path), pre_roll_seconds=0,
            post_roll_seconds=1.0,
        )
        mgr = RecordingManager(cfg)
        mgr.enable_live_post_roll = True
        completed = []
        mgr.on_take_completed = completed.append
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.4))
        assert mgr._io_device is not None  # 本机 offscreen 可启动设备
        take = mgr.stop_recording()
        assert take is None  # 延迟落盘
        assert mgr.state == "stopping"
        mgr.inject_pcm(_sine(0.5, 880, 0.6))  # post-roll 尾巴（stopping 继续采）
        mgr._on_post_roll_finished()
        assert mgr.state == "idle"
        assert len(completed) == 1  # 单一完成事件源
        assert abs(completed[0].duration_seconds - 1.0) < 0.01

    def test_postroll_disabled_keeps_sync_stop(self, tmp_path):
        """默认（未启用 live post-roll）保持同步 stop 返回 take。"""
        cfg = RecordingConfig(
            output_dir=str(tmp_path), pre_roll_seconds=0,
            post_roll_seconds=1.0,
        )
        mgr = RecordingManager(cfg)
        mgr.start_recording()
        mgr.inject_pcm(_sine(0.5, 440, 0.2))
        take = mgr.stop_recording()
        assert take is not None and take.status == "completed"


# ─────────────────────────────────────────────────────────────────────────────
# 输入监听回调（on_monitor_block）
# ─────────────────────────────────────────────────────────────────────────────


class _FakeIODevice:
    """QIODevice 替身：吐出固定 PCM 块。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def readAll(self):
        from PyQt6.QtCore import QByteArray
        return QByteArray(self._payload)


class TestInputMonitor:

    def test_monitor_block_forwarded_during_preroll(self):
        """pre-roll 期间实时块转发给监听回调（演员要提前听到输入）。"""
        cfg = RecordingConfig(pre_roll_seconds=2.0)
        mgr = RecordingManager(cfg)
        monitored = []
        mgr.on_monitor_block = monitored.append
        block = _sine(0.3, 440, 0.05)
        mgr._io_device = _FakeIODevice(block.astype(np.float32).tobytes())
        mgr._state = RecordingManager.STATE_PRE_ROLL
        mgr._on_audio_ready()
        assert len(monitored) == 1
        np.testing.assert_array_equal(monitored[0], block)
        assert mgr._rolling_samples == block.shape[1]  # 同时进 rolling
        mgr.cancel_recording()

    def test_monitor_block_forwarded_while_recording(self):
        cfg = RecordingConfig(pre_roll_seconds=0)
        mgr = RecordingManager(cfg)
        monitored = []
        mgr.on_monitor_block = monitored.append
        block = _sine(0.3, 440, 0.05)
        mgr._io_device = _FakeIODevice(block.astype(np.float32).tobytes())
        mgr._state = RecordingManager.STATE_RECORDING
        mgr._current_take = TakeInfo()
        mgr._on_audio_ready()
        assert len(monitored) == 1
        assert mgr._pcm_buffer is not None
        assert mgr._pcm_buffer.shape[1] == block.shape[1]


# ─────────────────────────────────────────────────────────────────────────────
# 校准测量：click 偏移估计
# ─────────────────────────────────────────────────────────────────────────────


class TestClickOffsetEstimator:

    def _synth_capture(self, click_at_s: float, total_s: float = 1.0):
        rng = np.random.default_rng(7)
        n = int(SR * total_s)
        signal = (rng.standard_normal(n) * 0.01).astype(np.float32)
        click = default_click_template(SR)
        start = int(click_at_s * SR)
        signal[start:start + click.size] += click
        return signal, click

    def test_estimates_known_offset_within_2ms(self):
        signal, click = self._synth_capture(0.5)
        offset = estimate_click_offset_ms(signal, SR, click)
        assert offset is not None
        assert abs(offset - 500.0) < 2.0  # 远小于 10ms 验收阈值

    def test_no_click_returns_none(self):
        rng = np.random.default_rng(3)
        noise = (rng.standard_normal(int(SR * 0.5)) * 0.005).astype(np.float32)
        assert estimate_click_offset_ms(noise, SR) is None

    def test_multichannel_input_mixdown(self):
        signal, click = self._synth_capture(0.25)
        stereo = np.stack([signal, signal])
        offset = estimate_click_offset_ms(stereo, SR, click)
        assert offset is not None
        assert abs(offset - 250.0) < 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Take Sheet
# ─────────────────────────────────────────────────────────────────────────────


def _completed_take(number: int, scene: str = "S1") -> TakeInfo:
    take = TakeInfo(
        take_number=number, name=f"{scene}_Take{number:02d}",
        scene_name=scene, status="completed",
        duration_seconds=2.0 + number,
        peak_dbfs=-6.0 - number,
        start_timecode="00:00:10:00", time_reference_samples=480000,
        in_point_frame=250, start_timestamp="2026-08-27T10:00:00",
    )
    return take


class TestTakeSheet:

    def test_add_and_lookup(self):
        sheet = TakeSheet()
        t1 = _completed_take(1)
        t2 = _completed_take(2)
        sheet.add_take(t1, asset_id="a1", track_id="trk")
        sheet.add_take(t2, asset_id="a2")
        assert len(sheet) == 2
        assert sheet.entry_for_asset("a2").take_id == t2.take_id
        assert sheet.entry_for_take(t1.take_id).track_id == "trk"

    def test_add_is_idempotent_by_take_id(self):
        sheet = TakeSheet()
        t1 = _completed_take(1)
        sheet.add_take(t1, asset_id="a1")
        t1.duration_seconds = 9.9
        sheet.add_take(t1, asset_id="a1")
        assert len(sheet) == 2 - 1
        assert sheet.entries[0].duration_seconds == 9.9

    def test_mark_best_exclusive_per_scene(self):
        sheet = TakeSheet()
        t1 = _completed_take(1)
        t2 = _completed_take(2)
        t3 = _completed_take(3, scene="S2")
        sheet.add_take(t1)
        sheet.add_take(t2)
        sheet.add_take(t3)
        assert sheet.mark_best(t1.take_id) is True
        assert sheet.mark_best(t2.take_id) is True  # 换最佳
        assert sheet.best_take("S1").take_id == t2.take_id
        assert sheet.entry_for_take(t1.take_id).is_best is False
        # 其他场次不受影响
        assert sheet.best_take("S2") is None
        assert sheet.mark_best("nope") is False

    def test_serialization_roundtrip(self):
        sheet = TakeSheet()
        sheet.add_take(_completed_take(1), asset_id="a1")
        sheet.mark_best(sheet.entries[0].take_id)
        restored = TakeSheet.from_dict(sheet.to_dict())
        assert len(restored) == 1
        assert restored.entries[0].is_best is True
        assert restored.entries[0].start_timecode == "00:00:10:00"


class TestTakeSheetProjectRebuild:
    """take sheet 持久化随 asset metadata，重开工程可重建。"""

    def _project_with_take(self, tmp_path, scene="S1", mark_best=False):
        from src.features.edit.core.project import EditProject
        take = _completed_take(1, scene=scene)
        path = str(tmp_path / f"{take.name}.wav")
        assert write_take_wav(path, _sine(0.5, 440, 0.2), SR, 24, take)
        project = EditProject.create(name="adr")
        metadata = take_metadata_for_asset(take)
        if mark_best:
            metadata["is_best_take"] = True
        return project.import_asset(
            path=path, media_type="audio", metadata=metadata,
            display_name=take.name), take

    def test_rebuild_from_project(self, tmp_path):
        project, take = self._project_with_take(tmp_path, mark_best=True)
        sheet = build_take_sheet_from_project(project)
        assert len(sheet) == 1
        entry = sheet.entries[0]
        assert entry.take_id == take.take_id
        assert entry.scene_name == "S1"
        assert entry.is_best is True
        assert entry.start_timecode == "00:00:10:00"
        assert entry.time_reference_samples == 480000

    def test_non_recording_assets_ignored(self, tmp_path):
        from src.features.edit.core.project import EditProject
        project, _ = self._project_with_take(tmp_path)
        # 再导入一个普通素材（非录音）
        other = str(tmp_path / "plain.wav")
        assert write_take_wav(other, _sine(0.3, 440, 0.1))
        project = project.import_asset(
            path=other, media_type="audio", metadata={})
        sheet = build_take_sheet_from_project(project)
        assert len(sheet) == 1

    def test_json_serializable_metadata(self):
        """asset metadata 里的 take 字段可 JSON 序列化（工程保存依赖）。"""
        import json
        take = _completed_take(1)
        take.peak_dbfs = float("-inf")
        payload = json.dumps(take_metadata_for_asset(take))
        assert "Infinity" not in payload
        assert json.loads(payload)["scene"] == "S1"


# ─────────────────────────────────────────────────────────────────────────────
# 命令：一次 Undo 的导入 / 最佳 take 标记
# ─────────────────────────────────────────────────────────────────────────────


class TestImportMediaAssetCommand:

    def _project(self):
        from src.features.edit.core.project import EditProject
        return EditProject.create(name="cmd")

    def _take_file(self, tmp_path, name="take_cmd"):
        path = str(tmp_path / f"{name}.wav")
        assert write_take_wav(path, _sine(0.5, 440, 0.2))
        return path

    def test_apply_undo_redo_roundtrip(self, tmp_path):
        from src.features.edit.core.commands import ImportMediaAssetCommand
        project = self._project()
        path = self._take_file(tmp_path)
        cmd = ImportMediaAssetCommand.create(
            path, "audio", metadata={"is_recording": True}, label="Record Take")
        after_apply = cmd.apply(project)
        assert len(after_apply.assets) == len(project.assets) + 1
        asset = after_apply.get_asset_by_path(path)
        assert asset is not None
        assert asset.metadata["is_recording"] is True

        after_undo = cmd.undo(after_apply)
        assert path not in [a.path for a in after_undo.assets.values()]
        # redo 幂等：再次 apply 恢复
        after_redo = cmd.apply(after_undo)
        assert after_redo.get_asset_by_path(path) is not None

    def test_dedupe_apply_is_noop_and_undo_safe(self, tmp_path):
        from src.features.edit.core.commands import ImportMediaAssetCommand
        project = self._project()
        path = self._take_file(tmp_path)
        once = ImportMediaAssetCommand.create(path, "audio").apply(project)
        # 第二次（路径已存在）：no-op
        dup = ImportMediaAssetCommand.create(path, "audio")
        same = dup.apply(once)
        assert same is once
        # no-op 的 undo 不会误删他人 asset
        after = dup.undo(once)
        assert after.get_asset_by_path(path) is not None

    def test_stack_single_undo_removes_take(self, tmp_path):
        """验收：R/停止 → 一个 asset + 一次 Undo。"""
        from src.features.edit.core.commands import (
            ImportMediaAssetCommand, ProjectCommandStack,
        )
        stack = ProjectCommandStack()
        project = self._project()
        path = self._take_file(tmp_path)
        cmd = ImportMediaAssetCommand.create(path, "audio", label="Record Take")
        project = stack.execute(cmd, project)
        assert len(project.assets) == 1
        project = stack.undo(project)
        assert len(project.assets) == 0


class TestMarkBestTakeCommand:

    def _project_with_takes(self, tmp_path, scenes=("S1", "S1", "S2")):
        from src.features.edit.core.project import EditProject
        project = EditProject.create(name="best")
        ids = []
        for i, scene in enumerate(scenes):
            take = _completed_take(i + 1, scene=scene)
            path = str(tmp_path / f"take{i}.wav")
            assert write_take_wav(path, _sine(0.4, 440, 0.1), SR, 24, take)
            project = project.import_asset(
                path=path, media_type="audio",
                metadata=take_metadata_for_asset(take))
            ids.append(project.get_asset_by_path(path).asset_id)
        return project, ids

    def test_mark_best_exclusive_and_undoable(self, tmp_path):
        from src.features.edit.core.commands import (
            MarkBestTakeCommand, ProjectCommandStack,
        )
        project, ids = self._project_with_takes(tmp_path)
        stack = ProjectCommandStack()

        cmd1 = MarkBestTakeCommand.create(ids[0])
        project = stack.execute(cmd1, project)
        assert project.assets[ids[0]].metadata["is_best_take"] is True

        # 换最佳：S1 内互斥
        cmd2 = MarkBestTakeCommand.create(ids[1])
        project = stack.execute(cmd2, project)
        assert "is_best_take" not in project.assets[ids[0]].metadata
        assert project.assets[ids[1]].metadata["is_best_take"] is True
        # 其他场次（S2）不受影响
        assert "is_best_take" not in project.assets[ids[2]].metadata

        # 两次 undo 回到初始
        project = stack.undo(project)  # 撤销 cmd2
        assert project.assets[ids[0]].metadata["is_best_take"] is True
        project = stack.undo(project)  # 撤销 cmd1
        assert all("is_best_take" not in a.metadata for a in project.assets.values())

    def test_missing_asset_is_noop(self, tmp_path):
        from src.features.edit.core.commands import MarkBestTakeCommand
        project, _ = self._project_with_takes(tmp_path)
        cmd = MarkBestTakeCommand.create("nonexistent")
        assert cmd.apply(project) is project


# ─────────────────────────────────────────────────────────────────────────────
# RecordingConfig 序列化新增字段
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigSerialization:

    def test_new_fields_serialized(self):
        cfg = RecordingConfig(fps=25.0, sequence_start_frame=100,
                              latency_ms=12.5)
        d = cfg.to_dict()
        assert d["fps"] == 25.0
        assert d["sequence_start_frame"] == 100
        assert d["latency_ms"] == 12.5

    def test_rational_fps_serializes_to_float(self):
        from fractions import Fraction
        d = RecordingConfig(fps=Fraction(30000, 1001)).to_dict()
        assert abs(d["fps"] - 30000.0 / 1001.0) < 1e-9

    def test_takeinfo_new_fields_serialized(self):
        d = TakeInfo(in_point_frame=50, start_timecode="00:00:02:00").to_dict()
        assert d["in_point_frame"] == 50
        assert d["start_timecode"] == "00:00:02:00"
