"""角色聚类：把同一角色的多个人脸向量归到一类。

输入：face.faiss 里所有 512 维人脸向量（每个 = 一次检测）。
输出：cluster_id 写回 face_track.cluster_id；可选 character 名（人工标注）。

算法选择（DBSCAN 优先）：
    - DBSCAN：不需要预设簇数，自动按密度聚成类，噪声点标 -1（"未识别角色"）。
      适合人脸场景——同一角色的人脸在向量空间扎堆，不同角色间距大。
    - K-Means：需要预设 K（角色数未知时不方便），作为备选。
    - 都不可用（sklearn 没装）：降级为简单阈值分组。

依赖缺失时降级：无 sklearn → 用 numpy 实现简化版（按余弦相似度贪心聚类）。

人工标注：聚类只给 cluster_id（数字），character 名留空，由 GUI 人工指派。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from apps.topos_vault.db.faiss_store import get_vector_store
from apps.topos_vault.db.repository import VaultRepository

logger = logging.getLogger(__name__)


# 聚类参数（余弦空间，512 维 InsightFace 向量经验值）
_DEFAULT_EPS = 0.4       # DBSCAN eps（余弦距离，越小越严；0.4 ≈ cos sim 0.6）
_DEFAULT_MIN_SAMPLES = 2  # DBSCAN min_samples（少于 N 个样本不成簇 → 噪声）
_DEFAULT_KMEANS_MAX_K = 20  # K-Means 最大簇数


@dataclass
class ClusterResult:
    """聚类结果。"""
    n_clusters: int          # 簇数（不含噪声）
    n_noise: int             # 噪声点数（cluster_id=-1）
    n_total: int             # 总人脸数
    cluster_ids: dict[int, int]  # face_id → cluster_id


def cluster_characters(
    repo: VaultRepository,
    *,
    eps: float = _DEFAULT_EPS,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
) -> ClusterResult | None:
    """对所有已检测的人脸做角色聚类，写回 face_track.cluster_id。

    幂等：重复跑会覆盖原 cluster_id（适合加入新素材后重聚类）。

    Args:
        repo: 仓储层。
        eps: DBSCAN eps（余弦距离阈值）。
        min_samples: DBSCAN min_samples。

    Returns:
        ClusterResult；无人脸时返回 None。
    """
    store = get_vector_store("face")
    if store.size == 0:
        logger.info("无人脸向量，跳过聚类")
        return None

    # 拿所有 (face_id, vector)
    all_tracks = repo.list_all_face_tracks()
    if not all_tracks:
        return None

    face_ids: list[str] = []
    vectors: list[np.ndarray] = []
    for track in all_tracks:
        if track.face_id is None:
            continue
        vec = store.get_vector(str(track.face_id))
        if vec is None:
            continue
        face_ids.append(str(track.face_id))
        vectors.append(vec)

    if not vectors:
        return None

    matrix = np.array(vectors, dtype=np.float32)  # (N, 512)
    # 余弦距离矩阵（向量已归一化，1 - 内积 = 余弦距离）
    cos_dist = 1.0 - matrix @ matrix.T
    np.clip(cos_dist, 0.0, 2.0, out=cos_dist)

    labels = _dbscan(cos_dist, eps=eps, min_samples=min_samples)

    # 写回 cluster_id
    fid_to_cluster: dict[int, int] = {}
    n_clusters = 0
    n_noise = 0
    unique_labels = set(labels)
    unique_labels.discard(-1)
    n_clusters = len(unique_labels)
    n_noise = int(sum(1 for l in labels if l == -1))

    for fid_str, label in zip(face_ids, labels):
        fid = int(fid_str)
        cluster_id = int(label)
        # 重新聚类时重置已标注的 character（cluster_id 变了，旧 character 失效）
        # 但保留人工已填的 character（不覆盖）—— 这里只更新 cluster_id
        repo.update_face_cluster(fid, cluster_id)
        fid_to_cluster[fid] = cluster_id

    logger.info("聚类完成：%d 簇 / %d 噪声 / %d 总计",
                n_clusters, n_noise, len(face_ids))
    return ClusterResult(
        n_clusters=n_clusters,
        n_noise=n_noise,
        n_total=len(face_ids),
        cluster_ids=fid_to_cluster,
    )


def _dbscan(dist_matrix: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """DBSCAN 聚类（距离矩阵版）。

    优先用 sklearn（更快更稳）；没装则用 numpy 实现。

    Args:
        dist_matrix: (N, N) 距离矩阵。
        eps: 邻域半径。
        min_samples: 核心点最小邻居数。

    Returns:
        labels: (N,) 簇标签，-1 = 噪声。
    """
    try:
        from sklearn.cluster import DBSCAN
        db = DBSCAN(eps=eps, min_samples=min_samples, metric="precomputed")
        return db.fit_predict(dist_matrix)
    except ImportError:
        return _dbscan_numpy(dist_matrix, eps, min_samples)


def _dbscan_numpy(dist_matrix: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """纯 numpy 实现的 DBSCAN（sklearn 未装时的降级）。

    行为对齐 sklearn DBSCAN(metric='precomputed')：
        - 核心点：eps 邻域内点数 ≥ min_samples
        - 边界点：在某个核心点的 eps 邻域内，但自己不是核心点
        - 噪声：既不是核心点也不在任何核心点邻域内 → label=-1
    """
    n = dist_matrix.shape[0]
    if n == 0:
        return np.array([], dtype=int)

    # 每个点的邻居（含自己，距离 ≤ eps）
    neighbors: list[list[int]] = []
    for i in range(n):
        nb = np.where(dist_matrix[i] <= eps)[0].tolist()
        neighbors.append(nb)

    is_core = np.array([len(nb) >= min_samples for nb in neighbors])
    labels = np.full(n, -1, dtype=int)
    cluster_id = 0
    visited = np.zeros(n, dtype=bool)

    for i in range(n):
        if visited[i]:
            continue
        if not is_core[i]:
            continue  # 非核心点，留给后面边界处理或最终成噪声

        # 新簇：从核心点 i 开始 BFS 扩展
        queue = [i]
        visited[i] = True
        labels[i] = cluster_id
        while queue:
            cur = queue.pop(0)
            for nb in neighbors[cur]:
                if labels[nb] == -1 or not visited[nb]:
                    labels[nb] = cluster_id
                if not visited[nb]:
                    visited[nb] = True
                    if is_core[nb]:
                        queue.append(nb)
        cluster_id += 1

    return labels


def assign_character_name(repo: VaultRepository, cluster_id: int,
                          character: str) -> int:
    """给一个聚类簇指派角色名（人工标注调用）。

    Args:
        cluster_id: 聚类簇 id。
        character: 角色名（如 "张三"）。

    Returns:
        更新的 face_track 行数。
    """
    count = 0
    for track in repo.list_all_face_tracks():
        if track.cluster_id == cluster_id and track.face_id is not None:
            repo.update_face_cluster(track.face_id, cluster_id, character=character)
            count += 1
    return count
