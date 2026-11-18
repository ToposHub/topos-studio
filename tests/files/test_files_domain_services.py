"""C1：领域服务（query/metadata/collections/library/scan/task）接新 repository。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.features.files.contracts import FilesError, QueryRequest
from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.services import (
    FilesCollectionsService,
    FilesLibraryService,
    FilesMetadataService,
    FilesQueryService,
    FilesScanService,
    FilesTaskService,
)
from src.features.files.transactions import CancelToken


@pytest.fixture()
def env(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    yield dict(
        root=tmp_path, catalog=catalog, library=library, lib=lib,
        query=FilesQueryService(catalog),
        libraries=FilesLibraryService(catalog),
        metadata=FilesMetadataService(catalog),
        collections=FilesCollectionsService(catalog),
        scan=FilesScanService(catalog),
        tasks=FilesTaskService(catalog),
    )
    catalog.close()


def _seed_file(lib: Path, name: str, payload: bytes = b"data") -> Path:
    asset = lib / name
    asset.write_bytes(payload)
    return asset


# ---------------------------------------------------------------- query


def test_list_directory_overlays_catalog_user_fields(env):
    asset = _seed_file(env["lib"], "a.png")
    env["catalog"].upsert_asset(env["library"].library_id, asset, size=4, width=8)
    env["catalog"].patch_user_fields(env["library"].library_id, asset, rating=5)
    request = QueryRequest(request_id="r1", path=str(env["lib"]))
    result = env["query"].list_directory(request, library_id=env["library"].library_id)
    assert result.total == 1 and result.request_id == "r1"
    entry = result.entries[0]
    assert entry.kind == "image" and entry.user.rating == 5
    assert entry.technical.width == 8
    assert result.revision == env["catalog"].revision


def test_list_directory_raises_structured_errors(env):
    request = QueryRequest(request_id="r2", path=str(env["root"] / "ghost"))
    with pytest.raises(FilesError) as info:
        env["query"].list_directory(request)
    assert info.value.code == "not_found"


def test_tree_lists_directories_only(env):
    (env["lib"] / "sub").mkdir()
    _seed_file(env["lib"], "x.png")
    nodes = env["query"].tree(str(env["lib"]))
    assert [n["name"] for n in nodes] == ["sub"]


def test_search_returns_entries_with_identity(env):
    asset = _seed_file(env["lib"], "needle.png")
    env["scan"].scan_library(env["library"].library_id)
    result = env["query"].search("needle", library_id=env["library"].library_id,
                                 request_id="s1")
    assert result.total == 1 and result.entries[0].path == str(asset)


# ---------------------------------------------------------------- metadata


def test_patch_metadata_field_level_and_tags_ops(env):
    lib_id = env["library"].library_id
    asset = _seed_file(env["lib"], "a.png")
    env["catalog"].upsert_asset(lib_id, asset)

    first = env["metadata"].patch_metadata([str(asset)], {"rating": 4, "tags": ["红"]},
                                           library_id=lib_id)
    assert first.ok and first.items[0].result["tags"] == ["红"]
    # 只 patch color：rating/tags 原样
    second = env["metadata"].patch_metadata([str(asset)], {"color": 2},
                                            library_id=lib_id)
    assert second.items[0].result["rating"] == 4
    assert second.items[0].result["tags"] == ["红"]
    # add 语义
    env["metadata"].patch_metadata([str(asset)], {"tags": ["蓝"]},
                                   library_id=lib_id, tags_op="add")
    got = env["metadata"].get_metadata([str(asset)], library_id=lib_id)
    assert sorted(got.items[0].result["tags"]) == ["红", "蓝"]
    # 未知字段拒绝
    bad = env["metadata"].patch_metadata([str(asset)], {"phash": 1}, library_id=lib_id)
    assert not bad.ok and bad.error_code == "INVALID_PATCH"


def test_get_metadata_reports_unregistered_assets(env):
    asset = _seed_file(env["lib"], "unregistered.png")
    result = env["metadata"].get_metadata([str(asset)],
                                          library_id=env["library"].library_id)
    assert result.items[0].ok is False and result.items[0].error_code == "NOT_FOUND"


# ---------------------------------------------------------------- collections / library


def test_collections_crud_and_members(env):
    lib_id = env["library"].library_id
    asset = _seed_file(env["lib"], "a.png")
    env["catalog"].upsert_asset(lib_id, asset)
    created = env["collections"].create_collection(lib_id, "选片")
    assert created.ok
    cid = int(created.message)
    env["collections"].add_members(cid, [str(asset)])
    listing = env["collections"].list_collections(lib_id)
    assert listing.items[0].result["name"] == "选片"
    assert env["catalog"].collection_member_paths(cid) == [str(asset)]
    empty = env["collections"].create_collection(lib_id, "  ")
    assert not empty.ok and empty.error_code == "INVALID_NAME"


def test_library_services_roundtrip_and_lookup(env):
    service = env["libraries"]
    assert [r.library_id for r in service.list_libraries()] == [
        env["library"].library_id]
    deep = env["lib"] / "x" / "y"
    deep.mkdir(parents=True)
    assert service.library_for_path(deep).library_id == env["library"].library_id
    assert service.library_for_path(env["root"] / "outside") is None
    assert service.unpin(env["library"].library_id) is True
    assert service.list_libraries() == ()


# ---------------------------------------------------------------- scan


def test_scan_registers_assets_and_marks_missing(env):
    lib_id = env["library"].library_id
    keep = _seed_file(env["lib"], "keep.png")
    doomed = _seed_file(env["lib"], "doomed.png")
    env["scan"].scan_library(lib_id)
    assert env["catalog"].asset(lib_id, keep)["size"] == 4

    doomed.unlink()
    result = env["scan"].scan_library(lib_id)
    assert result.ok
    assert result.items[0].result["missing_marked"] == 1
    assert env["catalog"].asset(lib_id, doomed)["missing"] == 1
    assert env["catalog"].asset(lib_id, keep)["missing"] == 0


def test_scan_is_cancellable_between_chunks(env):
    lib_id = env["library"].library_id
    for index in range(5):
        _seed_file(env["lib"], f"f{index}.png")
    cancel = CancelToken()
    cancel.request()  # 立即取消：0 项入库且不谎报完成
    result = env["scan"].scan_library(lib_id, cancel=cancel)
    assert result.stage == "cancelled" and result.ok is False


# ---------------------------------------------------------------- task


def test_task_move_and_trash_restore_via_service(env):
    lib_id = env["library"].library_id
    asset = _seed_file(env["lib"], "move.png")
    env["catalog"].upsert_asset(lib_id, asset)
    env["catalog"].patch_user_fields(lib_id, asset, rating=3)
    target_dir = env["root"] / "dest"
    target_dir.mkdir()

    moved = env["tasks"].start_task(lib_id, "move", [str(asset)],
                                    options={"destination": str(target_dir)})
    assert moved.ok and moved.items[0].ok
    final = target_dir / "move.png"
    assert final.exists()
    assert env["catalog"].asset(lib_id, final)["rating"] == 3, "任务路径带关系随行"

    trashed = env["tasks"].start_task(lib_id, "trash", [str(final)])
    assert trashed.ok
    record = env["tasks"]._operations.trash_service.list_items(lib_id)[0]
    restored = env["tasks"].start_task(lib_id, "restore", [str(record.id)])
    assert restored.ok and final.exists()
    assert env["catalog"].asset(lib_id, final)["rating"] == 3


def test_task_rejects_unknown_operation_and_bad_targets(env):
    lib_id = env["library"].library_id
    bad = env["tasks"].start_task(lib_id, "format_disk", ["/x"])
    assert not bad.ok and bad.error_code == "INVALID_OPERATION"
    no_restore_target = env["tasks"].start_task(lib_id, "restore", ["not-an-id"])
    assert no_restore_target.items[0].error_code == "BAD_TARGET"
    empty = env["tasks"].start_task(lib_id, "move", [])
    assert not empty.ok and empty.error_code == "EMPTY_TARGETS"


def test_task_rename_conflict_is_itemized_not_silent(env):
    lib_id = env["library"].library_id
    a = _seed_file(env["lib"], "a.png")
    b = _seed_file(env["lib"], "b.png")
    result = env["tasks"].start_task(lib_id, "rename", [str(a), str(b)],
                                     options={"new_name": "same.png"})
    # 第一项成功；第二项冲突逐项可见，不静默跳过也不中断批次
    statuses = [(i.ok, i.error_code) for i in result.items]
    assert statuses[0][0] is True
    assert statuses[1] == (False, "DEST_EXISTS")


def test_tags_clear_removes_all_tags(env):
    """UI 空输入=清空 的显式语义（tags_op=clear）。"""
    lib_id = env["library"].library_id
    path = _seed_file(env["lib"], "clear.png")
    env["catalog"].upsert_asset(lib_id, path, size=4)
    env["metadata"].patch_metadata([str(path)], {"tags": "a, b"},
                                   library_id=lib_id)
    assert env["catalog"].tags_of(lib_id, str(path)) == ["a", "b"]
    result = env["metadata"].patch_metadata(
        [str(path)], {"tags": ""}, library_id=lib_id, tags_op="clear")
    assert result.ok
    assert env["catalog"].tags_of(lib_id, str(path)) == []
    # 快照语义：结果里 tags 为空列表
    assert result.items[0].result["tags"] == []


def test_task_job_id_and_progress_reporting(env):
    """D1：调用方预生成 job_id（UI 先发 task_started 再执行）+ 逐项进度。"""
    lib_id = env["library"].library_id
    a = _seed_file(env["lib"], "p1.png")
    b = _seed_file(env["lib"], "p2.png")
    events: list[tuple[int, int]] = []
    result = env["tasks"].start_task(
        lib_id, "rename", [str(a), str(b)],
        options={"new_name": "p1.png"},  # 第一项 UNCHANGED 改名自身、第二项冲突
        job_id="job-ui-1", progress=lambda done, total: events.append((done, total)))
    assert result.job_id == "job-ui-1"
    assert events == [(1, 2), (2, 2)]
    # 完成后 cancel 该 job：不在运行中，如实拒绝
    late = env["tasks"].cancel_task("job-ui-1")
    assert not late.ok and late.error_code == "JOB_UNKNOWN"


def test_collection_member_paths_preserve_order(env):
    lib_id = env["library"].library_id
    cid = env["catalog"].create_collection(lib_id, "选集")
    a = _seed_file(env["lib"], "m1.png")
    b = _seed_file(env["lib"], "m2.png")
    env["collections"].add_members(cid, [str(b), str(a)])
    assert env["collections"].member_paths(cid) == [str(b), str(a)]


# ---------------------------------------------------------------- D2 补口
def test_collection_rename_remove_delete(env):
    lib_id = env["library"].library_id
    cid = env["catalog"].create_collection(lib_id, "原名")
    duplicate = env["collections"].create_collection(lib_id, "原名")
    assert not duplicate.ok and duplicate.error_code == "CONFLICT"  # 同库重名被拒
    renamed = env["collections"].rename_collection(cid, "新名")
    assert renamed.ok
    missing = env["collections"].rename_collection(99999, "不存在")
    assert not missing.ok and missing.error_code == "NOT_FOUND"

    asset = _seed_file(env["lib"], "member.png")
    env["collections"].add_members(cid, [str(asset)])
    removed = env["collections"].remove_members(cid, [str(asset)])
    assert removed.ok and removed.items[0].ok
    ghost = env["collections"].remove_members(cid, [str(asset)])
    assert ghost.accepted and not ghost.ok and not ghost.items[0].ok

    env["collections"].add_members(cid, [str(asset)])
    deleted = env["collections"].delete_collection(cid)
    assert deleted.ok
    assert env["catalog"].collection_member_paths(cid) == []  # FK 级联清成员


def test_task_service_lists_trash(env):
    lib_id = env["library"].library_id
    asset = _seed_file(env["lib"], "tossed.png")
    trashed = env["tasks"].start_task(lib_id, "trash", [str(asset)])
    assert trashed.ok
    records = env["tasks"].list_trash()
    assert [r.item_name for r in records] == ["tossed.png"]
    assert records[0].original_path == str(asset)


# ---------------------------------------------------------------- D5'/B3 生产接线
def test_facade_journal_records_terminal_state(env):
    """facade 默认持久 journal：任务终态落 job_log（此前生产路径纯内存）。"""
    from src.features.files.services.facade import FilesFacade
    lib_id = env["library"].library_id
    a = _seed_file(env["lib"], "j1.png")
    target_dir = env["lib"] / "归档"
    target_dir.mkdir()
    facade = FilesFacade(env["catalog"], max_workers=1)
    try:
        result = facade.tasks.start_task(lib_id, "move", [str(a)],
                                         options={"destination": str(target_dir)})
        assert result.ok
        pending = facade.tasks.pending_recoveries()
        assert pending == []  # 终态不遗留
        rows = facade.journal.pending()  # 无 ACTIVE 态
        assert rows == []
        with env["catalog"]._read() as cursor:
            row = cursor.execute(
                "SELECT state, args FROM job_log WHERE op = 'move'").fetchone()
        assert row["state"] == "completed"
        import json as _json
        args = _json.loads(row["args"])
        assert args["destination"].endswith("j1.png")  # 意图目的地入账
    finally:
        facade.shutdown()


def test_pending_recoveries_and_evidence_classification(env):
    """崩溃遗留（begin 未 finish）→ 待恢复清单 → 磁盘证据分类。"""
    from src.features.files.transactions.journal import OperationJournal
    from src.features.files.services.facade import FilesFacade
    lib_id = env["library"].library_id
    moved = _seed_file(env["lib"], "left.png")
    facade = FilesFacade(env["catalog"], max_workers=1)
    journal = facade.journal
    try:
        # 只有同名目标，缺少事务的提交证据，仍须保留恢复记录。
        journal.begin(lib_id, "move", {"source": str(moved),
                                       "destination": str(moved) + ".moved"},
                      idempotency_key="move:fake")
        (env["lib"] / "left.png.moved").write_bytes(b"x")
        moved.unlink()
        pending = facade.tasks.pending_recoveries()
        assert [row["op"] for row in pending] == ["move"]

        verdict = facade.tasks.recover(pending[0]["id"])
        assert verdict.ok
        result = verdict.items[0].result
        assert result["state"] == "pending_recovery"
        assert [row["id"] for row in facade.tasks.pending_recoveries()] == [pending[0]["id"]]
    finally:
        facade.shutdown()
