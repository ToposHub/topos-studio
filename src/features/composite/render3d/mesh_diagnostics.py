# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Mesh Diagnostics — F2：Mesh 显示模式的诊断数据生成（纯 numpy）。

设计目标（master_plan §5 / §21.5）：
- 提供 wireframe / points / normals / tangents / UV stretch 等显示模式所需的
  线段/点 buffer 生成，纯 CPU/numpy，无 GL/PyQt 依赖，可独立单测。
- 这些 buffer 由 viewport_overlays / scene3d_node 在渲染线程上传为 VBO，
  用 GL_LINES / GL_POINTS 绘制；本模块只负责「生成数据」。

核心函数：
    build_unique_edges(indices)     → 去重边索引 (E,2) uint32
    build_normal_lines(...)         → 法线线段顶点 (K,6) [start.xyz, end.xyz]
    build_tangent_lines(...)        → 切线线段顶点（normal map 调试用）
    compute_uv_stretch(...)         → 每三角形 UV 拉伸系数

不变量：
- wireframe 用 edge buffer + GL_LINES（不用 glPolygonMode，避免状态泄漏，§5.3）。
- shaded wire 先 textured pass 再叠加 edge lines（depth bias，§5.3）。
- edge buffer 对大模型可能很大，调用方应缓存（按 mesh content_hash）。
"""

from __future__ import annotations

import numpy as np


# ──────────────────────────────────────────────────────────────────────
# 边缓冲区（wireframe / shaded wire 共用）
# ──────────────────────────────────────────────────────────────────────

def build_unique_edges(indices: np.ndarray) -> np.ndarray:
    """从三角形索引数组生成去重的唯一边列表。

    每个三角形 (i0,i1,i2) 产生 3 条边：(i0,i1)、(i1,i2)、(i2,i0)。
    用 (min,max) 规范化 + 去重，保证共享边的相邻三角形只产生一条边。

    Args:
        indices: (M,) uint32 三角形索引（M = 3 * triangle_count）。

    Returns:
        (E,2) uint32 去重后的边顶点索引对，每对 (a,b) 满足 a < b。
        空输入或 <3 个索引返回空数组。
    """
    idx = np.asarray(indices, dtype=np.uint32).reshape(-1)
    if idx.shape[0] < 3:
        return np.zeros((0, 2), dtype=np.uint32)
    # 三角形 i 的三条边
    i0 = idx[0::3]
    i1 = idx[1::3]
    i2 = idx[2::3]
    # 每条边规范化为 (min,max)，堆叠所有边
    e01 = np.stack([np.minimum(i0, i1), np.maximum(i0, i1)], axis=1)
    e12 = np.stack([np.minimum(i1, i2), np.maximum(i1, i2)], axis=1)
    e20 = np.stack([np.minimum(i2, i0), np.maximum(i2, i0)], axis=1)
    all_edges = np.concatenate([e01, e12, e20], axis=0)
    # 去重：np.unique(axis=0) 直接对行去重，无 int64 编码溢出风险。
    unique_edges = np.unique(all_edges, axis=0)
    return unique_edges.astype(np.uint32)


def edge_count_from_indices(indices: np.ndarray) -> int:
    """返回唯一边数量（不分配完整 buffer，用于统计/签名）。"""
    return int(build_unique_edges(indices).shape[0])


# ──────────────────────────────────────────────────────────────────────
# 法线 / 切线 overlay（线段）
# ──────────────────────────────────────────────────────────────────────

def build_normal_lines(vertices: np.ndarray, normals: np.ndarray,
                       length: float = 0.05,
                       stride: int = 1) -> np.ndarray:
    """生成法线可视化线段顶点（GL_LINES 用）。

    每隔 ``stride`` 个顶点画一条从顶点位置沿法线延伸 ``length`` 的线段。

    Args:
        vertices: (N,3) 顶点位置。
        normals: (N,3) 顶点法线（应已归一化）。
        length: 线段长度（世界单位）。
        stride: 采样步长（>1 时只画部分顶点，大模型用）。

    Returns:
        (2K,3) float32 线段顶点，每两个连续顶点构成一条线段。
    """
    verts = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
    norms = np.asarray(normals, dtype=np.float32).reshape(-1, 3)
    n = verts.shape[0]
    if n == 0 or norms.shape[0] != n:
        return np.zeros((0, 3), dtype=np.float32)
    stride = max(1, int(stride))
    sel = verts[::stride]
    sel_n = norms[::stride]
    # 归一化法线（防御未归一化输入）
    lens = np.linalg.norm(sel_n, axis=1, keepdims=True)
    lens[lens < 1e-9] = 1.0
    sel_n = sel_n / lens
    ends = sel + sel_n * float(length)
    # 交错：start, end, start, end, ...
    lines = np.empty((2 * sel.shape[0], 3), dtype=np.float32)
    lines[0::2] = sel
    lines[1::2] = ends
    return lines


def build_tangent_lines(vertices: np.ndarray, tangents: np.ndarray,
                        length: float = 0.05,
                        stride: int = 1) -> np.ndarray:
    """生成切线可视化线段顶点（normal map 调试用）。

    与 build_normal_lines 结构相同，tangents 取前 3 分量（xyz，忽略 w handedness）。

    Args:
        vertices: (N,3) 顶点位置。
        tangents: (N,4) 切线 (x,y,z,handedness) 或 (N,3)。
        length: 线段长度。
        stride: 采样步长。

    Returns:
        (2K,3) float32 线段顶点。
    """
    verts = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
    tan = np.asarray(tangents, dtype=np.float32)
    # (N,4) tangent → 取 xyz 前 3 分量；(N,3) 直接用
    if tan.ndim == 2 and tan.shape[1] >= 3:
        tan = tan[:, :3]
    elif tan.ndim == 1:
        # 一维数组：可能是单个 tangent 或扁平化的 (N*3)
        if tan.shape[0] == 3:
            tan = tan.reshape(1, 3)
        elif tan.shape[0] >= 3 and tan.shape[0] % 3 == 0:
            tan = tan.reshape(-1, 3)
        else:
            return np.zeros((0, 3), dtype=np.float32)
    else:
        return np.zeros((0, 3), dtype=np.float32)
    tan = np.ascontiguousarray(tan, dtype=np.float32)
    n = verts.shape[0]
    if n == 0 or tan.shape[0] != n:
        return np.zeros((0, 3), dtype=np.float32)
    return build_normal_lines(verts, tan, length=length, stride=stride)


# ──────────────────────────────────────────────────────────────────────
# UV 拉伸热力图（UV Stretch Heatmap）
# ──────────────────────────────────────────────────────────────────────

def compute_uv_stretch(vertices: np.ndarray, uvs: np.ndarray,
                       indices: np.ndarray) -> np.ndarray:
    """计算每三角形的 UV 拉伸系数（面积比）。

    拉伸系数 = 3D 面积 / UV 面积（归一化）。比值偏离 1 越多，UV 拉伸越严重。
    用于 F2 UV stretch heatmap（§5.2 F2-7）。

    Args:
        vertices: (N,3) 顶点位置。
        uvs: (N,2) UV 坐标。
        indices: (M,) 三角形索引。

    Returns:
        (T,) float32 每三角形拉伸系数。T = len(indices)//3。
        UV 面积为 0 的退化三角形返回 0（不参与热力图）。
    """
    verts = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
    uv = np.asarray(uvs, dtype=np.float32).reshape(-1, 2)
    idx = np.asarray(indices, dtype=np.uint32).reshape(-1)
    tri_count = idx.shape[0] // 3
    if tri_count == 0:
        return np.zeros(0, dtype=np.float32)
    if verts.shape[0] == 0 or uv.shape[0] != verts.shape[0]:
        return np.zeros(tri_count, dtype=np.float32)

    i0 = idx[0::3][:tri_count]
    i1 = idx[1::3][:tri_count]
    i2 = idx[2::3][:tri_count]

    # 3D 三角形面积（叉积模长 / 2）
    v0 = verts[i0]
    e1_3d = verts[i1] - v0
    e2_3d = verts[i2] - v0
    cross_3d = np.cross(e1_3d, e2_3d)
    area_3d = 0.5 * np.linalg.norm(cross_3d, axis=1)

    # UV 三角形面积
    uv0 = uv[i0]
    e1_uv = uv[i1] - uv0
    e2_uv = uv[i2] - uv0
    cross_uv = e1_uv[:, 0] * e2_uv[:, 1] - e1_uv[:, 1] * e2_uv[:, 0]
    area_uv = 0.5 * np.abs(cross_uv)

    # 拉伸比 = 3D面积 / UV面积；UV面积为0 → 0（退化）
    safe_uv = np.where(area_uv > 1e-12, area_uv, 1.0)
    stretch = area_3d / safe_uv
    stretch[area_uv <= 1e-12] = 0.0
    return stretch.astype(np.float32)


def uv_stretch_to_color(stretch: np.ndarray) -> np.ndarray:
    """把拉伸系数映射到热力图颜色（蓝→绿→黄→红）。

    stretch≈1（无拉伸）→ 绿色；偏离 1 越多越红。0（退化）→ 灰色。

    Args:
        stretch: (T,) 拉伸系数。

    Returns:
        (T,3) float32 RGB（0..1）。
    """
    s = np.asarray(stretch, dtype=np.float32)
    if s.shape[0] == 0:
        return np.zeros((0, 3), dtype=np.float32)
    # 归一化：偏离 1 的程度（拉伸量），clamp 到 [0,1]
    # t=0 → 无拉伸（绿色）；t=1 → 最大拉伸（红色）
    deviation = np.abs(np.log1p(np.abs(s - 1.0)))  # log 缩放使分布更均匀
    t = np.clip(deviation / (deviation.max() + 1e-9), 0.0, 1.0)
    # 绿(0,无拉伸)→黄(0.5)→红(1,最大拉伸)
    r = np.clip(2.0 * t, 0.0, 1.0)
    g = np.clip(1.0 - 2.0 * t, 0.0, 1.0)
    b = np.zeros_like(t)
    colors = np.stack([r, g, b], axis=1).astype(np.float32)
    # 退化三角形（stretch=0）→ 灰色
    colors[s <= 0.0] = np.array([0.4, 0.4, 0.4], dtype=np.float32)
    return colors


# ──────────────────────────────────────────────────────────────────────
# 点云 buffer（Points 显示模式）
# ──────────────────────────────────────────────────────────────────────

def build_point_buffer(vertices: np.ndarray, stride: int = 1) -> np.ndarray:
    """采样顶点位置用于 GL_POINTS 显示。

    Args:
        vertices: (N,3) 顶点位置。
        stride: 采样步长（大模型降采样显示）。

    Returns:
        (K,3) float32 采样后的顶点位置。
    """
    verts = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
    if verts.shape[0] == 0:
        return verts
    stride = max(1, int(stride))
    return verts[::stride]
