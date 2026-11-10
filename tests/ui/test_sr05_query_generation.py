"""SR05：异步请求的代际归属、回填与提交原子性验收。

对应《第二轮复查计划》SR05（2026-11-10）：
- **代际随请求**：旧任务排队期间切代际、最新请求并入 pending——工作
  项消费**请求自己的**代际，最新结果不再被旧工作项的代际当 stale
  丢弃（复现场景）；
- 排队期间被代际切换丢弃的任务计入 ``dropped_stale``（未执行即取消）；
- pending 相册数有硬上限（FIFO 丢最旧，``dropped_overflow``）；
- submit／shutdown 原子：关闭竞态下提交不抛异常、返回 False；
- 面板：同步扫描共用单调 token——同步完成后迟到的旧异步结果不回填；
  导入/改名在大库异步回填未就绪时的待选随 token 回填。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.color.core.gallery_contracts import GalleryQuery
from src.features.color.core.gallery_query_service import (
    AsyncGalleryQueryRunner,
    GalleryQueryIndex,
)


class _BlockingIndex:
    """可控阻塞的索引：``block`` 相册先卡住工作线程直到放行。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def query(self, query: GalleryQuery) -> object:
        if query.album_id == "block":
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("test worker timed out")
        return object()


def _drain(runner: AsyncGalleryQueryRunner) -> None:
    runner._executor.submit(lambda: None).result(timeout=5)


class TestGenerationOwnership:
    def test_latest_request_survives_generation_change(self):
        """复现场景（repro SR05）：最新请求被旧代际任务消费后必须回填。"""
        index = _BlockingIndex()
        delivered: list = []
        runner = AsyncGalleryQueryRunner(
            index, on_result=lambda q, _: delivered.append(q.request_revision))
        try:
            assert runner.submit(GalleryQuery(album_id="block"))
            assert index.started.wait(5)
            assert runner.submit(GalleryQuery(album_id="A",
                                              request_revision=1))
            runner.begin_generation()
            assert runner.submit(GalleryQuery(album_id="A",
                                              request_revision=2))
            index.release.set()
            _drain(runner)
            assert 2 in delivered          # 最新请求的结果没有丢
        finally:
            index.release.set()
            runner.shutdown()

    def test_queued_task_discarded_before_execution_counts_stale(self):
        index = _BlockingIndex()
        runner = AsyncGalleryQueryRunner(index, on_result=lambda *_: None)
        try:
            assert runner.submit(GalleryQuery(album_id="A"))
            runner.begin_generation()       # 排队任务还没执行就被作废
            _drain(runner)
            stats = runner.stats
            assert stats["dropped_stale"] >= 1
            assert stats["delivered"] == 0
        finally:
            runner.shutdown()


class TestBoundedPending:
    def test_pending_album_overflow_drops_oldest(self):
        index = _BlockingIndex()
        runner = AsyncGalleryQueryRunner(index, on_result=lambda *_: None)
        try:
            first = "album-000"
            assert runner.submit(GalleryQuery(album_id=first))
            for i in range(1, runner.MAX_PENDING_ALBUMS + 2):
                assert runner.submit(GalleryQuery(
                    album_id=f"album-{i:03d}"))
            assert runner.stats["dropped_overflow"] >= 1
            assert runner.pending_count <= runner.MAX_PENDING_ALBUMS
        finally:
            runner.shutdown()


class TestSubmitShutdownAtomicity:
    def test_submit_racing_shutdown_is_rejected_not_raised(self):
        index = _BlockingIndex()
        runner = AsyncGalleryQueryRunner(index, on_result=lambda *_: None)
        runner.shutdown()
        for _ in range(8):
            assert runner.submit(GalleryQuery(album_id="A")) is False
        assert runner.is_shutdown

    def test_submit_after_shutdown_from_other_thread(self):
        index = _BlockingIndex()
        runner = AsyncGalleryQueryRunner(index, on_result=lambda *_: None)
        errors: list = []
        stop = threading.Event()

        def spam() -> None:
            while not stop.is_set():
                try:
                    runner.submit(GalleryQuery(album_id="A"))
                except Exception as exc:            # noqa: BLE001
                    errors.append(exc)
                    return

        worker = threading.Thread(target=spam)
        worker.start()
        time.sleep(0.02)
        runner.shutdown()
        stop.set()
        worker.join(timeout=5)
        assert errors == []                # 关闭竞态不产生异常


class _PanelCase:
    @staticmethod
    def make_panel(qapp, monkeypatch, tmp_path: Path):
        monkeypatch.chdir(tmp_path)
        from src.features.color.ui import presets_panel
        monkeypatch.setattr(presets_panel, "_resolve_presets_root",
                            lambda: str(tmp_path / "Presets"))
        panel = presets_panel.PresetsPanel()
        return panel

    @staticmethod
    def pump(qapp, rounds: int = 60) -> None:
        from PyQt6.QtCore import QEventLoop
        for _ in range(rounds):
            qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)


def _seed(folder: Path, stem: str, *, still_id: str = "S-sr05") -> None:
    import json
    import zipfile
    from src.features.color.core.preset_meta import (
        PresetMeta, save_preset_meta,
    )
    folder.mkdir(parents=True, exist_ok=True)
    topos = folder / f"{stem}.topos"
    with zipfile.ZipFile(topos, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))
    (folder / f"{stem}.thumb.jpg").write_bytes(b"thumb")
    save_preset_meta(str(topos), PresetMeta(still_id=still_id))


class TestPanelTokenUnification:
    def test_sync_scan_token_drops_late_async_result(self, qapp,
                                                     monkeypatch,
                                                     tmp_path):
        """同步扫描递增 token：随后的旧异步回填不得覆盖同步结果。"""
        root = tmp_path / "Presets"
        (root / "User").mkdir(parents=True)
        for index in range(3):
            _seed(root / "User", f"Async{index}")
        panel = _PanelCase.make_panel(qapp, monkeypatch, tmp_path)
        panel._async_query_threshold = 0
        panel.scan_presets()               # 异步提交（token=N）
        panel._async_query_threshold = 10 ** 9
        panel.scan_presets()               # 同步完成（token=N+1）
        assert panel.list_widget.count() == 3
        _PanelCase.pump(qapp)              # 迟到的异步结果到达
        assert panel.list_widget.count() == 3   # 未被清空/回滚
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_pending_selection_backfills_after_async_scan(self, qapp,
                                                          monkeypatch,
                                                          tmp_path):
        """大库异步未回填时导入的待选：回填后自动选中（SR05.4）。"""
        root = tmp_path / "Presets"
        (root / "User").mkdir(parents=True)
        _seed(root / "User", "Existing", still_id="S-exist")
        panel = _PanelCase.make_panel(qapp, monkeypatch, tmp_path)
        panel._async_query_threshold = 0
        panel.scan_presets()
        _PanelCase.pump(qapp)
        assert panel.list_widget.count() == 1
        # 模拟异步在途时的新条目（列表尚未包含）
        _seed(root / "User", "Fresh", still_id="S-fresh")
        panel._async_query_threshold = 0
        panel.scan_presets()               # 新一轮异步（回填前列表仍旧）
        panel._select_items_by_stems(("Fresh",))   # 列表里还没有 → 待选
        assert panel._pending_selection is not None
        _PanelCase.pump(qapp)              # 异步回填（含 Fresh）
        assert panel.list_widget.count() == 2
        selected = [item.text() for item in
                    panel.list_widget.selectedItems()]
        assert selected == ["Fresh"]       # 待选随回填生效
        panel._query_service.shutdown()
        panel.deleteLater()
