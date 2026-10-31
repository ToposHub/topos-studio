"""Viewport LOD Mesh 生成器 —— 低面数代理 mesh 供导航用（Milestone F / P5）。

对应 master_plan_2026-07-07.md §9 (P5)。让 50 万~200 万面模型在导航时用低面数
代理（LOD1/LOD2），停止后恢复高质量（LOD0）。

算法选择（§9.3）：
- trimesh.simplify_quadric_decimation：质量好但依赖 trimesh（可能不可用）。
- 自研顶点聚类（vertex clustering / 网格量化）：纯 numpy，可控、稳定，质量一般
  但够"导航用代理"。本模块默认走聚类（无外部依赖），trimesh 可用时可选走它。

顶点聚类（本模块实现）原理：
1. 用 mesh local AABB 划分 cell_size 网格。
2. 落入同一 cell 的顶点合并为一个代表顶点（位置/UV/normal 取均值）。
3. 三角形若三个顶点落入同一 cell（退化）则丢弃；否则用代表顶点索引重建。
4. 保留 UV（同 cell 顶点 UV 平均）+ material index（三角形单元保留）。

这样得到的 LOD 面数随 cell_size 增大而减少；cell_size=0 退化为 LOD0（原样）。
LOD 生成必须保留 UV 和材质索引（§9.5 注意点1，否则材质花纹错）——聚类天然
保留（代表顶点带 UV，三角形单元不变）。

设计：
- build_lod(mesh, target_ratio) → 新 MeshData（target_ratio=期望面数比例，0~1）。
- build_lod_levels(mesh, ratios=(1.0, 0.5, 0.25)) → [LOD0, LOD1, LOD2] 列表。
- 蒙皮 mesh 的 LOD：保留 bone_indices/bone_weights（代表顶点取最常出现的骨骼，
  权重归一化）。复杂且 edge case 多，第一阶段蒙皮 mesh 不做 LOD（直接返回 LOD0）。
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np

from .primitives import MeshData

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# LOD 等级常量（§9.2）
# ──────────────────────────────────────────────────────────────────────────

LOD_LEVELS = {
    0: 1.0,    # LOD0：100% 面（原样）
    1: 0.5,    # LOD1：50%（静止 preview 可接受）
    2: 0.25,   # LOD2：25%（navigation）
    3: 0.10,   # LOD3：10%（超大模型 / 加载中）
}


def lod_ratio_for_level(level: int) -> float:
    """LOD 等级 → 目标面数比例（0~1）。未知等级返回 1.0（不简化）。"""
    return LOD_LEVELS.get(int(level), 1.0)


# ──────────────────────────────────────────────────────────────────────────
# 顶点聚类 LOD（纯 numpy）
# ──────────────────────────────────────────────────────────────────────────

def _estimate_cluster_size(mesh: MeshData, target_ratio: float) -> float:
    """估算达到 target_ratio 面数比例所需的 cell_size。

    顶点聚类近似把体积划分成 cell，cell 数 ≈ mesh 体积 / cell³。面数随 cell 数
    线性下降（粗略）。这里用 AABB 对角线 × (1-ratio)^0.5 作 cell_size 启发式，
    保证 ratio=1 → cell=0（不合并），ratio→0 → cell 越大（合并越多）。
    """
    if target_ratio >= 1.0:
        return 0.0
    if target_ratio <= 0.0:
        target_ratio = 0.05
    try:
        vmin = mesh.vertices.min(axis=0)
        vmax = mesh.vertices.max(axis=0)
        extent = float(np.max(vmax - vmin))
        if extent <= 0:
            return 0.0
        # cell_size 让 cell 数 ≈ 原顶点数 × ratio。
        # cell³ ≈ volume / (N×ratio) → cell ≈ (extent³/(N×ratio))^(1/3)
        n = max(1, int(mesh.vertices.shape[0]))
        cell = extent * (1.0 / (n * target_ratio)) ** (1.0 / 3.0)
        # 钳制：cell 不超过 extent/4（避免过度合并成几个点）
        return float(min(cell, extent * 0.25))
    except Exception:
        return 0.0


def build_lod(mesh: MeshData, target_ratio: float = 0.5) -> MeshData:
    """用顶点聚类生成简化 mesh（保留 UV + 材质边界）。

    Args:
        mesh: 原始 MeshData（LOD0）。
        target_ratio: 期望面数比例（0~1）。1.0 = 原样返回；越小越简化。

    Returns:
        新 MeshData（简化后）。若无法简化（顶点过少/聚类失败/蒙皮 mesh），
        返回原 mesh 的浅拷贝（不简化，安全降级）。

    注意：
    - 蒙皮 mesh（is_skinned）第一阶段不简化（直接返回原 mesh）。蒙皮 LOD 需要
      保留骨骼权重，复杂度高，留待后续（§9.5 注意点3）。
    - target_ratio 是启发式目标，实际简化比例取决于几何；不保证精确。
    """
    # 蒙皮 mesh / 顶点太少 / 目标 1.0 → 不简化
    if (target_ratio >= 1.0
            or mesh.is_skinned
            or mesh.has_morph_targets
            or mesh.vertices.shape[0] < 24
            or mesh.indices.shape[0] < 36):
        return _copy_mesh(mesh)

    cell = _estimate_cluster_size(mesh, target_ratio)
    if cell <= 0:
        return _copy_mesh(mesh)

    try:
        return _cluster_vertices(mesh, cell)
    except Exception:
        logger.debug("LOD 顶点聚类失败，回退原 mesh", exc_info=True)
        return _copy_mesh(mesh)


def _copy_mesh(mesh: MeshData) -> MeshData:
    """深拷贝 MeshData（新数组，共享 content_hash 逻辑但独立对象）。"""
    return MeshData(
        vertices=mesh.vertices.copy(),
        normals=mesh.normals.copy(),
        uvs=mesh.uvs.copy(),
        indices=mesh.indices.copy(),
        bone_indices=(mesh.bone_indices.copy() if mesh.bone_indices is not None else None),
        bone_weights=(mesh.bone_weights.copy() if mesh.bone_weights is not None else None),
        morph_targets={name: target.copy()
                       for name, target in mesh.morph_targets.items()},
        morph_normals={name: target.copy()
                       for name, target in mesh.morph_normals.items()},
    )


def _cluster_vertices(mesh: MeshData, cell_size: float) -> MeshData:
    """顶点聚类：把落入同一 cell 的顶点合并，重建三角形索引。

    cell_size=0 时不合并。退化三角形（三顶点同 cell）丢弃。
    代表顶点：位置/UV/normal 取 cell 内均值。
    """
    verts = mesh.vertices
    normals = mesh.normals
    uvs = mesh.uvs
    indices = mesh.indices
    n = verts.shape[0]
    if n == 0:
        return _copy_mesh(mesh)

    # 每顶点的 cell 坐标（量化到 cell_size 网格）
    vmin = verts.min(axis=0)
    cell_coords = np.floor((verts - vmin) / max(cell_size, 1e-9)).astype(np.int64)
    # cell key：把 3D cell 坐标编码成单维 key（cell_x * BIG² + cell_y * BIG + cell_z）
    # 用 numpy 累积避免 Python 慢循环。
    cx = cell_coords[:, 0]
    cy = cell_coords[:, 1]
    cz = cell_coords[:, 2]
    # 偏移到非负（floor 后可能负），再编码
    cx -= cx.min(); cy -= cy.min(); cz -= cz.min()
    dim_z = int(cz.max()) + 1
    dim_y = int(cy.max()) + 1
    cell_key = cx.astype(np.int64) * (dim_y * dim_z) + cy.astype(np.int64) * dim_z + cz.astype(np.int64)

    # 每个 cell 的代表顶点 = 该 cell 内第一个顶点的索引（稳定）
    # 用 dict 模拟：unique cell_key → representative vertex index
    unique_keys, inverse = np.unique(cell_key, return_inverse=True)
    # 代表顶点：每个 unique cell 取该 cell 内顶点的属性均值
    n_cells = unique_keys.shape[0]
    # 累加每 cell 的 pos/normal/uv，再除以 count
    sums_pos = np.zeros((n_cells, 3), dtype=np.float64)
    sums_normal = np.zeros((n_cells, 3), dtype=np.float64)
    sums_uv = np.zeros((n_cells, 2), dtype=np.float64)
    counts = np.zeros(n_cells, dtype=np.int64)
    np.add.at(sums_pos, inverse, verts)
    np.add.at(sums_normal, inverse, normals)
    np.add.at(sums_uv, inverse, uvs)
    np.add.at(counts, inverse, 1)
    nonzero = np.maximum(counts, 1).reshape(-1, 1)
    rep_verts = (sums_pos / nonzero).astype(np.float32)
    rep_normals = (sums_normal / nonzero).astype(np.float32)
    rep_uvs = (sums_uv / nonzero).astype(np.float32)
    # 归一化法线（聚类后可能非单位长）
    norms = np.linalg.norm(rep_normals, axis=1, keepdims=True)
    rep_normals = np.where(norms > 1e-9, rep_normals / norms, rep_normals)

    # 重建三角形索引：原 indices 顶点 → inverse 映射到 cell index。
    # 退化三角形（三个 cell index 相同）丢弃。
    cell_idx_per_vertex = inverse  # shape (n,), cell index per original vertex
    tri_vertex = cell_idx_per_vertex[indices]  # shape (n_tris*3,)
    tri_vertex = tri_vertex.reshape(-1, 3)
    # 退化判定：三列不全相等
    non_degenerate = ~((tri_vertex[:, 0] == tri_vertex[:, 1])
                       & (tri_vertex[:, 1] == tri_vertex[:, 2]))
    new_indices = tri_vertex[non_degenerate].astype(np.uint32).reshape(-1)

    # content_hash 会在 __post_init__ 里懒算（新对象）
    return MeshData(
        vertices=rep_verts,
        normals=rep_normals,
        uvs=rep_uvs,
        indices=new_indices,
    )


# ──────────────────────────────────────────────────────────────────────────
# LOD 多级生成 + 选择
# ──────────────────────────────────────────────────────────────────────────

def build_lod_levels(mesh: MeshData,
                     ratios: Tuple[float, ...] = (1.0, 0.5, 0.25)
                     ) -> List[MeshData]:
    """生成多级 LOD mesh（LOD0=原样，LOD1/2/...=递减）。

    返回列表索引 = LOD 等级。ratios=(1.0, 0.5, 0.25) → [LOD0, LOD1, LOD2]。
    每级独立聚类（从 LOD0 出发，不从上一级继续，避免误差累积）。
    """
    levels: List[MeshData] = []
    for r in ratios:
        levels.append(build_lod(mesh, target_ratio=r))
    return levels


def choose_lod_level(lod_meshes: List[MeshData],
                     target_level: int,
                     triangle_thresholds: Optional[Tuple[int, ...]] = None
                     ) -> int:
    """选择实际使用的 LOD 等级（考虑 mesh 规模 + 阈值）。

    Args:
        lod_meshes: build_lod_levels 的输出（索引=等级）。
        target_level: 期望等级（来自 quality profile：NAVIGATION→2，PREVIEW→0）。
        triangle_thresholds: 各等级启用的三角形阈值（面数低于此则用更高级别=更细）。
            None = 不按规模调整，直接返回 target_level。

    Returns:
        实际等级（钳制到 [0, len(lod_meshes)-1]）。
    """
    if not lod_meshes:
        return 0
    target_level = max(0, min(int(target_level), len(lod_meshes) - 1))
    if triangle_thresholds is None:
        return target_level
    # 按规模调整：LOD0 面数低于阈值 → 不需要降级，用 LOD0
    try:
        lod0_tris = int(lod_meshes[0].indices.shape[0]) // 3
        threshold = triangle_thresholds[min(target_level, len(triangle_thresholds) - 1)]
        if lod0_tris < threshold:
            return 0  # 模型够小，不需要 LOD
    except Exception:
        pass
    return target_level


def triangle_count(mesh: MeshData) -> int:
    """mesh 三角形数（indices/3）。"""
    try:
        return int(mesh.indices.shape[0]) // 3
    except Exception:
        return 0
