"""Mesh component picking（P7-5/P1-22）—— CPU ray-mesh intersection。

用于 component selection（点/边/面选择）：从屏幕点击位置生成射线，对选中模型
的 mesh 做 ray-triangle intersection，返回命中的 triangle index（面级）或最近的
vertex/edge（点/边级）。

设计要点：
- 纯 numpy 实现（Möller–Trumbore 算法），无 GL/Qt 依赖，可在任意线程调用。
- 输入：ray_origin + ray_dir（世界空间）+ mesh vertices/indices（局部空间）+
  model_matrix（local→world）。
- 输出：命中的 triangle index（或 -1 表示未命中）+ 命中点的 world 坐标 + 命中距离。
- 性能：默认 API 保留 O(triangle_count) 的兼容路径；P1-22 的
  :class:`TriangleBVH` + :class:`MeshPickCache` 提供按 AABB 树剪枝的加速路径，
  不依赖 trimesh/GL/Qt，可在渲染线程之外安全构建和查询。

BVH 保留原始 triangle index（包括输入中间的无效三角形），所以 GPU ID pass、
SelectionState 和 CPU 拾取可以共享同一套面编号。组件模式用同一个面命中结果
推导 vertex/edge，避免 Point/Edge/Polygon 产生不一致的遮挡排序。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import time
import weakref
from typing import Any, Optional, Tuple

import numpy as np

_EPSILON = 1e-7


@dataclass
class RayHit:
    """Ray-mesh intersection 结果（单个最近命中）。"""
    triangle_index: int = -1     # 命中的三角形索引（indices 数组的起始位置 // 3）
    distance: float = float('inf')  # 射线原点到命中点的距离（世界单位）
    point: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # 命中点（世界空间）
    barycentric: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # 重心坐标

    @property
    def hit(self) -> bool:
        """是否命中（triangle_index >= 0）。"""
        return self.triangle_index >= 0


@dataclass
class ComponentHit:
    """统一的 Point/Edge/Polygon 命中结果。

    ``triangle_index`` 始终保留，便于 Polygon 选择和调试；Point/Edge 额外
    填充对应的稳定局部索引。``point`` 与 ``distance`` 始终为世界空间值。
    """

    component_mode: str = "polygon"
    triangle_index: int = -1
    vertex_index: Optional[int] = None
    edge: Optional[Tuple[int, int]] = None
    distance: float = float("inf")
    point: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    barycentric: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def hit(self) -> bool:
        """是否命中有效组件。"""
        return self.triangle_index >= 0


@dataclass
class _BVHNode:
    """TriangleBVH 内部节点；叶子通过 ``start/end`` 指向排序后的面表。"""

    bounds_min: np.ndarray
    bounds_max: np.ndarray
    left: int = -1
    right: int = -1
    start: int = 0
    end: int = 0

    @property
    def is_leaf(self) -> bool:
        return self.left < 0 and self.right < 0


class TriangleBVH:
    """按三角形 AABB 构建的静态 BVH。

    BVH 只持有 CPU 几何数据，不持有 OpenGL 对象。构建时会忽略越界三角形，
    但叶子返回的索引仍是原始 ``indices.reshape(-1, 3)`` 的 triangle index，
    这样坏数据不会导致拾取线程异常，也不会改变合法面的编号。
    """

    def __init__(self, vertices: np.ndarray, indices: np.ndarray,
                 leaf_size: int = 8) -> None:
        verts = np.asarray(vertices, dtype=np.float64)
        if verts.ndim != 2 or verts.shape[1] != 3:
            verts = np.empty((0, 3), dtype=np.float64)
        flat_indices = np.asarray(indices).reshape(-1)
        tri_count = flat_indices.shape[0] // 3
        triangles = np.asarray(
            flat_indices[:tri_count * 3], dtype=np.int64).reshape(-1, 3)
        if triangles.size:
            valid = np.all(
                (triangles >= 0) & (triangles < verts.shape[0]), axis=1)
            source_ids = np.flatnonzero(valid).astype(np.int64)
            valid_triangles = triangles[valid]
        else:
            source_ids = np.empty((0,), dtype=np.int64)
            valid_triangles = np.empty((0, 3), dtype=np.int64)

        self.vertices = verts
        self.indices = triangles
        self._source_triangle_ids = source_ids
        self._triangles = valid_triangles
        self.leaf_size = max(1, int(leaf_size))
        self._triangle_mins = np.empty((0, 3), dtype=np.float64)
        self._triangle_maxs = np.empty((0, 3), dtype=np.float64)
        self._triangle_centroids = np.empty((0, 3), dtype=np.float64)
        self._ordered_triangles: list[int] = []
        self._nodes: list[_BVHNode] = []
        self._root = -1

        if valid_triangles.shape[0] == 0:
            return
        tri_vertices = verts[valid_triangles]
        self._triangle_mins = tri_vertices.min(axis=1)
        self._triangle_maxs = tri_vertices.max(axis=1)
        self._triangle_centroids = (
            self._triangle_mins + self._triangle_maxs) * 0.5
        self._root = self._build(np.arange(valid_triangles.shape[0],
                                           dtype=np.int64))

    @property
    def triangle_count(self) -> int:
        """输入中合法且可被 BVH 查询的三角形数量。"""
        return int(self._triangles.shape[0])

    @property
    def node_count(self) -> int:
        """BVH 节点数量，供性能诊断和测试使用。"""
        return len(self._nodes)

    def _build(self, triangle_ids: np.ndarray) -> int:
        bounds_min = self._triangle_mins[triangle_ids].min(axis=0)
        bounds_max = self._triangle_maxs[triangle_ids].max(axis=0)
        node_index = len(self._nodes)
        self._nodes.append(_BVHNode(bounds_min, bounds_max))
        if triangle_ids.shape[0] <= self.leaf_size:
            start = len(self._ordered_triangles)
            self._ordered_triangles.extend(int(i) for i in triangle_ids)
            self._nodes[node_index] = _BVHNode(
                bounds_min, bounds_max, start=start,
                end=len(self._ordered_triangles))
            return node_index

        extent = bounds_max - bounds_min
        axis = int(np.argmax(extent))
        ordered = triangle_ids[np.argsort(
            self._triangle_centroids[triangle_ids, axis], kind="stable")]
        middle = max(1, min(ordered.shape[0] - 1, ordered.shape[0] // 2))
        left = self._build(ordered[:middle])
        right = self._build(ordered[middle:])
        self._nodes[node_index] = _BVHNode(
            bounds_min, bounds_max, left=left, right=right)
        return node_index

    @staticmethod
    def _ray_aabb(origin: np.ndarray, direction: np.ndarray,
                  bounds_min: np.ndarray,
                  bounds_max: np.ndarray) -> Optional[Tuple[float, float]]:
        """Return slab interval or ``None`` for a miss/parallel outside slab."""
        t_enter = -float("inf")
        t_exit = float("inf")
        for axis in range(3):
            o = float(origin[axis])
            d = float(direction[axis])
            lo = float(bounds_min[axis])
            hi = float(bounds_max[axis])
            if abs(d) <= _EPSILON:
                if o < lo - _EPSILON or o > hi + _EPSILON:
                    return None
                continue
            t0 = (lo - o) / d
            t1 = (hi - o) / d
            if t0 > t1:
                t0, t1 = t1, t0
            t_enter = max(t_enter, t0)
            t_exit = min(t_exit, t1)
            if t_enter > t_exit:
                return None
        if t_exit < 0.0:
            return None
        return max(0.0, t_enter), t_exit

    def intersect(self, ray_origin: np.ndarray, ray_dir: np.ndarray,
                  max_distance: float = 1e9) -> RayHit:
        """Query the nearest triangle with AABB pruning."""
        if self._root < 0:
            return RayHit()
        origin = np.asarray(ray_origin, dtype=np.float64)
        direction = np.asarray(ray_dir, dtype=np.float64)
        if origin.shape != (3,) or direction.shape != (3,):
            return RayHit()
        root_interval = self._ray_aabb(
            origin, direction, self._nodes[self._root].bounds_min,
            self._nodes[self._root].bounds_max)
        if root_interval is None or root_interval[0] > max_distance:
            return RayHit()

        best_t = float(max_distance)
        best_source_index = -1
        best_u = 0.0
        best_v = 0.0
        # Store the AABB entry time alongside each node so far-away subtrees
        # can be discarded after a nearer triangle has been found.
        stack: list[Tuple[int, float]] = [(self._root, root_interval[0])]
        while stack:
            node_index, node_enter = stack.pop()
            if node_enter > best_t:
                continue
            node = self._nodes[node_index]
            if node.is_leaf:
                for ordered_index in self._ordered_triangles[node.start:node.end]:
                    tri = self._triangles[ordered_index]
                    t, u, v = ray_triangle_intersect(
                        origin, direction,
                        self.vertices[tri[0]], self.vertices[tri[1]],
                        self.vertices[tri[2]])
                    if 0.0 < t < best_t:
                        best_t = t
                        best_source_index = int(ordered_index)
                        best_u = u
                        best_v = v
                continue
            children: list[Tuple[float, int]] = []
            for child_index in (node.left, node.right):
                if child_index < 0:
                    continue
                child = self._nodes[child_index]
                interval = self._ray_aabb(
                    origin, direction, child.bounds_min, child.bounds_max)
                if interval is not None and interval[0] <= best_t:
                    children.append((interval[0], child_index))
            # Push the farther child first; the nearer child is popped first.
            children.sort(reverse=True)
            stack.extend((child_index, child_enter)
                         for child_enter, child_index in children)

        if best_source_index < 0:
            return RayHit()
        tri = self._triangles[best_source_index]
        point = ((1.0 - best_u - best_v) * self.vertices[tri[0]]
                 + best_u * self.vertices[tri[1]]
                 + best_v * self.vertices[tri[2]])
        return RayHit(
            triangle_index=int(self._source_triangle_ids[best_source_index]),
            distance=float(best_t),
            point=(float(point[0]), float(point[1]), float(point[2])),
            barycentric=(float(1.0 - best_u - best_v),
                         float(best_u), float(best_v)),
        )


class MeshPickCache:
    """Bounded mesh→BVH cache with explicit revision invalidation.

    ``revision`` should be incremented by the producer when a deform changes
    vertex positions in-place. If omitted, array identity/shape is used, which
    is sufficient for immutable imported meshes and newly allocated geometry.
    """

    def __init__(self, max_entries: int = 64, leaf_size: int = 8) -> None:
        self.max_entries = max(1, int(max_entries))
        self.leaf_size = max(1, int(leaf_size))
        # (signature, bvh, vertices 弱引用, indices 弱引用)
        self._entries: "OrderedDict[str, Tuple[tuple, TriangleBVH, Any, Any]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _weak_or_none(obj: Any) -> Any:
        """对不可弱引用对象（如临时 list）返回 None，退回纯签名判别。"""
        try:
            return weakref.ref(obj)
        except TypeError:
            return None

    @staticmethod
    def _signature(vertices: np.ndarray, indices: np.ndarray,
                   revision: Any) -> tuple:
        verts = np.asarray(vertices)
        idx = np.asarray(indices)
        return (
            revision,
            id(vertices), id(indices),
            tuple(verts.shape), str(verts.dtype),
            tuple(idx.shape), str(idx.dtype),
        )

    def get(self, mesh_id: str, vertices: np.ndarray, indices: np.ndarray,
            revision: Any = None) -> TriangleBVH:
        """Return a cached BVH, rebuilding only after the signature changes."""
        key = str(mesh_id)
        signature = self._signature(vertices, indices, revision)
        entry = self._entries.get(key)
        if entry is not None and entry[0] == signature and self._entry_alive(
                entry, vertices, indices):
            self.hits += 1
            self._entries.move_to_end(key)
            return entry[1]
        self.misses += 1
        bvh = TriangleBVH(vertices, indices, leaf_size=self.leaf_size)
        self._entries[key] = (
            signature, bvh,
            self._weak_or_none(vertices), self._weak_or_none(indices),
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return bvh

    @staticmethod
    def _entry_alive(entry: Tuple[tuple, TriangleBVH, Any, Any],
                     vertices: np.ndarray, indices: np.ndarray) -> bool:
        """AUD-8916：id() 签名的地址复用防线。

        原数组被 GC 后其地址可能被同形状新数组复用，仅凭 id/shape/dtype
        签名会误命中已失效的旧 BVH。条目持有的弱引用随原对象消亡——
        活性校验不通过即视为 miss 重建。不可弱引用的输入（临时 list 等）
        无弱引用可校验，维持纯签名判别（其 id 本就不稳定，天然多 miss）。
        """
        for ref, obj in ((entry[2], vertices), (entry[3], indices)):
            if ref is None:
                continue
            if ref() is not obj:
                return False
        return True

    def invalidate(self, mesh_id: Optional[str] = None) -> None:
        """Invalidate one mesh or the entire cache."""
        if mesh_id is None:
            self._entries.clear()
        else:
            self._entries.pop(str(mesh_id), None)

    def clear_stats(self) -> None:
        """Reset hit/miss counters without evicting BVHs."""
        self.hits = 0
        self.misses = 0

    @property
    def size(self) -> int:
        """当前缓存条目数，供性能诊断和基准报告使用。"""
        return len(self._entries)


@dataclass(frozen=True)
class MeshPickBenchmarkResult:
    """Deterministic BVH build/query benchmark result."""

    triangle_count: int
    ray_count: int
    hit_count: int
    build_ms: float
    query_ms: float
    query_p50_ms: float
    query_p95_ms: float
    bvh_node_count: int
    leaf_size: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe benchmark record."""
        return {
            "triangle_count": int(self.triangle_count),
            "ray_count": int(self.ray_count),
            "hit_count": int(self.hit_count),
            "build_ms": float(self.build_ms),
            "query_ms": float(self.query_ms),
            "query_p50_ms": float(self.query_p50_ms),
            "query_p95_ms": float(self.query_p95_ms),
            "bvh_node_count": int(self.bvh_node_count),
            "leaf_size": int(self.leaf_size),
        }


def benchmark_triangle_bvh(
    vertices: np.ndarray,
    indices: np.ndarray,
    ray_origins: np.ndarray,
    ray_directions: np.ndarray,
    leaf_size: int = 8,
    max_distance: float = 1e9,
) -> MeshPickBenchmarkResult:
    """Build and query a :class:`TriangleBVH`, returning bounded timings.

    The function intentionally accepts explicit rays instead of generating
    random data, so a caller can reproduce a result on the same mesh. Timing
    is wall-clock CPU time; it is a diagnostic baseline, not a hard CI gate.
    """
    origins = np.asarray(ray_origins, dtype=np.float64)
    directions = np.asarray(ray_directions, dtype=np.float64)
    if origins.ndim != 2 or origins.shape[1] != 3:
        raise ValueError("ray_origins must have shape (N, 3)")
    if directions.shape != origins.shape:
        raise ValueError("ray_directions must have the same shape as ray_origins")

    build_start = time.perf_counter()
    bvh = TriangleBVH(vertices, indices, leaf_size=leaf_size)
    build_ms = (time.perf_counter() - build_start) * 1000.0

    query_times: list[float] = []
    hit_count = 0
    query_start = time.perf_counter()
    for origin, direction in zip(origins, directions):
        start = time.perf_counter()
        hit = bvh.intersect(origin, direction, max_distance=max_distance)
        query_times.append((time.perf_counter() - start) * 1000.0)
        hit_count += int(hit.hit)
    query_ms = (time.perf_counter() - query_start) * 1000.0
    if query_times:
        query_p50_ms, query_p95_ms = np.percentile(query_times, (50.0, 95.0))
    else:
        query_p50_ms = query_p95_ms = 0.0
    return MeshPickBenchmarkResult(
        triangle_count=bvh.triangle_count,
        ray_count=int(origins.shape[0]),
        hit_count=hit_count,
        build_ms=build_ms,
        query_ms=query_ms,
        query_p50_ms=float(query_p50_ms),
        query_p95_ms=float(query_p95_ms),
        bvh_node_count=bvh.node_count,
        leaf_size=bvh.leaf_size,
    )


def ray_triangle_intersect(ray_origin: np.ndarray, ray_dir: np.ndarray,
                           v0: np.ndarray, v1: np.ndarray, v2: np.ndarray
                           ) -> Tuple[float, float, float]:
    """Möller–Trumbore ray-triangle intersection（单个三角形）。

    返回 (t, u, v)：t=距离（< 0 = 未命中），u/v=重心坐标。
    命中点 = (1-u-v)*v0 + u*v1 + v*v2。
    """
    edge1 = v1 - v0
    edge2 = v2 - v0
    h = np.cross(ray_dir, edge2)
    a = float(np.dot(edge1, h))
    if -_EPSILON < a < _EPSILON:
        return (-1.0, 0.0, 0.0)  # 射线与三角形平行
    f = 1.0 / a
    s = ray_origin - v0
    u = f * float(np.dot(s, h))
    if u < 0.0 or u > 1.0:
        return (-1.0, 0.0, 0.0)
    q = np.cross(s, edge1)
    v = f * float(np.dot(ray_dir, q))
    if v < 0.0 or (u + v) > 1.0:
        return (-1.0, 0.0, 0.0)
    t = f * float(np.dot(edge2, q))
    if t > _EPSILON:
        return (t, u, v)
    return (-1.0, 0.0, 0.0)


def pick_face(ray_origin: np.ndarray, ray_dir: np.ndarray,
              vertices: np.ndarray, indices: np.ndarray,
              model_matrix: Optional[np.ndarray] = None,
              max_distance: float = 1e9) -> RayHit:
    """对 mesh 做 ray-triangle intersection，返回最近命中的面。

    AUD-8915：命中测试按 Möller–Trumbore 对全部三角形一次性向量化
    （numpy 批量运算），替代逐三角形的纯 Python 循环——大模型点击不再
    阻塞调用线程 O(三角形数)。语义与逐三角形版本一致：|det|<=eps 视为
    平行 miss；u∈[0,1]、v>=0、u+v<=1；t > _EPSILON 且 t < max_distance；
    距离并列时取索引最小的三角形。需要更低单次查询延迟用
    :func:`pick_face_bvh`（BVH 加速版）。

    Args:
        ray_origin: 射线原点（世界空间）。
        ray_dir: 射线方向（世界空间，需归一化）。
        vertices: mesh 顶点 (N, 3)，局部空间。
        indices: mesh 三角形索引 (M,)，每 3 个一组。
        model_matrix: local→world 变换矩阵 (4,4)。None=无变换。
        max_distance: 最大命中距离（剔除远处误命中）。

    Returns:
        RayHit：命中的三角形 index（= indices 起始位置 // 3）+ 世界坐标 + 距离。
        未命中时 triangle_index=-1。
    """
    if vertices is None or indices is None or len(indices) < 3:
        return RayHit()
    # 变换顶点到世界空间（如果需要）
    if model_matrix is not None:
        ones = np.ones((vertices.shape[0], 1), dtype=vertices.dtype)
        v4 = np.hstack([vertices, ones])  # (N, 4)
        world_verts = (model_matrix @ v4.T).T[:, :3]  # (N, 3)
    else:
        world_verts = np.asarray(vertices, dtype=np.float64)
    world_verts = np.asarray(world_verts, dtype=np.float64)

    ray_origin = np.asarray(ray_origin, dtype=np.float64).reshape(3)
    ray_dir = np.asarray(ray_dir, dtype=np.float64).reshape(3)

    idx = np.asarray(indices).reshape(-1)
    n_tris = idx.size // 3
    if n_tris == 0 or world_verts.shape[0] == 0:
        return RayHit()
    tri_idx = idx[: n_tris * 3].astype(np.int64)
    v0 = world_verts[tri_idx[0::3]]  # (T, 3)
    v1 = world_verts[tri_idx[1::3]]
    v2 = world_verts[tri_idx[2::3]]

    # Möller–Trumbore 批量化（与 ray_triangle_intersect 单三角形版本同式）
    edge1 = v1 - v0
    edge2 = v2 - v0
    h = np.cross(ray_dir, edge2)                       # (T, 3)
    a = np.einsum('ij,ij->i', edge1, h)                # 行人 dot(edge1, h)
    valid = np.abs(a) >= _EPSILON                      # 平行 → miss
    with np.errstate(divide='ignore', invalid='ignore'):
        f = np.where(valid, 1.0 / np.where(valid, a, 1.0), 0.0)
        s = ray_origin - v0
        u = f * np.einsum('ij,ij->i', s, h)
        q = np.cross(s, edge1)
        v = f * np.einsum('j,ij->i', ray_dir, q)
        t = f * np.einsum('ij,ij->i', edge2, q)
    valid &= (u >= 0.0) & (u <= 1.0)
    valid &= (v >= 0.0) & ((u + v) <= 1.0)
    valid &= (t > _EPSILON) & (t < max_distance)

    if not valid.any():
        return RayHit()
    # 最近命中（并列取最小索引）：无效行置 +inf 后 argmin
    t_masked = np.where(valid, t, np.inf)
    best = int(np.argmin(t_masked))
    best_t = float(t_masked[best])
    best_u = float(u[best])
    best_v = float(v[best])
    # 计算命中点世界坐标
    i0 = int(tri_idx[best * 3])
    i1 = int(tri_idx[best * 3 + 1])
    i2 = int(tri_idx[best * 3 + 2])
    point = (1.0 - best_u - best_v) * world_verts[i0] + \
            best_u * world_verts[i1] + best_v * world_verts[i2]
    return RayHit(
        triangle_index=best,
        distance=float(best_t),
        point=(float(point[0]), float(point[1]), float(point[2])),
        barycentric=(float(1.0 - best_u - best_v), float(best_u), float(best_v)),
    )


def pick_face_bvh(ray_origin: np.ndarray, ray_dir: np.ndarray,
                  vertices: np.ndarray, indices: np.ndarray,
                  model_matrix: Optional[np.ndarray] = None,
                  max_distance: float = 1e9,
                  bvh: Optional[TriangleBVH] = None) -> RayHit:
    """BVH-accelerated counterpart of :func:`pick_face`.

    The BVH is built in local space. For a model transform, the world-space ray
    is transformed into local space, then the hit point is transformed back so
    the returned distance remains a world-space distance even with uniform or
    non-uniform scale.
    """
    if vertices is None or indices is None:
        return RayHit()
    accelerator = bvh or TriangleBVH(vertices, indices)
    origin = np.asarray(ray_origin, dtype=np.float64)
    direction = np.asarray(ray_dir, dtype=np.float64)
    if origin.shape != (3,) or direction.shape != (3,):
        return RayHit()
    direction_norm = float(np.linalg.norm(direction))
    if direction_norm <= _EPSILON:
        return RayHit()
    world_direction = direction / direction_norm

    if model_matrix is None:
        return accelerator.intersect(origin, world_direction, max_distance)

    matrix = np.asarray(model_matrix, dtype=np.float64)
    if matrix.shape != (4, 4):
        return RayHit()
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError:
        return RayHit()
    local_origin4 = inverse @ np.array([origin[0], origin[1], origin[2], 1.0])
    if abs(float(local_origin4[3])) <= _EPSILON:
        return RayHit()
    local_origin = local_origin4[:3] / local_origin4[3]
    local_direction = inverse[:3, :3] @ world_direction
    local_norm = float(np.linalg.norm(local_direction))
    if local_norm <= _EPSILON:
        return RayHit()
    local_hit = accelerator.intersect(
        local_origin, local_direction / local_norm, float("inf"))
    if not local_hit.hit:
        return RayHit()
    local_point4 = matrix @ np.array(
        [*local_hit.point, 1.0], dtype=np.float64)
    if abs(float(local_point4[3])) <= _EPSILON:
        return RayHit()
    world_point = local_point4[:3] / local_point4[3]
    world_distance = float(np.dot(world_point - origin, world_direction))
    if world_distance <= _EPSILON or world_distance >= float(max_distance):
        return RayHit()
    return RayHit(
        triangle_index=local_hit.triangle_index,
        distance=world_distance,
        point=(float(world_point[0]), float(world_point[1]), float(world_point[2])),
        barycentric=local_hit.barycentric,
    )


def pick_component(ray_origin: np.ndarray, ray_dir: np.ndarray,
                   vertices: np.ndarray, indices: np.ndarray,
                   component_mode: Any = "polygon",
                   model_matrix: Optional[np.ndarray] = None,
                   max_distance: float = 1e9,
                   bvh: Optional[TriangleBVH] = None) -> ComponentHit:
    """Pick a Point, Edge, or Polygon using one shared nearest-face query.

    Point mode chooses the vertex with the largest barycentric weight at the
    visible hit. Edge mode chooses the edge opposite the smallest barycentric
    weight. Both rules are deterministic on shared vertices/edges and retain
    the face hit for diagnostics.
    """
    mode = str(getattr(component_mode, "value", component_mode)).lower()
    aliases = {"face": "polygon", "vertex": "point"}
    mode = aliases.get(mode, mode)
    if mode not in {"point", "edge", "polygon"}:
        raise ValueError(f"Unsupported component mode: {component_mode!r}")
    face_hit = pick_face_bvh(
        ray_origin, ray_dir, vertices, indices, model_matrix,
        max_distance=max_distance, bvh=bvh)
    if not face_hit.hit:
        return ComponentHit(component_mode=mode)
    flat_indices = np.asarray(indices).reshape(-1)
    start = face_hit.triangle_index * 3
    if start < 0 or start + 2 >= flat_indices.shape[0]:
        return ComponentHit(component_mode=mode)
    triangle = tuple(int(value) for value in flat_indices[start:start + 3])
    result = ComponentHit(
        component_mode=mode,
        triangle_index=face_hit.triangle_index,
        distance=face_hit.distance,
        point=face_hit.point,
        barycentric=face_hit.barycentric,
    )
    if mode == "point":
        result.vertex_index = triangle[int(np.argmax(face_hit.barycentric))]
    elif mode == "edge":
        edge_slots = ((1, 2), (0, 2), (0, 1))
        first, second = edge_slots[int(np.argmin(face_hit.barycentric))]
        result.edge = tuple(sorted((triangle[first], triangle[second])))
    return result


def screen_to_world_ray(screen_pos: Tuple[float, float],
                        width: int, height: int,
                        proj_matrix: np.ndarray, view_matrix: np.ndarray
                        ) -> Tuple[np.ndarray, np.ndarray]:
    """屏幕坐标 → 世界空间射线（原点 + 方向）。

    用于 component picking：点击位置 (px, py) 屏幕坐标 → unproject → 射线。

    Args:
        screen_pos: 屏幕像素坐标 (x, y)，y 从顶部开始（需翻转到 GL 底部原点）。
        width, height: 渲染目标尺寸。
        proj_matrix: 投影矩阵 (4,4)。
        view_matrix: 视图矩阵 (4,4)。

    Returns:
        (ray_origin, ray_dir)：射线原点（相机位置）+ 方向（归一化，世界空间）。
    """
    # y 翻转：屏幕 y 从顶部，GL NDC y 从底部
    x = (2.0 * float(screen_pos[0])) / float(max(1, width)) - 1.0
    y = 1.0 - (2.0 * float(screen_pos[1])) / float(max(1, height))
    # NDC 近/远点 (x, y, -1) 和 (x, y, 1)
    near_ndc = np.array([x, y, -1.0, 1.0], dtype=np.float64)
    far_ndc = np.array([x, y, 1.0, 1.0], dtype=np.float64)
    # unproject：NDC → world = inverse(proj @ view) @ ndc
    # math_utils and the Scene3D shaders use column-vector composition in
    # row-major numpy form, so projection must be applied after the view.
    vp = np.asarray(proj_matrix, dtype=np.float64) @ np.asarray(
        view_matrix, dtype=np.float64)
    try:
        inv_vp = np.linalg.inv(vp)
    except np.linalg.LinAlgError:
        # 退化矩阵：返回零射线
        return (np.zeros(3), np.array([0.0, 0.0, -1.0]))
    near_world = (inv_vp @ near_ndc)
    far_world = (inv_vp @ far_ndc)
    if abs(near_world[3]) < _EPSILON:
        return (np.zeros(3), np.array([0.0, 0.0, -1.0]))
    near_world = near_world[:3] / near_world[3]
    far_world = far_world[:3] / far_world[3]
    direction = far_world - near_world
    norm = float(np.linalg.norm(direction))
    if norm < _EPSILON:
        return (near_world, np.array([0.0, 0.0, -1.0]))
    return (near_world, direction / norm)
