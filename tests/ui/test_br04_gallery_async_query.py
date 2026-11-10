"""BR04：查询失效协议、后台 runner 与面板异步回填验收。

对应《Gallery/Viewport 后端复查整改计划》BR04（2026-11-10）：
- 分层文件签名（逐条目 stat 多重集）：原地改 meta/sidecar 不改目录
  mtime 也失效；扫描途中 invalidate 的结果不回填缓存；扫描失败不缓存
  成完整空库；
- runner 同 album 合并保留最新（快速切相册/搜索时队列有界）、shutdown
  丢弃未执行任务；
- 面板：大相册走后台 runner，结果经队列化桥回 GUI 线程并复核代际/
  相册；showEvent 按签名同步外部原地修改；选择按 still_id 跟随
  （无 ID 条目回退路径）；
- 搜索补齐 label/still_id/timeline_name。
"""
from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.color.core.gallery_contracts import GalleryQuery
from src.features.color.core.gallery_query_service import (
    AsyncGalleryQueryRunner,
    GalleryQueryIndex,
    GalleryQueryService,
)
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.preset_meta import PresetMeta, matches_query, \
    save_preset_meta


def _seed(folder: Path, stem: str, *, still_id: str = None,
          label: str = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / f"{stem}.topos", "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))
    (folder / f"{stem}.thumb.jpg").write_bytes(b"t")
    meta = PresetMeta(still_id=still_id, label=label)
    save_preset_meta(str(folder / f"{stem}.topos"), meta)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    for section in ("User", "System"):
        (tmp_path / "Presets" / section).mkdir(parents=True)
    return tmp_path / "Presets"


ALBUM = "legacy:User"


class TestLayeredSignature:
    def test_in_place_meta_write_invalidates_cache(self, root: Path):
        _seed(root / "User", "Look", still_id="S-1", label="before")
        repo = GalleryFolderRepository(str(root))
        service = GalleryQueryService(repo)
        query = GalleryQuery(album_id=ALBUM)
        assert service.query(query).stills[0].meta.label == "before"
        # 原地改 meta：不改目录条目，只改文件内容
        meta_path = root / "User" / "Look.meta.json"
        meta = json.loads(meta_path.read_text())
        meta["label"] = "after"
        meta_path.write_text(json.dumps(meta))
        stills = service.query(query).stills
        assert stills[0].meta.label == "after"

    def test_invalidate_during_scan_rejects_cache_publish(self, root,
                                                          monkeypatch):
        _seed(root / "User", "Look")
        repo = GalleryFolderRepository(str(root))
        service = GalleryQueryService(repo)
        original = repo.query

        def slow_query(*args, **kwargs):
            result = original(*args, **kwargs)
            service.index.invalidate()      # 扫描途中失效（真实并发窗口）
            return result

        monkeypatch.setattr(repo, "query", slow_query)
        service.query(GalleryQuery(album_id=ALBUM))
        monkeypatch.undo()
        assert service.index.stats["publish_rejected"] >= 1
        # 未回填缓存：下一次查询重新扫磁盘（miss 而非 hit）
        misses_before = service.index.stats["index_misses"]
        service.query(GalleryQuery(album_id=ALBUM))
        assert service.index.stats["index_misses"] == misses_before + 1

    def test_failed_scan_not_cached_as_empty_library(self, root,
                                                     monkeypatch):
        _seed(root / "User", "Look")
        repo = GalleryFolderRepository(str(root))
        service = GalleryQueryService(repo)
        original = repo.query
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                from src.features.color.core.gallery_contracts import (
                    GalleryQueryResult,
                )
                return GalleryQueryResult(
                    album=None, stills=(), request_revision=0,
                    scan_error="device gone")
            return original(*args, **kwargs)

        monkeypatch.setattr(repo, "query", flaky)
        first = service.query(GalleryQuery(album_id=ALBUM))
        assert first.scan_error == "device gone"
        second = service.query(GalleryQuery(album_id=ALBUM))
        assert second.scan_error is None and second.stills
        assert calls["n"] == 2               # 失败结果没有挡住重试

    def test_signature_ignores_hidden_txn_dirs(self, root: Path):
        user = root / "User"
        _seed(user, "Look")
        repo = GalleryFolderRepository(str(root))
        base = GalleryQueryIndex.folder_signature(str(user))
        (user / ".still-txn-probe").mkdir()
        (user / ".still-txn-probe" / "x").write_bytes(b"y")
        assert GalleryQueryIndex.folder_signature(str(user)) == base


class TestRunnerBackpressure:
    def test_same_album_submissions_coalesce_to_latest(self, root: Path):
        for index in range(3):
            _seed(root / "User", f"Look{index}")
        repo = GalleryFolderRepository(str(root))
        index = GalleryQueryIndex(repo)

        original = repo.query

        def slow(*args, **kwargs):
            time.sleep(0.02)
            return original(*args, **kwargs)

        repo.query = slow
        delivered = []
        runner = AsyncGalleryQueryRunner(
            index, on_result=lambda q, r: delivered.append(q))
        for revision in range(50):           # 快速连发同 album 查询
            runner.submit(GalleryQuery(album_id=ALBUM,
                                       request_revision=revision))
            assert runner.pending_count <= 1      # 有界：合并保留最新
        deadline = time.monotonic() + 10
        while len(delivered) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        runner.shutdown()
        assert delivered                     # 至少交付最新之一
        stats = runner.stats
        assert stats["dropped_superseded"] >= 45   # 旧请求被最新取代
        assert len(delivered) <= 5           # 不逐条排队回填

    def test_shutdown_drops_pending(self, root: Path):
        _seed(root / "User", "Look")
        repo = GalleryFolderRepository(str(root))
        original = repo.query
        repo.query = lambda *a, **k: (time.sleep(0.2),
                                     original(*a, **k))[1]
        index = GalleryQueryIndex(repo)
        delivered = []
        runner = AsyncGalleryQueryRunner(
            index, on_result=lambda q, r: delivered.append(q))
        runner.submit(GalleryQuery(album_id=ALBUM))
        runner.shutdown()
        time.sleep(0.4)
        assert delivered == []               # shutdown 后未执行任务丢弃
        assert runner.submit(GalleryQuery(album_id=ALBUM)) is False

    def test_begin_generation_drops_stale_results(self, root: Path):
        _seed(root / "User", "Look")
        repo = GalleryFolderRepository(str(root))
        original = repo.query

        def slow(*args, **kwargs):
            time.sleep(0.05)
            return original(*args, **kwargs)

        repo.query = slow
        index = GalleryQueryIndex(repo)
        delivered = []
        runner = AsyncGalleryQueryRunner(
            index, on_result=lambda q, r: delivered.append(q))
        runner.submit(GalleryQuery(album_id=ALBUM))
        runner.begin_generation()            # 提交后立即切走上下文
        time.sleep(0.3)
        assert delivered == []
        assert runner.stats["dropped_stale"] >= 1


class TestSearchFields:
    def test_matches_query_covers_new_metadata(self):
        meta = PresetMeta(still_id="S-abc123", label="夜景",
                          timeline_name="Main Cut")
        assert matches_query("Look", meta, "S-abc123")
        assert matches_query("Look", meta, "夜景")
        assert matches_query("Look", meta, "main cut")
        assert not matches_query("Look", meta, "不存在词")


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
    def pump(qapp, rounds: int = 40) -> None:
        from PyQt6.QtCore import QEventLoop
        for _ in range(rounds):
            qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)


class TestPanelAsyncScan:
    def test_large_album_scans_off_ui_thread(self, qapp, monkeypatch,
                                             tmp_path):
        root = tmp_path / "Presets"
        (root / "User").mkdir(parents=True)
        for index in range(3):
            _seed(root / "User", f"Look{index}")
        panel = _PanelCase.make_panel(qapp, monkeypatch, tmp_path)
        panel._async_query_threshold = 0     # 强制走后台路径
        panel.scan_presets()
        _PanelCase.pump(qapp)                # 等队列化回填
        assert panel.list_widget.count() == 3
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_stale_generation_result_discarded(self, qapp, monkeypatch,
                                               tmp_path):
        root = tmp_path / "Presets"
        (root / "User").mkdir(parents=True)
        for index in range(3):
            _seed(root / "User", f"Look{index}")
        panel = _PanelCase.make_panel(qapp, monkeypatch, tmp_path)
        panel._async_query_threshold = 0
        panel.list_widget.clear()
        panel.scan_presets()
        panel._scan_generation += 99          # 提交后立即有更新请求在途
        _PanelCase.pump(qapp)
        assert panel.list_widget.count() == 0  # 旧结果不回填
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_switched_album_result_discarded(self, qapp, monkeypatch,
                                             tmp_path):
        root = tmp_path / "Presets"
        (root / "User").mkdir(parents=True)
        (root / "System").mkdir(parents=True)
        for index in range(3):
            _seed(root / "User", f"Look{index}")
        panel = _PanelCase.make_panel(qapp, monkeypatch, tmp_path)
        panel._async_query_threshold = 0
        panel.list_widget.clear()
        panel.scan_presets()                  # 针对 User 提交
        panel._current_folder = str(root / "System")   # 用户切走
        _PanelCase.pump(qapp)
        assert panel.list_widget.count() == 0  # 旧相册结果不回填
        panel._query_service.shutdown()
        panel.deleteLater()


class TestPanelSelectionAndRefresh:
    def test_selection_follows_still_id_across_rename(self, qapp,
                                                      monkeypatch,
                                                      tmp_path):
        root = tmp_path / "Presets"
        (root / "User").mkdir(parents=True)
        _seed(root / "User", "Old", still_id="S-keep")
        _seed(root / "User", "Other")
        panel = _PanelCase.make_panel(qapp, monkeypatch, tmp_path)
        assert panel.list_widget.count() == 2
        old_item = next(panel.list_widget.item(i)
                        for i in range(panel.list_widget.count())
                        if panel.list_widget.item(i).text() == "Old")
        old_item.setSelected(True)
        # 外部改名（bundle 侧路径变了，still_id 不变）
        for suffix in (".topos", ".thumb.jpg", ".meta.json"):
            os.rename(root / "User" / f"Old{suffix}",
                      root / "User" / f"New{suffix}")
        panel.scan_presets()
        selected = [item.text() for item in panel.list_widget.selectedItems()]
        assert selected == ["New"]           # 选择跟随 still_id
        panel._query_service.shutdown()
        panel.deleteLater()

    def test_show_event_rescans_after_in_place_meta_write(
            self, qapp, monkeypatch, tmp_path):
        root = tmp_path / "Presets"
        (root / "User").mkdir(parents=True)
        _seed(root / "User", "Look", label="before")
        panel = _PanelCase.make_panel(qapp, monkeypatch, tmp_path)
        assert panel.list_widget.count() == 1
        # 原地改 label（不改目录条目）
        meta_path = root / "User" / "Look.meta.json"
        meta = json.loads(meta_path.read_text())
        meta["label"] = "after"
        meta_path.write_text(json.dumps(meta))
        rescans = []
        original_scan = panel.scan_presets

        def spy(*args, **kwargs):
            rescans.append(1)
            return original_scan(*args, **kwargs)

        panel.scan_presets = spy
        panel.show()                         # 触发 showEvent
        _PanelCase.pump(qapp)
        panel.hide()
        assert rescans                        # 签名变化 → 重扫
        panel._query_service.shutdown()
        panel.deleteLater()
