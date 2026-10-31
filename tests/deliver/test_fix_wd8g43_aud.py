"""Regression tests for remediation batch wd8-g43 (plan batch D, deliver core).

Covers the 16 Major entries fixed in this batch:

- AUD-11904: resume() restarts the queue after the pause drain loop
- AUD-11905: load_export_queue tolerates wrong-shape (valid JSON) files
- AUD-11906: _process_next schedules iteratively (no RecursionError)
- AUD-11909: restore_from_disk skips duplicate/bad snapshots instead of raising
- AUD-11914: color delivery preflight failures are logged, never silent
- AUD-12000: filename templates sanitise the whole expanded result
- AUD-12010: per-job exporter teardown releases the QObject graph
- AUD-12011: nested-loop exit without result aborts instead of chain-running
- AUD-12012: request_cancel in the pre-exporter window is applied, not dropped
- AUD-12100: facade guards the workspace import against AttributeError
- AUD-12201: _reset_ui clears _is_exporting (start failure no longer locks UI)
- AUD-12202: set_timeline_sequences no longer touches a nonexistent panel
- AUD-12203: switching Source back to Timeline restores Custom Range inputs
- AUD-12204: estimates work without an output path and refresh on path change
- AUD-12205: page teardown stops the export worker thread
- AUD-12209: workspace emits started/completed/cancelled from real queue state
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QApplication

from src.features.deliver.core.export_queue import (
    ExportJob,
    ExportQueue,
    JobStatus,
    clear_export_queue,
    _get_queue_path,
    load_export_queue,
    save_export_queue,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def isolated_queue_path(tmp_path, monkeypatch):
    """Point the queue store at a temp dir for every test."""
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
    clear_export_queue()
    yield
    clear_export_queue()


# ═══════════════════════════ AUD-11904 / AUD-11906 ═══════════════════════════


class TestExportQueueScheduling:
    def test_resume_restarts_after_pause_drain(self):
        """AUD-11904: pause parks the scheduler (_running=False); resume must
        re-arm it — previously the guard ``if self._running`` made resume a
        no-op and the remaining jobs never ran."""
        record: list[str] = []
        q: ExportQueue = ExportQueue()

        def runner(job, pcb):
            record.append(job.name)
            if job.name == "B":
                q.pause()  # park the scheduler after this job completes
            return True

        q.set_runner(runner)
        for name in ("A", "B", "C"):
            q.enqueue(ExportJob(name=name, output_path=f"/tmp/{name}.mp4"))
        q.start()

        # Scheduler parked after B: C must not have run yet.
        assert record == ["A", "B"]
        assert not q.is_running

        q.resume()
        assert record == ["A", "B", "C"]
        assert q.get_job  # queue intact
        assert all(q.jobs[i].status == JobStatus.DONE for i in range(3))

    def test_thousands_of_jobs_do_not_recursion_error(self, monkeypatch):
        """AUD-11906: the tail-recursive scheduler blew the stack near the
        default recursion limit; the iterative loop must handle 1200 jobs."""
        monkeypatch.setattr(ExportQueue, "_persist", lambda self: None)
        q = ExportQueue(runner=lambda job, pcb: True)
        for i in range(1200):
            q._jobs.append(ExportJob(job_id=f"j{i}", name=f"j{i}", output_path="/tmp/x.mp4"))
            q._by_id[f"j{i}"] = q._jobs[-1]
        q.start()  # must not raise RecursionError
        assert q.jobs[0].status == JobStatus.DONE
        assert q.jobs[-1].status == JobStatus.DONE
        assert not q.is_running


# ═══════════════════════════════ AUD-11905 ═══════════════════════════════


class TestLoadExportQueueShapeTolerance:
    def _write_raw(self, payload: str) -> None:
        _get_queue_path().write_text(payload, encoding="utf-8")

    def test_top_level_list(self):
        self._write_raw("[]")
        assert load_export_queue() == []

    def test_top_level_string(self):
        self._write_raw('"hello"')
        assert load_export_queue() == []

    def test_jobs_not_a_list(self):
        self._write_raw('{"jobs": "nope"}')
        assert load_export_queue() == []

    def test_non_dict_entries_skipped_and_dict_entries_coerced(self):
        self._write_raw(
            '{"jobs": ['
            '"garbage", 42, null,'
            '{"job_id": "keep-1", "name": "Keep", "output_path": "/tmp/k.mp4"},'
            '{"job_id": "bad-types", "progress": "not-a-float", "retry_count": null,'
            ' "created_at": "bad", "config": 42, "status": "running"}'
            "]}"
        )
        jobs = load_export_queue()
        assert [j.job_id for j in jobs] == ["keep-1", "bad-types"]
        bad = jobs[1]
        assert bad.progress == 0.0  # coerced, not ValueError
        assert bad.retry_count == 0
        assert bad.created_at == 0.0
        assert bad.config == {}  # non-dict config degraded to empty dict
        assert bad.status == JobStatus.PENDING  # stale RUNNING demotion still works

    def test_stale_running_demotion_survives(self):
        save_export_queue(
            [ExportJob(name="A", output_path="/tmp/a.mp4", status=JobStatus.RUNNING, progress=0.5)]
        )
        loaded = load_export_queue()
        assert loaded[0].status == JobStatus.PENDING
        assert loaded[0].progress == 0.0


# ═══════════════════════════════ AUD-11909 ═══════════════════════════════


class TestRestoreFromDiskGuards:
    def _controller(self):
        from src.features.deliver.core.video_batch_export_controller import (
            VideoBatchExportController,
        )

        return VideoBatchExportController()

    def test_duplicate_persisted_job_id_is_skipped(self):
        controller = self._controller()
        controller.queue.enqueue(ExportJob(job_id="dup-1", name="A", output_path="/x.mp4"))
        # The queue file still contains the same id (e.g. written while the
        # job was running during a project switch).
        save_export_queue([ExportJob(job_id="dup-1", name="A", output_path="/x.mp4")])
        controller.restore_from_disk()  # must not raise
        assert [j.job_id for j in controller.queue.jobs] == ["dup-1"]

    def test_bad_snapshot_is_skipped(self):
        controller = self._controller()
        bad = ExportJob(job_id="bad-1", name="B", config={"start_frame": "abc"}, output_path="")
        save_export_queue([bad])  # from_legacy_job raises ValueError on int("abc")
        controller.restore_from_disk()  # must not raise
        assert all(j.job_id != "bad-1" for j in controller.queue.jobs)

    def test_unresolvable_timeline_still_marks_needs_relink(self):
        from src.features.deliver.core.export_queue import JobStatus as JS
        from src.features.deliver.core.video_export_job import VideoExportJob

        controller = self._controller()
        job = VideoExportJob(timeline_id="ghost-timeline", output_path="/x.mov")
        save_export_queue([job.to_legacy_job()])
        controller.restore_from_disk()
        restored = controller.queue.get_job(job.job_id)
        assert restored is not None
        assert restored.status == JS.NEEDS_RELINK


# ═══════════════════════════════ AUD-11914 ═══════════════════════════════


class _StubSequence:
    def __init__(self) -> None:
        self.frame_count = 100
        self.fps = 25.0
        self.audio_tracks: list = []
        self.subtitle_tracks: list = []

    @property
    def all_tracks(self):
        return []


class TestColorPreflightLogging:
    def _run_check(self, monkeypatch, caplog):
        from src.features.deliver.core.video_export_job import SourceKind, VideoExportJob
        from src.features.deliver.core.video_export_preflight import (
            PreflightContext,
            PreflightResult,
            _check_color_delivery_preflight,
        )

        job = VideoExportJob(
            source_kind=SourceKind.TIMELINE, timeline_id="t1", output_path="/x.mov"
        )
        ctx = PreflightContext(project=object(), sequence=_StubSequence())
        result = PreflightResult()
        caplog.set_level(logging.INFO, logger="src.features.deliver.core.video_export_preflight")
        _check_color_delivery_preflight(job, ctx, result)
        return result

    def test_report_failure_is_logged_not_silent(self, monkeypatch, caplog):
        import src.features.color.core.delivery_matrix as dm

        def boom(sequence, project):
            raise RuntimeError("stack explosion")

        monkeypatch.setattr(dm, "sequence_color_stacks", boom)
        result = self._run_check(monkeypatch, caplog)
        assert result.warnings == [] or all(
            w.field != "color_delivery" for w in result.warnings
        )
        assert any("Color delivery preflight failed" in r.message for r in caplog.records)

    def test_missing_color_feature_logs_info(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "src.features.color.core.delivery_matrix", None)
        result = self._run_check(monkeypatch, caplog)
        assert result.issues == []
        assert any("Color delivery matrix unavailable" in r.message for r in caplog.records)


# ═══════════════════════════════ AUD-12000 ═══════════════════════════════


class TestFilenameTemplateSanitisation:
    def test_template_literal_separators_are_sanitised(self):
        from src.features.deliver.core.video_export_presets import expand_filename_template

        for template in ("../../{project}", "/abs/{project}", "a\\b\\{project}", "..\\..\\x"):
            out = expand_filename_template(template, project_name="P")
            assert not set(out) & set('\\/:*?"<>|'), f"unsanitised: {out!r}"
            assert os.path.basename(out) == out  # basename-only contract

    def test_build_job_output_stays_inside_output_dir(self):
        import os as _os

        from src.features.deliver.core.video_export_presets import (
            DEFAULT_VIDEO_PRESETS,
            build_job_from_preset,
        )

        job = build_job_from_preset(
            DEFAULT_VIDEO_PRESETS[0],
            project_name="P",
            timeline_id="t1",
            output_dir="/tmp/wd8_out",
            filename_template="../../escape/{project}",
        )
        assert _os.path.dirname(job.output_path) == "/tmp/wd8_out"
        assert job.name == _os.path.basename(job.output_path)


# ═══════════════════════ AUD-12010 / 12011 / 12012 ═══════════════════════


class _FakeThread:
    def isFinished(self) -> bool:
        return True


class _FakeExporter(QObject):
    """Stand-in for VideoExporter driven by the event loop (like the real
    worker, whose callbacks arrive via queued signals after exec() starts)."""

    def __init__(self, runner, mode: str = "success") -> None:
        super().__init__()
        self._runner = runner
        self._mode = mode
        self.cancel_called = False
        self.cleanup_called = False
        self.delete_later_called = False

    def export(self, project, sequence, config, on_finished=None, on_error=None, on_progress=None, **kw):
        if self._mode == "success":
            QTimer.singleShot(0, lambda: on_finished("/tmp/wd8_out.mov"))
        elif self._mode == "no-result":
            # Simulate the nested loop being interrupted (app shutdown):
            # quit the loop directly, without any worker callback.
            loop = self._runner._loop
            QTimer.singleShot(0, lambda: loop is not None and loop.quit())

    def cancel(self) -> None:
        self.cancel_called = True

    def cleanup(self) -> None:
        self.cleanup_called = True

    def deleteLater(self) -> None:
        self.delete_later_called = True
        super().deleteLater()


def _make_runner(monkeypatch, mode: str = "success"):
    from src.features.deliver.core.video_job_runner import JobContext, VideoJobRunner

    runner = VideoJobRunner(
        context_provider=lambda job: JobContext(
            project=object(), sequence=object(), project_id="p", project_revision=1
        )
    )
    exporter = _FakeExporter(runner, mode=mode)
    monkeypatch.setattr(runner, "_create_worker", lambda p, s, c: (exporter, _FakeThread()))
    return runner, exporter


def _legacy_job():
    from src.features.deliver.core.video_export_job import VideoExportJob

    return VideoExportJob(timeline_id="t1", output_path="/tmp/wd8.mov").to_legacy_job()


class TestVideoJobRunner:
    def test_exporter_torn_down_after_job(self, qapp, monkeypatch):
        """AUD-12010: cleanup + deleteLater release the per-job exporter."""
        runner, exporter = _make_runner(monkeypatch)
        assert runner.run_job(_legacy_job(), lambda p: None) is True
        assert exporter.cleanup_called
        assert exporter.delete_later_called

    def test_loop_exit_without_result_aborts(self, qapp, monkeypatch):
        """AUD-12011: an externally interrupted loop reports 'aborted' and
        returns False instead of raising a generic failure."""
        runner, exporter = _make_runner(monkeypatch, mode="no-result")
        aborted: list[str] = []
        runner.aborted.connect(lambda job_id: aborted.append(job_id))
        assert runner.run_job(_legacy_job(), lambda p: None) is False
        assert len(aborted) == 1

    def test_abort_pauses_the_controller_queue(self, qapp, monkeypatch):
        """AUD-12011 (controller wiring): abort parks the queue instead of
        scheduling the next job."""
        from src.features.deliver.core.video_batch_export_controller import (
            VideoBatchExportController,
        )

        controller = VideoBatchExportController()
        runner, _exporter = _make_runner(monkeypatch, mode="no-result")
        controller._queue.set_runner(runner.run_job)
        controller._runner.aborted.disconnect(controller._on_runner_aborted)
        runner.aborted.connect(controller._on_runner_aborted)
        for i in range(2):
            controller._queue.enqueue(ExportJob(name=f"J{i}", output_path=f"/tmp/{i}.mp4"))
        controller.start_queue()
        assert controller.queue.is_paused
        assert controller.queue.jobs[1].status == JobStatus.PENDING  # never started

    def test_cancel_in_pre_exporter_window_is_applied(self, qapp, monkeypatch):
        """AUD-12012: request_cancel between job acceptance and exporter
        construction is remembered and forwarded to the exporter."""
        from src.features.deliver.core.video_job_runner import VideoJobRunner

        runner, exporter = _make_runner(monkeypatch)
        original_create = runner._create_worker

        def create_with_race(project, sequence, config):
            # Inside the window: job accepted, exporter not yet assigned.
            assert runner._current_job_id is not None
            assert runner._current_exporter is None
            runner.request_cancel()  # exporter is None here — must be pending
            return original_create(project, sequence, config)

        monkeypatch.setattr(runner, "_create_worker", create_with_race)
        cancels: list[str] = []
        runner.cancel_requested.connect(lambda job_id: cancels.append(job_id))
        assert runner.run_job(_legacy_job(), lambda p: None) is True
        assert exporter.cancel_called  # pending cancel applied, not dropped
        assert len(cancels) == 1


# ═══════════════════════════════ AUD-12100 ═══════════════════════════════


class TestDeliverFacadeImportGuards:
    def test_workspace_attributeerror_falls_back_to_page(self, monkeypatch):
        from src.features.deliver import facade

        mod = types.ModuleType("src.features.deliver.ui.deliver_workspace")

        def _raise(name):
            raise AttributeError(name)

        mod.__getattr__ = _raise  # from-import now raises AttributeError
        monkeypatch.setitem(sys.modules, "src.features.deliver.ui.deliver_workspace", mod)
        cls = facade.get_deliver_page_class()
        assert cls is not None and cls.__name__ == "DeliverPage"

    def test_both_broken_returns_none(self, monkeypatch):
        from src.features.deliver import facade

        monkeypatch.setitem(sys.modules, "src.features.deliver.ui.deliver_workspace", None)
        monkeypatch.setitem(sys.modules, "src.features.deliver.ui.deliver_page", None)
        assert facade.get_deliver_page_class() is None


# ═══════════════════════════ AUD-12201 … AUD-12205 ═══════════════════════════


class _SilentMessageBox:
    @staticmethod
    def warning(*_a, **_k):
        return None

    @staticmethod
    def critical(*_a, **_k):
        return None

    @staticmethod
    def information(*_a, **_k):
        return None


class _FakeWorker:
    def __init__(self) -> None:
        self.quit_called = False
        self.wait_ms = None

    def quit(self) -> None:
        self.quit_called = True

    def wait(self, ms=None) -> bool:
        self.wait_ms = ms
        return True


class TestDeliverPage:
    def _page(self, qapp):
        from src.features.deliver.ui.deliver_page import DeliverPage

        return DeliverPage()

    def test_reset_ui_clears_is_exporting(self, qapp):
        page = self._page(qapp)
        page._is_exporting = True
        page._reset_ui()
        assert page._is_exporting is False

    def test_start_export_failure_unlocks_ui(self, qapp, monkeypatch):
        """AUD-12201: a crashing create_timeline_exporter must not leave
        _is_exporting latched (button dead until page rebuild)."""
        import src.features.deliver.ui.deliver_page as dp_mod

        monkeypatch.setattr(dp_mod, "QMessageBox", _SilentMessageBox)

        def boom(**_kw):
            raise RuntimeError("exporter exploded")

        monkeypatch.setattr(dp_mod, "create_timeline_exporter", boom)
        page = self._page(qapp)
        page._sequence = SimpleNamespace(timeline_id="t1", name="T", fps=25.0)
        page._path_input.setText("/tmp/wd8_out.mp4")
        page._start_export()
        assert page._is_exporting is False
        assert page._export_btn.isEnabled()

    def test_set_timeline_sequences_has_no_dead_panel_forward(self, qapp):
        """AUD-12202: the _settings_panel forward (AttributeError swallowed
        by a broad except) is gone."""
        from src.features.deliver.ui.deliver_page import DeliverPage

        source = inspect.getsource(DeliverPage.set_timeline_sequences)
        assert "self._settings_panel" not in source
        page = self._page(qapp)
        page.set_timeline_sequences({"t1": object()})
        assert set(page._timeline_sequences) == {"t1"}

    def test_source_switch_back_to_timeline_restores_custom_range(self, qapp):
        """AUD-12203: Custom Range inputs reappear when Source returns to
        Timeline even though the Range combo index never changed."""
        from src.features.edit.core.timeline_export import ExportRangeType

        page = self._page(qapp)
        page._range_combo.setCurrentIndex(page._range_combo.findData(ExportRangeType.CUSTOM))
        assert page._custom_range_widget.isVisibleTo(page)

        page._source_combo.setCurrentIndex(1)  # Color Image
        assert not page._custom_range_widget.isVisibleTo(page)

        page._source_combo.setCurrentIndex(0)  # back to Timeline
        assert page._custom_range_widget.isVisibleTo(page)

    def test_estimates_work_without_output_path(self, qapp, monkeypatch):
        """AUD-12204: the output_path-must-exist validation no longer gates
        the duration/size estimates."""
        import src.features.deliver.ui.deliver_page as dp_mod

        monkeypatch.setattr(dp_mod, "estimate_export_duration", lambda cfg, seq: 65.0)
        monkeypatch.setattr(dp_mod, "estimate_export_size", lambda cfg, seq: 5 * 1024 * 1024)
        page = self._page(qapp)
        page._sequence = SimpleNamespace(timeline_id="t1")
        assert page._path_input.text() == ""
        page._update_estimates()
        assert "1:05" in page._duration_label.text()
        assert "5.0 MB" in page._size_label.text()

    def test_path_change_refreshes_estimates(self, qapp, monkeypatch):
        """AUD-12204: browsing/typing a path triggers a refresh."""
        import src.features.deliver.ui.deliver_page as dp_mod

        calls = {"n": 0}

        def counting_duration(cfg, seq):
            calls["n"] += 1
            return 1.0

        monkeypatch.setattr(dp_mod, "estimate_export_duration", counting_duration)
        monkeypatch.setattr(dp_mod, "estimate_export_size", lambda cfg, seq: 1)
        page = self._page(qapp)
        page._sequence = SimpleNamespace(timeline_id="t1")
        before = calls["n"]
        page._path_input.setText("/tmp/new_output.mp4")
        assert calls["n"] > before

    def test_get_export_config_still_requires_path(self, qapp):
        """Locked behaviour: real export config validation still demands an
        output path (only the estimate path bypasses it)."""
        page = self._page(qapp)
        page._sequence = SimpleNamespace(timeline_id="t1")
        assert page.get_export_config() is None
        page._path_input.setText("/tmp/wd8_valid.mp4")
        assert page.get_export_config() is not None

    def test_teardown_worker_waits_and_detaches(self, qapp):
        """AUD-12205: page teardown quits + waits the worker and clears the
        exporting latch."""
        page = self._page(qapp)
        fake = _FakeWorker()
        page._worker = fake  # type: ignore[assignment]
        page._is_exporting = True
        page._teardown_worker()
        assert fake.quit_called
        assert fake.wait_ms is not None
        assert page._worker is None
        assert page._is_exporting is False

    def test_close_event_tears_down_worker(self, qapp):
        from PyQt6.QtGui import QCloseEvent

        page = self._page(qapp)
        fake = _FakeWorker()
        page._worker = fake  # type: ignore[assignment]
        page.closeEvent(QCloseEvent())
        assert fake.quit_called
        assert page._worker is None


# ═══════════════════════════════ AUD-12209 ═══════════════════════════════


class TestWorkspaceSignalContract:
    def test_signals_follow_real_queue_state(self, qapp):
        from src.features.deliver.ui.deliver_workspace import DeliverWorkspace

        ws = DeliverWorkspace()
        events: list = []
        ws.export_started.connect(lambda: events.append("started"))
        ws.export_completed.connect(lambda ok: events.append(("completed", ok)))
        ws.export_cancelled.connect(lambda: events.append("cancelled"))

        from src.features.deliver.core.video_export_job import VideoExportJob

        job = VideoExportJob(timeline_id="t1", output_path="/tmp/wd8.mov")
        ws._on_add_jobs([job])
        assert events == []  # enqueue-only must NOT emit export_started

        ws._controller.queue.set_runner(lambda j, p: True)
        ws._controller.start_queue()
        assert events == ["started", ("completed", True)]

        # A failing run reports completed(False).
        job2 = VideoExportJob(timeline_id="t1", output_path="/tmp/wd8_2.mov")
        ws._on_add_jobs([job2])

        def failing(job, pcb):
            raise RuntimeError("encode failed")

        ws._controller.queue.set_runner(failing)
        ws._controller.start_queue()
        assert events == ["started", ("completed", True), "started", ("completed", False)]

        # A forwarded cancellation emits export_cancelled.
        ws._on_export_cancel_requested("some-job")
        assert events[-1] == "cancelled"
