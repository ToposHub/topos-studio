"""N12 · QC 异步执行/批内隔离/代次拒旧/失败显式测试。

审计句：queue_drained 同主线程槽同步测量全部输出（逐文件完整解码）
——注释"主线程外"不符实现；只测本轮成功任务不混历史 DONE；质量失败
不影响成功的静默口径改为显式失败信号。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.deliver_core


def _make_ctrl(monkeypatch, tmp_path, jobs):
    from src.features.deliver.core.video_batch_export_controller import (
        VideoBatchExportController,
    )
    ctrl = VideoBatchExportController()
    monkeypatch.setattr(
        type(ctrl.queue), "jobs", property(lambda self: list(jobs)),
        raising=False)
    return ctrl


def _job(tmp_path, name, status):
    from src.features.deliver.core.export_queue import ExportJob
    media = tmp_path / name
    media.write_bytes(b"x")
    return ExportJob(config={"output_path": str(media)},
                     output_path=str(media), status=status)


def _install_measure(monkeypatch, result_fn):
    monkeypatch.setattr(
        "src.features.edit.ai_editing.delivery_qc_batch."
        "measure_version_lufs", result_fn)


def _drain_and_wait(qapp, ctrl):
    ctrl._on_queue_drained()
    assert ctrl._qc_worker is not None
    assert ctrl._qc_worker.wait(5000)
    for _ in range(10):
        qapp.processEvents()


class TestQcAsyncBatch:
    def test_measurement_runs_off_main_thread(
            self, qapp, monkeypatch, tmp_path):
        threads = []

        def measure(paths):
            threads.append(threading.current_thread().ident)
            return {p: {"input": object(), "error": ""} for p in paths}

        _install_measure(monkeypatch, measure)
        job = _job(tmp_path, "v.mp4",
                   __import__("src.features.deliver.core.export_queue",
                              fromlist=["JobStatus"]).JobStatus.DONE)
        ctrl = _make_ctrl(monkeypatch, tmp_path, [job])
        ctrl._qc_batch_ids.add(str(job.job_id))
        _drain_and_wait(qapp, ctrl)
        assert threads and threads[0] != threading.current_thread().ident

    def test_only_current_batch_measured(
            self, qapp, monkeypatch, tmp_path):
        from src.features.deliver.core.export_queue import JobStatus
        seen = []

        def measure(paths):
            seen.extend(paths)
            return {p: {"input": object(), "error": ""} for p in paths}

        _install_measure(monkeypatch, measure)
        current = _job(tmp_path, "new.mp4", JobStatus.DONE)
        historical = _job(tmp_path, "old.mp4", JobStatus.DONE)
        ctrl = _make_ctrl(monkeypatch, tmp_path, [current, historical])
        ctrl._qc_batch_ids.add(str(current.job_id))  # 只登记本轮
        _drain_and_wait(qapp, ctrl)
        assert seen == [str(current.output_path)]  # 历史 DONE 不混入
        assert ctrl.last_multi_version_qc["lufs"]

    def test_stale_generation_discarded(
            self, qapp, monkeypatch, tmp_path):
        from src.features.deliver.core.export_queue import JobStatus
        reports = []

        def measure(paths):
            return {p: {"input": object(), "error": ""} for p in paths}

        _install_measure(monkeypatch, measure)
        job = _job(tmp_path, "v.mp4", JobStatus.DONE)
        ctrl = _make_ctrl(monkeypatch, tmp_path, [job])
        ctrl._qc_batch_ids.add(str(job.job_id))
        ctrl.multi_version_qc_ready.connect(lambda r: reports.append(r))
        ctrl._on_queue_drained()
        worker1 = ctrl._qc_worker
        # 测量在途时新批入队 → 代次前进
        ctrl._qc_batch_ids.add("next-batch-job")
        ctrl._qc_generation += 1
        assert worker1.wait(5000)
        for _ in range(10):
            qapp.processEvents()
        assert reports == []  # 旧代结果被拒收
        assert ctrl.last_multi_version_qc == {}

    def test_failed_measurement_emits_failure_signal(
            self, qapp, monkeypatch, tmp_path):
        from src.features.deliver.core.export_queue import JobStatus
        _install_measure(
            monkeypatch,
            lambda paths: {p: {"input": None, "error": "测量失败"}
                           for p in paths})
        job = _job(tmp_path, "bad.mp4", JobStatus.DONE)
        ctrl = _make_ctrl(monkeypatch, tmp_path, [job])
        ctrl._qc_batch_ids.add(str(job.job_id))
        failures = []
        ctrl.multi_version_qc_failed.connect(lambda paths: failures.extend(paths))
        _drain_and_wait(qapp, ctrl)
        assert failures == [str(job.output_path)]  # 未测/失败≠交付成功

    def test_worker_unavailable_reports_all_failed(
            self, qapp, monkeypatch, tmp_path):
        from src.features.deliver.core.export_queue import JobStatus

        def boom(paths):
            raise RuntimeError("probe broken")

        _install_measure(monkeypatch, boom)
        job = _job(tmp_path, "v.mp4", JobStatus.DONE)
        ctrl = _make_ctrl(monkeypatch, tmp_path, [job])
        ctrl._qc_batch_ids.add(str(job.job_id))
        failures = []
        ctrl.multi_version_qc_failed.connect(lambda p: failures.extend(p))
        _drain_and_wait(qapp, ctrl)
        assert failures == [str(job.output_path)]  # QC 不可用不报成功
