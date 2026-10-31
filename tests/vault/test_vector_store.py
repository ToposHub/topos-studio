"""向量存储层测试（numpy 后端，faiss 装了也会跑 faiss 路径）。"""

from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前清空全局 store 缓存（隔离）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    clear_store_cache()
    yield
    clear_store_cache()


# ---------------------------------------------------------------------- #
# 基础 add / search
# ---------------------------------------------------------------------- #

def test_add_and_size():
    """添加向量，size 正确。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=8)
    assert store.size == 0
    store.add("a", [1, 0, 0, 0, 0, 0, 0, 0])
    store.add("b", [0, 1, 0, 0, 0, 0, 0, 0])
    assert store.size == 2


def test_add_normalizes_vector():
    """存储的向量被 L2 归一化（这样内积 = 余弦相似度）。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=3)
    store.add("x", [3, 4, 0])  # norm = 5
    vec = store.get_vector("x")
    norm = np.linalg.norm(vec)
    assert abs(norm - 1.0) < 1e-5, f"归一化后 norm 应为 1，得到 {norm}"


def test_search_returns_similar():
    """搜索返回最相似的。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=4)
    store.add("apple", [1, 0, 0, 0])
    store.add("banana", [0, 1, 0, 0])
    store.add("cherry", [0, 0, 1, 0])

    # 查 [0.9, 0.1, 0, 0] → apple 最相似
    results = store.search([0.9, 0.1, 0, 0], top_k=2)
    assert len(results) == 2
    assert results[0].id == "apple"
    assert results[0].score > results[1].score


def test_search_top_k():
    """top_k 限制返回数量。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=4)
    for i in range(10):
        vec = [0] * 4
        vec[i % 4] = 1
        store.add(f"item_{i}", vec)

    results = store.search([1, 0, 0, 0], top_k=3)
    assert len(results) == 3


def test_search_empty_store():
    """空索引搜索返回空。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=8)
    results = store.search([1, 0, 0, 0, 0, 0, 0, 0])
    assert results == []


def test_search_candidate_filter():
    """candidate_ids 限定搜索范围（粗筛后加速）。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=4)
    store.add("a", [1, 0, 0, 0])
    store.add("b", [0.99, 0.01, 0, 0])  # 和 a 很像
    store.add("c", [0, 0, 1, 0])

    # 不限范围：b 和 a 都很像
    all_results = store.search([1, 0, 0, 0], top_k=3)
    all_ids = {r.id for r in all_results}
    assert "a" in all_ids and "b" in all_ids

    # 限定只搜 c：只返回 c
    filtered = store.search([1, 0, 0, 0], top_k=3, candidate_ids=["c"])
    assert len(filtered) == 1
    assert filtered[0].id == "c"


def test_candidate_ids_nonexistent():
    """candidate_ids 全是不存在的 id → 空结果。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=4)
    store.add("a", [1, 0, 0, 0])
    results = store.search([1, 0, 0, 0], candidate_ids=["nonexistent"])
    assert results == []


# ---------------------------------------------------------------------- #
# 余弦相似度正确性
# ---------------------------------------------------------------------- #

def test_cosine_similarity_orthogonal():
    """正交向量相似度接近 0。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=3)
    store.add("x", [1, 0, 0])
    results = store.search([0, 1, 0])
    assert results[0].score < 0.01  # 正交 → 余弦 ≈ 0


def test_cosine_similarity_identical():
    """相同方向相似度接近 1。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=3)
    store.add("x", [1, 2, 3])
    results = store.search([2, 4, 6])  # 同方向
    assert results[0].score > 0.999


def test_cosine_similarity_opposite():
    """相反方向相似度接近 -1。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=3)
    store.add("x", [1, 0, 0])
    results = store.search([-1, 0, 0])
    assert results[0].score < -0.999


# ---------------------------------------------------------------------- #
# 更新 / 删除
# ---------------------------------------------------------------------- #

def test_update_existing_id():
    """同 id 重复 add → 更新向量。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=3)
    store.add("x", [1, 0, 0])
    store.add("x", [0, 1, 0])  # 更新

    assert store.size == 1  # 没有重复
    vec = store.get_vector("x")
    # 新向量
    assert abs(vec[1] - 1.0) < 1e-5


def test_has():
    """has 检查。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=3)
    store.add("a", [1, 0, 0])
    assert store.has("a")
    assert not store.has("b")


def test_get_vector_nonexistent():
    """不存在的 id 返回 None。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=3)
    assert store.get_vector("nope") is None


# ---------------------------------------------------------------------- #
# 持久化
# ---------------------------------------------------------------------- #

def test_save_and_load(vault_dir):
    """保存后重新加载，数据一致。"""
    from apps.topos_vault.db.faiss_store import VectorStore, clear_store_cache
    clear_store_cache()

    store = VectorStore("persist_test", dim=4)
    store.add("a", [1, 0, 0, 0])
    store.add("b", [0, 1, 0, 0])
    store.add("c", [0, 0, 1, 0])
    store.save()

    # 新实例加载
    store2 = VectorStore("persist_test", dim=4)
    assert store2.load()
    assert store2.size == 3
    assert store2.has("a")
    assert store2.has("b")
    assert store2.has("c")

    # 搜索仍正常
    results = store2.search([1, 0, 0, 0], top_k=1)
    assert results[0].id == "a"


def test_load_nonexistent():
    """加载不存在的索引返回 False。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("nonexistent_index", dim=8)
    assert not store.load()


def test_factory_get_with_default_dim():
    """工厂用 DEFAULT_DIMS 创建已知索引。"""
    from apps.topos_vault.db.faiss_store import get_vector_store
    store = get_vector_store("transcript")
    assert store.dim == 1024
    assert store.name == "transcript"


def test_factory_unknown_name_requires_dim():
    """未知索引名必须传 dim。"""
    from apps.topos_vault.db.faiss_store import get_vector_store
    with pytest.raises(ValueError, match="未知索引名"):
        get_vector_store("totally_unknown")


def test_factory_caches():
    """工厂缓存实例（同 name 返回同对象）。"""
    from apps.topos_vault.db.faiss_store import get_vector_store
    s1 = get_vector_store("transcript")
    s2 = get_vector_store("transcript")
    assert s1 is s2


def test_factory_clear_cache(vault_dir):
    """清缓存后重新获取是新实例。"""
    from apps.topos_vault.db.faiss_store import get_vector_store, clear_store_cache
    s1 = get_vector_store("transcript")
    clear_store_cache()
    s2 = get_vector_store("transcript")
    assert s1 is not s2


# ---------------------------------------------------------------------- #
# 批量 + 大小
# ---------------------------------------------------------------------- #

def test_add_batch():
    """批量添加。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=4)
    ids = [f"item_{i}" for i in range(5)]
    vectors = np.eye(4, 4).tolist() + [[1, 1, 1, 1]]  # 5 个
    # 修正：eye(4,4) 只有 4 行
    vectors = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1], [1, 1, 1, 1]]
    store.add_batch(ids[:5], vectors)
    assert store.size == 5


def test_dim_mismatch_raises():
    """维度不匹配报错。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    store = VectorStore("test", dim=4)
    with pytest.raises(ValueError, match="维度不匹配"):
        store.add("x", [1, 0, 0])  # 3 维，应该是 4


# ---------------------------------------------------------------------- #
# 后端检测
# ---------------------------------------------------------------------- #

def test_backend_property():
    """backend 属性正确反映后端。"""
    from apps.topos_vault.db.faiss_store import VectorStore, is_faiss_available
    store = VectorStore("test", dim=4)
    if is_faiss_available():
        assert store.backend == "faiss"
    else:
        assert store.backend == "numpy"
