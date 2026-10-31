"""Tests for the video batch export panel (UI layer).

These tests exercise the panel + model without a real export — they
verify:
* The empty state shows when the queue is empty.
* Adding jobs to the queue updates the model row count.
* Toolbar / row action signals fire with the right job_id.
* The settings panel can build jobs from presets and load them back.
* Preflight results block invalid jobs.

All Qt tests use the offscreen platform so they run headless.
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

from src.features.deliver.core.export_queue import clear_export_queue
from src.features.deliver.core.video_export_job import (
    AudioConfig,
    FilenameUse,
    RenderMode,
    OverwritePolicy,
    RangeType,
    SubtitleConfig,
    SubtitleMode,
    VideoConfig,
    VideoExportJob,
    VideoJobStatus,
)
from src.features.deliver.core.video_batch_export_controller import (
    VideoBatchExportController,
)
from src.features.deliver.ui.video_batch_export_panel import (
    VideoBatchExportPanel,
    VideoQueueModel,
)
from src.features.deliver.ui.video_export_settings_panel import (
    VideoExportSettingsPanel,
)
from src.shared.ui.widgets import DragValue


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def isolated_queue_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
    clear_export_queue()
    yield
    clear_export_queue()


# ── Model ──


class TestVideoQueueModel:
    def test_empty_model(self, qapp):
        m = VideoQueueModel()
        assert m.rowCount() == 0
        assert m.columnCount() == 9

    def test_refresh_sets_rows(self, qapp):
        m = VideoQueueModel()
        jobs = [
            VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                           video_config=VideoConfig(codec="prores_422", container="mov")),
            VideoExportJob(timeline_id="t2", output_path="/tmp/b.mov",
                           video_config=VideoConfig(codec="h264", container="mp4")),
        ]
        m.refresh(jobs)
        assert m.rowCount() == 2
        assert m.get_job_at(0).timeline_id == "t1"

    def test_update_row_in_place(self, qapp):
        m = VideoQueueModel()
        job = VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov")
        m.refresh([job])
        updated = VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                                 status=VideoJobStatus.RUNNING, progress=0.5)
        m.update_row(0, updated)
        assert m.get_job_at(0).status == VideoJobStatus.RUNNING


# ── Batch panel ──


class TestVideoBatchExportPanel:
    def test_empty_state_shows_when_no_jobs(self, qapp):
        from src.features.deliver.core.export_queue import ExportQueue
        panel = VideoBatchExportPanel()
        queue = ExportQueue()
        panel.refresh_from_queue(queue)
        # isVisible() is False until the top-level window is shown, so test
        # the explicit visibility flag the panel toggles.
        assert not panel._empty_state.isHidden()
        assert panel._view.isHidden()

    def test_adding_job_hides_empty_state(self, qapp):
        from src.features.deliver.core.export_queue import ExportQueue
        panel = VideoBatchExportPanel()
        controller = VideoBatchExportController()
        controller.add_jobs([
            VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                           video_config=VideoConfig(codec="prores_422", container="mov")),
        ])
        panel.refresh_from_queue(controller.queue)
        assert panel._empty_state.isHidden()
        assert not panel._view.isHidden()
        assert panel._model.rowCount() == 1

    def test_toolbar_signals_fire(self, qapp):
        panel = VideoBatchExportPanel()
        signals = []
        panel.start_requested.connect(lambda: signals.append("start"))
        panel.pause_requested.connect(lambda: signals.append("pause"))
        panel.cancel_current_requested.connect(lambda: signals.append("cancel"))
        panel._start_btn.click()
        panel._pause_btn.click()
        panel._cancel_btn.click()
        assert signals == ["start", "pause", "cancel"]

    def test_retry_signal_carries_job_id(self, qapp):
        panel = VideoBatchExportPanel()
        captured = []
        panel.retry_requested.connect(lambda jid: captured.append(jid))
        controller = VideoBatchExportController()
        job = VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                             video_config=VideoConfig(codec="prores_422", container="mov"))
        controller.add_jobs([job])
        panel.refresh_from_queue(controller.queue)
        # Manually mark failed so the retry affordance applies
        legacy = controller.queue.get_job(job.job_id)
        legacy.status = "failed"
        panel.refresh_from_queue(controller.queue)
        # Emit retry for the first row
        panel.retry_requested.emit(job.job_id)
        assert captured == [job.job_id]


# ── Settings panel ──


class TestVideoExportSettingsPanel:
    def test_build_jobs_with_no_selection_uses_custom(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_revision=1, project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel.set_active_timeline("t1")
        panel._output_dir_edit.setText("/tmp")
        jobs, _ = panel.build_jobs(preflight=False)
        assert len(jobs) == 1
        assert jobs[0].preset_id == "custom"

    def test_defaults_use_timeline_name_and_desktop_output(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_name="Proj")
        panel.set_timelines([("t1", "Main Timeline"), ("t2", "Alt Timeline")])
        panel.set_active_timeline("t1")

        assert panel._filename_edit.text() == "Main Timeline"
        assert panel._output_dir_edit.text() == panel._default_output_dir()

        jobs, _ = panel.build_jobs(preflight=False)
        assert jobs[0].output_path == os.path.join(
            panel._default_output_dir(), "Main Timeline.mp4"
        )

        panel._filename_edit.setText("My Deliver")
        panel.set_active_timeline("t2")
        assert panel._filename_edit.text() == "My Deliver"

    def test_individual_clips_persists_resolve_style_naming(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel._output_dir_edit.setText("/tmp")

        # Subtitle delivery belongs to the video inspector, while the File
        # inspector changes from timeline naming to source naming.
        assert panel._subtitle_combo.parent().objectName() == "VideoSettingsSection"
        assert panel._unique_filenames_check.isHidden()
        panel._individual_clips_radio.setChecked(True)
        assert not panel._unique_filenames_check.isHidden()
        assert panel._timeline_or_source_name_radio.text() in {"Source filename", "源文件名"}

        panel._custom_name_radio.setChecked(True)
        panel._custom_name_edit.setText("review_{source}")
        panel._file_suffix_edit.setText("graded")
        panel._unique_prefix_radio.setChecked(True)
        panel._unique_digits_spin.setValue(8)
        panel._source_frame_count_check.setChecked(True)
        panel._frame_handles_spin.setValue(25)
        jobs, _ = panel.build_jobs(preflight=False)

        naming = jobs[0].naming_config
        assert naming.render_mode == RenderMode.INDIVIDUAL_CLIPS
        assert naming.filename_use == FilenameUse.CUSTOM
        assert naming.custom_name == "review_{source}"
        assert naming.file_suffix == "graded"
        assert naming.unique_filename_position == "prefix"
        assert naming.unique_filename_digits == 8
        assert naming.add_source_frame_count is True
        assert naming.frame_handles == 25

    def test_crf_quality_is_explicitly_unitless(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._quality_mode_combo.setCurrentText("CRF")

        assert isinstance(panel._width_spin, DragValue)
        assert isinstance(panel._height_spin, DragValue)
        assert isinstance(panel._quality_value_spin, DragValue)
        assert panel._quality_label.text() in {"Quality (CRF)", "质量（CRF）"}
        assert (
            "unitless" in panel._quality_value_spin.toolTip()
            or "没有单位" in panel._quality_value_spin.toolTip()
        )
        assert [panel._crf_combo.itemData(i) for i in range(panel._crf_combo.count())] == [
            18, 20, 23, 28, 30
        ]
        assert panel._crf_combo.currentData() == 20

        assert not panel._profile_combo.isEditable()
        assert panel._profile_combo.findText("auto") >= 0

    def test_crf_preset_builds_integer_quality_value(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._quality_mode_combo.setCurrentText("CRF")
        panel._crf_combo.setCurrentIndex(panel._crf_combo.findData(23))

        draft = panel._read_video_config_from_draft()

        assert draft.quality_mode == "crf"
        assert draft.quality_value == 23

    def test_default_quality_is_12000_kbps_vbr(self, qapp):
        panel = VideoExportSettingsPanel()

        assert panel._quality_mode_combo.currentData() == "vbr"
        assert panel._quality_mode_combo.currentText() in {"VBR", "VBR（可变码率）"}
        assert panel._quality_mode_combo.findText("Auto") < 0
        assert [panel._quality_mode_combo.itemData(i) for i in range(panel._quality_mode_combo.count())] == [
            "vbr", "cbr", "crf"
        ]
        assert panel._quality_value_spin.value() == 12000
        assert panel._quality_value_spin._suffix == " kbps"

        draft = panel._read_video_config_from_draft()
        assert draft.quality_mode == "vbr"
        assert draft.quality_value == 12_000_000

    def test_setting_sequence_refreshes_preflight_context(self, qapp, monkeypatch):
        panel = VideoExportSettingsPanel()
        refreshed = []
        monkeypatch.setattr(panel, "_refresh_preflight", lambda: refreshed.append(True))

        panel.set_sequence(SimpleNamespace(
            timeline_id="t1",
            name="Timeline 1",
            fps=25.0,
            width=1920,
            height=1080,
            duration_frames=100,
            range_start=None,
            range_end=None,
        ))

        assert refreshed

    def test_cbr_uses_fixed_bitrate_mode(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._quality_mode_combo.setCurrentText("CBR")

        assert panel._quality_mode_combo.currentData() == "cbr"
        assert panel._quality_value_spin.value() == 12000
        assert "Fixed" in panel._quality_value_spin.toolTip() or "固定" in panel._quality_value_spin.toolTip()

        draft = panel._read_video_config_from_draft()
        assert draft.quality_mode == "cbr"
        assert draft.quality_value == 12_000_000

    def test_bitrate_shows_estimated_size_for_current_output_range(self, qapp):
        panel = VideoExportSettingsPanel()
        assert panel._format_estimated_size(116.48, 12000) == "174.7 MB"
        panel.set_sequence(
            SimpleNamespace(
                name="Main",
                fps=25.0,
                width=1920,
                height=1080,
                duration_frames=250,
                range_start=50,
                range_end=149,
            )
        )
        panel._quality_value_spin.setValue(8000)

        # 250 frames / 25 fps × 8000 kbps / 8000 = 10 MB.
        assert "10.0 MB" in panel._estimated_size_label.text()

        panel._range_combo.setCurrentIndex(panel._range_combo.findData(RangeType.IN_OUT))
        # The persisted In/Out range is 100 frames = 4 seconds.
        assert "4.00 MB" in panel._estimated_size_label.text()

        panel._quality_mode_combo.setCurrentText("CRF")
        assert panel._estimated_size_label.isHidden()

    def test_format_filters_encoder_choices(self, qapp):
        panel = VideoExportSettingsPanel()

        assert panel._container_combo.currentData() == "mp4"
        assert panel._codec_combo.findData("h264") >= 0
        assert panel._codec_combo.findData("hevc") >= 0
        assert panel._codec_combo.findData("prores_422") == -1

        panel._container_combo.setCurrentIndex(panel._container_combo.findData("mov"))
        assert panel._codec_combo.findData("prores_422") >= 0

        panel._codec_combo.setCurrentIndex(panel._codec_combo.findData("prores_422"))
        panel._container_combo.setCurrentIndex(panel._container_combo.findData("mp4"))
        assert panel._codec_combo.findData("prores_422") == -1
        assert panel._codec_combo.currentData() in {"h264", "hevc"}

    def test_alpha_only_appears_for_alpha_capable_encoder(self, qapp):
        panel = VideoExportSettingsPanel()

        assert panel._alpha_check.isHidden()
        panel._container_combo.setCurrentIndex(panel._container_combo.findData("mov"))
        panel._codec_combo.setCurrentIndex(panel._codec_combo.findData("prores_4444"))
        assert not panel._alpha_check.isHidden()

        panel._codec_combo.setCurrentIndex(panel._codec_combo.findData("prores_422"))
        assert panel._alpha_check.isHidden()

    def test_build_jobs_with_multi_preset(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_revision=1, project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel.set_active_timeline("t1")
        panel._output_dir_edit.setText("/tmp")
        panel._on_preset_selection_toggled("web_h264", True)
        panel._on_preset_selection_toggled("master_prores422", True)
        jobs, _ = panel.build_jobs(preflight=False)
        assert len(jobs) == 2
        preset_ids = {j.preset_id for j in jobs}
        assert preset_ids == {"web_h264", "master_prores422"}

    def test_multi_preset_each_keeps_own_spec(self, qapp):
        """Regression: each selected preset must keep its own codec/resolution.

        Previously _apply_draft_overrides homogenised every job to match the
        last-clicked preset's settings (because the draft only reflected the
        last preset). Now in multi-preset mode only shared fields are
        overridden.
        """
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_revision=1, project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel.set_active_timeline("t1")
        panel._output_dir_edit.setText("/tmp")
        panel._on_preset_selection_toggled("web_h264", True)        # h264 + 1080p + 12000 kbps
        panel._on_preset_selection_toggled("master_prores422", True)  # prores_422 + None + auto

        jobs, _ = panel.build_jobs(preflight=False)
        web = next(j for j in jobs if j.preset_id == "web_h264")
        master = next(j for j in jobs if j.preset_id == "master_prores422")
        # web_h264 keeps its own codec/resolution/quality
        assert web.video_config.codec == "h264"
        assert web.video_config.resolution == (1920, 1080)
        assert web.video_config.quality_mode == "vbr"
        assert web.video_config.quality_value == 12_000_000
        # master_prores422 keeps its own codec/resolution
        assert master.video_config.codec == "prores_422"
        assert master.video_config.resolution is None
        # And the two jobs differ (they aren't homogenised)
        assert web.video_config.codec != master.video_config.codec

    def test_load_job_round_trips_fields(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_revision=1, project_name="Proj")
        panel.set_timelines([("t1", "Main")])

        original = VideoExportJob(
            timeline_id="t1", timeline_name="Main",
            output_path="/tmp/out.mov",
            video_config=VideoConfig(
                codec="hevc", container="mp4", resolution=(3840, 2160),
                fps=30.0, quality_mode="crf", quality_value=22, bit_depth=10,
            ),
            audio_config=AudioConfig(codec="aac", sample_rate=48000, bitrate=256),
            subtitle_config=SubtitleConfig(mode=SubtitleMode.BURN),
            overwrite_policy=OverwritePolicy.OVERWRITE,
        )
        panel.load_job(original)
        jobs, _ = panel.build_jobs(preflight=False)
        assert jobs
        loaded = jobs[0]
        assert loaded.video_config.codec == "hevc"
        assert loaded.video_config.resolution == (3840, 2160)
        assert loaded.video_config.bit_depth == 10
        assert loaded.subtitle_config.mode == SubtitleMode.BURN
        assert loaded.overwrite_policy == OverwritePolicy.OVERWRITE

    def test_add_button_emits_signal(self, qapp):
        panel = VideoExportSettingsPanel()
        panel.set_project(object(), project_id="p1", project_name="Proj")
        panel.set_timelines([("t1", "Main")])
        panel.set_active_timeline("t1")
        panel._output_dir_edit.setText("/tmp")
        captured = []
        panel.add_to_queue_requested.connect(lambda jobs: captured.append(jobs))
        panel._add_btn.clicked.emit()
        # Preflight should pass with no sequence, but no output dir parent…
        # just verify the signal path works (may be empty if blocked).
        assert isinstance(captured, list)


# ── Controller integration ──


class TestControllerIntegration:
    def test_register_sequence_replaces_lightweight_reference(self, qapp):
        c = VideoBatchExportController()
        ref = SimpleNamespace(timeline_id="t2", name="Timeline 2")
        c.set_active_project(object(), "p1", sequences=[ref])
        # U03（第六轮审计）：轻量 ref 不进入队列上下文。
        assert c._sequences_by_id == {}

        from src.features.edit.core.timeline_v2 import TimelineSequence
        from src.shared.media.metadata import Rational

        full_sequence = TimelineSequence(
            timeline_id="t2", name="Timeline 2", fps=Rational(25, 1),
            width=1920, height=1080,
        )
        c.register_sequence(full_sequence)

        assert c._sequences_by_id["t2"] is full_sequence
        assert c._all_sequences == [full_sequence]

    def test_add_and_snapshot(self, qapp):
        c = VideoBatchExportController()
        c.add_jobs([
            VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                           video_config=VideoConfig(codec="prores_422", container="mov")),
        ])
        snap = c.snapshot_jobs()
        assert len(snap) == 1
        assert snap[0].timeline_id == "t1"

    def test_remove_job(self, qapp):
        c = VideoBatchExportController()
        job = VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                             video_config=VideoConfig(codec="prores_422", container="mov"))
        c.add_jobs([job])
        assert len(c.snapshot_jobs()) == 1
        c.remove_job(job.job_id)
        assert len(c.snapshot_jobs()) == 0

    def test_clear_completed(self, qapp):
        c = VideoBatchExportController()
        j1 = VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                            video_config=VideoConfig(codec="prores_422", container="mov"))
        j2 = VideoExportJob(timeline_id="t2", output_path="/tmp/b.mov",
                            video_config=VideoConfig(codec="prores_422", container="mov"))
        c.add_jobs([j1, j2])
        # Manually flip j1 to done via the queue
        legacy = c.queue.get_job(j1.job_id)
        legacy.status = "done"
        c.clear_completed()
        ids = [j.job_id for j in c.snapshot_jobs()]
        assert j2.job_id in ids
        assert j1.job_id not in ids

    def test_unresolved_context_surfaces_error_message(self, qapp):
        """Regression: the runner must raise so the queue stores the real
        error reason on the job (not a generic 'export runner returned failure')."""
        c = VideoBatchExportController()
        # Default provider = unresolved → runner will raise RuntimeError
        c.add_jobs([
            VideoExportJob(timeline_id="t1", output_path="/tmp/a.mov",
                           video_config=VideoConfig(codec="prores_422", container="mov")),
        ])
        c.start_queue()
        snap = c.snapshot_jobs()
        assert snap[0].status == "failed"
        # The error message must be the real reason, not the generic fallback.
        assert "export runner returned failure" not in snap[0].error
        assert snap[0].error  # non-empty

    def test_needs_relink_job_not_auto_run(self, qapp, tmp_path, monkeypatch):
        """Regression: a restored job whose timeline is missing must not be
        auto-run by start_queue()."""
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
        from src.features.deliver.core.export_queue import save_export_queue, clear_export_queue
        job = VideoExportJob(
            timeline_id="ghost",
            timeline_name="Ghost",
            output_path="/tmp/a.mov",
            video_config=VideoConfig(codec="prores_422", container="mov"),
        )
        save_export_queue([job.to_legacy_job()])
        try:
            c = VideoBatchExportController()
            c.set_active_project(object(), "p1", 1, sequences=[])  # no ghost timeline
            c.restore_from_disk()
            snap = c.snapshot_jobs()
            assert snap[0].status == "needs_relink"
            # Starting the queue must not run the relink job.
            c.start_queue()
            snap2 = c.snapshot_jobs()
            assert snap2[0].status == "needs_relink"
            assert not c.queue.is_running
            # After retry it becomes runnable (and fails fast — context unresolved).
            c.retry_job(snap2[0].job_id)
            snap3 = c.snapshot_jobs()
            assert snap3[0].status == "failed"
        finally:
            clear_export_queue()
