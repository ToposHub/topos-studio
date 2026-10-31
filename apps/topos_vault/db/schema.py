"""SQLite schema 定义与迁移。

所有建表 SQL 集中在此，便于审查与版本化。迁移用 ``user_version`` PRAGMA 管理，
每次 schema 变更追加一个 migration 函数，按版本号顺序执行。

核心表关系：

    media_source (fingerprint PK)          ← 三类素材统一身份层
      ├── clip (video)                     ← 镜头片段（检索基本单位）
      │     ├── transcript_line            ← 台词
      │     └── face_track                 ← 人脸
      ├── audio_meta (audio)               ← 音乐/音频整曲元数据
      ├── image_meta (image)               ← 图片整图元数据
      └── vector_index                     ← FAISS 向量统一映射
"""

from __future__ import annotations

import sqlite3
from typing import Any

from apps.topos_vault.paths import get_db_path


# ---------------------------------------------------------------------- #
# Schema 版本（每次 breaking 变更 +1，追加 migration 函数）
# ---------------------------------------------------------------------- #

SCHEMA_VERSION = 2


# ---------------------------------------------------------------------- #
# 建表 SQL
# ---------------------------------------------------------------------- #

# 主表：三类素材统一身份层（指纹为主键）
_SQL_MEDIA_SOURCE = """
CREATE TABLE IF NOT EXISTS media_source (
    fingerprint    TEXT PRIMARY KEY,
    media_type     TEXT NOT NULL,              -- video / audio / image / image_sequence
    -- 路径线索（relink 时逐级尝试，见 fingerprint/relink.py）
    path_original  TEXT NOT NULL,              -- 首次导入的绝对路径
    path_current   TEXT,                       -- 最近一次确认有效的路径
    path_relative  TEXT,                       -- 相对某基准目录（可选）
    -- 通用元数据
    file_size      INTEGER,
    duration_sec   REAL,                       -- video/audio 用
    width          INTEGER,                    -- video/image 用
    height         INTEGER,
    fps            REAL,                       -- video 用
    codec          TEXT,
    -- 状态
    path_status    TEXT NOT NULL DEFAULT 'valid',  -- valid / missing / relocated / unknown
    title          TEXT,                       -- 影片名 / 曲名 / 图片名
    topos_asset_id TEXT,                       -- 软关联主项目 MediaAsset.asset_id
    indexed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 视频专属：镜头片段（一个 video source → N 个 clip）
_SQL_CLIP = """
CREATE TABLE IF NOT EXISTS clip (
    clip_id        TEXT PRIMARY KEY,           -- UUID
    fingerprint    TEXT NOT NULL,
    clip_index     INTEGER NOT NULL,           -- 在源中的第几个镜头
    start_sec      REAL NOT NULL,
    end_sec        REAL NOT NULL,
    duration_sec   REAL NOT NULL,
    shot_type      TEXT,                       -- 特写/近景/中景/全景/远景
    keyframes      TEXT,                       -- JSON: 关键帧路径数组
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending/done/failed
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (fingerprint) REFERENCES media_source(fingerprint) ON DELETE CASCADE
);
"""

# 视频专属：台词（一个 clip 可有多句台词）
_SQL_TRANSCRIPT_LINE = """
CREATE TABLE IF NOT EXISTS transcript_line (
    line_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id        TEXT NOT NULL,
    text           TEXT NOT NULL,
    start_sec      REAL,
    end_sec        REAL,
    speaker        TEXT,                       -- S1/S2（diarization 输出）
    character      TEXT,                       -- 角色名（人工/聚类标注后填）
    emotion        TEXT,                       -- 情绪标签
    has_faiss      INTEGER NOT NULL DEFAULT 0, -- 是否已写入 transcript.faiss
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (clip_id) REFERENCES clip(clip_id) ON DELETE CASCADE
);
"""

# 视频专属：人脸
_SQL_FACE_TRACK = """
CREATE TABLE IF NOT EXISTS face_track (
    face_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id        TEXT NOT NULL,
    character      TEXT,                       -- 角色名
    cluster_id     INTEGER,                    -- 聚类簇 ID
    face_angle     TEXT,                       -- 正面/侧脸/背面
    completeness   TEXT,                       -- 完整/遮挡
    has_faiss      INTEGER NOT NULL DEFAULT 0, -- 是否已写入 face.faiss
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (clip_id) REFERENCES clip(clip_id) ON DELETE CASCADE
);
"""

# 音乐/音频专属（整曲级，不切分）
_SQL_AUDIO_META = """
CREATE TABLE IF NOT EXISTS audio_meta (
    fingerprint    TEXT PRIMARY KEY,
    bpm            REAL,                       -- librosa 算（非大模型）
    key            TEXT,                       -- 调性，如 "C major"
    genre          TEXT,                       -- 流派
    mood           TEXT,                       -- 情绪标签
    instruments    TEXT,                       -- JSON: ["strings","percussion"]
    energy         REAL,                       -- 0.0-1.0 能量值
    vocal_type     TEXT,                       -- instrumental / vocal / mixed
    description    TEXT,                       -- Qwen2.5-Omni 生成的自然语言描述
    has_faiss      INTEGER NOT NULL DEFAULT 0, -- 是否已写入 audio_semantic.faiss
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (fingerprint) REFERENCES media_source(fingerprint) ON DELETE CASCADE
);
"""

# 图片专属（整图级）
_SQL_IMAGE_META = """
CREATE TABLE IF NOT EXISTS image_meta (
    fingerprint    TEXT PRIMARY KEY,
    tags           TEXT,                       -- JSON: ["夜景","街道","人像"]
    caption        TEXT,                       -- Qwen3-VL 生成的描述
    dominant_color TEXT,                       -- 主色调
    orientation    TEXT,                       -- landscape / portrait / square
    has_faiss      INTEGER NOT NULL DEFAULT 0, -- 是否已写入 visual.faiss
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (fingerprint) REFERENCES media_source(fingerprint) ON DELETE CASCADE
);
"""

# 统一 FAISS 向量映射表（所有类型共用）
# 每个 FAISS 内部 vector_id 通过此表映射回业务表（transcript_line/face_track/clip/media_source）
_SQL_VECTOR_INDEX = """
CREATE TABLE IF NOT EXISTS vector_index (
    vector_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name     TEXT NOT NULL,              -- transcript / face / visual / audio_semantic
    ref_table      TEXT NOT NULL,              -- transcript_line / face_track / clip / media_source
    ref_id         TEXT NOT NULL,              -- 对应业务表的 PK
    model          TEXT NOT NULL,              -- qwen3-embedding / siglip2 / insightface / qwen2.5-omni
    dim            INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 索引（加速常用查询）
_SQL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_media_source_type ON media_source(media_type);
CREATE INDEX IF NOT EXISTS idx_media_source_status ON media_source(path_status);
CREATE INDEX IF NOT EXISTS idx_clip_fingerprint ON clip(fingerprint);
CREATE INDEX IF NOT EXISTS idx_transcript_clip ON transcript_line(clip_id);
CREATE INDEX IF NOT EXISTS idx_face_clip ON face_track(clip_id);
CREATE INDEX IF NOT EXISTS idx_vector_index_name ON vector_index(index_name);
CREATE INDEX IF NOT EXISTS idx_vector_ref ON vector_index(ref_table, ref_id);
"""

# 打标任务历史（P2-7）：记录每次 index/cluster 命令的运行
_SQL_INDEX_HISTORY = """
CREATE TABLE IF NOT EXISTS index_history (
    run_id         TEXT PRIMARY KEY,           -- UUID
    command        TEXT NOT NULL,              -- index / cluster / relink
    args           TEXT,                       -- JSON: 命令参数
    started_at     TEXT NOT NULL,              -- 开始时间 ISO
    finished_at    TEXT,                       -- 结束时间 ISO
    duration_sec   REAL,                       -- 耗时
    status         TEXT NOT NULL DEFAULT 'running',  -- running/done/failed
    total          INTEGER DEFAULT 0,          -- 处理总数
    done           INTEGER DEFAULT 0,
    skipped        INTEGER DEFAULT 0,
    failed         INTEGER DEFAULT 0,
    detail         TEXT                        -- JSON: 详细信息（错误/阶段/等）
);
"""


# ---------------------------------------------------------------------- #
# 迁移
# ---------------------------------------------------------------------- #

def _migration_v1(conn: sqlite3.Connection) -> None:
    """v1：初始 schema。"""
    conn.executescript(_SQL_MEDIA_SOURCE)
    conn.executescript(_SQL_CLIP)
    conn.executescript(_SQL_TRANSCRIPT_LINE)
    conn.executescript(_SQL_FACE_TRACK)
    conn.executescript(_SQL_AUDIO_META)
    conn.executescript(_SQL_IMAGE_META)
    conn.executescript(_SQL_VECTOR_INDEX)
    conn.executescript(_SQL_INDEXES)


def _migration_v2(conn: sqlite3.Connection) -> None:
    """v2：打标任务历史表（P2-7）。"""
    conn.executescript(_SQL_INDEX_HISTORY)


# 迁移函数注册表：版本号 → 迁移函数
# 新增 schema 变更时，追加 _migration_v2 等，并更新 SCHEMA_VERSION
_MIGRATIONS = {
    1: _migration_v1,
    2: _migration_v2,
}


def _get_user_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema 版本（SQLite user_version PRAGMA）。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """设置 schema 版本（必须用 PRAGMA user_version = N，不能用占位符）。"""
    conn.execute(f"PRAGMA user_version = {int(version)}")


def init_db(db_path: str | None = None) -> str:
    """初始化数据库：建表 + 迁移到最新版本。

    幂等：已存在的表不会重建，已执行的迁移不会重复执行。

    Args:
        db_path: 数据库文件路径。None 则用 ``get_db_path()``。

    Returns:
        实际使用的数据库路径。
    """
    if db_path is None:
        db_path = str(get_db_path())

    # WAL 模式：读不阻塞写，适合打标进程写 + 查询服务读的场景
    # （但跨进程并发仍需注意——打标进程独占写，查询服务只读）
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")  # 5s 写锁等待

        current = _get_user_version(conn)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"No migration registered for schema v{version}")
            migration(conn)
            _set_user_version(conn, version)
        conn.commit()
    finally:
        conn.close()

    return db_path


def get_schema_version(db_path: str | None = None) -> int:
    """读取数据库当前的 schema 版本（未初始化返回 0）。"""
    if db_path is None:
        db_path = str(get_db_path())
    if not __import__("os").path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return _get_user_version(conn)
    finally:
        conn.close()


def get_table_info(db_path: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """返回每张表的列信息（用于诊断和 CLI status）。"""
    if db_path is None:
        db_path = str(get_db_path())
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        tables = [
            "media_source", "clip", "transcript_line", "face_track",
            "audio_meta", "image_meta", "vector_index", "index_history",
        ]
        result = {}
        for t in tables:
            try:
                rows = conn.execute(f"PRAGMA table_info({t})").fetchall()
                result[t] = [
                    {"name": r[1], "type": r[2], "notnull": bool(r[3]),
                     "default": r[4], "pk": bool(r[5])}
                    for r in rows
                ]
            except sqlite3.OperationalError:
                result[t] = []  # 表不存在
        return result
    finally:
        conn.close()
