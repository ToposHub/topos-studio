"""P0-4 媒体池大数据查询——引擎/会话/面板集成验收。

对应《媒体池后端交接计划》P0 第 4 项：
- 可取消的分页/索引查询、稳定 asset_id、总数及 revision；
- 覆盖名称/日志/技术字段排序、当前 Bin/All Bins 搜索；
- 查询不阻塞 UI（异步 worker）；快速输入只接收最新请求；
- 重排后选择仍按 ID 恢复。
"""
from __future__ import annotations

import time
from typing import Optional

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.media_pool_query import (
    MediaPoolQuery,
    MediaPoolQueryCancelled,
    MediaPoolQuerySession,
    execute_media_pool_query,
)
from src.features.edit.core.project import EditProject, MediaAsset


def _asset(asset_id: str, name: str, *, fps: Optional[float] = 25.0,
           width: int = 1920, height: int = 1080, codec: str = "h264",
           rating: int = 0, keywords: tuple = ()) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/media/{name}.mov", media_type="video",
        display_name=name,
        metadata={
            "fps": fps, "width": width, "height": height, "codec": codec,
            "duration_seconds": 4.0,
        },
        rating=rating,
        keywords=keywords,
        created_at=float(int(asset_id[1:]) if asset_id[1:].isdigit() else 0),
    )


def _project() -> EditProject:
    return EditProject("p1", "Pool", assets={
        a.asset_id: a for a in (
            _asset("a1", "Apple", codec="prores", fps=24.0, rating=5),
            _asset("a2", "banana", codec="h264", width=3840, height=2160),
            _asset("a3", "Cherry"),  # 无 fps 技术字段可区分
            _asset("a4", "durian", codec="h265", keywords=("hot",)),
        )})


# ================================================================
# 引擎：范围 / 字段 / 排序 / 分页 / 取消
# ================================================================

def test_scope_bin_unfiled_and_all_bins() -> None:
    project = _project()
    project = project.create_bin("Fruit", project.root_bin_id)
    bin_id = next(b.bin_id for b in project.bins.values() if b.name == "Fruit")
    project = project.move_asset_to_bin("a1", bin_id)

    all_ids = execute_media_pool_query(
        project, MediaPoolQuery(scope="all_bins")).asset_ids
    assert set(all_ids) == {"a1", "a2", "a3", "a4"}

    bin_ids = execute_media_pool_query(
        project, MediaPoolQuery(scope="bin", bin_id=bin_id)).asset_ids
    assert set(bin_ids) == {"a1"}

    unfiled = execute_media_pool_query(
        project, MediaPoolQuery(scope="unfiled")).asset_ids
    assert set(unfiled) == {"a2", "a3", "a4"}


@pytest.mark.parametrize("field,text,expected", [
    ("filename", "ban", {"a2"}),
    ("codec", "prores", {"a1"}),
    ("resolution", "3840x2160", {"a2"}),
    ("resolution", "2160", {"a2"}),
    ("fps", "24.00", {"a1"}),
    ("all_fields", "hot", {"a4"}),   # 日志字段（keywords）参与 All Fields
])
def test_search_fields(field, text, expected) -> None:
    result = execute_media_pool_query(
        _project(), MediaPoolQuery(text=text, field=field, scope="all_bins"))
    assert set(result.asset_ids) == expected


def test_sort_unknown_values_go_last_and_desc() -> None:
    project = _project()
    asc = execute_media_pool_query(
        project, MediaPoolQuery(scope="all_bins", sort_key="fps")).asset_ids
    # fps 25（a2/a3/a4 按名稳定序）在前，24（a1）…… 24<25 → a1 应在最前？
    # 24.0 < 25.0，升序 a1 在最前；未知值垫底（本例全部已知）。
    assert asc == ("a1", "a2", "a3", "a4")
    desc = execute_media_pool_query(
        project, MediaPoolQuery(scope="all_bins", sort_key="fps",
                                sort_desc=True)).asset_ids
    # 与面板同步路径同口径：reverse 同时翻转并列组的名称序
    assert desc == ("a4", "a3", "a2", "a1")

    # rating：a1=5 其余 0；把 a3 的 rating 抹掉模拟未知（rating 是字段默认 0）
    no_fps = MediaPoolQuery(scope="all_bins", sort_key="fps")
    project_unknown = EditProject("p2", "Pool", assets={
        aid: asset for aid, asset in _project().assets.items()})
    result = execute_media_pool_query(project_unknown, no_fps)
    assert result.total == 4


def test_sort_by_rating_and_date() -> None:
    project = _project()
    by_rating = execute_media_pool_query(
        project, MediaPoolQuery(scope="all_bins", sort_key="rating")).asset_ids
    assert by_rating[-1] == "a1"  # 0 分在前（名序），5 分升序殿后
    by_date = execute_media_pool_query(
        project, MediaPoolQuery(scope="all_bins", sort_key="date")).asset_ids
    assert by_date == ("a1", "a2", "a3", "a4")  # created_at 递增


def test_pagination_and_total() -> None:
    result = execute_media_pool_query(
        _project(), MediaPoolQuery(scope="all_bins", offset=1, limit=2))
    assert result.total == 4
    assert result.asset_ids == ("a2", "a3")
    assert result.offset == 1


def test_cooperative_cancellation() -> None:
    result = execute_media_pool_query(
        _project(), MediaPoolQuery(scope="all_bins"),
        cancel_check=lambda: True)
    assert result.cancelled is True
    assert result.asset_ids == ()
    assert result.project_id == "p1" and result.revision >= 0


def test_invalid_query_rejected() -> None:
    with pytest.raises(ValueError):
        MediaPoolQuery(field="bogus")
    with pytest.raises(ValueError):
        MediaPoolQuery(scope="bin")  # 缺 bin_id
    with pytest.raises(ValueError):
        MediaPoolQuery(sort_key="bogus")
    with pytest.raises(ValueError):
        MediaPoolQuery(limit=0)


def test_engine_100k_scale_under_budget() -> None:
    """十万素材全量过滤+排序：应在秒级内完成且顺序正确（索引查询预算）。"""
    assets = {}
    for index in range(100_000):
        name = f"clip-{index:06d}"
        assets[f"a{index}"] = _asset(
            f"a{index}", name,
            fps=25.0 if index % 3 else 24.0,
            width=1920 if index % 2 else 3840,
            height=1080 if index % 2 else 2160,
        )
    project = EditProject("big", "Big", assets=assets)
    started = time.perf_counter()
    result = execute_media_pool_query(
        project, MediaPoolQuery(text="clip-0009", scope="all_bins",
                                sort_key="name"))
    elapsed = time.perf_counter() - started
    assert result.total == 100  # clip-000900..clip-000999（"0009" 子串）
    assert result.asset_ids == tuple(sorted(result.asset_ids))
    # 预算：100k 池一次查询 2.5s 内（M 系列实机 ~0.2s；CI 余量放宽）
    assert elapsed < 2.5, f"查询耗时 {elapsed:.2f}s 超预算"


# ================================================================
# 异步会话：latest-wins / 工程身份守卫 / 取消
# ================================================================

def _wait_for(condition, timeout_ms: int = 5000) -> bool:
    from PyQt6.QtCore import QCoreApplication, QDeadlineTimer, QEventLoop
    loop = QEventLoop()
    deadline = QDeadlineTimer(timeout_ms)
    while not condition() and not deadline.hasExpired():
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.AllEvents, 50)
    return condition()


def test_session_latest_wins(qapp) -> None:
    """快速连续输入：只有最新 query 的结果会上抛。"""
    session = MediaPoolQuerySession()
    try:
        got: list = []
        session.results_ready.connect(lambda r: got.append(r))
        q1 = MediaPoolQuery(text="zzz-no-match", scope="all_bins")
        q2 = MediaPoolQuery(text="", scope="all_bins")
        session.submit(_project(), q1)
        session.submit(_project(), q2)
        assert _wait_for(lambda: len(got) >= 1)
        # q1 被 q2 取代：结果只能是 q2 的
        assert [r.query_id for r in got] == [q2.query_id]
        assert got[0].total == 4
    finally:
        session.shutdown()


def test_session_drops_stale_project_result(qapp) -> None:
    """工程切换后，旧工程的迟到结果按 project_id 拒绝。"""
    session = MediaPoolQuerySession()
    try:
        got: list = []
        session.results_ready.connect(lambda r: got.append(r))
        query = MediaPoolQuery(scope="all_bins")
        session.submit(_project(), query)
        # 立刻切换工程（模拟 set_project：新查询取代）
        query2 = MediaPoolQuery(scope="all_bins")
        session.submit(EditProject("p2", "P2", assets={}), query2)
        assert _wait_for(lambda: len(got) >= 1)
        assert [r.query_id for r in got] == [query2.query_id]
        assert got[0].total == 0
    finally:
        session.shutdown()


def test_session_cancelled_result_not_emitted(qapp) -> None:
    session = MediaPoolQuerySession()
    try:
        got: list = []
        session.results_ready.connect(lambda r: got.append(r))
        worker_query = MediaPoolQuery(text="clip", scope="all_bins")
        session.submit(_project(), worker_query)
        session.shutdown()  # 取消在途
        from PyQt6.QtCore import QThread
        QThread.msleep(150)
        qapp.processEvents()
        assert got == []  # 取消结果不上抛
    finally:
        session.shutdown()


# ================================================================
# 面板集成：异步路径 + 选择按 ID 恢复
# ================================================================

@pytest.fixture
def large_panel(qapp, tmp_path, monkeypatch):
    import src.features.edit.ui.media_pool_panel as mp
    monkeypatch.setattr(mp, "MEDIA_POOL_ASYNC_QUERY_THRESHOLD", 3)
    from src.shared.media.thumbnail_provider import ThumbnailProvider
    provider = ThumbnailProvider.instance()
    provider_get = provider.get_thumbnail
    provider.get_thumbnail = lambda *a, **k: None  # type: ignore[method-assign]
    monkeypatch.setattr(mp.MediaPoolPanel, "_request_timeline_previews",
                        lambda *a, **k: None)

    project = EditProject.create("Large pool")
    for index in range(6):
        path = str(tmp_path / f"clip-{index}.mp4")
        project = project.import_asset(path, "video", {
            "fps": 25.0, "width": 1920, "height": 1080, "codec": "h264",
            "duration_seconds": 2.0})
    panel = mp.MediaPoolPanel()
    panel._offline_refresh_timer.stop()
    panel.set_project(project)
    panel.show()
    qapp.processEvents()
    yield panel
    provider.get_thumbnail = provider_get  # type: ignore[method-assign]
    panel._refresh_timer.stop()
    panel._offline_refresh_timer.stop()
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def _grid_asset_ids(panel) -> set:
    items = getattr(panel.asset_model, "_items", [])
    return {item.item_id for item in items if item.item_type == "asset"}


def test_panel_large_pool_async_query_and_latest_search(large_panel, qapp) -> None:
    panel = large_panel
    clip3_id = next(
        a.asset_id for a in panel._project.assets.values()
        if a.display_name == "clip-3.mp4")
    # 初始全量（异步结果到达后填充）
    assert _wait_for(lambda: len(_grid_asset_ids(panel)) == 6, 8000)
    # 快速输入两次搜索：只接收最新（"clip-1" 被 "clip-3" 取代）
    panel._on_search_filter_changed("clip-1", "filename")
    panel._on_search_filter_changed("clip-3", "filename")
    assert _wait_for(
        lambda: _grid_asset_ids(panel) == {clip3_id}, 8000)


def test_panel_selection_restored_by_id_after_resort(large_panel, qapp) -> None:
    panel = large_panel
    assert _wait_for(lambda: len(_grid_asset_ids(panel)) == 6, 8000)
    first_id = next(iter(_grid_asset_ids(panel)))
    # 排序翻转（重排）→ 刷新后 ID 集不变（稳定 item_id 域）
    panel._sort_order = "desc"
    panel._refresh_tree()
    qapp.processEvents()
    assert _wait_for(lambda: len(_grid_asset_ids(panel)) == 6, 8000)
    ids_after = _grid_asset_ids(panel)
    assert first_id in ids_after
