"""数据库 schema 和 repository 测试。"""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------- #
# schema 初始化与迁移
# ---------------------------------------------------------------------- #

def test_init_db_creates_all_tables(initialized_db):
    """init_db 建出全部 7 张表。"""
    import sqlite3
    conn = sqlite3.connect(str(initialized_db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()

    expected = {
        "media_source", "clip", "transcript_line", "face_track",
        "audio_meta", "image_meta", "vector_index",
    }
    assert expected.issubset(tables), f"缺少表：{expected - tables}"


def test_init_db_idempotent(initialized_db):
    """重复 init_db 不报错、版本不变。"""
    from apps.topos_vault.db import schema
    v1 = schema.get_schema_version()
    schema.init_db()
    v2 = schema.get_schema_version()
    assert v1 == v2 == schema.SCHEMA_VERSION


def test_schema_version_starts_at_current(initialized_db):
    """首次初始化后版本为当前 SCHEMA_VERSION（v2 加了 index_history 表）。"""
    from apps.topos_vault.db import schema
    assert schema.get_schema_version() == schema.SCHEMA_VERSION == 2


def test_wal_mode_enabled(initialized_db):
    """WAL 模式开启（支持并发读不阻塞写）。"""
    import sqlite3
    conn = sqlite3.connect(str(initialized_db))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


# ---------------------------------------------------------------------- #
# media_source 读写
# ---------------------------------------------------------------------- #

def test_upsert_and_get_media_source(repo):
    """插入并查回 media_source。"""
    from apps.topos_vault.db.repository import MediaSource
    src = MediaSource(
        fingerprint="test:abc", media_type="video",
        path_original="/m/test.mp4", file_size=1000,
    )
    repo.upsert_media_source(src)
    got = repo.get_media_source("test:abc")
    assert got is not None
    assert got.media_type == "video"
    assert got.path_original == "/m/test.mp4"
    assert got.path_current == "/m/test.mp4"  # 默认等于 original
    assert got.path_status == "valid"


def test_upsert_is_idempotent(repo):
    """重复 upsert 同指纹不报错，路径线索更新。"""
    from apps.topos_vault.db.repository import MediaSource
    src = MediaSource(
        fingerprint="test:dup", media_type="image",
        path_original="/p/a.jpg", file_size=100,
    )
    repo.upsert_media_source(src)
    # 再插一次，改路径
    src2 = MediaSource(
        fingerprint="test:dup", media_type="image",
        path_original="/p/a.jpg", path_current="/p/moved/a.jpg",
        file_size=100, path_status="relocated",
    )
    repo.upsert_media_source(src2)
    got = repo.get_media_source("test:dup")
    assert got.path_current == "/p/moved/a.jpg"
    assert got.path_status == "relocated"
    assert repo.count_media_sources() == 1  # 没有重复


def test_get_by_path(repo):
    """按路径查（增量去重用）。"""
    from apps.topos_vault.db.repository import MediaSource
    repo.upsert_media_source(MediaSource(
        fingerprint="fp1", media_type="video",
        path_original="/x/y.mp4", path_current="/x/y.mp4",
    ))
    # 原 path
    assert repo.get_media_source_by_path("/x/y.mp4").fingerprint == "fp1"
    # 不存在的
    assert repo.get_media_source_by_path("/nope") is None


def test_list_with_filters(repo):
    """按类型/状态过滤列表。"""
    from apps.topos_vault.db.repository import MediaSource
    for i, (mt, st) in enumerate([("video", "valid"), ("audio", "missing"),
                                   ("image", "valid"), ("video", "relocated")]):
        repo.upsert_media_source(MediaSource(
            fingerprint=f"fp{i}", media_type=mt,
            path_original=f"/p/{i}", path_status=st,
        ))
    assert repo.count_media_sources() == 4
    assert repo.count_media_sources("video") == 2
    assert len(repo.list_media_sources(media_type="video")) == 2
    assert len(repo.list_media_sources(path_status="missing")) == 1
    assert len(repo.list_media_sources(path_status="valid")) == 2


def test_update_path_status(repo):
    """relink 后更新状态。"""
    from apps.topos_vault.db.repository import MediaSource
    repo.upsert_media_source(MediaSource(
        fingerprint="fp", media_type="video", path_original="/old.mp4",
    ))
    repo.update_path_status("fp", "/new.mp4", "relocated")
    got = repo.get_media_source("fp")
    assert got.path_current == "/new.mp4"
    assert got.path_status == "relocated"


# ---------------------------------------------------------------------- #
# 视频专属表
# ---------------------------------------------------------------------- #

def test_clip_insert_and_list(repo):
    """镜头片段插入和列表。"""
    from apps.topos_vault.db.repository import MediaSource, Clip
    repo.upsert_media_source(MediaSource(
        fingerprint="fp", media_type="video", path_original="/m.mp4",
    ))
    c1 = Clip(clip_id="", fingerprint="fp", clip_index=0,
              start_sec=0, end_sec=5, duration_sec=5)
    c2 = Clip(clip_id="", fingerprint="fp", clip_index=1,
              start_sec=5, end_sec=10, duration_sec=5)
    repo.insert_clip(c1)
    repo.insert_clip(c2)
    assert c1.clip_id  # 自动生成了 UUID
    clips = repo.list_clips("fp")
    assert len(clips) == 2
    assert clips[0].clip_index == 0
    assert clips[1].clip_index == 1


def test_transcript_lines(repo):
    """台词批量插入。"""
    from apps.topos_vault.db.repository import MediaSource, Clip, TranscriptLine
    repo.upsert_media_source(MediaSource(
        fingerprint="fp", media_type="video", path_original="/m.mp4",
    ))
    clip = Clip(clip_id="c1", fingerprint="fp", clip_index=0,
                start_sec=0, end_sec=5, duration_sec=5)
    repo.insert_clip(clip)
    repo.insert_transcript_lines([
        TranscriptLine(line_id=None, clip_id="c1", text="你好", start_sec=0.5, end_sec=1.5),
        TranscriptLine(line_id=None, clip_id="c1", text="世界", start_sec=2.0, end_sec=3.0),
    ])
    lines = repo.list_transcript_lines("c1")
    assert len(lines) == 2
    assert lines[0].text == "你好"


def test_face_tracks(repo):
    """人脸记录批量插入。"""
    from apps.topos_vault.db.repository import MediaSource, Clip, FaceTrack
    repo.upsert_media_source(MediaSource(
        fingerprint="fp", media_type="video", path_original="/m.mp4",
    ))
    repo.insert_clip(Clip(clip_id="c1", fingerprint="fp", clip_index=0,
                          start_sec=0, end_sec=5, duration_sec=5))
    repo.insert_face_tracks([
        FaceTrack(face_id=None, clip_id="c1", character="角色A", cluster_id=0),
    ])


# ---------------------------------------------------------------------- #
# 音频/图片专属表
# ---------------------------------------------------------------------- #

def test_audio_meta_upsert(repo):
    """音乐元数据 upsert（含 JSON 字段）。"""
    from apps.topos_vault.db.repository import MediaSource, AudioMeta
    repo.upsert_media_source(MediaSource(
        fingerprint="afp", media_type="audio", path_original="/s.mp3",
    ))
    repo.upsert_audio_meta(AudioMeta(
        fingerprint="afp", bpm=120.0, mood="happy",
        instruments=["strings", "drums"],
    ))
    got = repo.get_audio_meta("afp")
    assert got.bpm == 120.0
    assert got.instruments == ["strings", "drums"]


def test_image_meta_upsert(repo):
    """图片元数据 upsert（含 JSON 字段）。"""
    from apps.topos_vault.db.repository import MediaSource, ImageMeta
    repo.upsert_media_source(MediaSource(
        fingerprint="ifp", media_type="image", path_original="/p.jpg",
    ))
    repo.upsert_image_meta(ImageMeta(
        fingerprint="ifp", tags=["夜景", "人像"], caption="夜晚的街道",
    ))
    got = repo.get_image_meta("ifp")
    assert got.tags == ["夜景", "人像"]
    assert got.caption == "夜晚的街道"


# ---------------------------------------------------------------------- #
# vector_index
# ---------------------------------------------------------------------- #

def test_vector_index_roundtrip(repo):
    """向量注册和反查。"""
    vid = repo.register_vector("transcript", "transcript_line", "42", "qwen3-emb", 1024)
    assert vid > 0
    ref = repo.get_vector_ref("transcript", vid)
    assert ref == ("transcript_line", "42")


def test_vector_index_batch_lookup(repo):
    """批量查 vector_id（粗筛后的小范围检索用）。"""
    repo.register_vector("visual", "clip", "c1", "siglip2", 1152)
    repo.register_vector("visual", "clip", "c2", "siglip2", 1152)
    mapping = repo.list_vector_ids("visual", ["c1", "c2", "nonexistent"])
    assert "c1" in mapping
    assert "c2" in mapping
    assert "nonexistent" not in mapping


# ---------------------------------------------------------------------- #
# 级联删除
# ---------------------------------------------------------------------- #

def test_cascade_delete(repo):
    """删 media_source 后，关联的 clip 也被级联删（外键 ON DELETE CASCADE）。"""
    import sqlite3
    from apps.topos_vault.db.repository import MediaSource, Clip
    repo.upsert_media_source(MediaSource(
        fingerprint="fp", media_type="video", path_original="/m.mp4",
    ))
    repo.insert_clip(Clip(clip_id="c1", fingerprint="fp", clip_index=0,
                          start_sec=0, end_sec=5, duration_sec=5))

    # 直接 SQL 删 media_source（repository 没暴露 delete，用原生连接）
    from apps.topos_vault.paths import get_db_path
    conn = sqlite3.connect(str(get_db_path()))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM media_source WHERE fingerprint = 'fp'")
    conn.commit()
    conn.close()

    # clip 应该也被删了
    assert repo.list_clips("fp") == []
