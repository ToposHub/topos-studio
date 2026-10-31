"""P2-3 高级检索测试：以图搜图 + BPM 匹配 + 组合查询。

测：
    1. search_similar_image：查询图 → visual 索引找相似（mock provider）
    2. search_by_bpm：按 BPM 范围找音频
    3. advanced_search：多条件 AND 过滤（character/mood/duration/bpm）
    4. 新增 API 端点 /api/similar_image / /api/by_bpm / /api/advanced
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# 以图搜图
# ---------------------------------------------------------------------- #

def test_search_similar_image_returns_hits(repo, sample_files, monkeypatch):
    """查询图 → visual 索引有数据 → 返回相似素材。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache, get_vector_store
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.server.query_service import QueryService
    from apps.topos_vault.providers import base as prov_base

    clear_store_cache()
    # 索引一张图片（stub 写入 visual 向量）
    orch = Orchestrator(repo)
    orch.index_file(str(sample_files["image"]))

    svc = QueryService(repo)
    # mock visual_embed provider 返回固定向量（与存的一致 → 高相似）
    class FakeEmbedProvider:
        provider_id = "fake"
        capability = prov_base.Capability.VISUAL_EMBED
        def is_available(self): return True
        def embed_image(self, path):
            from apps.topos_vault.providers.contracts import EmbeddingResult
            return EmbeddingResult(vector=[1.0] * 768, dim=768, model="fake")
    import apps.topos_vault.server.query_service as qs_mod
    monkeypatch.setattr(prov_base, "get_provider", lambda cap: FakeEmbedProvider())

    hits = svc.search_similar_image(str(sample_files["image"]), limit=5)
    # 应至少返回 1 条（索引的图片自己）
    assert len(hits) >= 1


def test_search_similar_image_empty_when_no_visual_index(repo, sample_files, monkeypatch):
    """visual 索引空 → 返回空。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.server.query_service import QueryService
    from apps.topos_vault.providers import base as prov_base

    clear_store_cache()
    class FakeEmbedProvider:
        provider_id = "fake"
        capability = prov_base.Capability.VISUAL_EMBED
        def is_available(self): return True
        def embed_image(self, path):
            from apps.topos_vault.providers.contracts import EmbeddingResult
            return EmbeddingResult(vector=[1.0] * 768, dim=768, model="fake")
    monkeypatch.setattr(prov_base, "get_provider", lambda cap: FakeEmbedProvider())

    svc = QueryService(repo)
    hits = svc.search_similar_image(str(sample_files["image"]), limit=5)
    assert hits == []


# ---------------------------------------------------------------------- #
# BPM 匹配
# ---------------------------------------------------------------------- #

def test_search_by_bpm_returns_nearby(repo):
    """按 BPM 找节奏相近的音频。"""
    from apps.topos_vault.db.repository import AudioMeta, MediaSource
    from apps.topos_vault.server.query_service import QueryService

    # 造 3 个不同 BPM 的音频
    for i, bpm in enumerate([90, 120, 140]):
        fp = f"fp_bpm_{i}"
        repo.upsert_media_source(MediaSource(
            fingerprint=fp, media_type="audio", path_original=f"/tmp/a{i}.mp3",
            path_status="valid",
        ))
        repo.upsert_audio_meta(AudioMeta(fingerprint=fp, bpm=bpm))

    svc = QueryService(repo)
    # 找 120 附近 ±5 → 应只命中 120
    hits = svc.search_by_bpm(120, tolerance=5)
    bpms = [h.source.fingerprint for h in hits]
    assert "fp_bpm_1" in bpms  # 120
    assert "fp_bpm_0" not in bpms  # 90 不在范围
    assert "fp_bpm_2" not in bpms  # 140 不在范围


def test_search_by_bpm_sorted_by_closeness(repo):
    """BPM 结果按差距升序（越接近越靠前）。"""
    from apps.topos_vault.db.repository import AudioMeta, MediaSource
    from apps.topos_vault.server.query_service import QueryService

    for i, bpm in enumerate([118, 122, 125]):
        fp = f"fp_sort_{i}"
        repo.upsert_media_source(MediaSource(
            fingerprint=fp, media_type="audio", path_original=f"/tmp/s{i}.mp3",
            path_status="valid",
        ))
        repo.upsert_audio_meta(AudioMeta(fingerprint=fp, bpm=bpm))

    svc = QueryService(repo)
    hits = svc.search_by_bpm(120, tolerance=10)
    # 118(diff2) 和 122(diff2) 应排在 125(diff5) 前面
    assert len(hits) == 3
    # relevance 越高（差距越小）越靠前
    assert hits[0].relevance >= hits[-1].relevance


def test_search_by_bpm_empty_when_none_match(repo):
    """没有 BPM 匹配 → 返回空。"""
    from apps.topos_vault.server.query_service import QueryService
    svc = QueryService(repo)
    hits = svc.search_by_bpm(100, tolerance=1)
    assert hits == []


# ---------------------------------------------------------------------- #
# 组合高级检索
# ---------------------------------------------------------------------- #

def test_advanced_search_filters_by_duration(repo):
    """按时长过滤。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.server.query_service import QueryService

    for i, dur in enumerate([1.0, 5.0, 30.0]):
        fp = f"fp_dur_{i}"
        repo.upsert_media_source(MediaSource(
            fingerprint=fp, media_type="video", path_original=f"/tmp/v{i}.mp4",
            path_status="valid", duration_sec=dur,
        ))

    svc = QueryService(repo)
    hits = svc.advanced_search(duration_range=(3.0, 10.0))
    durations = [h.source.duration_sec for h in hits]
    assert all(3.0 <= d <= 10.0 for d in durations)
    assert len(hits) == 1  # 只有 5.0 在范围内


def test_advanced_search_filters_by_character(repo):
    """按角色名过滤（需 face_track 有该角色）。"""
    from apps.topos_vault.db.repository import Clip, FaceTrack, MediaSource
    from apps.topos_vault.server.query_service import QueryService

    # 视频带张三角色
    fp1 = "fp_char_a"
    repo.upsert_media_source(MediaSource(
        fingerprint=fp1, media_type="video", path_original="/tmp/c1.mp4",
        path_status="valid",
    ))
    repo.insert_clip(Clip(clip_id="", fingerprint=fp1, clip_index=0,
                          start_sec=0, end_sec=5, duration_sec=5))
    clip1 = repo.list_clips(fp1)[0]
    repo.insert_face_tracks([FaceTrack(face_id=None, clip_id=clip1.clip_id,
                                       character="张三")])

    # 视频无角色
    fp2 = "fp_char_b"
    repo.upsert_media_source(MediaSource(
        fingerprint=fp2, media_type="video", path_original="/tmp/c2.mp4",
        path_status="valid",
    ))

    svc = QueryService(repo)
    hits = svc.advanced_search(media_type="video", character="张三")
    fps = {h.source.fingerprint for h in hits}
    assert fp1 in fps
    assert fp2 not in fps


def test_advanced_search_combines_keyword_and_filter(repo, sample_files):
    """关键词 + 过滤条件组合。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.server.query_service import QueryService

    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(sample_files["image"]))  # stub 写 caption

    svc = QueryService(repo)
    # 关键词 stub + 时长过滤（图片无 duration，应不命中时长过滤）
    hits = svc.advanced_search(query="stub", media_type="image",
                               duration_range=(100, 200))
    assert len(hits) == 0  # 图片 duration=None 不在 100-200

    # 只关键词
    hits2 = svc.advanced_search(query="stub", media_type="image")
    assert len(hits2) >= 1


def test_advanced_search_no_filters_returns_all(repo):
    """无任何过滤 → 返回所有素材。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.server.query_service import QueryService

    for i in range(3):
        repo.upsert_media_source(MediaSource(
            fingerprint=f"fp_all_{i}", media_type="image",
            path_original=f"/tmp/x{i}.jpg", path_status="valid",
        ))

    svc = QueryService(repo)
    hits = svc.advanced_search(media_type="image", limit=10)
    assert len(hits) == 3


# ---------------------------------------------------------------------- #
# API 端点
# ---------------------------------------------------------------------- #

@pytest.fixture
def flask_client(repo, initialized_db):
    from apps.topos_vault.server.app import create_app
    app = create_app(repo)
    app.testing = True
    return app.test_client()


def test_api_similar_image_endpoint(flask_client):
    """/api/similar_image 端点返回正确格式。"""
    resp = flask_client.get("/api/similar_image?path=/nonexistent.jpg")
    data = resp.get_json()
    assert resp.status_code == 200
    assert "files" in data
    assert data["query_path"] == "/nonexistent.jpg"


def test_api_similar_image_missing_path(flask_client):
    """无 path 参数 → 返回 error。"""
    resp = flask_client.get("/api/similar_image")
    data = resp.get_json()
    assert "error" in data


def test_api_by_bpm_endpoint(flask_client, repo):
    """/api/by_bpm 端点返回 BPM 匹配。"""
    from apps.topos_vault.db.repository import AudioMeta, MediaSource
    repo.upsert_media_source(MediaSource(
        fingerprint="fp_bpm_api", media_type="audio",
        path_original="/tmp/b.mp3", path_status="valid",
    ))
    repo.upsert_audio_meta(AudioMeta(fingerprint="fp_bpm_api", bpm=120))

    resp = flask_client.get("/api/by_bpm?bpm=121&tolerance=5")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["bpm"] == 121
    assert data["total"] >= 1


def test_api_by_bpm_invalid_bpm(flask_client):
    """非法 BPM → 返回 error。"""
    resp = flask_client.get("/api/by_bpm?bpm=abc")
    data = resp.get_json()
    assert "error" in data


def test_api_advanced_endpoint(flask_client, repo):
    """/api/advanced 组合检索端点。"""
    from apps.topos_vault.db.repository import MediaSource
    repo.upsert_media_source(MediaSource(
        fingerprint="fp_adv_api", media_type="video",
        path_original="/tmp/a.mp4", path_status="valid", duration_sec=10,
    ))

    resp = flask_client.get("/api/advanced?type=video&dur_min=5&dur_max=15")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["total"] >= 1
