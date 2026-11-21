"""B5：统一领域入口与改名/移动的关系全量随行（含子树）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.services.query import FilesQueryService
from src.features.files.transactions import (
    FileOperationService,
    OperationJournal,
)


@pytest.fixture()
def env(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    journal = OperationJournal(catalog)
    svc = FileOperationService(catalog, journal=journal)
    yield tmp_path, catalog, library, lib, svc, journal
    catalog.close()


def _find_or_create_tag(catalog, library, name: str) -> int:
    with catalog._read() as cursor:
        row = cursor.execute(
            "SELECT id FROM tags WHERE library_id = ? AND name = ?",
            (library.library_id, name)).fetchone()
    return row["id"] if row is not None else catalog.create_tag(library.library_id, name)


def _seed(env, name: str = "a.png", *, rating: int = 4) -> Path:
    tmp, catalog, library, lib, svc, journal = env
    asset = lib / name
    asset.write_bytes(b"payload")
    catalog.upsert_asset(library.library_id, asset, size=7, width=8)
    catalog.patch_user_fields(library.library_id, asset, rating=rating)
    catalog.tag_asset(library.library_id, asset, _find_or_create_tag(catalog, library, "选片"))
    return asset


@pytest.mark.parametrize("replace_directory", [False, True])
def test_create_library_rolls_back_only_the_directory_it_created(
    env, monkeypatch: pytest.MonkeyPatch, replace_directory: bool,
) -> None:
    tmp, catalog, _library, _lib, service, _journal = env
    destination = tmp / "Production"
    displaced = tmp / "Displaced"

    def fail_to_register(path: Path, _name: str) -> None:
        if replace_directory:
            path.rename(displaced)
            path.mkdir()
        raise OSError("registry write failed")

    monkeypatch.setattr(catalog, "register_library", fail_to_register)
    with pytest.raises(OSError, match="registry write failed"):
        service.create_library(str(tmp), destination.name)
    assert destination.exists() is replace_directory
    assert displaced.exists() is replace_directory


# ---------------------------------------------------------------- 单文件随行


def test_move_carries_metadata_tags_collections_and_fts(env):
    tmp, catalog, library, lib, svc, journal = env
    asset = _seed(env)
    cid = catalog.create_collection(library.library_id, "合集")
    catalog.add_to_collection(cid, asset)

    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    outcome = svc.move(library.library_id, asset, elsewhere)
    assert outcome.ok and outcome.relations_moved == 1

    moved = elsewhere / asset.name
    row = catalog.asset(library.library_id, moved)
    assert row["rating"] == 4 and row["width"] == 8, "用户列与技术列随行"
    assert catalog.tags_of(library.library_id, moved) == ["选片"]
    assert catalog.snapshot_relations(library.library_id, moved)["collections"]
    assert catalog.collection_member_paths(cid) == [str(moved)]
    rows, total = catalog.search("a.png", library_id=library.library_id)
    assert total >= 1 and rows[0]["path"] == str(moved), "FTS 键已迁移"
    assert FilesQueryService(catalog).search("a.png", library_id=library.library_id).total == 0
    assert journal.get(_last_job_id(journal))["state"] == "completed"


def test_move_transfers_metadata_to_the_receiving_library(env) -> None:
    root, catalog, source, _, service, _ = env
    asset = _seed(env)
    target = root / "receiving"
    target.mkdir()
    destination = catalog.register_library(target)
    (target / asset.name).write_bytes(b"existing")

    outcome = service.move(source.library_id, asset, target, policy="rename")

    assert outcome.ok
    moved = Path(outcome.destination)
    assert moved != target / asset.name and moved.read_bytes() == b"payload"
    assert (target / asset.name).read_bytes() == b"existing"
    assert catalog.asset(destination.library_id, moved)["rating"] == 4
    assert catalog.tags_of(destination.library_id, moved) == ["选片"]
    assert catalog.asset(source.library_id, asset) is None
    assert catalog.asset(source.library_id, moved) is None


def _last_job_id(journal: OperationJournal) -> str:
    with journal._catalog._read() as cursor:
        return cursor.execute(
            "SELECT id FROM job_log ORDER BY created_at DESC LIMIT 1").fetchone()[0]


def test_rename_keeps_relations(env):
    tmp, catalog, library, lib, svc, journal = env
    asset = _seed(env, "photo.png")
    outcome = svc.rename(library.library_id, asset, "photo-final.png")
    assert outcome.ok
    renamed = lib / "photo-final.png"
    assert renamed.exists()
    assert catalog.asset(library.library_id, renamed)["rating"] == 4
    assert catalog.tags_of(library.library_id, renamed) == ["选片"]
    assert catalog.asset(library.library_id, asset) is None


def test_conflict_does_not_touch_relations(env):
    tmp, catalog, library, lib, svc, journal = env
    asset = _seed(env, "a.png")
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.png").write_bytes(b"occupied")
    outcome = svc.move(library.library_id, asset, elsewhere)
    assert outcome.status == "conflict" and outcome.code == "DEST_EXISTS"
    assert catalog.asset(library.library_id, asset)["rating"] == 4
    assert journal.get(_last_job_id(journal))["state"] == "failed"


# ---------------------------------------------------------------- 子树随行


def test_directory_move_remaps_whole_subtree(env):
    tmp, catalog, library, lib, svc, journal = env
    folder = lib / "shoot"
    (folder / "day1").mkdir(parents=True)
    for name in ("shoot/a.png", "shoot/day1/b.png", "shoot/day1/c.png"):
        _seed(env, name)
    stray = _seed(env, "shoot-x.png")  # 前缀相似但不属于子树
    cid = catalog.create_collection(library.library_id, "合集")
    catalog.add_to_collection(cid, folder / "a.png")

    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    outcome = svc.move(library.library_id, folder, elsewhere)
    assert outcome.ok and outcome.relations_moved == 3

    moved_root = elsewhere / "shoot"
    assert (moved_root / "day1" / "b.png").exists()
    row = catalog.asset(library.library_id, moved_root / "day1" / "b.png")
    assert row is not None and row["rating"] == 4
    assert catalog.asset(library.library_id, folder / "a.png") is None
    assert catalog.collection_member_paths(cid) == [str(moved_root / "a.png")]
    # 前缀相似项不受影响
    assert catalog.asset(library.library_id, stray) is not None
    _, total = catalog.search("a.png", library_id=library.library_id)
    assert total >= 1


# ---------------------------------------------------------------- 幂等提交


def test_same_path_new_file_is_a_new_request_not_a_replayed_success(env):
    tmp, catalog, library, lib, svc, journal = env
    asset = _seed(env)
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    first = svc.move(library.library_id, asset, elsewhere)
    assert first.ok
    asset.write_bytes(b"second round")  # 同名新文件再提交同键任务
    second = svc.move(library.library_id, asset, elsewhere)
    assert second.code == "DEST_EXISTS" and second.status == "conflict"
    assert (elsewhere / asset.name).read_bytes() == b"payload", "未重复执行"


# ---------------------------------------------------------------- 回收站经同一入口


def test_trash_and_restore_through_service(env):
    tmp, catalog, library, lib, svc, journal = env
    asset = _seed(env)
    outcome = svc.trash(library.library_id, [asset])[0]
    assert outcome.ok
    record = svc.trash_service.list_items(library.library_id)[0]
    restored = svc.restore(library.library_id, record.id)
    assert restored.ok and catalog.asset(library.library_id, asset)["rating"] == 4
