"""D3：目录/搜索分页——真实续页，total/truncated 一致，选择不丢。

transport 口径用真实 facade（PAGE_SIZE 实例级收紧）；面板口径验证
首页/续页、状态按钮、防抖后旧结果不回写视图。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QItemSelectionModel  # noqa: E402

from src.app.ui.native_files_panel import NativeFilesPanel  # noqa: E402
from src.app.ui.native_files_transport import NativeFilesTransport  # noqa: E402
from src.features.files.repository import FilesDataLocator  # noqa: E402
from src.features.files.repository.catalog import FilesCatalog  # noqa: E402
from src.features.files.services.facade import FilesFacade  # noqa: E402


def _entry_dicts(count, start=0):
    return [{"name": f"f{i:04}.png", "path": f"/media/f{i:04}.png",
             "type": "image", "size_bytes": 1} for i in range(start, start + count)]


# ---------------------------------------------------------------------------
# transport：offset 续页 + page_meta
# ---------------------------------------------------------------------------

def test_transport_directory_pages_with_total_and_truncated(qtbot, tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    for index in range(7):
        (lib / f"f{index}.png").write_bytes(b"x")
    transport = NativeFilesTransport(
        lambda: "", None, facade=FilesFacade(catalog, max_workers=1))
    transport.PAGE_SIZE = 3  # 实例级收紧：7 项 → 3/3/1 三页
    try:
        pages = []
        metas = []
        transport.loaded.connect(lambda path, entries: pages.append(entries))
        transport.page_meta.connect(
            lambda path, total, truncated: metas.append((total, truncated)))

        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(lib))
        assert len(pages[0]) == 3
        assert metas[0] == (7, True)

        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(lib), offset=3)
        assert len(pages[1]) == 3
        assert metas[1] == (7, True)

        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(lib), offset=6)
        assert len(pages[2]) == 1
        assert metas[2] == (7, False)
        names = [entry.name for page in pages for entry in page]
        assert len(set(names)) == 7  # 三页合计无重复
    finally:
        transport.shutdown()
        catalog.close()


def test_transport_more_page_does_not_cancel_generation(qtbot, tmp_path):
    """续页不 bump 代际：晚到的续页与首页同代投递（导航才作废）。"""
    locator = FilesDataLocator.for_config_root(tmp_path / "d2")
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    catalog.register_library(tmp_path / "d2" / "lib")
    lib = tmp_path / "d2" / "lib"
    lib.mkdir()
    for index in range(5):
        (lib / f"g{index}.png").write_bytes(b"x")
    transport = NativeFilesTransport(
        lambda: "", None, facade=FilesFacade(catalog, max_workers=1))
    transport.PAGE_SIZE = 4
    try:
        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(lib))
        before = transport._nav_generation
        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(lib), offset=4)
        assert transport._nav_generation == before
    finally:
        transport.shutdown()
        catalog.close()


# ---------------------------------------------------------------------------
# 面板：首页/续页语义、按钮状态、选择保持
# ---------------------------------------------------------------------------

def _panel(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", _entry_dicts(2))
    return panel


def test_panel_first_page_then_append_preserves_selection(qtbot):
    panel = _panel(qtbot)
    panel.set_page_meta("/media", 12, True)
    assert panel.load_more_button.isVisibleTo(panel)
    assert "10" in panel.load_more_button.text()  # 还有 12-2 项

    panel.selection.select(
        panel.proxy.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
    panel._append_next_page = True
    panel.show_directory_page("/media", _entry_dicts(10, start=2))
    assert len(panel.model.entries) == 12
    assert len(panel.selection.selectedRows(0)) == 1  # 选择不因续页丢失
    assert "/media/f0011.png" in panel._entry_paths
    panel.set_page_meta("/media", 12, False)
    assert not panel.load_more_button.isVisibleTo(panel)


def test_panel_load_more_emits_directory_offset(qtbot):
    panel = _panel(qtbot)
    panel.set_page_meta("/media", 20, True)
    emitted = []
    panel.directory_more_requested.connect(
        lambda path, offset: emitted.append((path, offset)))
    panel._load_more()
    assert emitted == [("/media", 2)]  # offset = 已消费条数


def test_panel_stale_page_meta_does_not_apply(qtbot):
    panel = _panel(qtbot)
    panel.set_page_meta("/elsewhere", 99, True)
    assert panel._view_total == 0 and not panel._view_truncated
    assert not panel.load_more_button.isVisibleTo(panel)


def test_panel_navigation_resets_pagination_state(qtbot):
    panel = _panel(qtbot)
    panel.set_page_meta("/media", 12, True)
    assert panel._view_truncated
    panel.set_loading("/other")  # 导航：作废未回的续页与分页状态
    assert panel._view_total == 0 and not panel._view_truncated
    assert not panel._append_next_page
    assert not panel.load_more_button.isVisibleTo(panel)


def test_panel_search_first_page_and_append(qtbot):
    panel = _panel(qtbot)
    payload = {"files": _entry_dicts(2, start=100),
               "total": 5, "truncated": True, "query": "f"}
    panel._last_search_query = "f"
    panel.show_library_results(payload)
    assert panel._view_location["kind"] == "library-search"
    assert panel.load_more_button.isVisibleTo(panel)
    assert "3" in panel.load_more_button.text()

    emitted = []
    panel.library_search_requested.connect(
        lambda q, limit, offset: emitted.append((q, limit, offset)))
    panel._load_more()
    assert emitted == [("f", 200, 2)]

    panel._append_next_page = True
    panel.show_library_results({"files": _entry_dicts(3, start=102),
                                "total": 5, "truncated": False, "query": "f"})
    assert len(panel.model.entries) == 5
    assert not panel.load_more_button.isVisibleTo(panel)
    assert "5" in panel.status.text()


def test_panel_debounced_search_keeps_only_latest_view(qtbot):
    """防抖后旧结果不保留写权：新查询重建视图，旧 payload 迟到被换。"""
    panel = _panel(qtbot)
    panel._last_search_query = "needle"
    panel.show_library_results({"files": _entry_dicts(1, start=200),
                                "total": 1, "truncated": False, "query": "needle"})
    first_path = panel.model.entries[0].path
    # 新查询首屏到达：视图重建，旧条目不再占据模型
    panel.show_library_results({"files": _entry_dicts(1, start=300),
                                "total": 1, "truncated": False, "query": "other"})
    assert panel.model.entries[0].path != first_path
    assert len(panel.model.entries) == 1


# ---------------------------------------------------------------------------
# D 表第 2 项：不可变 location/request/generation——新意图作废旧上下文
# ---------------------------------------------------------------------------

def test_tree_not_invalidated_by_content_navigation(qtbot, tmp_path):
    """树请求独立于内容导航：目录导航不得作废已展开的树节点。"""
    locator = FilesDataLocator.for_config_root(tmp_path / "t")
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    catalog.register_library(tmp_path / "t" / "lib")
    lib = tmp_path / "t" / "lib"
    lib.mkdir(parents=True)
    (lib / "sub").mkdir()
    (lib / "f.png").write_bytes(b"x")
    transport = NativeFilesTransport(
        lambda: "", None, facade=FilesFacade(catalog, max_workers=1))
    try:
        with qtbot.waitSignal(transport.tree_loaded, timeout=8000):
            transport.load_tree(str(lib))
        # 树节点仍在待定集合之外：目录导航（cancel）不影响已完成的树；
        # 再发一次树请求也照常送达
        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(lib))
        with qtbot.waitSignal(transport.tree_loaded, timeout=8000) as again:
            transport.load_tree(str(lib))
        assert [item["name"] for item in again.args[1]] == ["sub"]
    finally:
        transport.shutdown()
        catalog.close()


def test_collection_switch_discards_stale_response(qtbot, tmp_path):
    """连续切换集合：旧集合响应晚到不得覆盖新集合成员。"""
    from src.features.files.contracts import identity_key

    class _GatedCollectionsFacade(FilesFacade):
        """闸住首个 collection_members 工作，制造旧响应在途。"""

        def collection_members(self, collection_id, *, request_id, listener):
            import threading as _t
            if not getattr(self, "_gate", None):
                self._gate = _t.Event()
            gate = self._gate
            def _work():
                gate.wait(5)
                library_id = self.collections.library_of(collection_id)
                return self.query.entries_for_paths(
                    self.collections.member_paths(collection_id),
                    request_id=request_id,
                    path_label=f"collection://{collection_id}",
                    library_id=library_id)
            return self.run(key="collection", request_id=request_id,
                            work=_work, listener=listener)

    locator = FilesDataLocator.for_config_root(tmp_path / "c")
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    library = catalog.register_library(tmp_path / "c" / "lib")
    lib = tmp_path / "c" / "lib"
    lib.mkdir()
    (lib / "one.png").write_bytes(b"1")
    (lib / "two.png").write_bytes(b"2")
    cid = catalog.create_collection(library.library_id, "c1")
    catalog.add_to_collection(cid, str(lib / "one.png"))
    other = catalog.create_collection(library.library_id, "c2")
    catalog.add_to_collection(other, str(lib / "two.png"))
    facade = _GatedCollectionsFacade(catalog, max_workers=1)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    loaded = []
    transport.collection_loaded.connect(lambda payload: loaded.append(payload))
    try:
        transport.load_collection(cid)      # 闸在 c1
        transport.load_collection(other)   # 同 key 作废 c1；c2 排队
        facade._gate.set()
        with qtbot.waitSignal(transport.collection_loaded, timeout=8000):
            qtbot.wait(20)
        qtbot.wait(150)
        assert len(loaded) == 1             # 只有新集合送达
        paths = [entry.path for entry in loaded[0]["files"]]
        assert paths == [str(lib / "two.png")]
    finally:
        if getattr(facade, "_gate", None) is not None:
            facade._gate.set()
        transport.shutdown()
        catalog.close()


def test_clearing_search_exits_library_view(qtbot):
    from PyQt6.QtCore import QItemSelectionModel  # noqa: F401

    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", _entry_dicts(2))
    panel._last_search_query = "needle"
    panel.library_button.setChecked(True)
    panel.show_library_results({"files": _entry_dicts(1, start=50),
                                "total": 1, "truncated": False, "query": "needle"})
    assert panel._view_location["kind"] == "library-search"
    replays = []
    panel.directory_requested.connect(lambda path: replays.append(path))
    panel._toggle_search(False)  # 收起/清空搜索
    assert panel._view_location["kind"] == "directory"
    assert not panel.library_button.isChecked()
    assert replays == ["/media"]  # 退回目录并重放，不滞留旧结果
    panel.shutdown()
