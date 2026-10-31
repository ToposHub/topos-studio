"""查询服务向量检索测试。

验证 SQL + 向量语义检索的合并行为。
关键场景：素材 caption 没写"微笑"，但向量库里存了它的语义向量，
搜索"微笑"能通过向量命中（这是语义检索的价值）。
"""

from __future__ import annotations

import pytest


def _seed_with_vectors(repo):
    """灌入带向量的测试数据。

    关键：某些素材的 caption/tags 里不含查询词，但向量库里存了
    语义相近的向量——验证向量检索能命中它们。
    """
    from apps.topos_vault.db.repository import MediaSource, ImageMeta
    from apps.topos_vault.db.faiss_store import get_vector_store, clear_store_cache

    clear_store_cache()

    # 图片 1：caption 里有"微笑" → SQL 能搜到
    repo.upsert_media_source(MediaSource(
        fingerprint="fp_smile1", media_type="image",
        path_original="/p/smile1.jpg", path_current="/p/smile1.jpg",
        title="smile1.jpg",
    ))
    repo.upsert_image_meta(ImageMeta(
        fingerprint="fp_smile1", caption="一个女孩在微笑", tags=["微笑"],
    ))

    # 图片 2：caption 没写"微笑"，但画面是微笑的 → 只有向量能搜到
    # 文件名叫 photo_001，caption 是"户外人像"
    repo.upsert_media_source(MediaSource(
        fingerprint="fp_hidden_smile", media_type="image",
        path_original="/p/photo_001.jpg", path_current="/p/photo_001.jpg",
        title="photo_001.jpg",
    ))
    repo.upsert_image_meta(ImageMeta(
        fingerprint="fp_hidden_smile", caption="户外人像", tags=["人像"],
    ))

    # 图片 3：完全无关（夜景）
    repo.upsert_media_source(MediaSource(
        fingerprint="fp_night", media_type="image",
        path_original="/p/night.jpg", path_current="/p/night.jpg",
        title="night.jpg",
    ))
    repo.upsert_image_meta(ImageMeta(
        fingerprint="fp_night", caption="城市夜景", tags=["夜景"],
    ))

    # 往 visual 向量库里灌数据
    # 用确定性的伪向量模拟："微笑"相关的向量在某方向聚集
    store = get_vector_store("visual", dim=8)

    # smile1 和 hidden_smile 的向量都接近"微笑方向"
    smile_direction = [0.9, 0.1, 0.05, 0.05, 0.0, 0.0, 0.0, 0.0]
    store.add("fp_smile1", smile_direction)
    store.add("fp_hidden_smile", [0.85, 0.15, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0])
    # night 的向量在完全不同方向
    store.add("fp_night", [0.0, 0.0, 0.0, 0.0, 0.9, 0.1, 0.0, 0.0])

    return smile_direction


def test_vector_search_finds_semantically_related(repo):
    """向量检索能命中 caption 没写关键词但语义相近的素材。

    这是语义检索的核心价值：搜"微笑"能找到画面是微笑但
    caption 只写"户外人像"的素材。
    """
    from apps.topos_vault.server.query_service import QueryService
    smile_direction = _seed_with_vectors(repo)
    svc = QueryService(repo)

    # 让 stub provider 返回"微笑方向"的向量
    # （stub 默认按 hash 生成，这里 monkeypatch 让它返回固定向量）
    from apps.topos_vault.providers import get_provider, Capability
    provider = get_provider(Capability.TRANSCRIPT_EMBED)
    original_embed = provider.embed_text
    provider.embed_text = lambda text: type(
        "R", (), {"vector": smile_direction, "dim": 8, "model": "test"}
    )()

    try:
        hits = svc.search("微笑", use_vector=True)
        fps = {h.source.fingerprint for h in hits}
        # smile1 通过 SQL 命中
        assert "fp_smile1" in fps
        # hidden_smile 通过向量命中（SQL 搜不到它）
        assert "fp_hidden_smile" in fps, "向量检索应命中语义相关但 caption 没写关键词的素材"
    finally:
        provider.embed_text = original_embed


def test_vector_search_disabled_returns_only_sql(repo):
    """use_vector=False 时只走 SQL，不查向量库。"""
    from apps.topos_vault.server.query_service import QueryService
    _seed_with_vectors(repo)
    svc = QueryService(repo)

    hits = svc.search("微笑", use_vector=False)
    fps = {h.source.fingerprint for h in hits}
    assert "fp_smile1" in fps
    # hidden_smile 的 caption 是"户外人像"，SQL 搜不到
    assert "fp_hidden_smile" not in fps


def test_vector_search_excludes_irrelevant(repo):
    """向量检索不会命中语义无关的素材。"""
    from apps.topos_vault.server.query_service import QueryService
    smile_direction = _seed_with_vectors(repo)
    svc = QueryService(repo)

    from apps.topos_vault.providers import get_provider, Capability
    provider = get_provider(Capability.TRANSCRIPT_EMBED)
    original = provider.embed_text
    provider.embed_text = lambda text: type(
        "R", (), {"vector": smile_direction, "dim": 8, "model": "test"}
    )()

    try:
        hits = svc.search("微笑", use_vector=True)
        fps = {h.source.fingerprint for h in hits}
        # night 的向量方向完全不同，不应命中
        assert "fp_night" not in fps
    finally:
        provider.embed_text = original


def test_vector_search_degrades_when_store_empty(repo):
    """向量库为空时，退化为纯 SQL，不报错。"""
    from apps.topos_vault.server.query_service import QueryService
    from apps.topos_vault.db.faiss_store import clear_store_cache
    clear_store_cache()

    # 只灌 SQL 数据，不灌向量
    from apps.topos_vault.db.repository import MediaSource, ImageMeta
    repo.upsert_media_source(MediaSource(
        fingerprint="fp1", media_type="image",
        path_original="/p/a.jpg", path_current="/p/a.jpg",
    ))
    repo.upsert_image_meta(ImageMeta(fingerprint="fp1", caption="微笑", tags=["微笑"]))

    svc = QueryService(repo)
    hits = svc.search("微笑", use_vector=True)
    # SQL 仍能搜到
    assert any(h.source.fingerprint == "fp1" for h in hits)


def test_vector_search_combines_sql_and_vector(repo):
    """SQL 和向量结果合并，去重。"""
    from apps.topos_vault.server.query_service import QueryService
    smile_direction = _seed_with_vectors(repo)
    svc = QueryService(repo)

    from apps.topos_vault.providers import get_provider, Capability
    provider = get_provider(Capability.TRANSCRIPT_EMBED)
    original = provider.embed_text
    provider.embed_text = lambda text: type(
        "R", (), {"vector": smile_direction, "dim": 8, "model": "test"}
    )()

    try:
        hits = svc.search("微笑", use_vector=True)
        # smile1 同时被 SQL 和向量命中，应只出现一次（去重）
        fps = [h.source.fingerprint for h in hits]
        assert fps.count("fp_smile1") == 1, "SQL 和向量重复命中应去重"
    finally:
        provider.embed_text = original


def test_vector_relevance_is_score(repo):
    """向量命中的 relevance 来自余弦相似度 score。"""
    from apps.topos_vault.server.query_service import QueryService
    smile_direction = _seed_with_vectors(repo)
    svc = QueryService(repo)

    from apps.topos_vault.providers import get_provider, Capability
    provider = get_provider(Capability.TRANSCRIPT_EMBED)
    original = provider.embed_text
    provider.embed_text = lambda text: type(
        "R", (), {"vector": smile_direction, "dim": 8, "model": "test"}
    )()

    try:
        hits = svc.search("微笑", use_vector=True)
        # hidden_smile 是纯向量命中，relevance 应 > 0
        hidden = next(h for h in hits if h.source.fingerprint == "fp_hidden_smile")
        assert hidden.relevance > 0.3
    finally:
        provider.embed_text = original
