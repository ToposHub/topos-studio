"""Deliver export queue tests.

Covers the ExportJob data model, on-disk persistence (with stale-RUNNING
recovery), and the ExportQueue controller (serial execution, progress,
failure + retry, cancel, remove, clear, duplicate-id rejection).

Each test uses its own TOPOS_CONFIG_PATH so the queue file is isolated and
cleaned up automatically.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.deliver.core.export_queue import (
    JobStatus,
    ExportJob,
    ExportQueue,
    clear_export_queue,
    has_restorable_jobs,
    load_export_queue,
    save_export_queue,
)


@pytest.fixture(autouse=True)
def isolated_queue_path(tmp_path, monkeypatch):
    """Point the queue store at a temp dir for every test."""
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path))
    clear_export_queue()
    yield
    clear_export_queue()


# ── Data model ──


class TestExportJob:
    def test_defaults(self):
        j = ExportJob(name="x", output_path="/tmp/x.mp4")
        assert j.status == JobStatus.PENDING
        assert j.progress == 0.0
        assert j.is_restorable

    def test_roundtrip(self):
        j = ExportJob(
            name="Scene", config={"codec": "h264", "fps": 24},
            output_path="/tmp/scene.mp4", status=JobStatus.FAILED,
            error="boom", retry_count=2,
        )
        restored = ExportJob.from_dict(j.to_dict())
        assert restored.name == "Scene"
        assert restored.config == {"codec": "h264", "fps": 24}
        assert restored.status == JobStatus.FAILED
        assert restored.retry_count == 2
        assert restored.error == "boom"

    def test_rejects_empty_id(self):
        with pytest.raises(ValueError):
            ExportJob(job_id="", name="x", output_path="/tmp/x.mp4")


# ── Persistence ──


class TestPersistence:
    def test_save_load_roundtrip(self):
        jobs = [
            ExportJob(name="A", output_path="/tmp/a.mp4"),
            ExportJob(name="B", output_path="/tmp/b.mp4"),
        ]
        save_export_queue(jobs)
        loaded = load_export_queue()
        assert {j.name for j in loaded} == {"A", "B"}
        assert has_restorable_jobs()

    def test_stale_running_demoted_on_load(self):
        jobs = [ExportJob(name="A", output_path="/tmp/a.mp4", status=JobStatus.RUNNING, progress=0.5)]
        save_export_queue(jobs)
        loaded = load_export_queue()
        # A job marked RUNNING by a now-dead process must come back as PENDING.
        assert loaded[0].status == JobStatus.PENDING
        assert loaded[0].progress == 0.0

    def test_clear(self):
        save_export_queue([ExportJob(name="A", output_path="/tmp/a.mp4")])
        assert has_restorable_jobs()
        clear_export_queue()
        assert load_export_queue() == []
        assert not has_restorable_jobs()

    def test_load_missing_file_returns_empty(self):
        assert load_export_queue() == []

    def test_config_path_pointing_at_file_does_not_crash(self, tmp_path, monkeypatch):
        """Regression: TOPOS_CONFIG_PATH points at a config FILE (not a dir).

        main.py sets TOPOS_CONFIG_PATH to ``.topos/config.json``. The queue
        store must resolve the queue file to the parent directory and not
        try to mkdir the config-file path (which crashed with
        FileExistsError on startup).
        """
        config_dir = tmp_path / ".topos"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        config_file.write_text("{}")  # an actual file, like real config.json
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(config_file))
        clear_export_queue()

        # Must not raise FileExistsError.
        from src.features.deliver.core.export_queue import _get_queue_path
        path = _get_queue_path()
        # Queue lives next to the config file, not inside it as a "directory".
        assert path.parent == config_dir
        assert path.name == "deliver_export_queue.json"

        # And load/save must round-trip cleanly.
        save_export_queue([ExportJob(name="A", output_path="/tmp/a.mp4")])
        loaded = load_export_queue()
        assert len(loaded) == 1
        assert loaded[0].name == "A"
        clear_export_queue()


# ── Queue controller ──


def _success_runner(record):
    def _r(job, progress_cb):
        for i in range(5):
            progress_cb((i + 1) / 5.0)
        record.append(job.name)
        return True
    return _r


class TestExportQueue:
    def test_serial_execution(self):
        record: list[str] = []
        q = ExportQueue(runner=_success_runner(record))
        events = {"completed": 0, "drained": 0, "progress": 0}
        q.job_completed.connect(lambda j, ok: events.__setitem__("completed", events["completed"] + 1))
        q.queue_drained.connect(lambda: events.__setitem__("drained", events["drained"] + 1))
        q.job_progress.connect(lambda j, p: events.__setitem__("progress", events["progress"] + 1))

        ja = ExportJob(name="JobA", output_path="/tmp/a.mp4")
        jb = ExportJob(name="JobB", output_path="/tmp/b.mp4")
        q.enqueue(ja)
        q.enqueue(jb)
        q.start()

        assert record == ["JobA", "JobB"]  # serial, in order
        assert q.get_job(ja.job_id).status == JobStatus.DONE
        assert q.get_job(jb.job_id).status == JobStatus.DONE
        assert events["completed"] == 2
        assert events["drained"] == 1
        assert events["progress"] == 10  # 5 per job × 2

    def test_failed_status_and_error(self):
        def fail_runner(job, pcb):
            raise RuntimeError("boom")
        q = ExportQueue(runner=fail_runner)
        jf = ExportJob(name="Fail", output_path="/tmp/f.mp4")
        q.enqueue(jf)
        q.start()
        job = q.get_job(jf.job_id)
        assert job.status == JobStatus.FAILED
        assert "boom" in job.error

    def test_retry_auto_starts(self):
        def fail_runner(job, pcb):
            raise RuntimeError("boom")
        q = ExportQueue(runner=fail_runner)
        jf = ExportJob(name="Fail", output_path="/tmp/f.mp4")
        q.enqueue(jf)
        q.start()
        assert q.get_job(jf.job_id).status == JobStatus.FAILED

        q.set_runner(_success_runner([]))
        assert q.retry(jf.job_id) is True
        # retry of an idle queue auto-starts processing
        assert q.get_job(jf.job_id).status == JobStatus.DONE
        assert q.get_job(jf.job_id).retry_count == 1

    def test_retry_rejects_non_failed(self):
        q = ExportQueue(runner=_success_runner([]))
        j = ExportJob(name="J", output_path="/tmp/j.mp4")
        q.enqueue(j)
        q.start()
        assert j.status == JobStatus.DONE
        assert q.retry(j.job_id) is False  # can't retry DONE

    def test_cancel_current(self):
        q = ExportQueue(runner=lambda j, p: not q._cancelled_current)
        jc = ExportJob(name="C", output_path="/tmp/c.mp4")
        q.enqueue(jc)
        q.cancel_current()  # pre-set before start
        q.start()
        assert q.get_job(jc.job_id).status == JobStatus.CANCELLED

    def test_remove_pending(self):
        q = ExportQueue(runner=_success_runner([]))
        a = ExportJob(name="A", output_path="/tmp/a.mp4")
        b = ExportJob(name="B", output_path="/tmp/b.mp4")
        q.enqueue(a)
        q.enqueue(b)
        assert q.remove(a.job_id) is True
        assert q.get_job(a.job_id) is None
        assert q.get_job(b.job_id) is not None
        # cannot remove unknown
        assert q.remove("nope") is False

    def test_cannot_remove_running(self):
        state = {"running": True}

        def blocking_runner(job, pcb):
            # The job is RUNNING during this call; removal must be refused.
            assert q.remove(job.job_id) is False
            state["running"] = False
            return True

        q = ExportQueue(runner=blocking_runner)
        j = ExportJob(name="J", output_path="/tmp/j.mp4")
        q.enqueue(j)
        q.start()

    def test_clear_completed(self):
        q = ExportQueue(runner=_success_runner([]))
        a = ExportJob(name="A", output_path="/tmp/a.mp4")
        b = ExportJob(name="B", output_path="/tmp/b.mp4")
        q.enqueue(a)
        q.enqueue(b)
        q.start()
        # both DONE → clear removes both
        q.clear_completed()
        assert q.jobs == []

    def test_duplicate_job_id_rejected(self):
        q = ExportQueue(runner=_success_runner([]))
        j = ExportJob(name="J", output_path="/tmp/j.mp4")
        q.enqueue(j)
        with pytest.raises(ValueError):
            q.enqueue(j)

    def test_persisted_after_run(self):
        q = ExportQueue(runner=_success_runner([]))
        j = ExportJob(name="Persisted", output_path="/tmp/p.mp4")
        q.enqueue(j)
        q.start()
        # Queue file should reflect the completed job.
        loaded = load_export_queue()
        assert len(loaded) == 1
        assert loaded[0].status == JobStatus.DONE
