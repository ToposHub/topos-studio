"""NR-02 回归：Location/查询/标记/任务状态一致性（FR-08/09/11/12/16）。

面板级：位置身份、虚拟位置拒绝目录写入、防抖、能力门控、快照回显；
transport 级（D1 换轨后）：在途搜索作废、多任务并行互不覆盖——
走真实 facade + 合成素材，不再有 HTTP 替身。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtCore import QItemSelectionModel

pytest.importorskip("PyQt6.QtWidgets")

from src.app.ui.native_files_panel import NativeFilesPanel  # noqa: E402
from src.app.ui.native_files_transport import NativeFilesTransport  # noqa: E402


def _dir_entries():
    return [
        {"name": "A.mov", "path": "/media/A.mov", "type": "video", "size_bytes": 10,
         "tags": "interview"},
        {"name": "B.png", "path": "/media/B.png", "type": "image", "size_bytes": 5},
    ]


def _search_payload():
    return {"files": [
        {"name": "sunset.mov", "path": "/media/sunset.mov", "type": "video"},
        {"name": "aerial.png", "path": "/other/aerial.png", "type": "image"},
    ], "total": 2, "truncated": False}


def _panel(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", _dir_entries())
    return panel


def _select_first(panel):
    panel.selection.select(
        panel.proxy.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows)


# ---------------------------------------------------------------------------
# FR-08：统一 Location——虚拟位置绝不假装上一个目录
# ---------------------------------------------------------------------------

def test_fr08_search_results_rebuild_path_lookup(qtbot):
    panel = _panel(qtbot)
    panel.show_library_results(_search_payload())
    # 新素材的路径索引已重建：lookup/strip 门槛不再丢新结果
    assert "/media/sunset.mov" in panel._entry_paths
    assert panel._entry_rows.get("/other/aerial.png") is not None
    assert panel._entry_for_path("/media/sunset.mov") is not None
    assert panel._view_location["kind"] == "library-search"


def test_fr08_virtual_location_rejects_directory_writes(qtbot):
    panel = _panel(qtbot)
    panel.show_library_results(_search_payload())
    # 虚拟位置：不提供"当前目录"——mkdir/粘贴不得落到上一个真实目录
    assert panel._current_directory() == ""
    panel.request_new_folder()          # 拒绝发生在 QInputDialog 之前
    assert "真实文件夹" in panel.meta_status.text()


def test_fr08_collection_location_identity(qtbot):
    panel = _panel(qtbot)
    panel._pending_collection_name = "精选"
    panel._pending_collection_id = 7
    panel.show_collection_results({"files": [
        {"name": "pick.mov", "path": "/media/pick.mov", "type": "video"}]})
    assert panel._view_location["kind"] == "collection"
    assert panel._view_location["collection_id"] == 7
    assert panel._view_location["writable"] is False
    assert panel._current_directory() == ""


def test_fr08_directory_restores_real_location_and_filter(qtbot):
    panel = _panel(qtbot)
    panel.show_library_results(_search_payload())
    panel.search_edit.setText("zzz-no-match")  # 本地过滤在虚拟视图不生效
    assert panel.proxy.query == ""
    panel.set_entries("/media", _dir_entries())  # 回到目录
    assert panel._view_location["kind"] == "directory"
    assert panel._current_directory() == "/media"
    panel.search_edit.setText("A")
    assert panel.proxy.query == "a"  # 目录视图恢复本地过滤语义


def test_fr08_refresh_replays_current_location(qtbot):
    panel = _panel(qtbot)
    emitted = []
    panel.library_search_requested.connect(
        lambda q, limit, offset: emitted.append(q))
    panel.show_library_results(_search_payload())
    panel._view_location["query"] = "sunset"
    panel.refresh()
    assert emitted == ["sunset"]  # 刷新重放搜索，而不是退回上一个目录

    emitted.clear()
    panel.collection_requested.connect(lambda cid, name: emitted.append(cid))
    panel._pending_collection_name = "精选"
    panel._pending_collection_id = 7
    panel.show_collection_results({"files": []})
    panel.refresh()
    assert emitted == [7]


# ---------------------------------------------------------------------------
# FR-09：连续输入防抖
# ---------------------------------------------------------------------------

def test_fr09_search_input_is_debounced(qtbot):
    panel = _panel(qtbot)
    panel.library_button.setChecked(True)
    emitted = []
    panel.library_search_requested.connect(
        lambda q, limit, offset: emitted.append(q))
    for text in ("s", "su", "sun", "sunset"):
        panel.search_edit.setText(text)
        panel._filter_changed()
    assert emitted == []  # 防抖窗口内不查询
    qtbot.wait(300)
    assert emitted == ["sunset"]  # 只发最后一次输入


# ---------------------------------------------------------------------------
# FR-11：标签替换语义 + 服务快照回显
# ---------------------------------------------------------------------------

def test_fr11_tags_submit_uses_replace_op(qtbot):
    panel = _panel(qtbot)
    _select_first(panel)
    emitted = []
    panel.metadata_patch_requested.connect(
        lambda paths, patch, op: emitted.append((patch, op)))
    panel.tag_edit.setText("夜,航拍")
    panel._tags_committed()
    assert emitted == [({"tags": "夜,航拍"}, "replace")]

    emitted.clear()
    panel.tag_edit.setText("")
    panel._tags_committed()
    assert emitted == [({"tags": ""}, "clear")]  # 空输入 = 清空（显式语义）


def test_fr11_snapshot_overrides_local_display(qtbot):
    panel = _panel(qtbot)
    _select_first(panel)
    panel.apply_metadata_snapshot(
        {"/media/A.mov": {"tags": "final,server", "rating": 4}})
    entry = panel._entry_for_path("/media/A.mov")
    assert entry.user.tag_text == "final,server"
    assert entry.user.rating == 4
    assert panel.tag_edit.text() == "final,server"  # 控件按服务快照回显


# ---------------------------------------------------------------------------
# FR-12：能力禁用优先于选择状态；提交复核
# ---------------------------------------------------------------------------

def test_fr12_capability_disables_and_stays_disabled_on_selection(qtbot):
    panel = _panel(qtbot)
    _select_first(panel)
    assert panel.tag_edit.isEnabled()  # 未拿到清单前不误禁
    panel.apply_capabilities([{"key": "metadata.patch", "available": False,
                               "reason": "旧服务"}])
    assert not panel.tag_edit.isEnabled()
    # 换选择 / 再回填：不得按"有选择"重新启用
    panel._fill_metadata_controls(panel._selected_entries())
    assert not panel.tag_edit.isEnabled()
    # 提交入口复核：不发写请求
    emitted = []
    panel.metadata_patch_requested.connect(lambda *args: emitted.append(args))
    panel._submit_patch({"rating": 5})
    assert emitted == []


# ---------------------------------------------------------------------------
# FR-09/16（transport）：在途搜索作废 + 多任务并行互不覆盖
# D1 换轨口径：数据面走 facade（真实 catalog + 合成素材），无 HTTP 替身。
# ---------------------------------------------------------------------------

import threading as _threading

from src.features.files.contracts import identity_key as _identity_key
from src.features.files.repository import FilesDataLocator as _Locator
from src.features.files.repository.catalog import FilesCatalog as _Catalog
from src.features.files.services.facade import FilesFacade as _FilesFacade
from src.features.files.services.query import FilesQueryService as _QueryService


class _GatedQuery(_QueryService):
    """按 key 闸住在途查询：让"旧响应未回"可控。"""

    def __init__(self, catalog):
        super().__init__(catalog)
        self.gates: dict[str, _threading.Event] = {}

    def search(self, query, **kwargs):
        gate = self.gates.get(f"search:{query}")
        if gate is not None:
            gate.wait(5)
        return super().search(query, **kwargs)


def _facade_env(tmp_path):
    locator = _Locator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = _Catalog(locator)
    catalog.open()
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    return catalog, library, lib


def test_fr09_stale_search_result_is_discarded(qtbot, tmp_path):
    catalog, library, lib = _facade_env(tmp_path)
    for name in ("old-hit.png", "new-hit.png"):
        asset = lib / name
        asset.write_bytes(b"x")
        catalog.upsert_asset(library.library_id, asset)
    query = _GatedQuery(catalog)
    gate = _threading.Event()
    query.gates["search:old"] = gate
    transport = NativeFilesTransport(
        lambda: "", None, facade=_FilesFacade(catalog, max_workers=1, query=query))
    loaded = []
    transport.search_loaded.connect(lambda payload: loaded.append(payload))
    try:
        transport.search_library("old")   # 在途（闸住）
        transport.search_library("new")   # 新请求：同 key 作废旧搜索
        gate.set()
        with qtbot.waitSignal(transport.search_loaded, timeout=8000) as received:
            qtbot.wait(10)
        qtbot.wait(150)
        assert len(loaded) == 1
        assert loaded[0] is received.args[0]
        assert {entry.name for entry in loaded[0]["files"]} == {"new-hit.png"}
    finally:
        gate.set()
        transport.shutdown()
        catalog.close()


def test_fr09_navigation_cancels_pending_search(qtbot, tmp_path):
    catalog, library, lib = _facade_env(tmp_path)
    (lib / "hit.png").write_bytes(b"x")
    catalog.upsert_asset(library.library_id, lib / "hit.png")
    query = _GatedQuery(catalog)
    gate = _threading.Event()
    query.gates["search:hit"] = gate
    transport = NativeFilesTransport(
        lambda: "", None, facade=_FilesFacade(catalog, max_workers=1, query=query))
    loaded = []
    transport.search_loaded.connect(lambda payload: loaded.append(payload))
    try:
        transport.search_library("hit")
        with qtbot.waitSignal(transport.loaded, timeout=8000):
            transport.load_directory(str(lib))  # 导航 cancel()：作废在途搜索
            gate.set()
        qtbot.wait(150)
        assert loaded == []  # 切目录后晚到的搜索结果不得覆盖新视图
    finally:
        gate.set()
        transport.shutdown()
        catalog.close()


def test_fr16_two_jobs_run_independently(qtbot, tmp_path):
    catalog, library, lib = _facade_env(tmp_path)
    a = lib / "a.png"
    b = lib / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    transport = NativeFilesTransport(
        lambda: "", None, facade=_FilesFacade(catalog, max_workers=1))
    finished = []
    transport.task_finished.connect(lambda job: finished.append(job))
    try:
        transport.run_task("rename", [str(a)], options={"new_name": "a2.png"})
        transport.run_task("rename", [str(b)], options={"new_name": "b2.png"})
        qtbot.waitUntil(lambda: len(finished) == 2, timeout=8000)
        job_ids = {job["job_id"] for job in finished}
        assert len(job_ids) == 2                 # 两个任务各自独立完成
        assert all(job["completed"] == 1 for job in finished)
        assert (lib / "a2.png").exists() and (lib / "b2.png").exists()
        assert transport.active_task_jobs() == []
    finally:
        transport.shutdown()
        catalog.close()
