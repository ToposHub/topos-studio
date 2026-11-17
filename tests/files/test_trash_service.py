"""B4：回收站版本化清单、恢复冲突语义与过期清理。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.transactions.trash_service import (
    DEFAULT_RETENTION_DAYS,
    TrashService,
)


@pytest.fixture()
def env(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    svc = TrashService(catalog, catalog.locator)
    yield tmp_path, catalog, library, lib, svc
    catalog.close()


def _seed_asset(env, name: str = "a.png", *, rating: int = 4) -> Path:
    tmp, catalog, library, lib, svc = env
    asset = lib / name
    asset.write_bytes(b"payload")
    catalog.upsert_asset(library.library_id, asset, size=7, width=8)
    catalog.patch_user_fields(library.library_id, asset, rating=rating, comment="保留备注")
    with catalog._read() as cursor:
        row = cursor.execute(
            "SELECT id FROM tags WHERE library_id = ? AND name = ?",
            (library.library_id, "选片")).fetchone()
    tag_id = row["id"] if row is not None else catalog.create_tag(
        library.library_id, "选片")
    catalog.tag_asset(library.library_id, asset, tag_id)
    with catalog._read() as cursor:
        row = cursor.execute(
            "SELECT id FROM collections WHERE library_id = ? AND name = ?",
            (library.library_id, "合集")).fetchone()
    collection_id = row["id"] if row is not None else catalog.create_collection(
        library.library_id, "合集")
    catalog.add_to_collection(collection_id, asset)
    return asset


# ---------------------------------------------------------------- 入站


def test_trash_captures_manifest_and_clears_live_rows(env):
    tmp, catalog, library, lib, svc = env
    asset = _seed_asset(env)
    outcome = svc.trash([asset], library.library_id)[0]
    assert outcome.ok
    assert not asset.exists(), "源已移入回收站"

    record = svc.list_items(library.library_id)[0]
    manifest = record.manifest
    assert manifest["version"] == 1
    assert manifest["asset"]["rating"] == 4 and manifest["asset"]["comment"] == "保留备注"
    assert [t["name"] for t in manifest["relations"]["tags"]] == ["选片"]
    assert manifest["relations"]["collections"][0]["name"] == "合集"
    assert (catalog.locator.trash_dir(library.library_id) / record.trashed_name).exists()

    assert catalog.asset(library.library_id, asset) is None, "活动行已清"
    assert catalog.snapshot_relations(library.library_id, asset)["tags"] == []


def test_manifest_failure_forbids_deletion(env, monkeypatch):
    tmp, catalog, library, lib, svc = env
    asset = _seed_asset(env)

    def _explode(*args, **kwargs):
        raise RuntimeError("快照通道故障")

    monkeypatch.setattr(catalog, "snapshot_relations", _explode)
    with pytest.raises(RuntimeError):
        svc.trash([asset], library.library_id)
    assert asset.exists(), "快照失败 → 文件绝不被删除"
    assert catalog.asset(library.library_id, asset) is not None, "库内行原样"
    assert svc.list_items(library.library_id) == []


def test_move_failure_keeps_rows_retryable(env, monkeypatch):
    tmp, catalog, library, lib, svc = env
    asset = _seed_asset(env)
    monkeypatch.setattr(
        svc._ops, "move",
        lambda *a, **k: type("R", (), {"ok": False, "status": "failed",
                                        "code": "PERMISSION_DENIED",
                                        "message": "denied"})())
    outcome = svc.trash([asset], library.library_id)[0]
    assert outcome.status == "failed" and outcome.code == "PERMISSION_DENIED"
    assert asset.exists() and catalog.asset(library.library_id, asset) is not None
    assert svc.list_items(library.library_id) == []


# ---------------------------------------------------------------- 恢复


def test_restore_roundtrip_rebuilds_asset_and_relations(env):
    tmp, catalog, library, lib, svc = env
    asset = _seed_asset(env, rating=5)
    record_id = svc.trash([asset], library.library_id)[0].record_id

    outcome = svc.restore(record_id)
    assert outcome.ok and outcome.restored_to == str(asset)
    row = catalog.asset(library.library_id, asset)
    assert row["rating"] == 5 and row["comment"] == "保留备注"
    assert catalog.tags_of(library.library_id, asset) == ["选片"]
    assert catalog.snapshot_relations(library.library_id, asset)["collections"]
    assert svc.list_items(library.library_id) == [], "记录已消费"
    assert not list(catalog.locator.trash_dir(library.library_id).iterdir())


def test_restore_conflict_error_policy_stays_retryable(env):
    tmp, catalog, library, lib, svc = env
    asset = _seed_asset(env)
    record_id = svc.trash([asset], library.library_id)[0].record_id
    asset.write_bytes("有人先恢复了同名文件".encode("utf-8"))  # 原路径被占

    outcome = svc.restore(record_id)
    assert outcome.status == "conflict" and outcome.code == "DEST_EXISTS"
    assert asset.read_bytes() == "有人先恢复了同名文件".encode("utf-8")
    record = svc.list_items(library.library_id)[0]
    assert record.id == record_id, "回收站记录保留可重试"
    assert (catalog.locator.trash_dir(library.library_id) / record.trashed_name).exists()


def test_restore_conflict_rename_policy_relinks_to_final_identity(env):
    tmp, catalog, library, lib, svc = env
    asset = _seed_asset(env)
    record_id = svc.trash([asset], library.library_id)[0].record_id
    asset.write_bytes(b"occupied")

    outcome = svc.restore(record_id, policy="rename")
    assert outcome.ok and outcome.restored_to.endswith("a 2.png")
    final = lib / "a 2.png"
    assert final.exists()
    row = catalog.asset(library.library_id, final)
    assert row is not None and row["rating"] == 4, "关系挂到最终身份"
    assert catalog.tags_of(library.library_id, final) == ["选片"]
    assert svc.list_items(library.library_id) == []


# ---------------------------------------------------------------- 彻底删除/过期


def test_purge_removes_payload_and_record(env):
    tmp, catalog, library, lib, svc = env
    asset = _seed_asset(env)
    record_id = svc.trash([asset], library.library_id)[0].record_id
    outcome = svc.purge(record_id)
    assert outcome.ok
    assert svc.list_items(library.library_id) == []
    assert not list(catalog.locator.trash_dir(library.library_id).iterdir())
    assert svc.purge(record_id).code == "RECORD_MISSING"


def test_purge_expired_only_removes_due_records(env):
    tmp, catalog, library, lib, svc = env
    fresh = _seed_asset(env, "fresh.png")
    old = _seed_asset(env, "old.png")
    svc.trash([fresh], library.library_id)
    svc.trash([old], library.library_id)
    # 把 old 的过期时间拨到已过期（删除时间同步回拨）
    with catalog._write() as cursor:
        cursor.execute(
            "UPDATE trash_items SET expire_at = expire_at - ?,"
            " deleted_at = deleted_at - ? WHERE original_path LIKE '%old.png%'",
            ((DEFAULT_RETENTION_DAYS + 1) * 86400,) * 2,
        )
    results = svc.purge_expired()
    assert [r.ok for r in results] == [True]
    remaining = svc.list_items(library.library_id)
    assert len(remaining) == 1 and remaining[0].original_path.endswith("fresh.png")


def test_directory_trash_and_restore(env):
    tmp, catalog, library, lib, svc = env
    folder = lib / "bundle"
    folder.mkdir()
    (folder / "inner.txt").write_bytes(b"inner")
    outcome = svc.trash([folder], library.library_id)[0]
    assert outcome.ok and not folder.exists()
    record = svc.list_items(library.library_id)[0]
    assert record.is_dir is True
    restored = svc.restore(record.id)
    assert restored.ok
    assert (folder / "inner.txt").read_bytes() == b"inner"
