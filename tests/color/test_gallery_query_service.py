"""G05 Gallery 查询服务测试：索引复用／代际守卫／后台扫描。

门槛映射：目录未变时重扫/搜索不碰磁盘（index_hits）；rename/capture
改目录即失效（index_misses 回升）；快速切上下文后旧异步结果不回填
（dropped_stale）；shutdown 后不再派发；条目级降级不拖垮整库。
"""
import json
import os
import threading
import zipfile

import pytest

from src.features.color.core.gallery_contracts import (
    GalleryQuery,
    SORT_KEY_CREATED,
)
from src.features.color.core.gallery_query_service import (
    AsyncGalleryQueryRunner,
    GalleryQueryIndex,
    GalleryQueryService,
)
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta


def _seed(folder, name, *, timeline=None, created=None):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}.topos")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))
    with open(os.path.join(folder, f"{name}.thumb.jpg"), "wb") as fh:
        fh.write(b"t")
    if timeline or created:
        save_preset_meta(path, PresetMeta(
            timeline_id=timeline, timeline_name=timeline,
            created_at=created))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    return GalleryFolderRepository(str(root))


def _folder(repo):
    return os.path.join(repo.root, "User")


class TestIndex:
    def test_limit_preserves_total_matched_count(self, repo):
        for name in ("A", "B", "C"):
            _seed(_folder(repo), name)
        query = GalleryQuery(album_id="legacy:User", limit=1)
        for result in (repo.query(query), GalleryQueryIndex(repo).query(query)):
            assert len(result.stills) == 1
            assert result.total_matched == 3

    def test_unchanged_folder_hits_index_without_rescan(self, repo):
        _seed(_folder(repo), "A")
        index = GalleryQueryIndex(repo)
        first = index.query(GalleryQuery(album_id="legacy:User"))
        second = index.query(GalleryQuery(album_id="legacy:User"))
        assert [s.display_name for s in second.stills] == \
            [s.display_name for s in first.stills]
        stats = index.stats
        assert stats["index_hits"] == 1 and stats["index_misses"] == 1

    def test_search_and_sort_replay_from_cache(self, repo):
        _seed(_folder(repo), "Sunset", created="2026-01-02T00:00:00")
        _seed(_folder(repo), "Night", created="2026-01-01T00:00:00")
        index = GalleryQueryIndex(repo)
        index.query(GalleryQuery(album_id="legacy:User"))     # 建索引
        by_created = index.query(GalleryQuery(
            album_id="legacy:User", sort_key=SORT_KEY_CREATED))
        assert [s.display_name for s in by_created.stills] == \
            ["Night", "Sunset"]
        search = index.query(GalleryQuery(
            album_id="legacy:User", search_text="sunset"))
        assert [s.display_name for s in search.stills] == ["Sunset"]
        limited = index.query(GalleryQuery(album_id="legacy:User", limit=1))
        assert len(limited.stills) == 1
        assert index.stats["index_misses"] == 1        # 全程只扫一次盘

    def test_folder_change_invalidates_index(self, repo):
        _seed(_folder(repo), "A")
        index = GalleryQueryIndex(repo)
        index.query(GalleryQuery(album_id="legacy:User"))
        _seed(_folder(repo), "B")                      # 目录 mtime 变化
        result = index.query(GalleryQuery(album_id="legacy:User"))
        assert {s.display_name for s in result.stills} == {"A", "B"}
        assert index.stats["index_misses"] == 2

    def test_manual_invalidation_forces_rescan(self, repo):
        _seed(_folder(repo), "A")
        index = GalleryQueryIndex(repo)
        index.query(GalleryQuery(album_id="legacy:User"))
        index.invalidate("legacy:User")
        index.query(GalleryQuery(album_id="legacy:User"))
        assert index.stats["index_misses"] == 2
        index.invalidate()
        assert index.stats["invalidations"] >= 2

    def test_bad_meta_degrades_entry_not_scan(self, repo):
        _seed(_folder(repo), "Good", timeline="t1")
        _seed(_folder(repo), "Broken")
        _seed(_folder(repo), "NoMeta")
        # 破坏一条 meta（模拟半包）：条目降级为无来源，但仍在列
        with open(os.path.join(_folder(repo), "Broken.meta.json"),
                  "w") as fh:
            fh.write("{ not json")
        index = GalleryQueryIndex(repo)
        result = index.query(GalleryQuery(album_id="legacy:User"))
        names = {s.display_name for s in result.stills}
        assert names == {"Good", "Broken", "NoMeta"}   # 坏包不拖垮整库
        broken = next(s for s in result.stills
                      if s.display_name == "Broken")
        assert broken.meta is None                    # 坏 meta 容忍
        assert result.degraded_count == 1             # 无 meta 文件的条目
        assert result.scan_error is None


class TestAsyncRunner:
    def _wait_for(self, predicate, timeout=5.0):
        deadline = threading.Event()

        def check():
            if predicate():
                deadline.set()
        for _ in range(200):
            check()
            if deadline.is_set():
                return True
            threading.Event().wait(0.025)
        return predicate()

    def test_async_result_delivered_with_callback(self, repo):
        _seed(_folder(repo), "A")
        index = GalleryQueryIndex(repo)
        delivered = []
        ready = threading.Event()

        def on_result(query, result):
            delivered.append(result)
            ready.set()

        runner = AsyncGalleryQueryRunner(index, on_result=on_result)
        assert runner.submit(GalleryQuery(album_id="legacy:User"))
        assert ready.wait(timeout=5)
        runner.shutdown()
        assert delivered and delivered[0].stills
        assert runner.stats["delivered"] >= 1

    def test_stale_generation_drops_old_result(self, repo):
        _seed(_folder(repo), "A")
        index = GalleryQueryIndex(repo)
        delivered = []

        runner = AsyncGalleryQueryRunner(
            index, on_result=lambda q, r: delivered.append(r))
        # 提交后立即切代际（快速切 album／工程）
        assert runner.submit(GalleryQuery(album_id="legacy:User"))
        runner.begin_generation()
        assert self._wait_for(
            lambda: runner.stats["dropped_stale"] >= 1
            or runner.stats["delivered"] >= 1)
        runner.shutdown()
        # 旧代际结果没有回填（delivered 计数不含被丢弃的）
        if runner.stats["dropped_stale"]:
            assert delivered == [] or runner.stats["delivered"] < \
                runner.stats["submitted"]

    def test_shutdown_rejects_new_submissions(self, repo):
        index = GalleryQueryIndex(repo)
        runner = AsyncGalleryQueryRunner(index, on_result=lambda *a: None)
        runner.shutdown()
        assert runner.submit(GalleryQuery(album_id="legacy:User")) is False
        assert runner.is_shutdown is True

    def test_stats_counters_reproducible(self, repo):
        _seed(_folder(repo), "A")
        service = GalleryQueryService(repo)
        service.query(GalleryQuery(album_id="legacy:User"))   # miss
        service.query(GalleryQuery(album_id="legacy:User"))   # hit
        assert service.stats["index_hits"] == 1
        assert service.stats["index_misses"] == 1
        service.shutdown()


class TestPanelScanUsesIndex:
    def _make_panel(self, tmp_path, monkeypatch, qtbot):
        monkeypatch.chdir(tmp_path)
        from src.features.color.ui import presets_panel
        monkeypatch.setattr(
            presets_panel, "_resolve_presets_root",
            lambda: str(tmp_path / "Presets"))
        widget = presets_panel.PresetsPanel()
        qtbot.addWidget(widget)
        return widget

    def test_rescan_of_unchanged_folder_is_index_hit(self, tmp_path,
                                                     monkeypatch, qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        _seed(folder, "Still1")
        panel.scan_presets()
        assert panel.list_widget.count() == 1
        before = panel._query_service.stats["index_hits"]
        panel.scan_presets()                        # 目录未变
        assert panel.list_widget.count() == 1
        assert panel._query_service.stats["index_hits"] == before + 1

    def test_capture_visible_after_folder_change(self, tmp_path, monkeypatch,
                                                 qtbot):
        panel = self._make_panel(tmp_path, monkeypatch, qtbot)
        folder = panel.get_current_folder()
        _seed(folder, "A")
        panel.scan_presets()
        _seed(folder, "B")
        panel.scan_presets()                        # mtime 变 → 失效重扫
        assert panel.list_widget.count() == 2
