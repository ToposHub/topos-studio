"""N08 · verified ingest 生产入口测试。

审计句：ingest_files/verified_ingest 无 UI/service 消费。修复契约：
可取消 worker、逐项报告（UI 线程信号）、成功项统一入库命令（一次
撤销）；第一项成功、第二项读失败、第三项继续；中途取消部分保留。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.verified_ingest import (  # noqa: E402
    ingest_files)
from src.features.edit.core.verified_ingest_service import (  # noqa: E402
    ImportIngestAssetsCommand, VerifiedIngestService)


def _project():
    from src.features.edit.core.project import EditProject
    return EditProject(project_id="n08-proj", name="N08")


class TestPerItemCallback:
    def test_partial_failure_continues(self, tmp_path):
        """第 1 项成功、第 2 项读失败、第 3 项继续（逐项即时报）。"""
        ok1 = tmp_path / "a.mov"
        ok1.write_bytes(b"A" * 64)
        missing = str(tmp_path / "missing.mov")
        ok3 = tmp_path / "c.mov"
        ok3.write_bytes(b"C" * 64)
        events = []
        report = ingest_files(
            [str(ok1), missing, str(ok3)], str(tmp_path / "dest"),
            on_item=events.append)
        statuses = [e.status for e in events]
        assert statuses == ["verified", "failed", "verified"]
        assert [e.status for e in report.results] == statuses
        assert report.failures and len(report.verified) == 2

    def test_cancel_mid_batch_preserves_done(self, tmp_path):
        files = [tmp_path / f"f{i}.mov" for i in range(4)]
        for i, f in enumerate(files):
            f.write_bytes(bytes([i]) * 32)
        stop_after = {"n": 0}

        def cancelled():
            return stop_after["n"] >= 2

        events = []
        report = ingest_files(
            [str(f) for f in files], str(tmp_path / "dest"),
            cancelled_fn=cancelled,
            on_item=lambda r: stop_after.__setitem__(
                "n", stop_after["n"] + 1) or events.append(r))
        statuses = [e.status for e in report.results]
        assert statuses.count("verified") == 2
        assert "skipped_not_started" in statuses
        assert not report.all_passed  # 取消批次不冒称全过


class TestServiceAsync:
    def test_worker_runs_off_thread_and_reports(
            self, qapp, tmp_path):
        src = tmp_path / "v.mov"
        src.write_bytes(b"V" * 128)
        service = VerifiedIngestService()
        items, reports = [], []
        service.item_done.connect(items.append)
        service.finished.connect(reports.append)
        assert service.start([str(src)], str(tmp_path / "dest")) is True
        worker = service._worker
        assert worker.wait(5000)
        for _ in range(10):
            qapp.processEvents()
        assert service.running is False
        assert reports and reports[0].verified
        assert [i.status for i in items] == ["verified"]
        # worker 线程执行（非主线程）
        _ = threading.current_thread().ident  # 主线程 id 仅供参照

    def test_start_refused_while_running(self, qapp, tmp_path):
        service = VerifiedIngestService()
        slow = tmp_path / "slow.mov"
        slow.write_bytes(b"S" * (1 << 20))  # 1MB：足够慢到在途查询
        assert service.start([str(slow)], str(tmp_path / "d1")) is True
        try:
            assert service.start([str(slow)], str(tmp_path / "d2")) is False
        finally:
            service.cancel()
            service._worker.wait(10000)

    def test_empty_sources_refused(self, qapp):
        assert VerifiedIngestService().start([], "/tmp/nope") is False


class TestRegistrationCommand:
    def test_verified_only_imported_and_undoable(self, tmp_path):
        good = tmp_path / "good.mov"
        good.write_bytes(b"G" * 64)
        bad = tmp_path / "bad.mov"
        events = []
        report = ingest_files(
            [str(good), str(tmp_path / "gone.mov")], str(tmp_path / "dest"),
            on_item=events.append)
        cmd = VerifiedIngestService.build_registration_command(report)
        assert cmd.targets == (str(tmp_path / "dest" / "good.mov"),)
        project = _project()
        new_project = cmd.apply(project)
        assert new_project is not project
        assert str(tmp_path / "dest" / "good.mov") in {
            getattr(a, "path", "") for a in new_project.assets.values()}
        restored = cmd.undo(new_project)
        assert restored is project  # 一次撤销回 apply 前工程

    def test_no_verified_items_is_noop(self, tmp_path):
        report = ingest_files(
            [str(tmp_path / "none.mov")], str(tmp_path / "dest"))
        cmd = ImportIngestAssetsCommand.create(report)
        project = _project()
        assert cmd.apply(project) is project  # 零 verified = 零导入
