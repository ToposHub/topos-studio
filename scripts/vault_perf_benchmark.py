#!/usr/bin/env python3
"""vault 向量检索性能基准（P2-5）。

对比 flat vs ivf 索引在不同规模下的搜索耗时，帮用户决定何时切 IVF。

用法：
    python3 scripts/vault_perf_benchmark.py
    python3 scripts/vault_perf_benchmark.py --n 5000 --dim 768
    python3 scripts/vault_perf_benchmark.py --repeats 50

需要 faiss-cpu（IVF 是 faiss 特性）。numpy 后端只能跑 flat。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))


def benchmark(n: int, dim: int, top_k: int, repeats: int) -> None:
    import numpy as np
    from apps.topos_vault.db.faiss_store import VectorStore, is_faiss_available

    np.random.seed(0)
    vectors = np.random.randn(n, dim).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)

    print(f"\n规模：{n} 向量 × {dim} 维，top_k={top_k}，重复 {repeats} 次")
    print(f"faiss 后端：{'可用' if is_faiss_available() else '不可用（numpy 暴力）'}")
    print("-" * 50)

    os.environ["TOPOS_VAULT_DIR"] = tempfile.mkdtemp(prefix="vault_bench_")

    for index_type in ("flat", "ivf", "auto"):
        store = VectorStore(f"bench_{index_type}", dim, index_type=index_type)
        t_add0 = time.perf_counter()
        for i, v in enumerate(vectors):
            store.add(f"id_{i}", v.tolist())
        t_add = time.perf_counter() - t_add0

        query = vectors[n // 2].tolist()
        # warmup
        for _ in range(3):
            store.search(query, top_k=top_k)

        t0 = time.perf_counter()
        for _ in range(repeats):
            results = store.search(query, top_k=top_k)
        t_search = (time.perf_counter() - t0) / repeats * 1000  # ms

        top1 = results[0].id if results else "?"
        print(f"  {index_type:>4s}  添加 {t_add*1000:7.0f}ms  "
              f"搜索 {t_search:6.2f}ms  top1={top1} (score={results[0].score:.3f})"
              if results else f"  {index_type:>4s}  搜索 {t_search:.2f}ms  无结果")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vault 向量检索性能基准")
    parser.add_argument("--n", type=int, default=2000, help="向量数（默认 2000）")
    parser.add_argument("--dim", type=int, default=768, help="维度（默认 768，SigLIP2）")
    parser.add_argument("--top-k", type=int, default=10, help="top_k（默认 10）")
    parser.add_argument("--repeats", type=int, default=30, help="每模式重复次数")
    args = parser.parse_args(argv)

    try:
        import faiss  # noqa: F401
    except ImportError:
        print("⚠️  faiss 未装，只能测 numpy 暴力（flat）后端")
        print("   装 faiss：pip install faiss-cpu")

    benchmark(args.n, args.dim, args.top_k, args.repeats)
    print()
    print("提示：规模 ≥ IVF_THRESHOLD（5000）时 auto 模式自动切 IVF。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
