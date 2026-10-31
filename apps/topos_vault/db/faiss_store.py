"""向量存储抽象 + 双后端实现（faiss / numpy）。

VectorStore 是统一接口，底层根据 faiss 是否安装自动选择后端：
    - faiss 后端：用 faiss.IndexFlatIP（内积=余弦，向量需 L2 归一化），支持持久化 .faiss
    - numpy 后端：暴力余弦相似度，持久化用 .npy + .json

两种后端行为完全一致：add/search/save/load/persist，上层无感知。

向量约定：
    - 存储前必须 L2 归一化（这样内积 = 余弦相似度）
    - search 返回的 score 就是余弦相似度（-1 到 1，越大越相似）
    - 每个 vector 关联一个 id（字符串，通常是业务表 PK 如 clip_id / fingerprint）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from apps.topos_vault.paths import get_faiss_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 检测 faiss 是否可用
# ---------------------------------------------------------------------- #

try:
    import faiss  # type: ignore
    _HAS_FAISS = True
except ImportError:
    _HAS_FAISS = False


def is_faiss_available() -> bool:
    """faiss-cpu 是否已安装。"""
    return _HAS_FAISS


# ---------------------------------------------------------------------- #
# 搜索结果
# ---------------------------------------------------------------------- #

@dataclass
class SearchResult:
    """单条向量搜索结果。"""
    id: str              # 业务表 PK（clip_id / fingerprint / ...）
    score: float         # 余弦相似度（-1 到 1，越大越相似）


# ---------------------------------------------------------------------- #
# VectorStore 抽象
# ---------------------------------------------------------------------- #

class VectorStore:
    """向量存储（统一接口，底层 faiss 或 numpy）。

    一个 VectorStore 实例对应一个索引（如 "transcript"）。
    所有向量维度必须一致（构造时指定）。

    索引类型（仅 faiss 后端，P2-5 性能优化）：
        - "flat"（默认）：IndexFlatIP，精确，小规模（<IVF_THRESHOLD）快
        - "ivf"：IndexIVFFlat，大规模（≥IVF_THRESHOLD）更快，需训练（自动）
        - "auto"：按规模自动选（<阈值 flat，≥阈值 ivf）

    numpy 后端忽略 index_type（暴力搜索）。
    """

    # IVF 切换阈值：向量数 ≥ 此值时，auto 模式用 IVF
    IVF_THRESHOLD = 5000
    # IVF nlist（聚类中心数），经验值：sqrt(N) 量级
    IVF_NLIST_MAX = 256

    def __init__(self, name: str, dim: int, *, index_type: str = "flat"):
        """创建一个向量存储。

        Args:
            name: 索引名（如 "transcript" / "visual"），用于持久化文件名
            dim: 向量维度（所有 add 的向量必须是这个维度）
            index_type: "flat" / "ivf" / "auto"（仅 faiss 后端生效）。
        """
        if dim <= 0:
            raise ValueError(f"dim 必须 > 0，得到 {dim}")

        self.name = name
        self.dim = dim
        self.index_type = index_type

        # id 映射：内部连续 index ↔ 业务字符串 id
        self._ids: list[str] = []
        self._id_to_idx: dict[str, int] = {}

        # 向量存储（懒初始化，第一次 add 时创建）
        self._vectors: np.ndarray | None = None  # shape: (N, dim), float32, L2 归一化
        self._faiss_index = None                  # faiss.Index（faiss 后端用）

        self._backend = "faiss" if _HAS_FAISS else "numpy"

    @property
    def backend(self) -> str:
        """当前后端："faiss" 或 "numpy"。"""
        return self._backend

    @property
    def size(self) -> int:
        """已存储的向量数。"""
        return len(self._ids)

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def add(self, id: str, vector: Sequence[float]) -> int:
        """添加一个向量。

        如果 id 已存在，更新其向量（去重）。
        向量会被 L2 归一化（存储归一化后的版本）。

        Returns:
            内部 index（faiss_id）。
        """
        vec = self._normalize(np.asarray(vector, dtype=np.float32))
        if vec.shape != (self.dim,):
            raise ValueError(
                f"向量维度不匹配：期望 {self.dim}，得到 {vec.shape[0]}"
            )

        # 已存在 → 更新
        if id in self._id_to_idx:
            idx = self._id_to_idx[id]
            self._vectors[idx] = vec
            if self._faiss_index is not None:
                self._faiss_index.reconstruct(idx, self._vectors[idx])
                # faiss 的 reconstruct 是只读的，更新要用 remove+add
                # 简化处理：faiss 后端更新走重建
                self._rebuild_faiss()
            return idx

        # 新增
        idx = len(self._ids)
        self._ids.append(id)
        self._id_to_idx[id] = idx

        if self._vectors is None:
            self._vectors = vec.reshape(1, -1).copy()
        else:
            self._vectors = np.vstack([self._vectors, vec.reshape(1, -1)])

        if self._backend == "faiss" and self._faiss_index is not None:
            self._faiss_index.add(vec.reshape(1, -1))

        return idx

    def add_batch(self, ids: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        """批量添加。"""
        if len(ids) != len(vectors):
            raise ValueError("ids 和 vectors 长度不一致")
        for id_, vec in zip(ids, vectors):
            self.add(id_, vec)

    def remove(self, id: str) -> bool:
        """删除一个向量。Returns: 是否删除成功。"""
        if id not in self._id_to_idx:
            return False
        # numpy 后端：标记删除（重建索引时清理）
        # 简化实现：不真正删除，标记为 0 向量（搜索时 score 极低）
        # 完整实现需要 rebuild，但删除不频繁，留到 save/load 时压缩
        idx = self._id_to_idx[id]
        if self._vectors is not None:
            self._vectors[idx] = 0  # 0 向量与任何归一化向量的内积都是 0
        if self._faiss_index is not None:
            self._rebuild_faiss()
        return True

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #

    def search(
        self, query: Sequence[float], top_k: int = 10,
        candidate_ids: Sequence[str] | None = None,
    ) -> list[SearchResult]:
        """搜索最相似的向量。

        Args:
            query: 查询向量（会被 L2 归一化）。
            top_k: 返回前 K 个。
            candidate_ids: 限定搜索范围（粗筛后的小集合，加速）。
                None = 搜索全部。

        Returns:
            SearchResult 列表（按 score 降序），最多 top_k 个。
        """
        if self._vectors is None or self.size == 0:
            return []

        q = self._normalize(np.asarray(query, dtype=np.float32))

        # 限定候选集
        if candidate_ids is not None:
            candidate_idxs = [
                self._id_to_idx[i] for i in candidate_ids if i in self._id_to_idx
            ]
            if not candidate_idxs:
                return []
            sub_vectors = self._vectors[candidate_idxs]
            scores = sub_vectors @ q  # (N,)
            # 取 top_k
            k = min(top_k, len(candidate_idxs))
            top_indices = np.argpartition(-scores, k - 1)[:k]
            results = [
                SearchResult(
                    id=self._ids[candidate_idxs[i]],
                    score=float(scores[i]),
                )
                for i in top_indices
            ]
            results.sort(key=lambda r: r.score, reverse=True)
            return results

        # 全量搜索
        if self._backend == "faiss" and self._faiss_index is not None:
            scores, indices = self._faiss_index.search(q.reshape(1, -1), min(top_k, self.size))
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx >= 0 and idx < len(self._ids):
                    results.append(SearchResult(id=self._ids[idx], score=float(score)))
            return results

        # numpy 暴力搜索
        scores = self._vectors @ q
        k = min(top_k, self.size)
        top_indices = np.argpartition(-scores, k - 1)[:k]
        results = [
            SearchResult(id=self._ids[i], score=float(scores[i]))
            for i in top_indices
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def get_vector(self, id: str) -> np.ndarray | None:
        """取某个 id 的向量（None = 不存在）。"""
        if id not in self._id_to_idx or self._vectors is None:
            return None
        return self._vectors[self._id_to_idx[id]]

    def has(self, id: str) -> bool:
        """是否包含某 id。"""
        return id in self._id_to_idx

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def save(self) -> Path:
        """保存到磁盘。Returns: 保存的文件路径。"""
        faiss_dir = get_faiss_dir()
        faiss_dir.mkdir(parents=True, exist_ok=True)

        if self._vectors is None:
            # 空索引也要保存 id 映射（表示"已初始化"）
            pass

        base = faiss_dir / self.name

        # 保存 id 映射（两后端共用）
        meta = {
            "name": self.name,
            "dim": self.dim,
            "backend": self._backend,
            "ids": self._ids,
            "size": self.size,
        }
        (base.with_suffix(".meta.json")).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

        if self._backend == "faiss" and self._faiss_index is not None:
            faiss.write_index(self._faiss_index, str(base.with_suffix(".faiss")))
        elif self._vectors is not None:
            np.save(str(base.with_suffix(".npy")), self._vectors)

        return base

    def load(self) -> bool:
        """从磁盘加载。Returns: 是否加载成功。"""
        faiss_dir = get_faiss_dir()
        base = faiss_dir / self.name
        meta_path = base.with_suffix(".meta.json")

        if not meta_path.exists():
            return False

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.name = meta["name"]
        self.dim = meta["dim"]
        self._ids = meta["ids"]
        self._id_to_idx = {id_: i for i, id_ in enumerate(self._ids)}

        # 优先用记录的 backend，但若 faiss 没装则降级到 numpy
        backend = meta.get("backend", "numpy")
        if backend == "faiss" and not _HAS_FAISS:
            logger.info("faiss 未装，降级到 numpy 后端加载 %s", self.name)
            backend = "numpy"

        if backend == "faiss":
            faiss_path = base.with_suffix(".faiss")
            if faiss_path.exists():
                self._faiss_index = faiss.read_index(str(faiss_path))
                # 同步 vectors（numpy 后端需要）
                self._vectors = np.zeros((self.size, self.dim), dtype=np.float32)
                for i in range(self.size):
                    self._faiss_index.reconstruct(i, self._vectors[i])
                self._backend = "faiss"
                return True
            # faiss 文件不在，降级 numpy

        # numpy 后端
        npy_path = base.with_suffix(".npy")
        if npy_path.exists():
            self._vectors = np.load(str(npy_path))
            self._backend = "numpy"
            self._faiss_index = None
            return True

        return False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize(vec: np.ndarray) -> np.ndarray:
        """L2 归一化（让内积 = 余弦相似度）。"""
        norm = np.linalg.norm(vec)
        if norm > 1e-12:
            vec = vec / norm
        return vec

    def _ensure_faiss_index(self) -> None:
        """懒初始化 faiss 索引（faiss 后端用）。"""
        if not _HAS_FAISS or self._faiss_index is not None:
            return
        self._faiss_index = self._build_faiss_index()
        if self._vectors is not None and self._vectors.shape[0] > 0:
            self._faiss_index.add(self._vectors)

    def _build_faiss_index(self):
        """按 index_type + 规模构造 faiss 索引。

        - flat：IndexFlatIP（精确，小规模快）
        - ivf：IndexIVFFlat（大规模快，需训练）
        - auto：< IVF_THRESHOLD 用 flat，≥ 用 ivf
        """
        if not _HAS_FAISS:
            return None

        n = self._vectors.shape[0] if self._vectors is not None else 0
        use_ivf = (self.index_type == "ivf"
                   or (self.index_type == "auto" and n >= self.IVF_THRESHOLD))

        if not use_ivf:
            return faiss.IndexFlatIP(self.dim)

        # IVF：需要训练量化器。向量已 L2 归一化 → 内积 = 余弦
        quantizer = faiss.IndexFlatIP(self.dim)
        nlist = max(1, min(self.IVF_NLIST_MAX, int(n ** 0.5)))
        # IVFFlat 用 METRIC_INNER_PRODUCT（余弦）
        index = faiss.IndexIVFFlat(quantizer, self.dim, nlist, faiss.METRIC_INNER_PRODUCT)
        # 训练 + 设置 nprobe（搜索时探查的聚类数，越多越准越慢）
        if n >= nlist * 2 and self._vectors is not None:
            index.train(self._vectors)
            index.add(self._vectors)
            # nprobe 经验值：nlist / 10，最少 1
            index.nprobe = max(1, min(nlist, nlist // 10))
        return index

    def _rebuild_faiss(self) -> None:
        """重建 faiss 索引（更新/删除后调用）。"""
        if not _HAS_FAISS:
            return
        self._faiss_index = self._build_faiss_index()

    def _sync_to_faiss(self) -> None:
        """add 后同步到 faiss（如果用 faiss 后端）。"""
        if self._backend == "faiss":
            if self._faiss_index is None:
                self._ensure_faiss_index()
            elif self._vectors is not None and self._vectors.shape[0] > 0:
                # 只 add 新的最后一个
                self._faiss_index.add(self._vectors[-1:].reshape(1, -1))


# ---------------------------------------------------------------------- #
# 工厂 + 缓存
# ---------------------------------------------------------------------- #

class VectorStoreFactory:
    """向量存储工厂 + 实例缓存（懒加载）。

    每个索引名对应一个 VectorStore 实例，首次访问时从磁盘加载，
    之后复用。get_faiss_dir() 变化时（如测试切目录）需调 clear_cache()。
    """

    # 已知索引的默认维度（与 provider 产出对齐）
    DEFAULT_DIMS = {
        "transcript": 1024,      # Qwen3-Embedding-0.6B
        "visual": 768,           # SigLIP 2 base-patch16-224（hidden_size=768）
        "face": 512,             # InsightFace buffalo_l
        "audio_semantic": 1024,  # Qwen2.5-Omni（近似，按实际模型调）
    }

    def __init__(self):
        self._stores: dict[str, VectorStore] = {}

    def get(self, name: str, dim: int | None = None, *,
            index_type: str = "flat") -> VectorStore:
        """获取或创建一个向量存储。

        Args:
            name: 索引名（如 "transcript"）。
            dim: 维度。None 则用 DEFAULT_DIMS[name]，未知名字必须传 dim。
            index_type: "flat" / "ivf" / "auto"（大规模时自动用 IVF 加速）。
        """
        if name in self._stores:
            return self._stores[name]

        if dim is None:
            dim = self.DEFAULT_DIMS.get(name)
            if dim is None:
                raise ValueError(f"未知索引名 {name!r}，需显式传 dim")

        store = VectorStore(name, dim, index_type=index_type)
        store.load()  # 尝试从磁盘加载（失败也无妨，空的）
        self._stores[name] = store
        return store

    def clear_cache(self) -> None:
        """清空实例缓存（测试切目录后调用）。"""
        self._stores.clear()


# 全局工厂单例
_factory = VectorStoreFactory()


def get_vector_store(name: str, dim: int | None = None, *,
                     index_type: str = "flat") -> VectorStore:
    """便捷函数：获取向量存储（懒加载 + 缓存）。"""
    return _factory.get(name, dim, index_type=index_type)


def clear_store_cache() -> None:
    """清空全局缓存（测试用）。"""
    _factory.clear_cache()
