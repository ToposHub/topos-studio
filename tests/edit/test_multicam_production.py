"""P1-01 多机位生产链路测试。

验收（计划 P1-01）：从多素材创建 Multicam Group，实时切换，切换点可编辑、
可撤销、可保存、重开后保持；波形自动同步 + 角度网格监看见
test_multicam_sync.py / test_multicam_panel_grid.py。

载体设计（asset-carried group）：组定义序列化进每个成员素材的 metadata；
时间线片段的 asset_id 始终指向当前角度的真实素材——渲染零改动，
split/trim/换源全部经既有命令保持组归属，素材 metadata 序列化天然持久。

已声明限制（capability_registry edit.multicam PARTIAL）：网格为 playhead
静帧（非多路实时解码）、无时间码/标记同步、音频不跟随切换、重同步不
追溯平移已放置片段。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.multicam import (
    METADATA_ASSET_GROUP_KEY,
    angle_by_asset_id,
    build_multicam_group_from_assets,
    group_with_sync_offsets,
    multicam_group_from_asset,
    stamp_multicam_group_on_assets,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


def _video_asset(asset_id: str, name: str, duration: int = 500) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/fake/{asset_id}.mp4", media_type="video",
        display_name=name, metadata={"duration_frames": duration},
    )


def _timeline(v_clips=()) -> TimelineSequence:
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=tuple(v_clips))
    return TimelineSequence(
        timeline_id="tl-mc", name="MC", fps=Rational(24, 1),
        video_tracks=(v1,), audio_tracks=(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 模型：创建 / 挂载 / 读取
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestGroupHelpers:

    def test_build_requires_two_assets(self):
        with pytest.raises(ValueError):
            build_multicam_group_from_assets(["only-one"])

    def test_build_order_defines_angle_keys(self):
        group = build_multicam_group_from_assets(
            ["a1", "a2", "a3"], {"a1": "Cam1", "a2": "Cam2"})
        assert [a.asset_id for a in group.angles] == ["a1", "a2", "a3"]
        assert group.angles[1].label == "Cam2"
        assert group.default_angle_id == group.angles[0].angle_id

    def test_stamp_and_read_round_trip(self):
        assets = (_video_asset("a1", "Cam1"), _video_asset("a2", "Cam2"))
        group = build_multicam_group_from_assets(
            ["a1", "a2"], {a.asset_id: a.display_name for a in assets})
        stamped = stamp_multicam_group_on_assets(assets, group)
        for original, new in zip(assets, stamped):
            assert new is not original
            assert new.metadata[METADATA_ASSET_GROUP_KEY]["group_id"] == group.group_id
            back = multicam_group_from_asset(new)
            assert back is not None and back.group_id == group.group_id
        # 原素材不受影响（stamp 是纯函数）
        assert multicam_group_from_asset(assets[0]) is None

    def test_non_member_and_corrupt_metadata(self):
        assert multicam_group_from_asset(_video_asset("x", "X")) is None
        corrupt = SimpleNamespace(metadata={METADATA_ASSET_GROUP_KEY: {"bad": 1}})
        assert multicam_group_from_asset(corrupt) is None

    def test_angle_by_asset_id(self):
        group = build_multicam_group_from_assets(["a1", "a2"])
        assert angle_by_asset_id(group, "a2").asset_id == "a2"
        assert angle_by_asset_id(group, "missing") is None


# ─────────────────────────────────────────────────────────────────────────────
# 切换语义：split+换源后组归属保持 + undo
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestSwitchKeepsGroup:

    def _project_with_group(self):
        assets = [_video_asset("a1", "Cam1", 500), _video_asset("a2", "Cam2", 500)]
        group = build_multicam_group_from_assets(
            ["a1", "a2"], {a.asset_id: a.display_name for a in assets})
        stamped = stamp_multicam_group_on_assets(assets, group)
        project = EditProject.create(name="mc")
        for a in stamped:
            project = project.with_media_asset_added(a)
        return project, group

    def test_switch_splits_and_group_survives(self):
        from src.features.edit.core.commands import (
            CommandStack,
            SwitchMulticamAngleCommand,
        )
        project, group = self._project_with_group()
        tl = _timeline([ClipInstance(
            clip_id="mc1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        cmd = SwitchMulticamAngleCommand.create(
            "v1", "mc1", 40, "a2", angle_label="Cam2")
        stack = CommandStack()
        new_tl = stack.execute(cmd, tl)

        clips = {c.clip_id: c for c in new_tl.get_track_by_id("v1").clips}
        # 切换点 40：右半指向角度 2 的素材
        right = next(c for c in clips.values()
                     if c.timeline_start == 40)
        assert right.asset_id == "a2"
        # 组归属经素材 metadata 保持：右半素材仍是组成员
        asset = project.assets.get(right.asset_id)
        restored = multicam_group_from_asset(asset)
        assert restored is not None and restored.group_id == group.group_id

        # undo：回到单一角度 1 片段
        undone = stack.undo(new_tl)
        clips_after = undone.get_track_by_id("v1").clips
        assert len(clips_after) == 1
        assert clips_after[0].asset_id == "a1"
        assert clips_after[0].timeline_duration == 100

    def test_switch_at_head_repoints_whole_clip(self):
        from src.features.edit.core.commands import CommandStack, SwitchMulticamAngleCommand
        project, group = self._project_with_group()
        tl = _timeline([ClipInstance(
            clip_id="mc1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        cmd = SwitchMulticamAngleCommand.create("v1", "mc1", 0, "a2")
        new_tl = CommandStack().execute(cmd, tl)
        clips = new_tl.get_track_by_id("v1").clips
        assert len(clips) == 1  # 片头切换不 split
        assert clips[0].asset_id == "a2"


# ─────────────────────────────────────────────────────────────────────────────
# 视图：playhead 多机位判定
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestViewMulticamDetection:

    def test_detects_multicam_under_playhead(self):
        from src.features.edit.ui.timeline_view import TimelineView
        assets = [_video_asset("a1", "Cam1"), _video_asset("a2", "Cam2")]
        group = build_multicam_group_from_assets(
            ["a1", "a2"], {a.asset_id: a.display_name for a in assets})
        stamped = stamp_multicam_group_on_assets(assets, group)
        project = EditProject.create(name="v")
        for a in stamped:
            project = project.with_media_asset_added(a)
        tl = _timeline([ClipInstance(
            clip_id="mc1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        view = SimpleNamespace(_timeline=tl, _project=project, _playhead_frame=50)
        assert TimelineView._multicam_clip_under_playhead(view) is True
        view._playhead_frame = 150  # playhead 超出片段
        assert TimelineView._multicam_clip_under_playhead(view) is False

    def test_plain_clip_not_detected(self):
        from src.features.edit.ui.timeline_view import TimelineView
        project = EditProject.create(name="v2")
        project = project.with_media_asset_added(_video_asset("plain", "P"))
        tl = _timeline([ClipInstance(
            clip_id="c1", asset_id="plain", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        view = SimpleNamespace(_timeline=tl, _project=project, _playhead_frame=10)
        assert TimelineView._multicam_clip_under_playhead(view) is False


# ─────────────────────────────────────────────────────────────────────────────
# 控制器：创建 / 键盘切换 / 面板刷新
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestControllerMulticamFlow:

    def _make_controller(self, project, timeline):
        from src.app.controllers.edit_controller import EditControllerMixin
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        ctrl._current_edit_project = project
        ctrl._current_timeline_sequences = {timeline.timeline_id: timeline}
        ctrl.edit_page = SimpleNamespace(_current_timeline=timeline, _current_frame=0)
        ctrl.status_bar = MagicMock()
        ctrl.media_pool_panel = MagicMock()

        executed = []

        class _FakeView:
            _timeline = timeline

            def execute_command(self, cmd):
                prepared_project = None
                if hasattr(cmd, "prepare_project"):
                    prepared_project, children = cmd.prepare_project(
                        ctrl._current_edit_project, ctrl._current_timeline_sequences)
                executed.append(cmd)
                current = ctrl.edit_page._current_timeline
                result = cmd.apply(current)
                if isinstance(result, tuple):
                    result = result[0]
                # 直接把新时间线发布回 edit_page（模拟真实视图信号链）
                ctrl.edit_page._current_timeline = result
                self._timeline = result
                if prepared_project is not None:
                    ctrl._current_edit_project = prepared_project
                    ctrl._current_timeline_sequences.update(children)
                return result

        view = _FakeView()
        panel = MagicMock()
        ctrl.multicam_panel = panel
        ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
        return ctrl, executed, panel

    def _group_project(self):
        assets = [_video_asset("a1", "Cam1", 400), _video_asset("a2", "Cam2", 500)]
        project = EditProject.create(name="ctrl-mc")
        for a in assets:
            project = project.with_media_asset_added(a)
        return project, assets

    def test_create_multicam_group_inserts_clip(self):
        project, assets = self._group_project()
        timeline = _timeline()
        ctrl, executed, panel = self._make_controller(project, timeline)

        ctrl._on_create_multicam_group(["a1", "a2"])

        from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand
        assert len(executed) == 1
        assert isinstance(executed[0], TimelineProjectTransactionCommand)
        # 成员素材被打上组 metadata（资产 id 不变，整条替换）
        new_project = ctrl._current_edit_project
        groups = [
            multicam_group_from_asset(a) for a in new_project.assets.values()
        ]
        assert sum(g is not None for g in groups) == 2
        # 执行后 playhead 处出现多机位片段（角度 1 素材）
        tl = ctrl.edit_page._current_timeline
        clips = tl.get_track_by_id("v1").clips
        assert len(clips) == 1
        assert clips[0].asset_id == "a1"
        assert clips[0].timeline_duration == 400  # min(400, 500)
        # 面板被刷新且展示了组
        panel.set_group.assert_called_once()
        args = panel.set_group.call_args[0]
        assert args[0] is not None and args[1] == "a1"

    def test_angle_key_switches(self):
        project, assets = self._group_project()
        timeline = _timeline([ClipInstance(
            clip_id="mc1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        # 预先打组（模拟创建流程已完成）
        group = build_multicam_group_from_assets(
            ["a1", "a2"], {a.asset_id: a.display_name for a in assets})
        stamped = stamp_multicam_group_on_assets(assets, group)
        for a in stamped:
            project = project.with_media_asset_added(a)

        ctrl, executed, panel = self._make_controller(project, timeline)
        ctrl._on_multicam_angle_key(2)

        from src.features.edit.core.commands import SwitchMulticamAngleCommand
        assert len(executed) == 1
        assert isinstance(executed[0], SwitchMulticamAngleCommand)
        assert executed[0].angle_asset_id == "a2"
        # 切换后 playhead(0) 片头重指角度 2
        tl = ctrl.edit_page._current_timeline
        assert tl.get_track_by_id("v1").clips[0].asset_id == "a2"
        # 面板高亮更新
        assert panel.set_group.call_args[0][1] == "a2"

    def test_angle_key_no_multicam_is_noop(self):
        project, assets = self._group_project()
        timeline = _timeline([ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        ctrl, executed, panel = self._make_controller(project, timeline)
        ctrl._on_multicam_angle_key(2)
        assert executed == []
        # 无多机位片段：不触碰面板
        panel.set_group.assert_not_called()

    def test_switch_to_same_angle_is_noop(self):
        project, assets = self._group_project()
        timeline = _timeline([ClipInstance(
            clip_id="mc1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        group = build_multicam_group_from_assets(
            ["a1", "a2"], {a.asset_id: a.display_name for a in assets})
        stamped = stamp_multicam_group_on_assets(assets, group)
        for a in stamped:
            project = project.with_media_asset_added(a)
        ctrl, executed, panel = self._make_controller(project, timeline)
        ctrl._on_multicam_angle_key(1)  # 已在角度 1
        assert executed == []

    def test_create_with_one_video_asset_shows_status(self):
        project, assets = self._group_project()
        timeline = _timeline()
        ctrl, executed, panel = self._make_controller(project, timeline)
        ctrl._on_create_multicam_group(["a1"])
        assert executed == []
        ctrl.status_bar.showMessage.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 控制器：波形同步编排 + 角度网格推帧
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
@pytest.mark.usefixtures("qapp")
class TestControllerMulticamSyncAndGrid:

    def _sync_project(self, tmp_path, offsets=None):
        """打组 + 真实路径素材（供 os.path.exists 检查）+ playhead 片段。"""
        def asset(aid, name):
            p = tmp_path / f"{aid}.mp4"
            p.write_bytes(b"\x00" * 16)
            return MediaAsset(
                asset_id=aid, path=str(p), media_type="video",
                display_name=name,
                metadata={"duration_frames": 400, "total_frames": 400,
                          "fps": 24.0},
            )

        assets = [asset("a1", "Cam1"), asset("a2", "Cam2")]
        group = build_multicam_group_from_assets(
            ["a1", "a2"], {a.asset_id: a.display_name for a in assets})
        if offsets:
            group = group_with_sync_offsets(group, offsets)
        stamped = stamp_multicam_group_on_assets(assets, group)
        project = EditProject.create(name="sync")
        for a in stamped:
            project = project.with_media_asset_added(a)
        timeline = _timeline([ClipInstance(
            clip_id="mc1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=400,
        )])
        return project, timeline, group

    def _make_controller(self, project, timeline):
        from src.app.controllers.edit_controller import EditControllerMixin
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        ctrl._current_edit_project = project
        ctrl.edit_page = SimpleNamespace(_current_timeline=timeline, _current_frame=50)
        ctrl.status_bar = MagicMock()
        ctrl.media_pool_panel = MagicMock()
        from src.features.edit.core.commands import ProjectCommandStack
        ctrl.media_pool_panel.get_command_stack.return_value = ProjectCommandStack()
        ctrl.multicam_panel = MagicMock()
        ctrl.timeline_panel = SimpleNamespace(
            get_timeline_view=lambda: MagicMock())
        return ctrl

    def test_sync_requested_starts_worker_and_locks_panel(self, tmp_path, monkeypatch):
        from PyQt6.QtCore import QObject, pyqtSignal

        project, timeline, group = self._sync_project(tmp_path)
        ctrl = self._make_controller(project, timeline)

        started = []

        class _FakeWorker(QObject):
            sync_finished = pyqtSignal(dict)
            sync_failed = pyqtSignal(str)
            finished = pyqtSignal()

            def __init__(self, jobs, ref, analysis_seconds=60.0,
                         max_shift_seconds=5.0, segment_starts=(), parent=None):
                super().__init__()
                self.jobs = jobs
                self.ref = ref

            def start(self):
                started.append(self)

            def deleteLater(self):  # noqa: N802
                pass

        import src.features.edit.core.multicam_sync as sync_mod
        monkeypatch.setattr(sync_mod, "MulticamSyncWorker", _FakeWorker)

        ctrl._on_multicam_sync_requested("a1")

        assert len(started) == 1
        worker = started[0]
        assert worker.ref == "a1"
        assert [j.asset_id for j in worker.jobs] == ["a1", "a2"]
        assert ctrl._multicam_sync_worker is worker
        ctrl.multicam_panel.set_sync_running.assert_called_once_with(True)

        # 同步运行中：重复请求不再起新 worker
        ctrl._on_multicam_sync_requested("a1")
        assert len(started) == 1

    def test_sync_done_writes_offsets_back_to_group(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QDialog

        from src.features.edit.ui.multicam_sync_result_dialog import (
            MulticamSyncResultDialog,
        )

        project, timeline, group = self._sync_project(tmp_path)
        ctrl = self._make_controller(project, timeline)
        # C4: 同步结果需用户确认——模拟用户点"应用"
        monkeypatch.setattr(
            MulticamSyncResultDialog, "exec",
            lambda self: QDialog.DialogCode.Accepted,
        )

        ctrl._on_multicam_sync_done({
            "a1": {"offset_frames": 0, "lag_seconds": 0.0,
                   "confidence": 1.0, "error": ""},
            "a2": {"offset_frames": 30, "lag_seconds": 1.25,
                   "confidence": 0.91, "error": ""},
        })

        # 组偏移写回成员素材 metadata
        restored = multicam_group_from_asset(
            ctrl._current_edit_project.assets["a2"])
        assert restored is not None
        assert angle_by_asset_id(restored, "a2").sync_offset == 30
        assert angle_by_asset_id(restored, "a1").sync_offset == 0
        # 面板解锁并刷新
        ctrl.multicam_panel.set_sync_running.assert_called_with(False)
        ctrl.multicam_panel.set_group.assert_called()
        # 状态栏给出摘要
        text = ctrl.status_bar.showMessage.call_args[0][0]
        assert "+30f" in text and "Cam2" in text
        assert ctrl._multicam_sync_worker is None

    def test_sync_done_cancelled_discards_offsets(self, tmp_path, monkeypatch):
        """C4: 用户在确认对话框取消 → 不写回偏移。"""
        from PyQt6.QtWidgets import QDialog

        from src.features.edit.ui.multicam_sync_result_dialog import (
            MulticamSyncResultDialog,
        )

        project, timeline, group = self._sync_project(tmp_path)
        ctrl = self._make_controller(project, timeline)
        monkeypatch.setattr(
            MulticamSyncResultDialog, "exec",
            lambda self: QDialog.DialogCode.Rejected,
        )

        ctrl._on_multicam_sync_done({
            "a1": {"offset_frames": 0, "lag_seconds": 0.0,
                   "confidence": 1.0, "error": ""},
            "a2": {"offset_frames": 30, "lag_seconds": 1.25,
                   "confidence": 0.91, "error": ""},
        })
        restored = multicam_group_from_asset(
            ctrl._current_edit_project.assets["a2"])
        assert angle_by_asset_id(restored, "a2").sync_offset == 0

    def test_sync_done_with_error_reports_failure(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QDialog

        from src.features.edit.ui.multicam_sync_result_dialog import (
            MulticamSyncResultDialog,
        )

        project, timeline, group = self._sync_project(tmp_path)
        ctrl = self._make_controller(project, timeline)
        monkeypatch.setattr(
            MulticamSyncResultDialog, "exec",
            lambda self: QDialog.DialogCode.Accepted,
        )
        ctrl._on_multicam_sync_done({
            "a1": {"offset_frames": 0, "lag_seconds": 0.0,
                   "confidence": 1.0, "error": ""},
            "a2": {"offset_frames": 0, "lag_seconds": 0.0,
                   "confidence": 0.0, "error": "no_audio_stream"},
        })
        restored = multicam_group_from_asset(
            ctrl._current_edit_project.assets["a2"])
        assert angle_by_asset_id(restored, "a2").sync_offset == 0
        text = ctrl.status_bar.showMessage.call_args[0][0]
        assert "失败" in text

    def test_sync_failed_shows_status(self, tmp_path):
        project, timeline, group = self._sync_project(tmp_path)
        ctrl = self._make_controller(project, timeline)
        ctrl._on_multicam_sync_failed("no_audio_stream")
        ctrl.multicam_panel.set_sync_running.assert_called_with(False)
        text = ctrl.status_bar.showMessage.call_args[0][0]
        assert "no_audio_stream" in text

    def test_playhead_frame_maps_all_angles(self, tmp_path):
        from src.features.edit.core.multicam import resolve_angle_source_frame
        project, timeline, group = self._sync_project(
            tmp_path, offsets={"a2": 24})
        ctrl = self._make_controller(project, timeline)

        ctrl._on_multicam_playhead_frame(60)

        ctrl.multicam_panel.request_angle_frames.assert_called_once()
        frames, infos = ctrl.multicam_panel.request_angle_frames.call_args[0]
        # playhead=60, clip start 0, active(a1, offset 0) → a1 显示 60；
        # a2 offset +24 → 与切换命令一致读 84
        assert frames["a1"] == 60
        assert frames["a2"] == 84
        assert infos["a1"]["total_frames"] == 400

    def test_playhead_frame_no_multicam_is_noop(self, tmp_path):
        project = EditProject.create(name="plain")
        project = project.with_media_asset_added(_video_asset("plain", "P"))
        timeline = _timeline([ClipInstance(
            clip_id="c1", asset_id="plain", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=500,
        )])
        ctrl = self._make_controller(project, timeline)
        ctrl._on_multicam_playhead_frame(10)
        ctrl.multicam_panel.request_angle_frames.assert_not_called()

    def test_refresh_panel_passes_asset_infos(self, tmp_path):
        project, timeline, group = self._sync_project(tmp_path)
        ctrl = self._make_controller(project, timeline)
        ctrl._refresh_multicam_panel()
        ctrl.multicam_panel.set_group.assert_called_once()
        args = ctrl.multicam_panel.set_group.call_args[0]
        assert args[0] is not None and args[1] == "a1"
        infos = args[2]
        assert infos["a2"]["total_frames"] == 400
        assert infos["a1"]["path"].endswith("a1.mp4")
