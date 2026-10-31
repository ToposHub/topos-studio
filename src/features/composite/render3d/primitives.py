"""
程序化几何生成器 — 为 Scene3D 节点提供内置 mesh（cube/plane/grid/sphere）。

所有生成器返回 `MeshData`，包含交错的顶点属性数组与索引数组，
可直接通过 `ctx.buffer()` 上传为 VBO/IBO。

顶点布局（交错，匹配 `_MESH_VERTEX` 的 in 属性）：
    position(vec3) | normal(vec3) | uv(vec2)
即每个顶点 8 个 float。

设计原则：
- 法线按面平直（flat）或按顶点平滑（smooth），取决于几何语义
- UV 在 [0,1] 范围内合理映射
- 索引使用 uint32 以兼容大网格
- 单位尺寸：cube/plane/sphere 默认半径/半边 = 1，可由节点 mesh_scale 缩放
"""

from dataclasses import dataclass, field
from typing import Mapping

import numpy as np


@dataclass
class MeshData:
    """可上传到 GPU 的网格数据。

    vertices: (N, 3) 顶点位置，float32
    normals:  (N, 3) 顶点法线，float32
    uvs:      (N, 2) 纹理坐标，float32
    indices:  (M,)   索引，uint32
    bone_indices: (N,4) uint32 | None  每顶点影响的骨骼索引（仅蒙皮网格）
    bone_weights: (N,4) float32 | None 每顶点对应的权重（仅蒙皮网格）
    """

    vertices: np.ndarray
    normals: np.ndarray
    uvs: np.ndarray
    indices: np.ndarray
    bone_indices: "np.ndarray | None" = None
    bone_weights: "np.ndarray | None" = None
    # Morph/blend-shape deltas.  Keys are stable target names; values have
    # shape (N, 3) and are applied before skinning.
    morph_targets: Mapping[str, np.ndarray] = field(default_factory=dict)
    morph_normals: Mapping[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 确保类型与连续性（上传 GPU 前必须 C-contiguous）
        self.vertices = np.ascontiguousarray(self.vertices, dtype=np.float32)
        self.normals = np.ascontiguousarray(self.normals, dtype=np.float32)
        self.uvs = np.ascontiguousarray(self.uvs, dtype=np.float32)
        self.indices = np.ascontiguousarray(self.indices, dtype=np.uint32)
        if self.bone_indices is not None:
            self.bone_indices = np.ascontiguousarray(self.bone_indices, dtype=np.uint32)
        if self.bone_weights is not None:
            self.bone_weights = np.ascontiguousarray(self.bone_weights, dtype=np.float32)
        self.morph_targets = self._normalize_morph_map(self.morph_targets, "position")
        self.morph_normals = self._normalize_morph_map(self.morph_normals, "normal")
        # 视锥剔除/VBO 池用的缓存（懒算）
        self._local_aabb = None       # (bmin, bmax)
        self._content_hash = None     # str
        # Model3D 材质升级：切线（normal mapping 用，懒算）。None = 未计算/不需要。
        self._tangents: "np.ndarray | None" = None

    def _normalize_morph_map(self, values, label: str) -> dict[str, np.ndarray]:
        """Normalize and validate imported morph delta arrays."""
        if values is None:
            return {}
        if not isinstance(values, Mapping):
            raise ValueError(f"morph {label} targets must be a mapping")
        normalized: dict[str, np.ndarray] = {}
        for name, value in values.items():
            target = np.ascontiguousarray(value, dtype=np.float32)
            if target.shape != self.vertices.shape:
                raise ValueError(
                    f"morph {label} target {name!r} must have shape {self.vertices.shape}")
            normalized[str(name)] = target
        return normalized

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])

    @property
    def is_skinned(self) -> bool:
        """是否带蒙皮数据（影响 VAO 顶点布局与 shader 选择）。"""
        return self.bone_indices is not None and self.bone_weights is not None

    @property
    def has_morph_targets(self) -> bool:
        """Whether this mesh carries at least one position morph target."""
        return bool(self.morph_targets)

    def interleaved(self) -> np.ndarray:
        """返回交错顶点数组 (N, 8)：pos.xyz | normal.xyz | uv.xy。

        一次性上传到 VBO，配合 ModernGL layout '3f 3f 2f'。
        procedural 几何（无骨骼）始终用此布局。
        """
        n = self.vertex_count
        buf = np.empty((n, 8), dtype=np.float32)
        buf[:, 0:3] = self.vertices
        buf[:, 3:6] = self.normals
        buf[:, 6:8] = self.uvs
        return np.ascontiguousarray(buf)

    def interleaved_skinned(self) -> np.ndarray:
        """返回带骨骼的交错顶点数组 (N, 16)：

        pos.xyz | normal.xyz | uv.xy | boneIds(4,f32) | weights(4)

        全 float32 缓冲区，配合 ModernGL layout '3f 3f 2f 4f 4f'；
        shader 端把 in_bone_ids 当 float 读后转 int（避免 float/int 混合缓冲区）。
        仅 is_skinned 为 True 时调用；否则用 interleaved()。
        """
        if not self.is_skinned:
            raise ValueError("interleaved_skinned() requires bone_indices/bone_weights")
        n = self.vertex_count
        buf = np.empty((n, 16), dtype=np.float32)
        buf[:, 0:3] = self.vertices
        buf[:, 3:6] = self.normals
        buf[:, 6:8] = self.uvs
        buf[:, 8:12] = self.bone_indices.astype(np.float32)   # float 编码 boneIds
        buf[:, 12:16] = self.bone_weights
        return np.ascontiguousarray(buf)

    def compute_tangents(self) -> np.ndarray:
        """计算每顶点切线 (N,4)，用于 normal mapping（TBN 基）。

        标准 Lengyel 算法：遍历三角形，累加 edge1/edge2 与 dUV 的叉积，
        得到 T（xyz）+ handedness（w）。第四分量 w 标记 bitangent 方向
        （DirectX normal map 需翻转）。无 UV 或顶点数 <3 时返回零切线。
        结果缓存到 self._tangents。

        实现为 numpy 向量化（无 Python 逐三角形/逐顶点循环），65K 面约 3ms
        （原逐循环实现约 2.8s，向量化后约 1000×）。
        """
        if self._tangents is not None:
            return self._tangents
        n = self.vertex_count
        idx = self.indices
        if (n < 3 or idx.shape[0] < 3 or self.uvs.shape[0] != n):
            self._tangents = np.zeros((n, 4), dtype=np.float32)
            return self._tangents
        pos = self.vertices
        uv = self.uvs
        # ── 向量化三角形累加（替代逐三角形 Python 循环）──
        tri = idx[: (idx.shape[0] // 3) * 3].reshape(-1, 3)  # (T,3)
        i0 = tri[:, 0]
        i1 = tri[:, 1]
        i2 = tri[:, 2]
        e1 = pos[i1] - pos[i0]   # (T,3)
        e2 = pos[i2] - pos[i0]   # (T,3)
        duv1 = uv[i1] - uv[i0]   # (T,2)
        duv2 = uv[i2] - uv[i0]   # (T,2)
        denom = duv1[:, 0] * duv2[:, 1] - duv1[:, 1] * duv2[:, 0]
        r = 1.0 / (denom + 1e-12)            # (T,)
        t_tri = (e1 * duv2[:, 1, None] - e2 * duv1[:, 1, None]) * r[:, None]  # (T,3)
        # 每顶点累加三个角点的 t（np.add.at 处理重复索引）
        tan = np.zeros((n, 3), dtype=np.float32)
        np.add.at(tan, i0, t_tri)
        np.add.at(tan, i1, t_tri)
        np.add.at(tan, i2, t_tri)
        # ── 向量化 Gram-Schmidt 正交化 + handedness（替代逐顶点循环）──
        norms = self.normals  # (N,3)
        nn = norms
        tt = tan
        d = np.einsum('ij,ij->i', nn, tt)             # (N,) nn·tt
        t_orth = tt - nn * d[:, None]                  # (N,3)
        nrm = np.linalg.norm(t_orth, axis=1)           # (N,)
        safe = nrm > 1e-12
        t_orth_norm = np.where(safe[:, None], t_orth / np.where(safe[:, None], nrm[:, None], 1.0), 0.0)
        # handedness：cross(nn, tt) 与 cross(nn, t_orth) 同向 → +1，否则 -1
        b1 = np.cross(nn, tt)                          # 原始 bitangent
        b2 = np.cross(nn, t_orth_norm)                 # 正交化后 bitangent
        w = np.where(np.einsum('ij,ij->i', b1, b2) >= 0.0, 1.0, -1.0)
        tan4 = np.empty((n, 4), dtype=np.float32)
        tan4[:, :3] = t_orth_norm
        tan4[:, 3] = w
        self._tangents = np.ascontiguousarray(tan4)
        return self._tangents

    @property
    def has_tangents(self) -> bool:
        """是否已计算切线（normal mapping 可用）。"""
        return self._tangents is not None

    def interleaved_with_tangent(self) -> np.ndarray:
        """返回含切线的交错顶点数组 (N, 11)：pos|normal|uv|tangent.xyz。

        切线第四分量（handedness）不进 VBO（shader 用 w=1 假设，多数 GL normal map 适用）。
        配合 ModernGL layout '3f 3f 2f 3f'。仅在需要 normal mapping 时用。
        """
        t = self.compute_tangents()
        n = self.vertex_count
        buf = np.empty((n, 11), dtype=np.float32)
        buf[:, 0:3] = self.vertices
        buf[:, 3:6] = self.normals
        buf[:, 6:8] = self.uvs
        buf[:, 8:11] = t[:, :3]
        return np.ascontiguousarray(buf)

    def content_hash(self) -> str:
        """内容哈希（顶点/索引/法线/UV 字节 + 骨骼/morph），用于 VBO 池共享。

        相同几何（如导入模型里重复的子部件）哈希相同，可共享 VBO/IBO。
        结果缓存到 self._content_hash。

        AUD-9109：interleaved() 产出的 VBO 包含 normals+uvs，二者必须参与
        哈希——顶点+索引相同但 UV/法线不同的 mesh（glTF 共享 POSITION/INDEX
        accessor、不同 TEXCOORD/NORMAL set）此前会得到相同哈希，VBO 池命中
        错误缓冲，渲染出错误的 UV/法线。
        """
        if self._content_hash is not None:
            return self._content_hash
        import hashlib
        h = hashlib.sha1()
        # 主属性（vertices/indices/normals/uvs）的完整字节
        h.update(self.vertices.tobytes())
        h.update(self.indices.tobytes())
        h.update(self.normals.tobytes())
        h.update(self.uvs.tobytes())
        if self.is_skinned:
            h.update(b'skinned')
            h.update(self.bone_indices.tobytes())
            h.update(self.bone_weights.tobytes())
        for name, target in sorted(self.morph_targets.items()):
            h.update(str(name).encode("utf-8", "ignore"))
            h.update(target.tobytes())
        for name, target in sorted(self.morph_normals.items()):
            h.update(b"normal:")
            h.update(str(name).encode("utf-8", "ignore"))
            h.update(target.tobytes())
        self._content_hash = h.hexdigest()[:16]
        return self._content_hash

    def local_aabb(self):
        """local-space AABB（对 vertices 取 min/max），缓存到 self._local_aabb。

        供视锥剔除用（每帧变换到 world 空间测试）。空 mesh 返回单位盒。
        """
        if self._local_aabb is not None:
            return self._local_aabb
        if self.vertices.shape[0] == 0:
            result = (np.array([-1, -1, -1], dtype=np.float32),
                      np.array([1, 1, 1], dtype=np.float32))
        else:
            result = (self.vertices.min(axis=0).astype(np.float32),
                      self.vertices.max(axis=0).astype(np.float32))
        self._local_aabb = result
        return result


def cube(half_size: float = 1.0) -> MeshData:
    """立方体（每面独立顶点，保证面法线平直）。

    24 顶点（6 面 × 4），36 索引（6 面 × 2 三角形 × 3）。
    """
    s = float(half_size)
    # 8 个角点
    p = np.array([
        [-s, -s, -s], [s, -s, -s], [s, s, -s], [-s, s, -s],  # 后面 z=-s
        [-s, -s,  s], [s, -s,  s], [s, s,  s], [-s, s,  s],  # 前面 z=+s
    ], dtype=np.float32)

    # 每面 4 个顶点索引（顺序：右、左、上、下、前、后），法线沿轴。
    # 各面顶点按"从法线方向看过去为 CCW"排列（右手系：CCW 为正面），
    # 保证 (v1-v0)×(v2-v0) 与声明法线同向。
    faces = [
        # (面顶点索引, 法线)
        ([1, 2, 6, 5], [1.0, 0.0, 0.0]),   # +X
        ([4, 7, 3, 0], [-1.0, 0.0, 0.0]),  # -X
        ([2, 3, 7, 6], [0.0, 1.0, 0.0]),   # +Y
        ([0, 1, 5, 4], [0.0, -1.0, 0.0]),  # -Y
        ([4, 5, 6, 7], [0.0, 0.0, 1.0]),   # +Z 前
        ([3, 2, 1, 0], [0.0, 0.0, -1.0]),  # -Z 后
    ]

    verts_list = []
    norms_list = []
    uvs_list = []
    idx_list = []
    vi = 0
    for face_idx, normal in faces:
        for k in range(4):
            verts_list.append(p[face_idx[k]])
            norms_list.append(normal)
        # 四边形 UV（左下→右下→右上→左上）
        uvs_list.extend([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
        # 两个三角形：(0,1,2) 和 (0,2,3)
        idx_list.extend([vi, vi + 1, vi + 2, vi, vi + 2, vi + 3])
        vi += 4

    return MeshData(
        vertices=np.array(verts_list, dtype=np.float32),
        normals=np.array(norms_list, dtype=np.float32),
        uvs=np.array(uvs_list, dtype=np.float32),
        indices=np.array(idx_list, dtype=np.uint32),
    )


def plane(size: float = 2.0) -> MeshData:
    """XZ 平面（在原点，法线 +Y）。

    4 顶点，6 索引。适合做地面或背景板。
    """
    s = float(size) * 0.5
    verts = np.array([
        [-s, 0.0, -s], [s, 0.0, -s], [s, 0.0, s], [-s, 0.0, s],
    ], dtype=np.float32)
    norms = np.tile([0.0, 1.0, 0.0], (4, 1)).astype(np.float32)
    uvs = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
    # 三角形 (0,2,1)/(0,3,2)：从 +Y 往下看为 CCW，几何法线与 +Y 顶点法线同向
    idx = np.array([0, 2, 1, 0, 3, 2], dtype=np.uint32)
    return MeshData(verts, norms, uvs, idx)


def grid(divisions: int = 10, size: float = 10.0) -> MeshData:
    """地面网格（线条集合，用 GL_LINES 渲染）。

    用于场景参考。注意：索引语义是线段对，调用方需用 moderngl.LINES 渲染，
    而非 TRIANGLES。Scene3D 节点为简化统一走 TRIANGLES 路径，
    grid 主要作为视觉参考；阶段一可选择性支持。
    """
    divisions = max(1, int(divisions))
    s = float(size) * 0.5
    step = (2.0 * s) / divisions

    verts = []
    uvs = []
    idx = []
    vi = 0
    # 平行 X 轴的线
    for i in range(divisions + 1):
        z = -s + i * step
        verts.extend([[-s, 0.0, z], [s, 0.0, z]])
        uvs.extend([[0.0, 0.0], [1.0, 0.0]])
        idx.extend([vi, vi + 1])
        vi += 2
    # 平行 Z 轴的线
    for i in range(divisions + 1):
        x = -s + i * step
        verts.extend([[x, 0.0, -s], [x, 0.0, s]])
        uvs.extend([[0.0, 0.0], [1.0, 0.0]])
        idx.extend([vi, vi + 1])
        vi += 2

    norms = np.tile([0.0, 1.0, 0.0], (len(verts), 1)).astype(np.float32)
    return MeshData(
        vertices=np.array(verts, dtype=np.float32),
        normals=norms,
        uvs=np.array(uvs, dtype=np.float32),
        indices=np.array(idx, dtype=np.uint32),
    )


def sphere(radius: float = 1.0, segments: int = 32, rings: int = 16) -> MeshData:
    """UV 球（平滑法线，适合验证光照）。

    通过经纬度细分生成。segments = 经度切片，rings = 纬度切片。
    """
    radius = float(radius)
    segments = max(4, int(segments))
    rings = max(2, int(rings))

    verts = []
    norms = []
    uvs = []
    for ring in range(rings + 1):
        theta = np.pi * ring / rings  # 0..π（从 +Y 顶点到 -Y 顶点）
        sin_t = np.sin(theta)
        cos_t = np.cos(theta)
        for seg in range(segments + 1):
            phi = 2.0 * np.pi * seg / segments  # 0..2π
            sin_p = np.sin(phi)
            cos_p = np.cos(phi)
            # 位置（法线 = 归一化位置，因为是球心在原点的球）
            nx, ny, nz = sin_t * cos_p, cos_t, sin_t * sin_p
            verts.append([radius * nx, radius * ny, radius * nz])
            norms.append([nx, ny, nz])
            uvs.append([seg / segments, ring / rings])

    idx = []
    for ring in range(rings):
        for seg in range(segments):
            a = ring * (segments + 1) + seg
            b = a + (segments + 1)
            # 两个三角形（CCW 缠绕，几何法线与径向顶点法线同向、朝球外）
            idx.extend([a, a + 1, b, a + 1, b + 1, b])

    return MeshData(
        vertices=np.array(verts, dtype=np.float32),
        normals=np.array(norms, dtype=np.float32),
        uvs=np.array(uvs, dtype=np.float32),
        indices=np.array(idx, dtype=np.uint32),
    )


# 基元名 → 生成器的映射（供 Scene3D 节点按 primitive 参数查找）
# P1 扩展：新增 cylinder / cone / torus / null（补齐常见 DCC 基本体）。
PRIMITIVES = {
    "cube": cube,
    "plane": plane,
    "grid": grid,
    "sphere": sphere,
    "cylinder": None,  # 占位，下方赋值（函数定义在 dict 之后）
    "cone": None,
    "torus": None,
    "null": None,
}


def cylinder(radius: float = 1.0, height: float = 2.0,
             segments: int = 32) -> MeshData:
    """圆柱（含顶/底盖，平滑侧面法线）。

    侧面按经度细分，顶/底盖用三角形扇。height 沿 Y 轴，中心在原点。

    Args:
        radius: 半径。
        height: 总高（半高 = height/2）。
        segments: 圆周细分段数。
    """
    radius = float(radius)
    height = float(height)
    segments = max(3, int(segments))
    half_h = height * 0.5

    verts = []
    norms = []
    uvs = []
    idx = []

    # 侧面顶点：上下两圈（每段 2 顶点），法线水平朝外。
    # 索引布局：side verts 占 [0, 2*(segments+1))。
    for s in range(segments + 1):
        ang = 2.0 * np.pi * s / segments
        cx = np.cos(ang)
        cz = np.sin(ang)
        # 下圈（y=-half_h）
        verts.append([radius * cx, -half_h, radius * cz])
        norms.append([cx, 0.0, cz])
        uvs.append([s / segments, 0.0])
        # 上圈（y=+half_h）
        verts.append([radius * cx, half_h, radius * cz])
        norms.append([cx, 0.0, cz])
        uvs.append([s / segments, 1.0])
    for s in range(segments):
        a = 2 * s
        b = a + 1
        c = a + 2
        d = a + 3
        idx.extend([a, b, c, b, d, c])  # CCW from outside（几何法线朝外）

    # 顶盖中心 + 环（triangle fan）。法线 +Y，从 +Y 看环顶点顺时针排布
    # （cos/sin 参数化在 XZ 平面为从 +Y 看的顺时针），故扇形索引取反绕向。
    top_center = len(verts)
    verts.append([0.0, half_h, 0.0])
    norms.append([0.0, 1.0, 0.0])
    uvs.append([0.5, 0.5])
    top_ring_start = top_center + 1
    for s in range(segments + 1):
        ang = 2.0 * np.pi * s / segments
        verts.append([radius * np.cos(ang), half_h, radius * np.sin(ang)])
        norms.append([0.0, 1.0, 0.0])
        uvs.append([0.5 + 0.5 * np.cos(ang), 0.5 + 0.5 * np.sin(ang)])
    for s in range(segments):
        idx.extend([top_center, top_ring_start + s + 1, top_ring_start + s])

    # 底盖中心 + 环（triangle fan，法线 -Y）。环顶点序同上，但从 -Y 看为
    # 逆时针，扇形直接 (center, s, s+1) 即几何法线 -Y。
    bot_center = len(verts)
    verts.append([0.0, -half_h, 0.0])
    norms.append([0.0, -1.0, 0.0])
    uvs.append([0.5, 0.5])
    bot_ring_start = bot_center + 1
    for s in range(segments + 1):
        ang = 2.0 * np.pi * s / segments
        verts.append([radius * np.cos(ang), -half_h, radius * np.sin(ang)])
        norms.append([0.0, -1.0, 0.0])
        uvs.append([0.5 + 0.5 * np.cos(ang), 0.5 + 0.5 * np.sin(ang)])
    for s in range(segments):
        idx.extend([bot_center, bot_ring_start + s, bot_ring_start + s + 1])

    return MeshData(
        vertices=np.array(verts, dtype=np.float32),
        normals=np.array(norms, dtype=np.float32),
        uvs=np.array(uvs, dtype=np.float32),
        indices=np.array(idx, dtype=np.uint32),
    )


def cone(radius: float = 1.0, height: float = 2.0,
         segments: int = 32) -> MeshData:
    """圆锥（底面盖 + 侧面收敛到顶点）。

    侧面法线为锥面斜法线（介于水平与上指向之间）。height 沿 Y 轴。

    Args:
        radius: 底面半径。
        height: 总高（顶点在 +height/2，底面在 -height/2）。
        segments: 底面圆周细分段数。
    """
    radius = float(radius)
    height = float(height)
    segments = max(3, int(segments))
    half_h = height * 0.5

    verts = []
    norms = []
    uvs = []
    idx = []

    # 侧面：每段一个底圈顶点 + 共享顶点（apex）。
    # 锥面斜法线：水平分量 cosθ 朝外，垂直分量按 slope = radius/height。
    slope = radius / max(height, 1e-6)
    side_n_len = float(np.sqrt(1.0 + slope * slope))
    side_ny = slope / side_n_len
    side_nh = 1.0 / side_n_len

    apex = 0
    verts.append([0.0, half_h, 0.0])  # 顶点
    norms.append([0.0, 1.0, 0.0])
    uvs.append([0.5, 1.0])

    ring_start = 1
    for s in range(segments + 1):
        ang = 2.0 * np.pi * s / segments
        cx = np.cos(ang)
        cz = np.sin(ang)
        verts.append([radius * cx, -half_h, radius * cz])
        norms.append([side_nh * cx, side_ny, side_nh * cz])
        uvs.append([s / segments, 0.0])
    for s in range(segments):
        a = ring_start + s
        b = ring_start + s + 1
        idx.extend([apex, b, a])  # 从外侧看 CCW

    # 底盖（triangle fan，法线 -Y，与 cylinder 底盖同约定：
    # (center, s, s+1) 的几何法线为 -Y）。
    bot_center = len(verts)
    verts.append([0.0, -half_h, 0.0])
    norms.append([0.0, -1.0, 0.0])
    uvs.append([0.5, 0.5])
    bot_ring_start = bot_center + 1
    for s in range(segments + 1):
        ang = 2.0 * np.pi * s / segments
        verts.append([radius * np.cos(ang), -half_h, radius * np.sin(ang)])
        norms.append([0.0, -1.0, 0.0])
        uvs.append([0.5 + 0.5 * np.cos(ang), 0.5 + 0.5 * np.sin(ang)])
    for s in range(segments):
        idx.extend([bot_center, bot_ring_start + s, bot_ring_start + s + 1])

    return MeshData(
        vertices=np.array(verts, dtype=np.float32),
        normals=np.array(norms, dtype=np.float32),
        uvs=np.array(uvs, dtype=np.float32),
        indices=np.array(idx, dtype=np.uint32),
    )


def torus(major_radius: float = 1.0, minor_radius: float = 0.3,
          major_segments: int = 32, minor_segments: int = 16) -> MeshData:
    """环面（甜甜圈）。

    主圆（大半径）在 XZ 平面，次圆（小半径/管半径）绕主圆扫掠。

    Args:
        major_radius: 中心到管中心的距离。
        minor_radius: 管半径。
        major_segments: 大圆细分段数。
        minor_segments: 管截面细分段数。
    """
    major_radius = float(major_radius)
    minor_radius = float(minor_radius)
    major_segments = max(3, int(major_segments))
    minor_segments = max(3, int(minor_segments))

    verts = []
    norms = []
    uvs = []
    for i in range(major_segments):
        u = 2.0 * np.pi * i / major_segments
        cu, su = np.cos(u), np.sin(u)
        for j in range(minor_segments):
            v = 2.0 * np.pi * j / minor_segments
            cv, sv = np.cos(v), np.sin(v)
            # 管中心 + 偏移
            r = major_radius + minor_radius * cv
            verts.append([r * cu, minor_radius * sv, r * su])
            # 法线：从管中心指向表面点（在管的局部 XY）
            norms.append([cv * cu, sv, cv * su])
            uvs.append([i / major_segments, j / minor_segments])

    idx = []
    for i in range(major_segments):
        for j in range(minor_segments):
            a = i * minor_segments + j
            b = ((i + 1) % major_segments) * minor_segments + j
            c = ((i + 1) % major_segments) * minor_segments + (j + 1) % minor_segments
            d = i * minor_segments + (j + 1) % minor_segments
            # CCW from outside：几何法线与径向顶点法线（管心→表面）同向
            idx.extend([a, d, b, d, c, b])

    return MeshData(
        vertices=np.array(verts, dtype=np.float32),
        normals=np.array(norms, dtype=np.float32),
        uvs=np.array(uvs, dtype=np.float32),
        indices=np.array(idx, dtype=np.uint32),
    )


def null_object() -> MeshData:
    """Null/轴对象（占位参考点）。

    DCC 里的 null/axis 对象：一个小的三轴指示器，用于定位/父级锚点，
    本身几乎不占视觉空间。用三根短线段（XYZ 轴）实现，is_lines 由
    调用方按 primitive 名判定。
    """
    s = 1.0  # 轴长度
    # 6 个顶点（原点 + 各轴端点），3 条线段
    verts = [
        [0.0, 0.0, 0.0], [s, 0.0, 0.0],   # X 轴
        [0.0, 0.0, 0.0], [0.0, s, 0.0],   # Y 轴
        [0.0, 0.0, 0.0], [0.0, 0.0, s],   # Z 轴
    ]
    norms = [[1.0, 0.0, 0.0]] * 2 + [[0.0, 1.0, 0.0]] * 2 + [[0.0, 0.0, 1.0]] * 2
    uvs = [[0.0, 0.0]] * 6
    idx = [0, 1, 2, 3, 4, 5]
    return MeshData(
        vertices=np.array(verts, dtype=np.float32),
        normals=np.array(norms, dtype=np.float32),
        uvs=np.array(uvs, dtype=np.float32),
        indices=np.array(idx, dtype=np.uint32),
    )


# 补回 PRIMITIVES 映射（函数定义在 dict 之后，此处绑定）。
PRIMITIVES["cylinder"] = cylinder
PRIMITIVES["cone"] = cone
PRIMITIVES["torus"] = torus
PRIMITIVES["null"] = null_object


def get_primitive(name: str, **kwargs) -> MeshData:
    """按名称获取程序化几何。

    Args:
        name: "cube" / "plane" / "grid" / "sphere" / "cylinder" / "cone"
              / "torus" / "null"。
        **kwargs: 传给具体生成器的参数。

    Raises:
        ValueError: 未知基元名。
    """
    gen = PRIMITIVES.get(name)
    if gen is None:
        raise ValueError(f"Unknown primitive: {name!r} (known: {list(PRIMITIVES)})")
    return gen(**kwargs)
