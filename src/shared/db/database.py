import json
import logging
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from typing import Iterator, Optional

from PyQt6.QtCore import QByteArray  # noqa: F401  (历史残留；见 AUD-32907)

logger = logging.getLogger(__name__)


def _meta_key(path):
    """[R1-4] file_meta 主键统一 NFC 归一。

    宿主与 file_explorer 共享 topos.db 的 file_meta 表，fe 侧一律以 NFC
    键读写；宿主此前用原始路径（macOS 目录列举常产出 NFD 字节）写入，
    会形成同文件 NFC/NFD 双行，fe 侧 NFC 查询 miss 后甚至把宿主行当
    孪生行清理。两端键空间必须一致。"""
    return unicodedata.normalize('NFC', path) if path else path


class Database:
    """SQLite 持久化层（settings / layouts / shortcuts / file_meta）。

    AUD-32901: 复用单一长生命周期连接。此前每个方法调用都 connect+
    close，而 LibraryProxyModel.lessThan 按评分排序时每次比较调用两次
    get_file_rating——数千文件的目录一次排序触发数万次连接建立/关闭，
    全部发生在 UI 线程。连接用 ``check_same_thread=False`` 创建并由
    ``_lock`` 串行化访问（当前调用方均在 UI 线程，锁是防未来跨线程
    使用，不改变现役行为）。

    AUD-32903: 所有方法经 ``_read``/``_write`` 上下文管理器执行——
    异常路径不再泄漏连接（共享连接不因异常销毁/残留持锁），游标总是
    关闭；连接采用显式事务控制（isolation_level=None + BEGIN
    IMMEDIATE），多语句写（迁移、save_default_layout）保持原子。
    """

    def __init__(self, db_path: str = "topos.db"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.init_db()

    # ------------------------------------------------------------------
    # connection / transaction plumbing
    # ------------------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        """懒创建并复用共享连接（AUD-32901）。"""
        if self._conn is None:
            conn = sqlite3.connect(
                self.db_path, timeout=5.0, check_same_thread=False
            )
            # 显式事务控制：DDL/多语句写在显式 BEGIN 下才是原子的
            # （legacy isolation_level 只对 DML 隐式开事务，RENAME/CREATE
            # 各自即时提交——layouts 迁移崩溃窗口的根因，AUD-32902）。
            conn.isolation_level = None
            self._conn = conn
        return self._conn

    def close(self) -> None:
        """释放共享连接（可选；实例析构时 sqlite 也会回收）。"""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Cursor]:
        """读路径：锁内取游标，异常也保证关闭（AUD-32903）。"""
        with self._lock:
            conn = self._connection()
            cursor = conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Cursor]:
        """写路径：锁内单事务执行，成功提交、异常回滚（AUD-32903）。"""
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

    @staticmethod
    def _table_columns(cursor: sqlite3.Cursor, table: str) -> list:
        cursor.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def init_db(self) -> None:
        """Initializes the database tables if they don't exist.

        AUD-32902: 整个建表/迁移在一个显式事务内执行。此前 RENAME/
        CREATE/回填/DROP 各自即时提交，进程在 RENAME 提交后崩溃会把
        全部布局预设永久留在孤儿 layouts_legacy 表里；下次启动建出空
        新表、检测到 work_mode 列存在即跳过迁移，预设静默丢失。
        另对历史崩溃遗留的 layouts_legacy 做恢复合并（旧行为窗口期
        可能已产生孤儿表）。
        """
        with self._write() as cursor:
            # Settings Table (Key-Value Store)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            # Layouts Table (Name, Geometry, State, PanelStates)
            # work_mode scopes a layout preset to a studio work mode (image/
            # video/all); the composite primary key (name, work_mode) lets
            # the same preset name exist once per mode. Migrated from a
            # name-only PK below.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS layouts (
                    name TEXT,
                    geometry TEXT,
                    state TEXT,
                    panel_states TEXT,
                    is_default INTEGER DEFAULT 0,
                    work_mode TEXT NOT NULL DEFAULT 'all',
                    PRIMARY KEY (name, work_mode)
                )
            ''')

            # AUD-32902: 恢复历史崩溃遗留的孤儿 layouts_legacy 表——
            # 把行合并回（当前为空的）新表后删除，避免预设静默丢失。
            legacy_cols = self._table_columns(cursor, "layouts_legacy")
            if legacy_cols:
                has_panel_states = 'panel_states' in legacy_cols
                if has_panel_states:
                    cursor.execute('''
                        INSERT OR IGNORE INTO layouts (name, geometry, state, panel_states, is_default, work_mode)
                        SELECT name, geometry, state, panel_states, is_default, 'all' FROM layouts_legacy
                    ''')
                else:
                    cursor.execute('''
                        INSERT OR IGNORE INTO layouts (name, geometry, state, is_default, work_mode)
                        SELECT name, geometry, state, is_default, 'all' FROM layouts_legacy
                    ''')
                cursor.execute('DROP TABLE layouts_legacy')
                logger.info(
                    "Recovered orphan layouts_legacy table into layouts "
                    "(pre-AUD-32902 crash leftover)"
                )

            # Migrate an older single-PK layouts table to the composite-key
            # schema. SQLite cannot ALTER a PRIMARY KEY in place, so detect
            # the legacy table (missing work_mode column) and rebuild it —
            # atomically, inside this transaction (AUD-32902).
            columns = self._table_columns(cursor, "layouts")
            if 'work_mode' not in columns:
                cursor.execute('ALTER TABLE layouts RENAME TO layouts_legacy')
                cursor.execute('''
                    CREATE TABLE layouts (
                        name TEXT,
                        geometry TEXT,
                        state TEXT,
                        panel_states TEXT,
                        is_default INTEGER DEFAULT 0,
                        work_mode TEXT NOT NULL DEFAULT 'all',
                        PRIMARY KEY (name, work_mode)
                    )
                ''')
                # Legacy rows had no panel_states column either in the
                # oldest form; copy whatever columns exist defensively.
                has_panel_states = 'panel_states' in columns
                if has_panel_states:
                    cursor.execute('''
                        INSERT INTO layouts (name, geometry, state, panel_states, is_default, work_mode)
                        SELECT name, geometry, state, panel_states, is_default, 'all' FROM layouts_legacy
                    ''')
                else:
                    cursor.execute('''
                        INSERT INTO layouts (name, geometry, state, is_default, work_mode)
                        SELECT name, geometry, state, is_default, 'all' FROM layouts_legacy
                    ''')
                cursor.execute('DROP TABLE layouts_legacy')
            elif 'panel_states' not in columns:
                cursor.execute('ALTER TABLE layouts ADD COLUMN panel_states TEXT')

            # Shortcuts Table (Action, Shortcut) - Placeholder for future use
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS shortcuts (
                    action_name TEXT PRIMARY KEY,
                    shortcut TEXT
                )
            ''')

            # File Metadata Table (Ratings, etc.) - 使用 file_meta 与 Flask 后端保持一致
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS file_meta (
                    path TEXT PRIMARY KEY,
                    rating INTEGER DEFAULT 0,
                    tags TEXT,
                    comments TEXT,
                    cached_info TEXT
                )
            ''')

    # ------------------------------------------------------------------
    # file_meta
    # ------------------------------------------------------------------
    def get_file_rating(self, path: str) -> int:
        with self._read() as cursor:
            cursor.execute(
                'SELECT rating FROM file_meta WHERE path = ?', (_meta_key(path),)
            )
            row = cursor.fetchone()
        return row[0] if row else 0

    def set_file_rating(self, path: str, rating: int) -> None:
        with self._write() as cursor:
            cursor.execute('''
                INSERT INTO file_meta (path, rating) VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET rating = excluded.rating
            ''', (_meta_key(path), rating))

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self._read() as cursor:
            cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
            row = cursor.fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value) -> None:
        with self._write() as cursor:
            cursor.execute(
                'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                (key, str(value)),
            )

    # ------------------------------------------------------------------
    # layouts
    # ------------------------------------------------------------------
    def save_layout(self, name: str, geometry: str, state: str,
                    panel_states=None, is_default: bool = False,
                    work_mode: str = "all") -> None:
        """
        Saves window geometry and state (dock layout).
        geometry and state should be hex strings (from QByteArray.toHex()).
        panel_states should be a JSON string mapping page names to their layout states.
        work_mode scopes the preset ("all"/"image"/"video") so presets do not
        leak across studio work modes.
        """
        panel_states_json = json.dumps(panel_states) if panel_states else None
        with self._write() as cursor:
            cursor.execute('''
                INSERT OR REPLACE INTO layouts (name, geometry, state, panel_states, is_default, work_mode)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, geometry, state, panel_states_json,
                  1 if is_default else 0, work_mode))

    def load_layout(self, name: str, work_mode: str = "all"):
        """Returns (geometry, state, panel_states) or (None, None, None).

        Looks up the preset scoped to ``work_mode``.
        """
        with self._read() as cursor:
            cursor.execute(
                'SELECT geometry, state, panel_states FROM layouts WHERE name = ? AND work_mode = ?',
                (name, work_mode),
            )
            row = cursor.fetchone()
        if row:
            panel_states = json.loads(row[2]) if row[2] else None
            return (row[0], row[1], panel_states)
        return (None, None, None)

    def list_layouts(self, work_mode: str = "all") -> list:
        """List preset names for the given work mode only."""
        with self._read() as cursor:
            cursor.execute(
                'SELECT name FROM layouts WHERE work_mode = ?', (work_mode,)
            )
            rows = cursor.fetchall()
        return [row[0] for row in rows]

    def delete_layout(self, name: str, work_mode: str = "all") -> None:
        with self._write() as cursor:
            cursor.execute(
                'DELETE FROM layouts WHERE name = ? AND work_mode = ?',
                (name, work_mode),
            )

    def clear_layouts(self) -> None:
        with self._write() as cursor:
            cursor.execute('DELETE FROM layouts')

    def get_default_layout(self, work_mode: str = "all"):
        """Returns (geometry, state, panel_states) for the default layout of the
        given work mode, or (None, None, None)."""
        with self._read() as cursor:
            cursor.execute(
                'SELECT geometry, state, panel_states FROM layouts WHERE is_default = 1 AND work_mode = ? LIMIT 1',
                (work_mode,),
            )
            row = cursor.fetchone()
        if row:
            panel_states = json.loads(row[2]) if row[2] else None
            return (row[0], row[1], panel_states)
        return (None, None, None)

    def save_default_layout(self, geometry: str, state: str,
                            panel_states=None, work_mode: str = "all") -> None:
        """Saves the given layout as the default layout (overwriting any existing default for the mode)."""
        panel_states_json = json.dumps(panel_states) if panel_states else None
        with self._write() as cursor:
            # First, clear any existing default for this work mode only, then
            # set the new default — one transaction keeps default uniqueness
            # atomic (AUD-32903).
            cursor.execute(
                'UPDATE layouts SET is_default = 0 WHERE is_default = 1 AND work_mode = ?',
                (work_mode,),
            )
            cursor.execute('''
                INSERT INTO layouts (name, geometry, state, panel_states, is_default, work_mode)
                VALUES ('Default', ?, ?, ?, 1, ?)
                ON CONFLICT(name, work_mode) DO UPDATE SET
                    geometry = excluded.geometry,
                    state = excluded.state,
                    panel_states = excluded.panel_states,
                    is_default = 1
            ''', (geometry, state, panel_states_json, work_mode))
