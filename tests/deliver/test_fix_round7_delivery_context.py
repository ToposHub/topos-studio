"""第七轮审计 0A/0C 回归：交付上下文一致性（R01/R02）。

R01：Deliver 页内继续编辑（Split/Delete/Undo/Redo）后，live 序列更新
     必须同时刷新 settings panel 的 _timeline_sequences map、batch
     controller 的 _sequences_by_id 和 JobContext——旧实现只更新 panel
     的 _sequence，draft/preflight 读旧 map、实际导出走 controller 的
     旧对象，任务 done 但成片是编辑前的旧时间线（100 帧删成 50 帧仍
     导出 100 帧）。任务记录的 project_revision 也要与工程实时一致。

R02：队列恢复先于宿主 promote 完整序列执行时，存在的序列被误标
     needs_relink；序列登记后必须重评估恢复 pending，真正离线的目标
     保持失联。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication

from src.features.deliver.core.export_queue import JobStatus
from src.features.deliver.core.video_batch_export_controller import (
    VideoBatchExportController,
)
from src.features.deliver.core.video_export_job import (
    AudioConfig,
    VideoConfig,
    VideoExportJob,
)
from src.features.edit.core.project import TimelineSequenceRef
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _sequence(timeline_id: str, name: str, duration: int) -> TimelineSequence:
    clip = ClipInstance(
        clip_id=f"{timeline_id}-v0", asset_id="asset-video",
        track_id=f"{timeline_id}-vt0", timeline_start=0,
        timeline_duration=duration, source_start=0, source_duration=duration,
    )
    track = EditTrack(
        track_id=f"{timeline_id}-vt0", name="V1", track_type="video",
        index=0, clips=(clip,),
    )
    return TimelineSequence(
        timeline_id=timeline_id, name=name, width=1920, height=1080,
        fps=Rational(25, 1), video_tracks=(track,),
    )


def _project(revision: int = 1):
    return SimpleNamespace(
        project_id="p1", name="Proj", revision=revision,
        timelines={
            "seq-a": TimelineSequenceRef(timeline_id="seq-a", name="A", fps=25.0),
            "seq-b": TimelineSequenceRef(timeline_id="seq-b", name="B", fps=25.0),
        },
        active_timeline=None, sequences=None,
    )


# ── R01：Deliver 页内编辑同步 draft/map/队列上下文 ──


class TestR01SharedTimelineEditSync:
    def test_edit_on_deliver_page_updates_all_sequence_contexts(self, qapp):
        """真实复现链：进入 Deliver（attach）→ live 时间线剪短 →
        draft/preflight/controller 必须全部指向新序列。"""
        from src.features.deliver.ui.deliver_workspace import DeliverWorkspace

        workspace = DeliverWorkspace()
        try:
            seq_b_old = _sequence("seq-b", "B", 100)
            seq_a = _sequence("seq-a", "A", 50)
            workspace.set_project(_project())
            workspace.set_timeline_sequences(
                {"seq-a": seq_a, "seq-b": seq_b_old})
            workspace.set_sequence(seq_b_old)
            # 模拟共享时间线面板 attach 后页内编辑（Split+Delete）：
            # 同 id 的修订序列（100 → 50 帧）。
            workspace._edit_panels_attached = True
            seq_b_new = _sequence("seq-b", "B", 50)
            workspace._on_shared_timeline_changed(seq_b_new)

            panel = workspace._settings_panel
            # draft/preflight 解析到新序列（map 不再持旧 100 帧对象）。
            assert panel._sequence_for_combo_selection() is seq_b_new
            assert panel._timeline_sequences["seq-b"] is seq_b_new
            assert panel._effective_timeline_frame_count(
                panel._sequence_for_combo_selection()) == 50

            # 队列上下文（运行时读取）也指向新序列。
            controller = workspace._controller
            assert controller._sequences_by_id["seq-b"] is seq_b_new
            job = VideoExportJob(
                timeline_id="seq-b", output_path="/tmp/r01.mov",
                video_config=VideoConfig(codec="prores_422", container="mov"),
                audio_config=AudioConfig(include_audio=False),
            )
            ctx = controller._resolve_context(job)
            assert ctx.is_resolvable
            assert ctx.sequence is seq_b_new
        finally:
            workspace.close()

    def test_detached_page_does_not_track_edits(self, qapp):
        """未 attach 时保持旧语义（不响应共享时间线变化）。"""
        from src.features.deliver.ui.deliver_workspace import DeliverWorkspace

        workspace = DeliverWorkspace()
        try:
            seq_b_old = _sequence("seq-b", "B", 100)
            workspace.set_project(_project())
            workspace.set_timeline_sequences({"seq-b": seq_b_old})
            workspace.set_sequence(seq_b_old)
            workspace._edit_panels_attached = False
            workspace._on_shared_timeline_changed(_sequence("seq-b", "B", 50))
            assert workspace._controller._sequences_by_id.get("seq-b") is seq_b_old
        finally:
            workspace.close()

    def test_build_jobs_stamps_live_revision(self, qapp, tmp_path):
        """编辑提交后 revision 递增，任务记录不得沿用 set_project 缓存值。"""
        from src.features.deliver.ui.video_export_settings_panel import (
            VideoExportSettingsPanel,
        )

        panel = VideoExportSettingsPanel()
        try:
            project = _project(revision=3)
            seq = _sequence("seq-a", "A", 100)
            panel.set_project(project, project_id="p1", project_revision=3,
                              project_name="Proj")
            panel.set_timelines([("seq-a", "A")])
            panel.set_timeline_sequences({"seq-a": seq})
            panel.set_sequence(seq)
            panel._output_dir_edit.setText(str(tmp_path))

            jobs, _ = panel.build_jobs(preflight=False)
            assert jobs and jobs[0].project_revision == 3

            # 页内编辑提交：revision 递增到 7。
            project.revision = 7
            jobs2, _ = panel.build_jobs(preflight=False)
            assert jobs2 and jobs2[0].project_revision == 7, \
                "任务 revision 必须实时读取活动工程"
        finally:
            panel.close()

    def test_resolve_context_reads_live_revision(self):
        controller = VideoBatchExportController()
        project = _project(revision=2)
        seq = _sequence("seq-a", "A", 100)
        controller.set_active_project(
            project, project_id="p1", project_revision=2, sequences=[seq])
        project.revision = 9
        job = VideoExportJob(
            timeline_id="seq-a", output_path="/tmp/r01b.mov",
            video_config=VideoConfig(codec="prores_422", container="mov"),
        )
        ctx = controller._resolve_context(job)
        assert ctx.is_resolvable
        assert ctx.project_revision == 9


# ── R02：序列登记重评估 needs_relink ──


class TestR02QueueRelinkOnRegister:
    def _job(self, timeline_id: str) -> VideoExportJob:
        return VideoExportJob(
            timeline_id=timeline_id, timeline_name=timeline_id,
            output_path="/tmp/r02.mov",
            video_config=VideoConfig(codec="prores_422", container="mov"),
        )

    def test_register_sequence_relinks_existing_target(self, qapp, tmp_path, monkeypatch):
        """恢复先于 promote：t1 被误标 needs_relink；register_sequence(t1)
        后恢复 pending（可自动运行），不能要求用户手动重连。"""
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
        controller = VideoBatchExportController()
        try:
            controller.set_active_project(
                _project(), project_id="p1", project_revision=1, sequences=[])
            legacy = self._job("t1").to_legacy_job()
            legacy.status = JobStatus.NEEDS_RELINK
            legacy.error = "Timeline not found in the current project"
            controller.queue.enqueue(legacy, preserve_status=True)
            assert controller.snapshot_jobs()[0].status == "needs_relink"

            # 宿主随后 promote 完整序列 t1。
            controller.register_sequence(_sequence("t1", "T1", 100))
            snap = controller.snapshot_jobs()
            assert snap[0].status == "pending"
            assert not snap[0].error
        finally:
            for legacy in list(controller.queue.jobs):
                controller.queue.remove(legacy.job_id)
            from src.features.deliver.core.export_queue import clear_export_queue
            clear_export_queue()

    def test_legacy_flat_config_job_relinks(self, qapp, tmp_path, monkeypatch):
        """更旧的扁平 config（pre-video-batch 队列）timeline_id 存顶层，
        同样要能被 register_sequence 重链。"""
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
        controller = VideoBatchExportController()
        try:
            controller.set_active_project(
                _project(), project_id="p1", project_revision=1, sequences=[])
            from src.features.deliver.core.export_queue import ExportJob
            flat = ExportJob(
                name="legacy", output_path="/tmp/r02flat.mov",
                config={"timeline_id": "t1", "timeline_name": "T1"},
                status=JobStatus.NEEDS_RELINK,
                error="Timeline not found in the current project",
            )
            controller.queue.enqueue(flat, preserve_status=True)

            controller.register_sequence(_sequence("t1", "T1", 100))
            snap = controller.snapshot_jobs()
            assert snap[0].status == "pending", \
                "扁平 config 的旧任务也必须重链"
        finally:
            for legacy in list(controller.queue.jobs):
                controller.queue.remove(legacy.job_id)
            from src.features.deliver.core.export_queue import clear_export_queue
            clear_export_queue()

    def test_offline_target_stays_relinked(self, qapp, tmp_path, monkeypatch):
        """真正离线的目标（登记的是别的序列）保持 needs_relink。"""
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
        controller = VideoBatchExportController()
        try:
            controller.set_active_project(
                _project(), project_id="p1", project_revision=1, sequences=[])
            legacy = self._job("ghost").to_legacy_job()
            legacy.status = JobStatus.NEEDS_RELINK
            controller.queue.enqueue(legacy, preserve_status=True)

            controller.register_sequence(_sequence("t1", "T1", 100))
            snap = controller.snapshot_jobs()
            assert snap[0].status == "needs_relink", \
                "ghost 目标仍不可解析，必须保持失联不被误恢复"
        finally:
            for legacy in list(controller.queue.jobs):
                controller.queue.remove(legacy.job_id)
            from src.features.deliver.core.export_queue import clear_export_queue
            clear_export_queue()
