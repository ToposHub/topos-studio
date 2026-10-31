"""查询服务测试：搜索 + 类型过滤 + file_explorer 格式。"""

from __future__ import annotations

import pytest


def _seed_library(repo):
    """灌入测试打标数据。"""
    from apps.topos_vault.db.repository import MediaSource, ImageMeta, AudioMeta

    images = [
        ("smile1", "一个女孩在微笑", ["微笑", "人像", "happy"]),
        ("smile2", "微笑的特写镜头", ["特写", "微笑"]),
        ("street", "夜景城市街道", ["夜景", "街道"]),
        ("serious", "严肃的人物肖像", ["人像", "严肃"]),
    ]
    for i, (name, caption, tags) in enumerate(images):
        fp = f"phash:{name}"
        repo.upsert_media_source(MediaSource(
            fingerprint=fp, media_type="image",
            path_original=f"/photos/{name}.jpg", path_current=f"/photos/{name}.jpg",
            file_size=10000 + i, title=f"{name}.jpg",
        ))
        repo.upsert_image_meta(ImageMeta(fingerprint=fp, caption=caption, tags=tags))

    # 音频
    repo.upsert_media_source(MediaSource(
        fingerprint="chroma:happy", media_type="audio",
        path_original="/music/happy.mp3", path_current="/music/happy.mp3",
        title="happy.mp3",
    ))
    repo.upsert_audio_meta(AudioMeta(
        fingerprint="chroma:happy", mood="happy", description="欢快的背景音乐", bpm=120.0,
    ))

    # 视频
    repo.upsert_media_source(MediaSource(
        fingerprint="xxh3:scene", media_type="video",
        path_original="/movies/scene.mp4", path_current="/movies/scene.mp4",
        title="scene.mp4",
    ))


def test_search_by_caption(repo):
    """通过 caption 搜到图片。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    hits = svc.search("微笑")
    assert len(hits) >= 2
    names = [h.source.title for h in hits]
    assert any("smile1" in n for n in names)
    assert any("smile2" in n for n in names)


def test_search_by_tags(repo):
    """通过 tags 搜到。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    hits = svc.search("夜景")
    assert len(hits) >= 1
    assert any("street" in h.source.title for h in hits)


def test_search_by_mood(repo):
    """通过 mood 搜到音频。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    hits = svc.search("happy")
    assert len(hits) >= 1
    assert any(h.source.media_type == "audio" for h in hits)


def test_search_by_filename(repo):
    """通过文件名兜底搜到视频。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    hits = svc.search("scene")
    assert len(hits) >= 1
    assert any(h.source.media_type == "video" for h in hits)


def test_search_type_filter(repo):
    """类型过滤：只返回指定类型。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    # 全部
    all_hits = svc.search("微笑", media_type="all")
    assert all(h.source.media_type in ("image", "image_sequence") for h in all_hits)

    # 只图片
    img_hits = svc.search("微笑", media_type="image")
    assert all(h.source.media_type == "image" for h in img_hits)

    # 只视频（微笑 在视频里没有打标，应该 0 条）
    vid_hits = svc.search("微笑", media_type="video")
    assert len(vid_hits) == 0


def test_empty_query(repo):
    """空查询返回空。"""
    from apps.topos_vault.server.query_service import QueryService
    svc = QueryService(repo)
    assert svc.search("") == []
    assert svc.search("   ") == []


def test_relevance_ordering(repo):
    """精确匹配排前面。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    hits = svc.search("微笑")
    if len(hits) >= 2:
        # 相关度应非递增
        relevances = [h.relevance for h in hits]
        assert relevances == sorted(relevances, reverse=True)


def test_to_file_explorer_dict(repo):
    """转换成 file_explorer 兼容格式。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    hits = svc.search("微笑")
    assert hits
    d = svc.to_file_explorer_dict(hits[0])

    # 必须有 file_explorer 渲染需要的字段
    assert "name" in d
    assert "path" in d
    assert "type" in d
    assert d["type"] in ("video", "image", "audio", "file")
    # vault 特有字段
    assert "vault_caption" in d
    assert "vault_relevance" in d


def test_search_excludes_missing(repo):
    """path_status=missing 的素材不出现在结果里。"""
    from apps.topos_vault.db.repository import MediaSource, ImageMeta
    from apps.topos_vault.server.query_service import QueryService

    repo.upsert_media_source(MediaSource(
        fingerprint="phash:missing", media_type="image",
        path_original="/missing.jpg", path_status="missing",
    ))
    repo.upsert_image_meta(ImageMeta(
        fingerprint="phash:missing", caption="丢失的微笑照片", tags=["微笑"],
    ))
    svc = QueryService(repo)

    hits = svc.search("微笑")
    fps = [h.source.fingerprint for h in hits]
    assert "phash:missing" not in fps


def test_search_limit(repo):
    """limit 限制返回数量。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_library(repo)
    svc = QueryService(repo)

    hits = svc.search("微笑", limit=1)
    assert len(hits) <= 1
