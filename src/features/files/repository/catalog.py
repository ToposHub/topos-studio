"""Files 新库 catalog.db（B 阶段，[数据边界](docs/plans/files-replacement-data-boundary-20261117.md) §3）。

新库与旧库（repo 根 topos.db / FE metadata.db）**不同名不同表**，不迁移
旧行。初始化在单一显式事务内完成并钉 schema 版本；版本不匹配拒绝打开
（开发阶段破坏性升级：升版本走"备份→重置→重建"，不做隐式迁移）。

库注册表 libraries.json 由本模块独占写（temp + os.replace 原子落盘）。
所有资产身份 = (library_id, NFC 绝对路径)。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from src.features.files.repository.locator import FilesDataLocator

CATALOG_SCHEMA_VERSION = 1


class CatalogError(Exception):
    """catalog 领域错误基类（结构化 code 供上层映射错误码）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CatalogSchemaMismatch(CatalogError):
    def __init__(self, found: int, expected: int) -> None:
        super().__init__(
            "SCHEMA_VERSION_MISMATCH",
            f"catalog.db schema 版本 {found} != 支持 {expected}；"
            "开发阶段不做隐式迁移（备份→重置→重建）",
        )
        self.found = found
        self.expected = expected


@dataclass(frozen=True)
class LibraryRecord:
    library_id: str
    name: str
    root: str
    created_at: float


def nfc(path: str | Path) -> str:
    return unicodedata.normalize("NFC", str(path))


_SCHEMA_V1 = (
    # 资产主表：身份 = (library_id, NFC path)；不预置 phash/dom_bucket 索引列
    """CREATE TABLE IF NOT EXISTS assets (
        library_id TEXT NOT NULL,
        path TEXT NOT NULL,
        name TEXT NOT NULL,
        is_dir INTEGER NOT NULL DEFAULT 0,
        rating INTEGER NOT NULL DEFAULT 0,
        color INTEGER NOT NULL DEFAULT 0,
        flag INTEGER NOT NULL DEFAULT 0,
        comment TEXT NOT NULL DEFAULT '',
        size INTEGER, mtime REAL, date_taken REAL,
        width INTEGER, height INTEGER, duration REAL, codec TEXT,
        camera TEXT, lens TEXT, iso TEXT, aperture TEXT, shutter TEXT,
        focal TEXT, gps_lat REAL, gps_lon REAL, dom_color INTEGER,
        missing INTEGER NOT NULL DEFAULT 0,
        indexed_at REAL,
        PRIMARY KEY (library_id, path)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_assets_missing"
    " ON assets(library_id, missing) WHERE missing = 1",
    "CREATE INDEX IF NOT EXISTS idx_assets_date ON assets(library_id, date_taken)",
    "CREATE INDEX IF NOT EXISTS idx_assets_rating ON assets(library_id, rating)",
    # 标签（层级保留 parent_id；防环由服务层 create_tag 校验）
    """CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id TEXT NOT NULL,
        name TEXT NOT NULL,
        color INTEGER NOT NULL DEFAULT 0,
        parent_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
        UNIQUE (library_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS asset_tags (
        library_id TEXT NOT NULL,
        path TEXT NOT NULL,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (library_id, path, tag_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag_id)",
    # 集合 / 保存的查询 / 堆叠（schema 预留，UI 去向待用户决定）
    """CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id TEXT NOT NULL,
        name TEXT NOT NULL,
        color INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        UNIQUE (library_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS collection_items (
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        added_at REAL NOT NULL,
        PRIMARY KEY (collection_id, path)
    )""",
    """CREATE TABLE IF NOT EXISTS saved_queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id TEXT NOT NULL,
        name TEXT NOT NULL,
        rules TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (library_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS stacks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id TEXT NOT NULL,
        cover_path TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS stack_members (
        stack_id INTEGER NOT NULL REFERENCES stacks(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        PRIMARY KEY (stack_id, path)
    )""",
    # 回收站（B4 使用；manifest = 版本化完整清单 JSON）
    """CREATE TABLE IF NOT EXISTS trash_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id TEXT NOT NULL,
        original_path TEXT NOT NULL,
        item_name TEXT NOT NULL,
        is_dir INTEGER NOT NULL DEFAULT 0,
        trashed_name TEXT NOT NULL,
        deleted_at REAL NOT NULL,
        expire_at REAL NOT NULL,
        manifest TEXT NOT NULL DEFAULT '{}',
        restore_state TEXT NOT NULL DEFAULT 'pending',
        UNIQUE (library_id, trashed_name)
    )""",
    # 持久操作日志（B3 使用；文件+DB 非原子时的补偿/待恢复依据）
    """CREATE TABLE IF NOT EXISTS job_log (
        id TEXT PRIMARY KEY,
        library_id TEXT NOT NULL,
        op TEXT NOT NULL,
        args TEXT NOT NULL DEFAULT '{}',
        idempotency_key TEXT,
        state TEXT NOT NULL,
        items TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (library_id, idempotency_key)
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        library_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (library_id, key)
    )""",
    # FTS：standalone + 触发器（沿旧库 trigram 经验；tag_names 由服务层维护）
    """CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
        library_id UNINDEXED, path UNINDEXED,
        name, comment, tag_names, ocr,
        tokenize='trigram'
    )""",
    """CREATE TRIGGER IF NOT EXISTS assets_fts_ai AFTER INSERT ON assets BEGIN
        INSERT INTO assets_fts(library_id, path, name, comment, tag_names, ocr)
        VALUES (new.library_id, new.path, new.name, new.comment, '', '');
    END""",
    """CREATE TRIGGER IF NOT EXISTS assets_fts_ad AFTER DELETE ON assets BEGIN
        DELETE FROM assets_fts WHERE path = old.path AND library_id = old.library_id;
    END""",
    # update 走删除+重插，避免漏列漂移
    """CREATE TRIGGER IF NOT EXISTS assets_fts_au AFTER UPDATE OF
        name, comment ON assets BEGIN
        DELETE FROM assets_fts WHERE path = old.path AND library_id = old.library_id;
        INSERT INTO assets_fts(library_id, path, name, comment, tag_names, ocr)
        VALUES (new.library_id, new.path, new.name, new.comment,
            COALESCE((SELECT group_concat(t.name, ' ') FROM asset_tags at
              JOIN tags t ON t.id = at.tag_id
              WHERE at.library_id = new.library_id AND at.path = new.path), ''),
            COALESCE((SELECT ocr FROM assets_fts
              WHERE library_id = new.library_id AND path = old.path), ''));
    END""",
)


class FilesCatalog:
    """新 Files 库 repository：单连接 + RLock（沿 shared Database 模式）。"""

    def __init__(self, locator: FilesDataLocator) -> None:
        self.locator = locator
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.open()

    # ------------------------------------------------------------ 打开/初始化

    def open(self) -> None:
        """幂等打开：首建在单事务内建全套表并钉版本；重开校验版本。"""
        with self._lock:
            self.locator.ensure_layout()
            fresh = not self.locator.catalog_db.exists()
            conn = self._connection()
            found = conn.execute("PRAGMA user_version").fetchone()[0]
            if fresh and found == 0:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA_V1:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            elif found != CATALOG_SCHEMA_VERSION:
                raise CatalogSchemaMismatch(found, CATALOG_SCHEMA_VERSION)
            self._write_schema_version_file()

    def _write_schema_version_file(self) -> None:
        """人读 + 程序读双保险；文件缺失但库内版本正确时自愈重写。"""
        target = self.locator.schema_version_file
        if target.exists() and target.read_text(encoding="utf-8").strip() == str(
            CATALOG_SCHEMA_VERSION
        ):
            return
        tmp = target.with_suffix(".version.tmp")
        tmp.write_text(str(CATALOG_SCHEMA_VERSION), encoding="utf-8")
        os.replace(tmp, target)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(
                str(self.locator.catalog_db), timeout=5.0, check_same_thread=False
            )
            conn.isolation_level = None  # 显式事务（DDL 原子性前提）
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            self._conn = conn
        return self._conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._connection().cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            try:
                yield cursor
                cursor.close()
                conn.execute("COMMIT")
            except BaseException:
                cursor.close()
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # ------------------------------------------------------------ 库注册表

    def libraries(self) -> tuple[LibraryRecord, ...]:
        file = self.locator.libraries_file
        if not file.exists():
            return ()
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        records = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and item.get("library_id") and item.get("root"):
                records.append(
                    LibraryRecord(
                        library_id=str(item["library_id"]),
                        name=str(item.get("name") or item["library_id"]),
                        root=str(item["root"]),
                        created_at=float(item.get("created_at") or 0.0),
                    )
                )
        return tuple(records)

    def register_library(self, root: str | Path, name: str = "") -> LibraryRecord:
        """固定目录为库；同一根重复注册幂等返回既有记录。"""
        abs_root = nfc(Path(root).resolve())
        existing = [lib for lib in self.libraries() if lib.root == abs_root]
        if existing:
            return existing[0]
        record = LibraryRecord(
            library_id=secrets.token_hex(4),
            name=name or Path(abs_root).name,
            root=abs_root,
            created_at=time.time(),
        )
        self.locator.trash_dir(record.library_id).mkdir(parents=True, exist_ok=True)
        self._save_libraries(self.libraries() + (record,))
        return record

    def unregister_library(self, library_id: str) -> bool:
        remaining = tuple(lib for lib in self.libraries() if lib.library_id != library_id)
        if len(remaining) == len(self.libraries()):
            return False
        self._save_libraries(remaining)
        return True

    def _save_libraries(self, records: tuple[LibraryRecord, ...]) -> None:
        """temp + os.replace 原子写（沿旧 save_config P2-12 模式）。"""
        payload = [
            {
                "library_id": lib.library_id,
                "name": lib.name,
                "root": lib.root,
                "created_at": lib.created_at,
            }
            for lib in records
        ]
        file = self.locator.libraries_file
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, file)

    # ------------------------------------------------------------ 资产

    def upsert_asset(self, library_id: str, path: str | Path, **technical: Any) -> None:
        """登记/更新资产技术列；已存在的行只更新给定技术列，不清用户列。"""
        key = nfc(path)
        name = os.path.basename(key)
        allowed = {
            "name", "is_dir", "size", "mtime", "date_taken", "width", "height",
            "duration", "codec", "camera", "lens", "iso", "aperture", "shutter",
            "focal", "gps_lat", "gps_lon", "dom_color", "indexed_at",
        }
        fields = {k: v for k, v in technical.items() if k in allowed}
        with self._write() as cursor:
            row = cursor.execute(
                "SELECT 1 FROM assets WHERE library_id = ? AND path = ?",
                (library_id, key),
            ).fetchone()
            if row is None:
                columns = ["library_id", "path", "name"]
                values: list[Any] = [library_id, key, fields.get("name", name)]
                for column in allowed - {"name"}:
                    if column in fields:
                        columns.append(column)
                        values.append(fields[column])
                placeholders = ",".join("?" for _ in columns)
                cursor.execute(
                    f"INSERT INTO assets ({','.join(columns)}) VALUES ({placeholders})",
                    values,
                )
            elif fields:
                assignments = ",".join(f"{c} = ?" for c in fields)
                cursor.execute(
                    f"UPDATE assets SET {assignments} WHERE library_id = ? AND path = ?",
                    (*fields.values(), library_id, key),
                )

    def asset(self, library_id: str, path: str | Path) -> dict[str, Any] | None:
        with self._read() as cursor:
            row = cursor.execute(
                "SELECT * FROM assets WHERE library_id = ? AND path = ?",
                (library_id, nfc(path)),
            ).fetchone()
        return dict(row) if row is not None else None

    def patch_user_fields(
        self, library_id: str, path: str | Path, **patch: Any
    ) -> dict[str, Any] | None:
        """字段级 patch：只更新给定用户列（rating/color/flag/comment）。"""
        allowed = {"rating", "color", "flag", "comment"}
        fields = {k: v for k, v in patch.items() if k in allowed}
        if not fields:
            return self.asset(library_id, path)
        with self._write() as cursor:
            assignments = ",".join(f"{c} = ?" for c in fields)
            cursor.execute(
                f"UPDATE assets SET {assignments} WHERE library_id = ? AND path = ?",
                (*fields.values(), library_id, nfc(path)),
            )
        return self.asset(library_id, path)

    def move_asset_rows(self, library_id: str, old_path: str | Path,
                        new_path: str | Path) -> int:
        """身份迁移（B5 关系随行的入口）：更新主表行并同步 FTS 键。

        关系表（asset_tags/collection_items/stack_members）由 B5 的
        propagate 层在同一次调用中处理；本方法只管主表与 FTS。
        """
        old, new = nfc(old_path), nfc(new_path)
        with self._write() as cursor:
            cursor.execute(
                "UPDATE assets SET path = ?, name = ? WHERE library_id = ? AND path = ?",
                (new, os.path.basename(new), library_id, old),
            )
            moved = cursor.rowcount
            cursor.execute(
                "UPDATE assets_fts SET path = ? WHERE library_id = ? AND path = ?",
                (new, library_id, old),
            )
        return moved

    def remove_asset_rows(self, library_id: str, paths: list[str]) -> int:
        keys = [(library_id, nfc(p)) for p in paths]
        with self._write() as cursor:
            removed = 0
            for lib_id, key in keys:
                cursor.execute(
                    "DELETE FROM assets WHERE library_id = ? AND path = ?", (lib_id, key)
                )
                removed += cursor.rowcount
                cursor.execute(
                    "DELETE FROM assets_fts WHERE library_id = ? AND path = ?",
                    (lib_id, key),
                )
            return removed

    # ------------------------------------------------------------ 搜索

    def search(self, query: str, *, library_id: str = "", limit: int = 200,
               offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """FTS 检索（trigram）；<3 字符回落 LIKE，返回 (rows, total)。"""
        query = query.strip()
        with self._read() as cursor:
            if len(query) >= 3:
                # FTS5 字面短语：内部双引号按 FTS 规则翻倍转义
                where = "assets_fts MATCH ?"
                args: list[Any] = [f'"{query.replace(chr(34), chr(34) * 2)}"']
            else:
                where = ("(assets_fts.name LIKE ? OR assets_fts.comment LIKE ?"
                         " OR assets_fts.tag_names LIKE ?)")
                like = f"%{query}%"
                args = [like, like, like]
            scope = ""
            if library_id:
                scope = " AND assets_fts.library_id = ?"
                args.append(library_id)
            total = cursor.execute(
                f"SELECT COUNT(*) FROM assets_fts WHERE {where}{scope}", args
            ).fetchone()[0]
            order = " ORDER BY assets_fts.path LIMIT ? OFFSET ?"
            rows = cursor.execute(
                f"SELECT assets_fts.library_id, assets_fts.path FROM assets_fts"
                f" WHERE {where}{scope}{order}",
                (*args, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows], total

    # ------------------------------------------------------------ 标签

    def create_tag(self, library_id: str, name: str, color: int = 0,
                   parent_id: int | None = None) -> int:
        """建标签；parent 链成环（含自引用）拒绝 TAG_CYCLE。"""
        if parent_id is not None:
            seen: set[int] = set()
            cursor_id: int | None = parent_id
            with self._read() as cursor:
                while cursor_id is not None:
                    if cursor_id in seen or cursor_id == 0:
                        raise CatalogError("TAG_CYCLE", "标签父链成环")
                    seen.add(cursor_id)
                    row = cursor.execute(
                        "SELECT parent_id FROM tags WHERE library_id = ? AND id = ?",
                        (library_id, cursor_id),
                    ).fetchone()
                    if row is None:
                        raise CatalogError("TAG_PARENT_MISSING", "父标签不存在")
                    cursor_id = row[0]
        with self._write() as cursor:
            cursor.execute(
                "INSERT INTO tags (library_id, name, color, parent_id)"
                " VALUES (?, ?, ?, ?)",
                (library_id, name, color, parent_id),
            )
            return int(cursor.lastrowid)

    def tag_asset(self, library_id: str, path: str | Path, tag_id: int) -> None:
        key = nfc(path)
        with self._write() as cursor:
            cursor.execute(
                "INSERT OR IGNORE INTO asset_tags (library_id, path, tag_id)"
                " VALUES (?, ?, ?)",
                (library_id, key, tag_id),
            )
            self._refresh_fts_tags(cursor, library_id, key)

    def untag_asset(self, library_id: str, path: str | Path, tag_id: int) -> None:
        key = nfc(path)
        with self._write() as cursor:
            cursor.execute(
                "DELETE FROM asset_tags WHERE library_id = ? AND path = ? AND tag_id = ?",
                (library_id, key, tag_id),
            )
            self._refresh_fts_tags(cursor, library_id, key)

    @staticmethod
    def _refresh_fts_tags(cursor: sqlite3.Cursor, library_id: str, path: str) -> None:
        cursor.execute(
            "UPDATE assets_fts SET tag_names = COALESCE(("
            " SELECT group_concat(t.name, ' ') FROM asset_tags at"
            " JOIN tags t ON t.id = at.tag_id"
            " WHERE at.library_id = assets_fts.library_id"
            "   AND at.path = assets_fts.path), '')"
            " WHERE library_id = ? AND path = ?",
            (library_id, path),
        )

    def tags_of(self, library_id: str, path: str | Path) -> list[str]:
        with self._read() as cursor:
            rows = cursor.execute(
                "SELECT t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id"
                " WHERE at.library_id = ? AND at.path = ? ORDER BY t.name",
                (library_id, nfc(path)),
            ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------ 集合

    def create_collection(self, library_id: str, name: str, color: int = 0) -> int:
        with self._write() as cursor:
            cursor.execute(
                "INSERT INTO collections (library_id, name, color, created_at)"
                " VALUES (?, ?, ?, ?)",
                (library_id, name, color, time.time()),
            )
            return int(cursor.lastrowid)

    def add_to_collection(self, collection_id: int, path: str | Path) -> None:
        with self._write() as cursor:
            cursor.execute(
                "INSERT OR IGNORE INTO collection_items"
                " (collection_id, path, sort_order, added_at) VALUES (?, ?, 0, ?)",
                (collection_id, nfc(path), time.time()),
            )

    def collection_member_paths(self, collection_id: int) -> list[str]:
        with self._read() as cursor:
            rows = cursor.execute(
                "SELECT path FROM collection_items WHERE collection_id = ?"
                " ORDER BY sort_order, added_at",
                (collection_id,),
            ).fetchall()
        return [row[0] for row in rows]
