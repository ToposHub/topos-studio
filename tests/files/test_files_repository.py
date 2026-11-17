"""B1：新库定位器 files-data/ 与 catalog repository（数据边界 §2/§3）。"""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from src.features.files.repository import (
    CATALOG_SCHEMA_VERSION,
    CatalogSchemaMismatch,
    FilesCatalog,
    FilesDataLocator,
)
from src.shared.paths.paths import get_config_root, get_host_db_path


# ---------------------------------------------------------------- 定位器


def test_locator_paths_all_under_config_root(tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    root = locator.root
    assert root == (tmp_path / "files-data").resolve()
    assert locator.catalog_db.parent == root
    assert locator.trash_dir("ab12cd34").parent == root / "trash"
    assert locator.plugins_state_file.parent == root / "plugins"
    assert locator.host_db == tmp_path / "host.db"
    for path in (locator.catalog_db, locator.libraries_file, locator.host_db):
        assert path.is_absolute()


def test_default_locator_honors_config_override(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(config_file))
    assert get_config_root() == str(tmp_path)
    assert FilesDataLocator.default().root == (tmp_path / "files-data").resolve()
    assert get_host_db_path() == str(tmp_path / "host.db")


def test_default_locator_independent_of_cwd(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(config_file))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert Path(get_host_db_path()).parent == tmp_path


def test_ensure_layout_is_idempotent(tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    locator.ensure_layout()
    for directory in (
        locator.root / "trash",
        locator.cache_thumbnails_dir,
        locator.plugins_install_root,
        locator.backups_dir,
    ):
        assert directory.is_dir()


# ---------------------------------------------------------------- catalog 打开/版本


@pytest.fixture()
def catalog(tmp_path):
    instance = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    yield instance
    instance.close()


def test_catalog_init_is_idempotent_and_pins_version(tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    first = FilesCatalog(locator)
    first.close()
    second = FilesCatalog(locator)  # 重开不重建、不报错
    try:
        with second._read() as cursor:
            assert cursor.execute("PRAGMA user_version").fetchone()[0] == CATALOG_SCHEMA_VERSION
            tables = {
                row[0]
                for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for required in ("assets", "tags", "asset_tags", "collections",
                         "collection_items", "saved_queries", "stacks",
                         "stack_members", "trash_items", "job_log", "settings"):
            assert required in tables
        assert locator.schema_version_file.read_text(encoding="utf-8").strip() == "1"
    finally:
        second.close()


def test_catalog_refuses_future_schema_version(tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    FilesCatalog(locator).close()
    conn = sqlite3.connect(str(locator.catalog_db))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(CatalogSchemaMismatch) as info:
        FilesCatalog(locator)
    assert info.value.code == "SCHEMA_VERSION_MISMATCH"
    assert info.value.found == 99


def test_schema_version_file_self_heals(tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    FilesCatalog(locator).close()
    locator.schema_version_file.write_text("corrupt", encoding="utf-8")
    instance = FilesCatalog(locator)
    try:
        assert locator.schema_version_file.read_text(encoding="utf-8").strip() == "1"
    finally:
        instance.close()


# ---------------------------------------------------------------- 库注册表


def test_register_library_is_idempotent_by_root(tmp_path, catalog):
    lib_root = tmp_path / "media"
    lib_root.mkdir()
    first = catalog.register_library(lib_root, "素材库")
    second = catalog.register_library(lib_root, "重复名")
    assert first == second
    assert catalog.libraries() == (first,)
    assert catalog.locator.trash_dir(first.library_id).is_dir()


def test_libraries_file_is_atomic_json(tmp_path, catalog):
    catalog.register_library(tmp_path, "A")
    raw = json.loads(catalog.locator.libraries_file.read_text(encoding="utf-8"))
    assert raw[0]["root"] == str(tmp_path.resolve())
    assert not list(catalog.locator.root.glob("*.json.tmp"))
    assert catalog.unregister_library("deadbeef") is False
    assert catalog.unregister_library(catalog.libraries()[0].library_id) is True
    assert catalog.libraries() == ()


# ---------------------------------------------------------------- 资产


def test_upsert_then_patch_keeps_user_fields(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    asset = tmp_path / "a.png"
    asset.write_bytes(b"x")
    catalog.upsert_asset(lib.library_id, asset, size=1, width=8, height=8)
    catalog.patch_user_fields(lib.library_id, asset, rating=4, comment="备注")
    catalog.upsert_asset(lib.library_id, asset, size=2)  # 技术更新不清用户列
    row = catalog.asset(lib.library_id, asset)
    assert row["rating"] == 4 and row["comment"] == "备注"
    assert row["size"] == 2 and row["width"] == 8
    assert row["missing"] == 0


def test_asset_identity_is_nfc_normalized(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    nfd = "cafe\u0301.png"  # NFD 形式的 é
    asset = tmp_path / nfd
    asset.write_bytes(b"x")
    catalog.upsert_asset(lib.library_id, asset)
    nfc_path = str(tmp_path / "caf\u00e9.png")
    assert catalog.asset(lib.library_id, nfc_path) is not None


def test_move_asset_rows_keeps_fts_findable(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    asset = tmp_path / "needle.png"
    asset.write_bytes(b"x")
    catalog.upsert_asset(lib.library_id, asset)
    rows, total = catalog.search("needle", library_id=lib.library_id)
    assert total == 1

    moved = tmp_path / "renamed.png"
    assert catalog.move_asset_rows(lib.library_id, asset, moved) == 1
    _, stale = catalog.search("needle", library_id=lib.library_id)
    rows, total = catalog.search("renamed", library_id=lib.library_id)
    assert stale == 0, "旧名检索随身份迁移失效（FTS 行已换键）"
    assert total == 1 and rows[0]["path"] == str(moved)
    assert catalog.asset(lib.library_id, asset) is None


def test_remove_asset_rows_clears_fts(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    asset = tmp_path / "gone.png"
    asset.write_bytes(b"x")
    catalog.upsert_asset(lib.library_id, asset, name="gone")
    assert catalog.remove_asset_rows(lib.library_id, [str(asset)]) == 1
    _, total = catalog.search("gone", library_id=lib.library_id)
    assert total == 0


def test_search_falls_back_to_like_for_short_query(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    asset = tmp_path / "ab.png"
    asset.write_bytes(b"x")
    catalog.upsert_asset(lib.library_id, asset)
    rows, total = catalog.search("ab", library_id=lib.library_id)
    assert total == 1 and rows[0]["path"] == str(asset)


# ---------------------------------------------------------------- 标签


def test_tag_flow_updates_fts_tag_names(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    asset = tmp_path / "a.png"
    asset.write_bytes(b"x")
    catalog.upsert_asset(lib.library_id, asset)
    tag_id = catalog.create_tag(lib.library_id, "外景")
    catalog.tag_asset(lib.library_id, asset, tag_id)
    assert catalog.tags_of(lib.library_id, asset) == ["外景"]
    _, total = catalog.search("外景", library_id=lib.library_id)
    assert total == 1
    catalog.untag_asset(lib.library_id, asset, tag_id)
    _, total = catalog.search("外景", library_id=lib.library_id)
    assert total == 0


def test_create_tag_rejects_missing_parent(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    from src.features.files.repository.catalog import CatalogError

    with pytest.raises(CatalogError) as info:
        catalog.create_tag(lib.library_id, "孤儿", parent_id=404)
    assert info.value.code == "TAG_PARENT_MISSING"


def test_create_tag_rejects_parent_cycle(tmp_path, catalog):
    """白盒构造成环父链，服务层必须拒绝在其下续挂。"""
    lib = catalog.register_library(tmp_path)
    with catalog._write() as cursor:
        cursor.execute(
            "INSERT INTO tags (library_id, name, parent_id) VALUES (?, 'a', NULL)",
            (lib.library_id,))
        id_a = cursor.lastrowid
        cursor.execute(
            "INSERT INTO tags (library_id, name, parent_id) VALUES (?, 'b', ?)",
            (lib.library_id, id_a))
        id_b = cursor.lastrowid
        cursor.execute("UPDATE tags SET parent_id = ? WHERE id = ?", (id_b, id_a))
    from src.features.files.repository.catalog import CatalogError

    with pytest.raises(CatalogError) as info:
        catalog.create_tag(lib.library_id, "c", parent_id=id_a)
    assert info.value.code == "TAG_CYCLE"


# ---------------------------------------------------------------- 集合


def test_collection_membership_roundtrip(tmp_path, catalog):
    lib = catalog.register_library(tmp_path)
    asset = tmp_path / "member.png"
    asset.write_bytes(b"x")
    catalog.upsert_asset(lib.library_id, asset)
    cid = catalog.create_collection(lib.library_id, "选片")
    catalog.add_to_collection(cid, asset)
    catalog.add_to_collection(cid, asset)  # 幂等
    assert catalog.collection_member_paths(cid) == [str(asset)]


# ---------------------------------------------------------------- CWD 双库守卫


def test_production_code_has_no_cwd_default_database():
    """[数据边界 §6] 生产代码禁止 Database() 无参默认（AST 扫描 src/）。"""
    repo_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for py in (repo_root / "src").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Database"
                and not node.args
                and not node.keywords
            ):
                offenders.append(str(py.relative_to(repo_root)))
    assert offenders == [], f"发现 CWD 默认库实例化: {offenders}"
