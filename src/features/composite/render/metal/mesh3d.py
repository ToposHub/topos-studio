"""阶段 5.5 第一步 · 最小 3D pass：基本网格 + 固定相机（验证约定）。

**这不是 Scene3D 的 Metal 实现**，也不接入图链。按迁移计划 5.5「先用
基本网格确认矩阵、裁剪空间和深度约定」，本模块用一张测试网格在 Metal
compute 上做软件光栅化，验证三件事与 ``render3d/`` 的 GL 参考一致：

1. **矩阵约定**：直接复用 ``render3d.math_utils`` 的
   ``perspective``/``look_at``（右手系、相机看向 -Z、numpy 行主序）；
   上传布局沿用 ``matrix_to_tuple`` 的**列主序 16 元组**——同一份 buffer
   可以原样喂给 GLSL ``u_proj/u_view/u_model`` 和本模块的 MSL kernel。
2. **裁剪空间**：GL 参考（``shaders.py::_MESH_VERTEX`` 的
   ``gl_Position = u_proj * u_view * u_model * vec4(in_pos, 1)``）是
   OpenGL 裁剪空间，NDC z ∈ [-1, 1]。Metal 的 NDC z ∈ [0, 1]，本 pass
   在**边界显式重映射** ``d = z_ndc * 0.5 + 0.5``——这正是 GL 写进深度
   缓冲的值（GL 深度范围 [0,1]），因此存储的深度值两端一致，差异只在
   重映射发生在哪一层。视口映射：NDC x 照常；NDC y 向上 → 图像行向下
   （``row = (1 - (y+1)/2) * h``，与 2D 链的 top-left 行序一致）。
3. **深度约定**：深度测试 ``less``（新片元深度严格小于已存深度才写入）
   ——与 GL 参考的 ``moderngl.DEPTH_TEST`` 默认比较函数一致；遮挡由
   深度测试解决，**不做正反面剔除**（剔除/透明排序按计划属后续批次）。

明示的**未实现项**（后续批次的范围，不在本步硬凑）：近平面裁剪（任意
顶点 ``clip.w <= ε`` 的三角形整体跳过；测试网格须完整位于近平面前）、
顶点规则（top-left rule；共享边像素被两侧三角形先后求值，深度测试 +
固定三角形顺序保证确定性）、法线/运动向量/Cryptomatte 等 AOV。

**Scene3D 映射评估（结论：不接入）**：Scene3D 系节点通过 ``scene`` 端口
携带运行时 ``ScenePayload``（GL VBO/纹理/灯光/骨骼状态，见
``render3d/scene_payload.py``），序列化图参数无法无歧义重建这些对象；
``node_support`` 对 3D 节点报 ``node.metal_3d_unmapped``，3D/AOV 保持
整图回退（`export_bridge` 的 AOV pass 因此显式回退 GL 路径）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from src.shared.gpu.metal import MetalComputeContext, MetalComputeError

from .renderer import MetalCompositeRenderer

# 三角形数上限（MSL 循环边界必须是编译期常量；动态 break 到实际数量）。
MAX_TRIANGLES = 256

# 与 GL 深度缓冲一致的无穷远初值（FLT_MAX）。
FAR_DEPTH = 3.402823466e+38

# 近平面 epsilon：clip.w <= 该值的三角形跳过（本 pass 不做近平面裁剪）。
NEAR_EPSILON = 1e-6

MESH3D_PARAMS_DT = np.dtype([
    # 列主序 MVP（matrix_to_tuple 布局；列 m[c*4+r]）。
    ("mvp", "<f4", 16),
    ("tri_count", "<u4"),
    ("width", "<u4"),
    ("height", "<u4"),
])

MESH3D_CLEAR_MSL = """
#include <metal_stdlib>
using namespace metal;

kernel void comp3d_clear(
        texture2d<float, access::read_write> color [[texture(0)]],
        texture2d<float, access::read_write> depth [[texture(1)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= color.get_width() || gid.y >= color.get_height()) { return; }
    color.write(float4(0.0), gid);
    // FLT_MAX：与 GL 深度清空值 glClearDepth(1.0) 不同——本 pass 用
    // 「远平面之外」的哨兵，深度测试 less 与 d∈[0,1] 语义不变。
    depth.write(3.402823466e+38, gid);
}
"""

MESH3D_RASTER_MSL = """
#include <metal_stdlib>
using namespace metal;

struct Mesh3DParams {
    float mvp[16];      // 列主序（与 GLSL mat4 uniform 同布局）
    uint32_t tri_count;
    uint32_t width;
    uint32_t height;
};

static inline float4 apply_mvp(constant const float* m,
                               packed_float3 pos) {
    // 列主序：clip_r = Σ_c m[c*4+r] * pos_c（等价 GLSL mat4 * vec4）。
    return float4(
        m[0] * pos.x + m[4] * pos.y + m[8] * pos.z + m[12],
        m[1] * pos.x + m[5] * pos.y + m[9] * pos.z + m[13],
        m[2] * pos.x + m[6] * pos.y + m[10] * pos.z + m[14],
        m[3] * pos.x + m[7] * pos.y + m[11] * pos.z + m[15]);
}

static inline float edge(float2 a, float2 b, float2 p) {
    return (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x);
}

kernel void comp3d_rasterize(
        constant Mesh3DParams& p [[buffer(0)]],
        const device packed_float3* positions [[buffer(1)]],
        const device float4* colors [[buffer(2)]],
        texture2d<float, access::read_write> color [[texture(0)]],
        texture2d<float, access::read_write> depth [[texture(1)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    float2 pixel = float2(gid) + 0.5;

    float4 best_color = color.read(gid);
    float best_depth = depth.read(gid).r;

    for (uint32_t t = 0u; t < 256u; ++t) {
        if (t >= p.tri_count) { break; }
        uint32_t i0 = t * 3u;
        float4 c0 = apply_mvp(p.mvp, positions[i0]);
        float4 c1 = apply_mvp(p.mvp, positions[i0 + 1u]);
        float4 c2 = apply_mvp(p.mvp, positions[i0 + 2u]);
        // 本 pass 不做近平面裁剪：任一顶点在相机后方/近平面即整三角跳过。
        if (c0.w <= 1e-6 || c1.w <= 1e-6 || c2.w <= 1e-6) { continue; }
        float2 s0 = float2(c0.x / c0.w, c0.y / c0.w);
        float2 s1 = float2(c1.x / c1.w, c1.y / c1.w);
        float2 s2 = float2(c2.x / c2.w, c2.y / c2.w);
        // 视口：NDC → 像素（y 翻转到图像 top-left 行序）。
        float2 v0 = float2((s0.x + 1.0) * 0.5 * (float)p.width,
                           (1.0 - (s0.y + 1.0) * 0.5) * (float)p.height);
        float2 v1 = float2((s1.x + 1.0) * 0.5 * (float)p.width,
                           (1.0 - (s1.y + 1.0) * 0.5) * (float)p.height);
        float2 v2 = float2((s2.x + 1.0) * 0.5 * (float)p.width,
                           (1.0 - (s2.y + 1.0) * 0.5) * (float)p.height);
        float area = edge(v0, v1, v2);
        if (fabs(area) < 1e-12) { continue; }
        float e0 = edge(v1, v2, pixel);
        float e1 = edge(v2, v0, pixel);
        float e2 = edge(v0, v1, pixel);
        bool any_neg = (e0 < 0.0) || (e1 < 0.0) || (e2 < 0.0);
        bool any_pos = (e0 > 0.0) || (e1 > 0.0) || (e2 > 0.0);
        if (any_neg && any_pos) { continue; }   // 在三角形外
        // 屏幕空间重心坐标（有向面积比）。
        float b0 = e0 / area;
        float b1 = e1 / area;
        float b2 = e2 / area;
        // 深度：z_ndc 屏幕空间线性插值（GPU 光栅器的等价行为），
        // 再做 GL→Metal 边界的 [0,1] 重映射。
        float z0 = c0.z / c0.w;
        float z1 = c1.z / c1.w;
        float z2 = c2.z / c2.w;
        float d = (b0 * z0 + b1 * z1 + b2 * z2) * 0.5 + 0.5;
        if (d < 0.0 || d > 1.0) { continue; }   // 裁剪空间外
        if (!(d < best_depth)) { continue; }    // 深度测试 less（GL 默认）
        // 颜色：透视校正插值（1/w 加权）。
        float p0 = b0 / c0.w;
        float p1 = b1 / c1.w;
        float p2 = b2 / c2.w;
        float sum = p0 + p1 + p2;
        float4 col = (colors[i0] * p0 + colors[i0 + 1u] * p1
                      + colors[i0 + 2u] * p2) / sum;
        best_depth = d;
        best_color = col;
    }
    if (best_depth < 3.402823466e+38) {
        color.write(best_color, gid);
        depth.write(float4(best_depth), gid);
    }
}
"""

MESH3D_KERNEL_SOURCES: Dict[str, str] = {
    "comp3d_clear": MESH3D_CLEAR_MSL,
    "comp3d_rasterize": MESH3D_RASTER_MSL,
}


# ── CPU 参考（同一约定的 numpy 光栅化） ─────────────────────────────────


def rasterize_mesh_cpu(
    positions: np.ndarray,
    colors: np.ndarray,
    mvp_row_major: np.ndarray,
    width: int,
    height: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """与 ``comp3d_rasterize`` 同式的 CPU 光栅化。

    Args:
        positions: (N, 3) float32 顶点（行主序矩阵的行向量乘法）。
        colors: (N, 4) float32 顶点色。
        mvp_row_major: 4×4 行主序 MVP（``P @ V @ M``，render3d 约定）。
        width/height: 目标尺寸。

    Returns:
        ``(color, depth)``：``(h, w, 4)`` float32 与 ``(h, w)`` float32。
    """
    pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    col = np.asarray(colors, dtype=np.float64).reshape(-1, 4)
    mvp = np.asarray(mvp_row_major, dtype=np.float64).reshape(4, 4)
    if pos.shape[0] != col.shape[0] or pos.shape[0] % 3:
        raise ValueError("positions/colors 需为同长 3 的倍数（三角形列表）")
    if pos.shape[0] < 3:
        raise ValueError("网格至少需要一个三角形")
    if pos.shape[0] // 3 > MAX_TRIANGLES:
        raise ValueError(
            f"三角形数 {pos.shape[0] // 3} 超过验证 pass 上限 {MAX_TRIANGLES}")
    w, h = int(width), int(height)

    homogeneous = np.concatenate(
        [pos, np.ones((pos.shape[0], 1), dtype=np.float64)], axis=1)
    # ``math_utils`` 的矩阵是列向量约定（``matrix_to_tuple`` 上传后
    # GLSL mat4 == numpy 矩阵本身），因此 clip = mvp · v（列乘）。
    clip = (mvp @ homogeneous.T).T
    if np.any(clip[:, 3] <= NEAR_EPSILON):
        raise ValueError("CPU 参考要求网格完整位于近平面前（本 pass 不做裁剪）")

    ndc = clip[:, :3] / clip[:, 3:4]
    screen = np.empty((pos.shape[0], 2), dtype=np.float64)
    screen[:, 0] = (ndc[:, 0] + 1.0) * 0.5 * w
    screen[:, 1] = (1.0 - (ndc[:, 1] + 1.0) * 0.5) * h
    depth_ndc = ndc[:, 2]

    color_out = np.zeros((h, w, 4), dtype=np.float32)
    depth_out = np.full((h, w), FAR_DEPTH, dtype=np.float32)

    def _edge(ax: float, ay: float, bx: float, by: float,
              px: float, py: float) -> float:
        return (bx - ax) * (py - ay) - (by - ay) * (px - ax)

    tri_count = pos.shape[0] // 3
    for py in range(h):
        for px in range(w):
            pixel_x, pixel_y = px + 0.5, py + 0.5
            best_depth = FAR_DEPTH
            best_color = None
            for t in range(tri_count):
                i0, i1, i2 = t * 3, t * 3 + 1, t * 3 + 2
                v0, v1, v2 = screen[i0], screen[i1], screen[i2]
                area = _edge(v0[0], v0[1], v1[0], v1[1], v2[0], v2[1])
                if abs(area) < 1e-12:
                    continue
                e0 = _edge(v1[0], v1[1], v2[0], v2[1], pixel_x, pixel_y)
                e1 = _edge(v2[0], v2[1], v0[0], v0[1], pixel_x, pixel_y)
                e2 = _edge(v0[0], v0[1], v1[0], v1[1], pixel_x, pixel_y)
                any_neg = e0 < 0 or e1 < 0 or e2 < 0
                any_pos = e0 > 0 or e1 > 0 or e2 > 0
                if any_neg and any_pos:
                    continue
                b0, b1, b2 = e0 / area, e1 / area, e2 / area
                d = (b0 * depth_ndc[i0] + b1 * depth_ndc[i1]
                     + b2 * depth_ndc[i2]) * 0.5 + 0.5
                if d < 0.0 or d > 1.0:
                    continue
                if not d < best_depth:
                    continue
                w0 = b0 / clip[i0, 3]
                w1 = b1 / clip[i1, 3]
                w2 = b2 / clip[i2, 3]
                total = w0 + w1 + w2
                best_color = (col[i0] * w0 + col[i1] * w1 + col[i2] * w2) / total
                best_depth = d
            if best_color is not None:
                color_out[py, px] = best_color.astype(np.float32)
                depth_out[py, px] = np.float32(best_depth)
    return color_out, depth_out


# ── Metal 执行 ───────────────────────────────────────────────────────────


class MetalMesh3DPass:
    """基本网格 + 固定相机的验证 pass（GPU 真机执行，含 R32F 深度）。

    生命周期：每次 :meth:`run` 独立提交一组命令（clear → rasterize），
    有界调度（复用 :class:`MetalCompositeRenderer` 的调度器/纹理池），
    完成后读回并归还纹理。用于阶段 5.5 的约定验证与后续接线的参考。
    """

    def __init__(self, context: Optional[MetalComputeContext] = None) -> None:
        self._renderer = MetalCompositeRenderer(context=context)

    @property
    def renderer(self) -> MetalCompositeRenderer:
        return self._renderer

    def cleanup(self) -> None:
        self._renderer.cleanup()

    def run(
        self,
        positions: np.ndarray,
        colors: np.ndarray,
        mvp_row_major: np.ndarray,
        width: int,
        height: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """光栅化并读回 ``(color, depth)``（与 CPU 参考同约定）。"""
        import Metal  # pyobjc-framework-Metal

        pos = np.ascontiguousarray(positions, dtype=np.float32).reshape(-1, 3)
        col = np.ascontiguousarray(colors, dtype=np.float32).reshape(-1, 4)
        if pos.shape[0] != col.shape[0] or pos.shape[0] % 3:
            raise ValueError("positions/colors 需为同长 3 的倍数（三角形列表）")
        tri_count = pos.shape[0] // 3
        if tri_count > MAX_TRIANGLES:
            raise ValueError(
                f"三角形数 {tri_count} 超过验证 pass 上限 {MAX_TRIANGLES}")
        w, h = int(width), int(height)
        if w <= 0 or h <= 0:
            raise ValueError(f"非法尺寸 {w}x{h}")

        renderer = self._renderer
        context = renderer._ensure_context()
        pool = renderer.pool
        scheduler = renderer.scheduler
        assert pool is not None and scheduler is not None

        # 列主序 MVP（matrix_to_tuple 同布局：行主序转置展开）。
        mvp = np.asarray(mvp_row_major, dtype=np.float32).reshape(4, 4)
        mvp_column_major = mvp.T.reshape(-1)
        params = np.zeros(1, dtype=MESH3D_PARAMS_DT)
        params["mvp"] = mvp_column_major
        params["tri_count"] = tri_count
        params["width"] = w
        params["height"] = h

        color_entry = pool.acquire(w, h)
        depth_entry = pool.acquire(w, h, pixel_format=Metal.MTLPixelFormatR32Float)
        try:
            sequence = scheduler.try_reserve()
            if sequence is None:
                renderer.poll_completions()
                sequence = scheduler.try_reserve()
            if sequence is None:
                raise MetalComputeError("Metal 提交环已满（back-pressure）")
            try:
                param_buffer = context.create_buffer_from_numpy(params)
                pos_buffer = context.create_buffer_from_numpy(pos)
                col_buffer = context.create_buffer_from_numpy(col)
                context.dispatch(
                    _compile_mesh3d_kernel(context, "comp3d_clear"),
                    textures={0: color_entry.texture, 1: depth_entry.texture},
                    threads=(w, h, 1))
                context.dispatch(
                    _compile_mesh3d_kernel(context, "comp3d_rasterize"),
                    buffers={0: param_buffer, 1: pos_buffer, 2: col_buffer},
                    textures={0: color_entry.texture, 1: depth_entry.texture},
                    threads=(w, h, 1))
            except Exception:
                context.discard_pending()
                scheduler.cancel_reservation(sequence)
                raise
            try:
                submission = scheduler.submit_reserved(
                    sequence, release_callbacks=(), on_complete=None)
            except Exception:
                context.discard_pending()
                raise
            if submission is None:
                context.discard_pending()
                raise MetalComputeError("Metal 3D pass 提交失败")
            # 验证 pass 的同步读回边界（诊断路径语义；帧循环热路径不走
            # 这里——合成链本体已是完成事件驱动的异步发布）。
            submission.command_buffer.waitUntilCompleted()
            scheduler.poll()
            color_raw = context.read_texture(
                color_entry.texture, w * 4, h, dtype=np.float16,
                region_width=w)
            depth_raw = context.read_texture(
                depth_entry.texture, w, h, dtype=np.float32, region_width=w)
        finally:
            pool.finalize_retire(color_entry)
            pool.release_direct(color_entry)
            pool.finalize_retire(depth_entry)
            pool.release_direct(depth_entry)
        color = color_raw.reshape(h, w, 4).astype(np.float32)
        depth = depth_raw.reshape(h, w).copy()
        depth[depth >= FAR_DEPTH] = np.inf
        return color, depth


def _compile_mesh3d_kernel(context: MetalComputeContext, name: str):
    return context.compile_kernel(name, MESH3D_KERNEL_SOURCES[name])


# ── Scene3D 映射评估（纯逻辑，供测试与诊断） ────────────────────────────


def evaluate_3d_mapping(graph_data: Optional[dict]) -> List[str]:
    """图中 3D 节点的「无法无歧义映射」诊断列表（含节点 ID/类型）。

    结论（阶段 5.5）：Scene3D 系节点依赖运行时 ScenePayload（GL 对象），
    序列化参数不能重建，**不接入** Metal 链；本函数把每个参与渲染锥的
    3D 节点显式列出，供预检与测试使用。
    """
    from .node_support import METAL_3D_NODE_TYPES
    if not graph_data:
        return []
    issues: List[str] = []
    for nd in graph_data.get("nodes") or []:
        ntype = str(nd.get("type", ""))
        if ntype in METAL_3D_NODE_TYPES:
            issues.append(
                f"[{nd.get('id', '')}] {ntype}: 依赖运行时 ScenePayload"
                "（GL VBO/纹理/灯光），序列化参数无法无歧义映射 → 整图回退")
    return issues
