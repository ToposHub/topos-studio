"""[ARCH-1] 持久化层：共享 topos.db（SQLite）+ config.json + 目录列表缓存。"""
import json
import logging
import os
import sqlite3
import threading
import time
import unicodedata

import const

logger = logging.getLogger(__name__)


# [FUNC-3] 配置/回退 DB 一律锚定本文件目录：CWD 不同（从任意目录启动）
# 不再产生分叉的 config.json / metadata.db
_FE_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 修改：使用主项目的 topos.db 以便与 library_panel 互通星级评定
# file_explorer 在 external/file_explorer/，topos.db 在项目根目录
_topos_db_path = os.path.join(os.path.dirname(os.path.dirname(_FE_BASE_DIR)), "topos.db")


# [BUG-P1-04] DB 选择延迟到首次连接时判定（旧实现 import 期一次性判定：
# topos.db 当时不存在则永久锚定 metadata.db，进程内不回切 → 元数据双库
# 分叉，现场两库并存且数据已漂移）。规则：
#   - topos.db 存在 → 恒用 topos.db（与宿主互通）；
#   - 发现历史 metadata.db → 自动把独有行合并进 topos.db 后改名留档；
#   - topos.db 不存在 → 退回 metadata.db（行为同旧版）。
_merge_failed = False
_merge_lock = threading.Lock()
# 回退 DB 路径提为模块全局（测试可隔离重定向；运行时恒为 file_explorer/metadata.db）
_fallback_db_path = os.path.join(_FE_BASE_DIR, "metadata.db")
DB_FILE = _fallback_db_path  # 兼容旧 from-import；_db() 每次重判
CONFIG_FILE = os.path.join(_FE_BASE_DIR, "config.json")


def _merge_metadata_db_into(src_path: str, dst_path: str) -> int:
    """把 metadata.db 的 file_meta/smart_folders 并入 topos.db，返回迁移行数。

    合并语义：两库都有同一 path 时以 topos.db 为准、只回填空值
    （对齐 _absorb_nfd_twin），不覆盖用户在新库已写的数据。"""
    src = sqlite3.connect(src_path)
    try:
        names = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        meta_rows = []
        folder_rows = []
        if 'file_meta' in names:
            meta_rows = src.execute(
                "SELECT path, tags, comments, rating, cached_info FROM file_meta").fetchall()
        if 'smart_folders' in names:
            folder_rows = src.execute(
                "SELECT name, rules, created_at FROM smart_folders").fetchall()
    finally:
        src.close()

    dst = sqlite3.connect(dst_path, timeout=const.DB_CONNECT_TIMEOUT_S)
    moved = 0
    try:
        c = dst.cursor()
        for path, tags, comments, rating, cached_info in meta_rows:
            if not path:
                continue
            row = c.execute("SELECT tags, comments, rating FROM file_meta WHERE path = ?",
                            (unicodedata.normalize('NFC', path),)).fetchone()
            if row is None:
                c.execute("INSERT INTO file_meta (path, tags, comments, rating, cached_info) "
                          "VALUES (?, ?, ?, ?, ?)",
                          (unicodedata.normalize('NFC', path), tags, comments,
                           rating or 0, cached_info))
                moved += 1
            else:
                new = (row[0] if row[0] else tags,
                       row[1] if row[1] else comments,
                       row[2] if row[2] else (rating or 0))
                if new != (row[0], row[1], row[2]):
                    c.execute("UPDATE file_meta SET tags = ?, comments = ?, rating = ? "
                              "WHERE path = ?",
                              (new[0], new[1], new[2], unicodedata.normalize('NFC', path)))
                    moved += 1
        for name, rules, created_at in folder_rows:
            dup = c.execute("SELECT 1 FROM smart_folders WHERE name = ? AND rules = ?",
                            (name, rules)).fetchone()
            if not dup:
                c.execute("INSERT INTO smart_folders (name, rules, created_at) VALUES (?, ?, ?)",
                          (name, rules, created_at))
                moved += 1
        dst.commit()
    finally:
        dst.close()
    return moved


def _resolve_db_file() -> str:
    """判定当前应使用的 DB 文件；发现 metadata.db 与 topos.db 并存时
    做一次性合并并把旧库改名留档（metadata.db.migrated-<时间戳>）。"""
    global DB_FILE
    fallback = _fallback_db_path
    topos_db = os.path.normpath(_topos_db_path)
    if not os.path.exists(topos_db):
        DB_FILE = fallback
        return fallback
    target = topos_db
    global _merge_failed
    if os.path.exists(fallback) and not _merge_failed:
        with _merge_lock:
            if os.path.exists(fallback) and not _merge_failed:
                try:
                    moved = _merge_metadata_db_into(fallback, topos_db)
                    stamp = time.strftime('%Y%m%d%H%M%S')
                    os.replace(fallback, fallback + f'.migrated-{stamp}')
                    logger.info(f"[DB] metadata.db 已合并进 topos.db（{moved} 行），"
                                f"旧库留档 metadata.db.migrated-{stamp}")
                except OSError as e:
                    # 改名失败（跨进程竞争等）：合并可能已成功，不回滚
                    logger.warning(f"[DB] metadata.db 合并后留档失败: {e}")
                except Exception as e:
                    # 合并失败：保留旧库，本进程内不再反复尝试（防每请求重试风暴）
                    _merge_failed = True
                    logger.error(f"[DB] metadata.db 合并失败（保留原文件，继续用 topos.db）: {e}")
    DB_FILE = target
    return target


def _db():
    """[FUNC-10] 统一 SQLite 连接入口。

    busy_timeout=5000 缓解与主应用共享 topos.db 的并发写冲突
    （此前 'database is locked' 被裸 except 静默吞掉 → 丢数据）。
    [BUG-P1-04] 每次连接时重判 DB 路径（含一次性双库合并）。"""
    conn = sqlite3.connect(_resolve_db_file(), timeout=const.DB_CONNECT_TIMEOUT_S)
    conn.execute(f"PRAGMA busy_timeout = {const.DB_BUSY_TIMEOUT_MS}")
    # [U9] REPLACE 语义（INSERT OR REPLACE）的隐式 DELETE 默认不触发 DELETE
    # 触发器（recursive_triggers 默认 OFF），导致 FTS 旧行残留、每次改评分
    # 都堆一条同 path 幽灵行。全局打开后隐式 DELETE 照常触发 fts_ad。
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn

def _meta_key(path) -> str:
    """[ARCH-10] file_meta 主键统一 NFC：macOS 目录列举可能产出 NFD 字节，
    两种形式写库会产生同文件双行、读库会 miss。所有 file_meta 读写键
    都必须经过本函数。"""
    return unicodedata.normalize('NFC', str(path).replace('\\', '/'))


DIR_CACHE, CACHE_TTL = {}, const.DIR_CACHE_TTL # 目录列表缓存
# [BUG-P2-03] 缓存条目上限：全量 files 列表进缓存，长会话浏览大量/超大
# 目录此前内存无限增长
DIR_CACHE_MAX = const.DIR_CACHE_MAX_ENTRIES


def put_dir_cache(path, data):
    """带淘汰的目录缓存写入：超限先清过期条目，仍超限按插入序淘汰最旧。"""
    with CACHE_LOCK:
        now = time.time()
        if len(DIR_CACHE) >= DIR_CACHE_MAX:
            expired = [k for k, (_, ts) in DIR_CACHE.items() if now - ts >= CACHE_TTL]
            for k in expired:
                del DIR_CACHE[k]
        while len(DIR_CACHE) >= DIR_CACHE_MAX:
            DIR_CACHE.pop(next(iter(DIR_CACHE)))
        DIR_CACHE[path] = (data, now)


CACHE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()


def invalidate_cache(path):
    """失效指定路径的目录列表缓存"""
    if not path: return
    try:
        # 统一路径分隔符并进行 NFC 标准化（解决 macOS 中文路径缓存失效失败的问题）
        path = unicodedata.normalize('NFC', path.replace('\\', '/'))
        # 尝试匹配带斜杠和不带斜杠的情况
        paths_to_clear = [path, path.rstrip('/'), path + '/' if not path.endswith('/') else path]
        with CACHE_LOCK:
            # [V5 复查] 递归聚合视图的缓存键是 "<path>|r<depth>"——只清三个
            # 精确变体清不掉它们，改名/删除后递归视图最长 60s 仍显示幽灵文件
            prefix = path.rstrip('/') + '|r'
            for k in [k for k in DIR_CACHE if k.startswith(prefix)]:
                del DIR_CACHE[k]
            for p in paths_to_clear:
                if p in DIR_CACHE:
                    del DIR_CACHE[p]
                    logger.info(f"[Cache] Invalidated: {p}")
    except Exception as e:
        logger.error(f"[Cache invalidation error: {e}")


def _absorb_nfd_twin(c, nfc_path: str, nfd_path: str) -> None:
    """[R1-3] 删除 NFD 孪生行前，把其非空 tags/comments/rating 合并进 NFC 行。

    此前直接 DELETE——宿主以 NFD 路径写过评分/标签时会被静默丢弃。
    只回填 NFC 行的空值，不覆盖用户在 NFC 行已写的数据。"""
    c.execute('''UPDATE file_meta SET
        tags = CASE WHEN file_meta.tags IS NULL OR file_meta.tags = ''
                    THEN (SELECT tags FROM file_meta WHERE path = ?) ELSE file_meta.tags END,
        comments = CASE WHEN file_meta.comments IS NULL OR file_meta.comments = ''
                    THEN (SELECT comments FROM file_meta WHERE path = ?) ELSE file_meta.comments END,
        rating = CASE WHEN COALESCE(file_meta.rating, 0) = 0
                    THEN COALESCE((SELECT rating FROM file_meta WHERE path = ?), 0) ELSE file_meta.rating END
        WHERE path = ?''', (nfd_path, nfd_path, nfd_path, nfc_path))
    c.execute("DELETE FROM file_meta WHERE path = ?", (nfd_path,))


def init_db():
    """初始化 SQLite 数据库并处理字段升级和迁移"""
    try:
        conn = _db()
        c = conn.cursor()
        
        # 检查是否存在旧的 file_metadata 表，如果有则迁移数据
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_metadata'")
        has_old_table = c.fetchone() is not None
        
        if has_old_table:
            # 迁移数据：file_metadata → file_meta
            # [ARCH-10] 此前只迁 rating，丢弃 tags/comments；且 path 未做 NFC
            # 归一（macOS 旧表常见 NFD 字节），与 NFC 新行形成同文件双行。
            try:
                # 旧表可能没有 tags/comments 列，按实际存在的列迁移
                c.execute("PRAGMA table_info(file_metadata)")
                old_cols = {info[1] for info in c.fetchall()}
                sel_cols = [col for col in ('path', 'tags', 'comments', 'rating') if col in old_cols]
                if 'path' in old_cols and 'rating' in old_cols:
                    c.execute('''CREATE TABLE IF NOT EXISTS file_meta (
                        path TEXT PRIMARY KEY,
                        tags TEXT,
                        comments TEXT,
                        rating INTEGER DEFAULT 0,
                        cached_info TEXT
                    )''')
                    col_list = ', '.join(sel_cols)
                    # NFC 归一后写入；ON CONFLICT 只回填 tags/comments 的空值，
                    # 不覆盖用户在 file_meta 里已写的评分（迁移可重复执行）
                    c.execute(f'''
                        INSERT INTO file_meta ({col_list})
                        SELECT {col_list} FROM file_metadata
                        WHERE path IS NOT NULL
                        ON CONFLICT(path) DO UPDATE SET
                            tags = CASE WHEN file_meta.tags IS NULL OR file_meta.tags = ''
                                        THEN excluded.tags ELSE file_meta.tags END,
                            comments = CASE WHEN file_meta.comments IS NULL OR file_meta.comments = ''
                                            THEN excluded.comments ELSE file_meta.comments END
                    ''')
                    # 历史 NFD 行就地归一（主键冲突 = 已有 NFC 孪生行，
                    # [R1-3] 合并非空数据后删旧行）
                    c.execute("SELECT path FROM file_meta")
                    for (p,) in c.fetchall():
                        nfc = unicodedata.normalize('NFC', p) if p else p
                        if nfc != p:
                            if c.execute("SELECT 1 FROM file_meta WHERE path = ?", (nfc,)).fetchone():
                                _absorb_nfd_twin(c, nfc, p)
                            else:
                                c.execute("UPDATE file_meta SET path = ? WHERE path = ?", (nfc, p))
                    conn.commit()
                    migrated = conn.total_changes
                    logger.info(f"[DB Migration] file_metadata→file_meta 完成"
                                f"（含 tags/comments，路径已 NFC 归一，变更 {migrated} 处）")
                    # [R1-5] 迁移成功后丢弃旧表：否则迁移块（含一份冗余 NFD
                    # 自愈）每次启动全量重跑
                    c.execute("DROP TABLE IF EXISTS file_metadata")
                    conn.commit()
            except Exception as e:
                logger.warning(f"[DB Migration] 迁移失败: {e}")
        
        # 确保 file_meta 表存在并包含所有必要字段
        c.execute('''CREATE TABLE IF NOT EXISTS file_meta (
            path TEXT PRIMARY KEY, 
            tags TEXT, 
            comments TEXT, 
            rating INTEGER DEFAULT 0,
            cached_info TEXT
        )''')
        
        # 检查并添加缺失的字段
        c.execute("PRAGMA table_info(file_meta)")
        columns = [info[1] for info in c.fetchall()]
        if 'rating' not in columns:
            c.execute("ALTER TABLE file_meta ADD COLUMN rating INTEGER DEFAULT 0")
        if 'cached_info' not in columns:
            c.execute("ALTER TABLE file_meta ADD COLUMN cached_info TEXT")

        # [ARCH-10] NFD 自愈（无条件执行）：macOS 目录列举可能产出 NFD 字节，
        # 旧版本未归一即写库 → 同一文件 NFC/NFD 双行、读取 miss。
        # 有 NFC 孪生行时合并数据后删 NFD 行，否则就地归一。
        # [R1-2] 纯 ASCII 路径在 NFC/NFD 下字节相同，先 SQL 预筛出含非 ASCII
        # 字符的行再归一，避免大库每次启动全表 fetchall + 逐行 normalize 的
        # 内存尖峰与启动变慢。
        c.execute("SELECT path FROM file_meta WHERE path GLOB '*[^ -~]*'")
        for (p,) in c.fetchall():
            nfc = unicodedata.normalize('NFC', p) if p else p
            if nfc != p:
                if c.execute("SELECT 1 FROM file_meta WHERE path = ?", (nfc,)).fetchone():
                    _absorb_nfd_twin(c, nfc, p)
                else:
                    c.execute("UPDATE file_meta SET path = ? WHERE path = ?", (nfc, p))
                logger.info(f"[DB Migration] file_meta NFD 行归一: {os.path.basename(p)}")
            
        # Smart folders 表
        c.execute('''CREATE TABLE IF NOT EXISTS smart_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            rules TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')

        # ==============================================================
        # [U6/GAP-05] 应用内回收站：文件移入 <fe>/.trash 目录，meta 行
        # 快照进 trash 表（file_meta 删除防幽灵记录），恢复时回迁并还原
        # 元数据；30 天过期由 trash.purge_expired 物理清理。
        # ==============================================================
        c.execute('''CREATE TABLE IF NOT EXISTS trash (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_path TEXT NOT NULL,
            item_name TEXT NOT NULL,
            is_dir INTEGER NOT NULL DEFAULT 0,
            trashed_name TEXT NOT NULL,
            deleted_at REAL NOT NULL,
            meta_json TEXT
        )''')

        # ==============================================================
        # [U5/GAP-01+02+03] 组织能力数据模型（全部幂等，可重复执行）
        # ==============================================================

        # —— GAP-03: 颜色标记（0=无 1红 2黄 3绿 4蓝 5紫）+ 旗标（0无 1选用 2拒收）——
        c.execute("PRAGMA table_info(file_meta)")
        columns = [info[1] for info in c.fetchall()]
        if 'color' not in columns:
            c.execute("ALTER TABLE file_meta ADD COLUMN color INTEGER DEFAULT 0")
        if 'flag' not in columns:
            c.execute("ALTER TABLE file_meta ADD COLUMN flag INTEGER DEFAULT 0")

        # ==============================================================
        # [V1/GAP-02] 技术元数据结构化列（索引器/元数据队列落库用）。
        # 全部可空：旧行升级后为 NULL，扫描/浏览到该文件时才回填。
        # 用户资产列（tags/comments/rating/color/flag/cached_info）永远
        # 不被索引器覆盖——索引写入只 touch 本块列。
        # ==============================================================
        c.execute("PRAGMA table_info(file_meta)")
        columns = [info[1] for info in c.fetchall()]
        _TECH_COLUMNS = (
            # 列名, 类型/默认
            ('mtime', 'REAL DEFAULT 0'),          # 磁盘 mtime（增量扫描比对键）
            ('size', 'INTEGER DEFAULT 0'),        # 字节（增量比对键）
            ('date_taken', 'REAL'),               # EXIF DateTimeOriginal / 视频 creation_time
            ('width', 'INTEGER'),
            ('height', 'INTEGER'),
            ('duration', 'REAL'),                 # 秒（视频/音频）
            ('codec', 'TEXT'),                    # 编解码显示名（H.264/ProRes…）
            ('camera', 'TEXT'),                   # "Make Model"
            ('lens', 'TEXT'),
            ('iso', 'INTEGER'),
            ('aperture', 'REAL'),                 # f 数值（4.0 = f/4.0）
            ('shutter', 'TEXT'),                  # "1/250"
            ('focal', 'REAL'),                    # mm
            ('gps_lat', 'REAL'),
            ('gps_lon', 'REAL'),
            ('dom_color', 'INTEGER'),             # [V3/GAP-09] 自动主色桶 0-11（-1=灰阶）
            ('phash', 'TEXT'),                    # [V3/GAP-08] 64bit 感知哈希 hex
            ('stack_id', 'INTEGER'),              # [V2/GAP-07] 堆叠组
            ('missing', 'INTEGER DEFAULT 0'),     # [V1/GAP-01] 索引时磁盘失联
            ('indexed_at', 'REAL'),               # 最后一次索引回填时间
        )
        for col, decl in _TECH_COLUMNS:
            if col not in columns:
                c.execute(f"ALTER TABLE file_meta ADD COLUMN {col} {decl}")

        # ==============================================================
        # [V2/GAP-05] 静态集合（手工圈选的素材包，区别于动态智能文件夹）
        # ==============================================================
        c.execute('''CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT,
            created_at REAL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS collection_items (
            collection_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            added_at REAL,
            PRIMARY KEY (collection_id, file_path)
        )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_coll_items_path ON collection_items(file_path)")

        # ==============================================================
        # [V2/GAP-07] 堆叠（连拍/多版本归组；成员挂 file_meta.stack_id）
        # ==============================================================
        c.execute('''CREATE TABLE IF NOT EXISTS stacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cover_path TEXT,
            created_at REAL
        )''')

        # ==============================================================
        # [V4/GAP-17] 图内文字（OCR）+ [V4/GAP-19] 视频标记 + [V4/GAP-18]
        # 导出预设 + [V4/GAP-22] 文件夹软锁
        # ==============================================================
        c.execute('''CREATE TABLE IF NOT EXISTS ocr_text (
            file_path TEXT PRIMARY KEY,
            text TEXT NOT NULL DEFAULT '',
            ocr_at REAL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS markers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            t_in REAL NOT NULL,
            t_out REAL,
            label TEXT,
            created_at REAL
        )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_markers_path ON markers(file_path)")
        c.execute('''CREATE TABLE IF NOT EXISTS export_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            config TEXT NOT NULL,
            created_at REAL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS folder_locks (
            path TEXT PRIMARY KEY,
            pwd_hash TEXT NOT NULL,
            hint TEXT
        )''')

        # —— GAP-01: 标签体系 2.0 —— tags 表（parent_id 预留层级）+ 多对多
        c.execute('''CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT,
            parent_id INTEGER
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS file_tags (
            file_path TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (file_path, tag_id)
        )''')

        # 旧逗号 tags 一次性迁移进 tags/file_tags（幂等：OR IGNORE；
        # legacy tags 列保留双写，老代码路径不受影响，迁移零丢失）
        for path, tags_str in c.execute(
                "SELECT path, tags FROM file_meta WHERE tags IS NOT NULL AND tags != ''") \
                .fetchall():
            for t in [x.strip() for x in str(tags_str).split(',') if x.strip()]:
                c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                c.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                             SELECT ?, id FROM tags WHERE name = ?""", (path, t))

        # —— GAP-02: FTS5 全库搜索（trigram 分词支持中文子串；不可用降级 LIKE）——
        fts_ready = False
        try:
            # [V4/GAP-17] 直接建 5 列（含 ocr）；旧库由下方探测分支 DROP 重建
            c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS file_meta_fts
                         USING fts5(path, name, tags, comments, ocr,
                                    tokenize='trigram')""")
            fts_ready = True
        except sqlite3.OperationalError as e:
            logger.warning(f"[U5] FTS5/trigram 不可用，全库搜索降级 LIKE: {e}")
        if fts_ready:
            # [V5 复查] 触发器定义升级（_ai/_au 携带 ocr 列，从 ocr_text 取权威
            # 值）——旧触发器只写 4 列，任何 tags/comments 更新都会把已索引的
            # OCR 文字静默抹掉。IF NOT EXISTS 不会替换旧定义，必须先 DROP
            c.execute('DROP TRIGGER IF EXISTS file_meta_fts_ai')
            c.execute('DROP TRIGGER IF EXISTS file_meta_fts_ad')
            c.execute('DROP TRIGGER IF EXISTS file_meta_fts_au')
            c.execute('''CREATE TRIGGER file_meta_fts_ai
                         AFTER INSERT ON file_meta BEGIN
                INSERT INTO file_meta_fts (path, name, tags, comments, ocr)
                VALUES (new.path, new.path, COALESCE(new.tags,''), COALESCE(new.comments,''),
                        COALESCE((SELECT text FROM ocr_text WHERE file_path = new.path), ''));
            END''')
            c.execute('''CREATE TRIGGER file_meta_fts_ad
                         AFTER DELETE ON file_meta BEGIN
                DELETE FROM file_meta_fts WHERE path = old.path;
            END''')
            c.execute('''CREATE TRIGGER file_meta_fts_au
                         AFTER UPDATE OF path, tags, comments ON file_meta BEGIN
                DELETE FROM file_meta_fts WHERE path = old.path;
                INSERT INTO file_meta_fts (path, name, tags, comments, ocr)
                VALUES (new.path, new.path, COALESCE(new.tags,''), COALESCE(new.comments,''),
                        COALESCE((SELECT text FROM ocr_text WHERE file_path = new.path), ''));
            END''')
            # 一次性回填（fe_markers 打点，幂等）；v2 = 修复 REPLACE 幽灵行后的
            # 全量重建（清掉历史累积的同 path 重复行）
            c.execute('''CREATE TABLE IF NOT EXISTS fe_markers (id TEXT PRIMARY KEY)''')
            if not c.execute("SELECT 1 FROM fe_markers WHERE id = 'fts_backfill_v2'").fetchone():
                c.execute("DELETE FROM file_meta_fts")
                c.execute("""INSERT INTO file_meta_fts (path, name, tags, comments, ocr)
                             SELECT fm.path, fm.path, COALESCE(fm.tags,''), COALESCE(fm.comments,''),
                                    COALESCE(ot.text, '')
                             FROM file_meta fm LEFT JOIN ocr_text ot ON ot.file_path = fm.path""")
                c.execute("INSERT OR REPLACE INTO fe_markers (id) VALUES ('fts_backfill_v2')")
                logger.info("[U5] file_meta_fts 全量回填完成")
            # [V4/GAP-17] FTS 增加 ocr 列：SQLite 不支持 ALTER 虚表列——检测旧
            # 结构（4 列）则 DROP 重建 + 等价回填（OCR 文字由 ocr 队列增量写入）
            _fts_cols = [r[1] for r in c.execute("PRAGMA table_info(file_meta_fts)").fetchall()]
            if _fts_cols and 'ocr' not in _fts_cols:
                c.execute("DROP TABLE IF EXISTS file_meta_fts")
                c.execute("""CREATE VIRTUAL TABLE file_meta_fts
                             USING fts5(path, name, tags, comments, ocr,
                                        tokenize='trigram')""")
                # [V5 复查] 回填 LEFT JOIN ocr_text：旧列结构下 ocr_text 可能
                # 已有数据，写死 '' 会把它们从索引里丢掉
                c.execute("""INSERT INTO file_meta_fts (path, name, tags, comments, ocr)
                             SELECT fm.path, fm.path, COALESCE(fm.tags,''), COALESCE(fm.comments,''),
                                    COALESCE(ot.text, '')
                             FROM file_meta fm LEFT JOIN ocr_text ot ON ot.file_path = fm.path""")
                logger.info("[V4] file_meta_fts 已重建（新增 ocr 列）并回填")

        # ==============================================================
        # [V1/GAP-15] 查询性能索引补齐（此前全库仅隐式主键索引：按标签查
        # 文件全表扫 file_tags，评分/日期/时长筛选全表扫 file_meta）。
        # ==============================================================
        c.execute("CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_meta_rating ON file_meta(rating)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_meta_date_taken ON file_meta(date_taken)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_meta_duration ON file_meta(duration)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_meta_missing ON file_meta(missing)")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_meta_stack ON file_meta(stack_id)
                     WHERE stack_id IS NOT NULL""")

        # [V1/GAP-15] WAL：waitress 4 线程 + 索引器/元数据队列并发写，
        # journal_mode 是 DB 文件的持久属性，置一次即可（部分网络卷不支持
        # 则保持原模式，不阻塞启动）。SQLite 在事务打开期间会静默拒绝切换
        # journal mode——先提交本连接的事务再设置；PRAGMA 带返回行，必须
        # 取回消费，否则悬挂语句会让随后的 commit 报 "statements in progress"。
        try:
            conn.commit()
            mode = c.execute("PRAGMA journal_mode=WAL").fetchone()
            logger.info(f"[V1] journal_mode = {mode[0] if mode else '?'}")
        except sqlite3.OperationalError as e:
            logger.warning(f"[V1] WAL 模式不可用（保持原 journal 模式）: {e}")

        conn.commit()
    except Exception as e:
        # [BUG-P2-13] 表创建失败必须拒启：旧实现仅记日志后继续运行，
        # 后续所有元数据操作持续 500 且用户无从得知原因
        logger.error(f"DB init error: {e}")
        raise RuntimeError(f"数据库初始化失败（{DB_FILE}）: {e}") from e
    finally:
        # [U10] 异常路径连接也须关闭（拒启场景泄漏的句柄会短暂锁库）
        conn.close()


def load_config():
    """线程安全加载配置"""
    with CONFIG_LOCK:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
            except Exception as e:
                logger.error(f"配置文件损坏，按空配置继续: {e}")
        return {}

def save_config(data):
    """保存配置到文件。
    [BUG-P2-12] 原子写：此前直接 open('w') 覆写，崩溃/断电时 config.json
    截断（libraries、窗口状态全丢）。改为临时文件 + fsync + os.replace。"""
    with CONFIG_LOCK: # 加锁
        try:
            # 必须在锁内重新读取，确保基于最新数据更新
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                        conf = json.load(f)
                except Exception:
                    conf = {}
            else:
                conf = {}

            conf.update(data)

            tmp = CONFIG_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(conf, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            logger.error(f"Config Save Error: {e}")
