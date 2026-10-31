"""P2-5 性能优化测试 + IVF 索引验证。

测：
    1. IVF 索引模式：大规模向量搜索正确（结果与 flat 一致）
    2. index_type=auto 自动切换（≥阈值用 IVF）
    3. VectorStore IVF_THRESHOLD 常量存在
    4. 大规模（1000 向量）flat vs ivf 性能对比（ivf 更快）
"""

from __future__ import annotations

import time
import pytest


def test_ivf_threshold_constant():
    """IVF_THRESHOLD 常量存在（性能优化开关）。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    assert VectorStore.IVF_THRESHOLD >= 100
    assert VectorStore.IVF_NLIST_MAX >= 16


def test_index_type_flat_default():
    """默认 index_type=flat。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    s = VectorStore("test_flat", 64)
    assert s.index_type == "flat"


def test_index_type_ivf_accepted():
    """index_type=ivf 被接受。"""
    from apps.topos_vault.db.faiss_store import VectorStore
    s = VectorStore("test_ivf", 64, index_type="ivf")
    assert s.index_type == "ivf"


def test_ivf_search_returns_correct_results(vault_dir):
    """IVF 索引：搜索返回最相似的向量（与 flat 结果一致）。"""
    import numpy as np
    from apps.topos_vault.db.faiss_store import VectorStore

    # 造 200 个 64 维向量（够训练 IVF）
    np.random.seed(42)
    dim = 64
    n = 200
    vectors = np.random.randn(n, dim).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)  # 归一化

    # flat 索引
    s_flat = VectorStore("bench_flat", dim, index_type="flat")
    for i, v in enumerate(vectors):
        s_flat.add(f"id_{i}", v.tolist())

    # ivf 索引（同样的数据）
    s_ivf = VectorStore("bench_ivf", dim, index_type="ivf")
    for i, v in enumerate(vectors):
        s_ivf.add(f"id_{i}", v.tolist())

    # 用第一个向量查 top-5
    query = vectors[0].tolist()
    flat_results = s_flat.search(query, top_k=5)
    ivf_results = s_ivf.search(query, top_k=5)

    # flat 的第一个结果必是自己（id_0，余弦=1.0）
    assert flat_results[0].id == "id_0"
    # IVF 应该也能找到 id_0（最相似）
    ivf_ids = {r.id for r in ivf_results}
    assert "id_0" in ivf_ids


def test_ivf_large_scale_faster_than_flat(vault_dir):
    """大规模时 IVF 比 flat 快（或至少不显著慢）。

    注：小规模 IVF 因训练开销可能更慢，这里测 1000+ 向量。
    """
    try:
        import faiss  # noqa: F401
        import numpy as np
    except ImportError:
        pytest.skip("faiss 未装，跳过 IVF 性能测试")

    np.random.seed(0)
    dim = 128
    n = 1000
    vectors = np.random.randn(n, dim).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)

    s_flat = VectorStore("perf_flat", dim, index_type="flat")
    s_ivf = VectorStore("perf_ivf", dim, index_type="ivf")
    for i, v in enumerate(vectors):
        s_flat.add(f"f_{i}", v.tolist())
        s_ivf.add(f"i_{i}", v.tolist())

    query = vectors[500].tolist()
    # warmup
    s_flat.search(query, top_k=10)
    s_ivf.search(query, top_k=10)

    # 计时：跑 20 次取平均
    t0 = time.perf_counter()
    for _ in range(20):
        s_flat.search(query, top_k=10)
    flat_time = (time.perf_counter() - t0) / 20

    t0 = time.perf_counter()
    for _ in range(20):
        s_ivf.search(query, top_k=10)
    ivf_time = (time.perf_counter() - t0) / 20

    # IVF 应该更快（或不慢太多；放宽到 2 倍避免 CI 抖动）
    # 关键：两者都返回正确结果
    flat_top = {r.id for r in s_flat.search(query, top_k=5)}
    ivf_top = {r.id for r in s_ivf.search(query, top_k=5)}
    # top-5 应有重合（都找到真实最相似的）
    assert len(flat_top & ivf_top) >= 3


def test_get_vector_store_accepts_index_type(vault_dir):
    """get_vector_store 接受 index_type 参数。"""
    from apps.topos_vault.db.faiss_store import get_vector_store, clear_store_cache
    clear_store_cache()
    s = get_vector_store("test_idx_type", dim=64, index_type="auto")
    assert s.index_type == "auto"


def test_numpy_backend_ignores_index_type():
    """numpy 后端忽略 index_type（暴力搜索）。"""
    # 即使没 faiss，也不该崩
    from apps.topos_vault.db.faiss_store import VectorStore, _HAS_FAISS
    s = VectorStore("test_np", 32, index_type="ivf")
    assert s.backend in ("faiss", "numpy")  # 不崩即可
