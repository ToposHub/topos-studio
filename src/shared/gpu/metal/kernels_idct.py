"""解码侧反量化 + IDCT + 重建的 MSL kernel（速度计划 v2 A2：GPU 化一期）。

与 ``native/topos_codec`` 的 sink 单一定义点（color_store.h +
transform.c 规范 scalar）**逐位一致**——数值契约：

- 反量化：``F'[i] = clamp(q[i]·Q[i], ±2^25)``（int64 中转，合法码流不
  触及钳位；Q 为解码端每 slice 建表的反量化步长，随 ``tc_dev_qcap_*
  `` 捕获交付）；
- 逆变换：``G = F' ⊙ W``（W 含 1/(E_uE_v) 归一，|G| < 2^39）→
  ``x̂[y][x] = round((Σ_u Σ_v M[u][y]·M[v][x]·G[u][v]) / 2^32)``——单一
  最终舍入（符号拆分四舍五入，见 tc_round_shift32），整数乘加在 int64
  域（|acc| < 2^53）内可任意重排 → GPU 两遍/平铺任意结构位精确；
- 重建：``v = clamp(x̂ + mid, 0, max)`` 写 u16 平面（可见区 clip，
  stride 按元素计）。

A2 稀疏系数契约（CPU rANS 符号环 → GPU，``inverse_dct_sp``/``w1``）：

- ``dc[nblocks]`` int32：每块 DC（natural 0，DC 预测已折叠——与 sink
  消费的 q 同源）；
- ``pair_off[nblocks+1]`` uint32：平面级 CSR（块行主序偏移）；
- ``pairs[npairs]`` uint64：``(uint64)(int64)level << 6 | nat``（nat =
  natural 系数号 1..63，level = 量化值 int32；MSL 侧 ``(long)pk >> 6``
  算术右移恢复符号）；
- ``qtables[nslices*64]`` uint32 + ``row_slice[rows]`` uint32：slice =
  块行带（同一块行恒同 slice），Q 表按 slice 拼接。

对 4K 444 qp58 稳态（nnz 17%，u 行占用 ~3/8）：稀疏上传 ≈ 12MB/平面
（pairs 8B/对为主，1.38M 对）vs 稠密 q 33MB/平面——上载量是 A1 F-回读
墙不适用于解码方向的直接原因：解码方向只有上载、无回读，且可与 CPU
符号环重叠。

数值等价变体（同一平面输出契约，三方对拍全过；4K VRAM 实测墙钟见
落档 topos_a2_decode_gpu_2026-09-12.md）：

- ``inverse_dct_px``：差分机契约版（一线程一像素，64 项 int64 直和，
  无 threadgroup 状态；仅作正确性基准）；
- ``inverse_dct_m8``：稠密两遍版（一组 8 块，group=(8,8,8)）；
- ``inverse_dct_sp``：稀疏契约版（m8 同构 + CSR 散射 + 行掩码跳过
  ——空 u 行的 B 项与像素项恒为 0，位不变）；
- ``inverse_dct_w1``：稀疏契约版（一块一组，64 线程 = RDNA2 单
  wave，barrier 组内免调度；4K VRAM 实测 0.24~0.28ms/平面，为空
  kernel 调度/存储地板 0.163ms 之上的 ~0.1ms 计算——最快）；
- ``inverse_dct_ws``：**生产版**——w1 同构，CSR 为 native 解码 worker
  直接发射的 slice 分段布局（``tc_dev_set_qcap_sparse``；段按最坏
  63 对/块预留，实际对数在 off 段尾——免平面级 post-join 组装）。

差分验收：``tests/shared/gpu/metal/test_metal_kernels_idct.py``
三方对拍（native 解码平面 == numpy 参考 == Metal 输出，真实包捕获 +
合成 q 双源）。
"""

from __future__ import annotations

METAL_INVERSE_DCT_MSL = """
#include <metal_stdlib>
using namespace metal;

// round(v / 2^32)，符号拆分四舍五入（transform.c tc_round_shift32）。
static inline long idct_round_shift32(long v)
{
    if (v >= 0L) { return (v + 0x80000000L) >> 32; }
    return -((-v + 0x80000000L) >> 32);
}

static inline long idct_dequant(int q, uint step, uint w)
{
    long f = (long)q * (long)step;                    // ±2^25 域内钳位
    if (f > 0x2000000L) { f = 0x2000000L; }
    if (f < -0x2000000L) { f = -0x2000000L; }
    return f * (long)w;                               // |G| < 2^39
}

// params: [cols, rows, vis_w, vis_h, mid, max, stride]（stride 以 u16 元素计）。

// 差分机契约版：一线程一像素（grid = coded 像素网格），64 项 int64 直和。
// q 为平面级稠密 [rows*cols][64]（natural 序，块行主序）。
kernel void inverse_dct_px(device const int* q        [[buffer(0)]],
                           device ushort* out         [[buffer(1)]],
                           constant const int* params [[buffer(2)]],
                           constant const short* mtable [[buffer(3)]],
                           constant const uint* wtable  [[buffer(4)]],
                           constant const uint* qtables [[buffer(5)]],
                           device const uint* row_slice [[buffer(6)]],
                           uint2 gid [[thread_position_in_grid]]) {
    const uint cols = (uint)params[0];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    if (gid.x >= vis_w || gid.y >= vis_h) { return; }

    const uint bx = gid.x >> 3u;
    const uint by = gid.y >> 3u;
    const uint x = gid.x & 7u;
    const uint y = gid.y & 7u;
    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    device const int* qb = q + ((size_t)by * (size_t)cols + bx) * 64u;

    long acc = 0L;
    #pragma unroll 8
    for (int u = 0; u < 8; ++u) {
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            const uint i = (uint)(u * 8 + v);
            const long g = idct_dequant(qb[i], Q[i], wtable[i]);
            acc += (long)mtable[u * 8 + y] * (long)mtable[v * 8 + x] * g;
        }
    }
    int pix = (int)idct_round_shift32(acc) + mid;
    if (pix < 0) { pix = 0; }
    else if (pix > maxv) { pix = maxv; }
    out[(size_t)gid.y * stride + gid.x] = (ushort)pix;
}

// 稠密两遍版：一组 8 块（group=(8,8,8) = 512 线程），G[512]（一线程一
// 系数反量化）→ B[u][x] = Σ_v G[u][v]·M[v][x] → 像素 Σ_u M[u][y]·B[u][x]。
// 行掩码：B 相先扫本 u 行 G 占用（x==0 线程置位 rmu；清零与置位分居
// barrier 两侧无竞态），空行 B=0 且 MAC 跳过、像素相跳读——G 全零 ⟹
// 各项恒 0，位不变（4K 稳态 u 行占用 ~3/8）。
kernel void inverse_dct_m8(device const int* q        [[buffer(0)]],
                           device ushort* out         [[buffer(1)]],
                           constant const int* params [[buffer(2)]],
                           constant const short* mtable [[buffer(3)]],
                           constant const uint* wtable  [[buffer(4)]],
                           constant const uint* qtables [[buffer(5)]],
                           device const uint* row_slice [[buffer(6)]],
                           uint3 grp [[threadgroup_position_in_grid]],
                           uint3 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint bx0 = grp.x * 8u;
    const uint by = grp.y;
    if (bx0 >= cols || by >= rows) { return; }   // 整组一致返回：barrier 无分歧

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[512];
    threadgroup long B[512];
    threadgroup atomic_uint rmu[8];

    {   // G 暂存：tid → (组内块 b, 系数 i)；越列尾块置 0（store 守卫丢弃）
        const uint tid = lid.x + lid.y * 8u + lid.z * 64u;
        const uint bx = bx0 + (tid >> 6);
        const uint i = tid & 63u;
        long g = 0L;
        if (bx < cols) {
            g = idct_dequant(q[((size_t)by * (size_t)cols + bx) * 64u + i],
                             Q[i], wtable[i]);
        }
        G[tid] = g;
        if (tid < 8u) {
            atomic_store_explicit(rmu + tid, 0u, memory_order_relaxed);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(rmu + lid.z, 1u << lid.y,
                                     memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.z * 64u + lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(rmu + lid.z, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[lid.z * 64u + (uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint bx = bx0 + lid.z;
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (bx < cols && px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// 稀疏契约生产版：m8 同构，G 暂存改读 CSR 对。组内 9 个 CSR 边界 loff
// 以行尾钳位（bx0+j ≥ cols 的块归到行尾偏移 → 0 对），pair 散射按
// k ∈ [loff[b], loff[b+1]) 上界搜索定位组内块号。
kernel void inverse_dct_sp(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* pair_off [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],
                           device const uint* row_slice [[buffer(8)]],
                           uint3 grp [[threadgroup_position_in_grid]],
                           uint3 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint bx0 = grp.x * 8u;
    const uint by = grp.y;
    if (bx0 >= cols || by >= rows) { return; }

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[512];
    threadgroup long B[512];
    threadgroup uint loff[9];
    threadgroup atomic_uint rmu[8];

    const uint tid = lid.x + lid.y * 8u + lid.z * 64u;
    {   // 清零 + DC + loff + 掩码清零（置位统一在 B 相，跨 barrier 无竞态）
        const uint b = tid >> 6;
        long g = 0L;
        if ((tid & 63u) == 0u && bx0 + b < cols) {
            g = idct_dequant(dc[(size_t)by * (size_t)cols + bx0 + b],
                             Q[0], wtable[0]);
        }
        G[tid] = g;
        if (tid < 9u) {
            const uint bj = bx0 + tid;              // tid 即组内块号 j
            const uint gidx = by * cols + (bj < cols ? bj : cols);
            loff[tid] = pair_off[gidx];
        }
        if (tid < 8u) {
            atomic_store_explicit(rmu + tid, 0u, memory_order_relaxed);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {   // pair 散射（组内全域 [p0, p1) 协作，512 路跨步）
        const uint p0 = loff[0];
        const uint p1 = loff[8];
        for (uint k = tid; k < p1 - p0; k += 512u) {
            const ulong pk = pairs[(size_t)p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;       // 算术右移恢复符号
            uint b = 0u;
            #pragma unroll 7
            for (uint j = 1u; j < 8u; ++j) {
                if (k >= loff[j] - p0) { b = j; }
            }
            G[(size_t)b * 64u + nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(rmu + lid.z, 1u << lid.y,
                                     memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.z * 64u + lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(rmu + lid.z, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[lid.z * 64u + (uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint bx = bx0 + lid.z;
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (bx < cols && px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// w1 对照变体：一块一组（group=(8,8,1) = 64 线程，RDNA2 恰单 wave），
// 稀疏契约同 sp——barrier 退化为组内等待，量测大组（512 线程 ×3
// barrier）的调度开销是否为主导。
kernel void inverse_dct_w1(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* pair_off [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],
                           device const uint* row_slice [[buffer(8)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint bx = grp.x;
    const uint by = grp.y;
    if (bx >= cols || by >= rows) { return; }

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[64];
    threadgroup long B[64];
    threadgroup atomic_uint rmu;

    const uint tid = lid.x + lid.y * 8u;
    const size_t gidx = (size_t)by * (size_t)cols + bx;
    {
        long g = 0L;
        if (tid == 0u) {
            g = idct_dequant(dc[gidx], Q[0], wtable[0]);
            atomic_store_explicit(&rmu, 0u, memory_order_relaxed);
        }
        G[tid] = g;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {   // pair 散射（块内 [p0, p1)，64 路跨步）
        const uint p0 = pair_off[gidx];
        const uint p1 = pair_off[gidx + 1u];
        for (uint k = tid; k < p1 - p0; k += 64u) {
            const ulong pk = pairs[(size_t)p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;
            G[nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(&rmu, 1u << lid.y, memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(&rmu, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[(uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// ws 生产变体：w1 同构，CSR 为 C 侧解码 worker 直接发射的 slice 分段
// 布局（免平面级 post-join 组装——slice 并行解码时对数未知，段按最坏
// 63 对/块预留，实际对数在 off 段尾）。段基址由辅助数组给出：
//   sblk0[s] = slice 带首全局块号（= block_y0*cols）；spbase[s] = 该
//   slice pairs 段基；off 按 off_stride 定长分段（每 slice blocks+1，
//   Python 侧装配时补齐）。slice 均为 plane 局部编号（与 row_slice 同域）。
kernel void inverse_dct_ws(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* off    [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],
                           device const uint* row_slice [[buffer(8)]],
                           device const uint* sblk0    [[buffer(9)]],
                           device const uint* spbase   [[buffer(10)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint off_stride = (uint)params[7];
    const uint bx = grp.x;
    const uint by = grp.y;
    if (bx >= cols || by >= rows) { return; }

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[64];
    threadgroup long B[64];
    threadgroup atomic_uint rmu;

    const uint tid = lid.x + lid.y * 8u;
    const size_t gidx = (size_t)by * (size_t)cols + bx;
    {
        long g = 0L;
        if (tid == 0u) {
            g = idct_dequant(dc[gidx], Q[0], wtable[0]);
            atomic_store_explicit(&rmu, 0u, memory_order_relaxed);
        }
        G[tid] = g;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        const uint s = row_slice[by];
        const uint bi = (uint)gidx - sblk0[s];
        const uint ob = s * off_stride;
        const uint p0 = off[ob + bi];
        const uint p1 = off[ob + bi + 1u];
        for (uint k = tid; k < p1 - p0; k += 64u) {
            const ulong pk = pairs[(size_t)spbase[s] + p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;
            G[nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(&rmu, 1u << lid.y, memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(&rmu, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[(uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// A2-4 生产直发版（wsd）：ws 同构，off 改为 C 发射的原生连续布局——
// 每 slice [blocks+1] 段依 reserve 序拼接（无 per-plane 定长 stride 重排），
// 段基址 soff[s] = Σ_{s'<s}(blocks_{s'}+1) 静态可预建；配套
// tc_dev_qcap_sp_set_targets 的 worst-case pairs 段（spbase 同为确定性
// 前缀）。buffer(11) = soff[nslices]；params[7]（off_stride）忽略。
kernel void inverse_dct_wsd(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* off    [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],
                           device const uint* row_slice [[buffer(8)]],
                           device const uint* sblk0    [[buffer(9)]],
                           device const uint* spbase   [[buffer(10)]],
                           device const uint* soff     [[buffer(11)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint dc0 = (uint)params[7];
    const uint bx = grp.x;
    const uint by = grp.y;
    if (bx >= cols || by >= rows) { return; }

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[64];
    threadgroup long B[64];
    threadgroup atomic_uint rmu;

    const uint tid = lid.x + lid.y * 8u;
    const size_t gidx = (size_t)by * (size_t)cols + bx;
    {
        long g = 0L;
        if (tid == 0u) {
            g = idct_dequant(dc[dc0 + gidx], Q[0], wtable[0]);
            atomic_store_explicit(&rmu, 0u, memory_order_relaxed);
        }
        G[tid] = g;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        const uint s = row_slice[by];
        const uint bi = (uint)gidx - sblk0[s];
        const uint ob = soff[s];
        const uint p0 = off[ob + bi];
        const uint p1 = off[ob + bi + 1u];
        for (uint k = tid; k < p1 - p0; k += 64u) {
            const ulong pk = pairs[(size_t)spbase[s] + p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;
            G[nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(&rmu, 1u << lid.y, memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(&rmu, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[(uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}
"""

METAL_INVERSE_DCT_WSD_V8_MSL = """
#include <metal_stdlib>
using namespace metal;

// round(v / 2^32)，符号拆分四舍五入（transform.c tc_round_shift32）。
static inline long idct_round_shift32(long v)
{
    if (v >= 0L) { return (v + 0x80000000L) >> 32; }
    return -((-v + 0x80000000L) >> 32);
}

static inline long idct_dequant(int q, uint step, uint w)
{
    long f = (long)q * (long)step;                    // ±2^25 域内钳位
    if (f > 0x2000000L) { f = 0x2000000L; }
    if (f < -0x2000000L) { f = -0x2000000L; }
    return f * (long)w;                               // |G| < 2^39
}

// params: [cols, rows, vis_w, vis_h, mid, max, stride]（stride 以 u16 元素计）。

// 差分机契约版：一线程一像素（grid = coded 像素网格），64 项 int64 直和。
// q 为平面级稠密 [rows*cols][64]（natural 序，块行主序）。
kernel void inverse_dct_px(device const int* q        [[buffer(0)]],
                           device ushort* out         [[buffer(1)]],
                           constant const int* params [[buffer(2)]],
                           constant const short* mtable [[buffer(3)]],
                           constant const uint* wtable  [[buffer(4)]],
                           constant const uint* qtables [[buffer(5)]],
                           device const uint* row_slice [[buffer(6)]],
                           uint2 gid [[thread_position_in_grid]]) {
    const uint cols = (uint)params[0];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    if (gid.x >= vis_w || gid.y >= vis_h) { return; }

    const uint bx = gid.x >> 3u;
    const uint by = gid.y >> 3u;
    const uint x = gid.x & 7u;
    const uint y = gid.y & 7u;
    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    device const int* qb = q + ((size_t)by * (size_t)cols + bx) * 64u;

    long acc = 0L;
    #pragma unroll 8
    for (int u = 0; u < 8; ++u) {
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            const uint i = (uint)(u * 8 + v);
            const long g = idct_dequant(qb[i], Q[i], wtable[i]);
            acc += (long)mtable[u * 8 + y] * (long)mtable[v * 8 + x] * g;
        }
    }
    int pix = (int)idct_round_shift32(acc) + mid;
    if (pix < 0) { pix = 0; }
    else if (pix > maxv) { pix = maxv; }
    out[(size_t)gid.y * stride + gid.x] = (ushort)pix;
}

// 稠密两遍版：一组 8 块（group=(8,8,8) = 512 线程），G[512]（一线程一
// 系数反量化）→ B[u][x] = Σ_v G[u][v]·M[v][x] → 像素 Σ_u M[u][y]·B[u][x]。
// 行掩码：B 相先扫本 u 行 G 占用（x==0 线程置位 rmu；清零与置位分居
// barrier 两侧无竞态），空行 B=0 且 MAC 跳过、像素相跳读——G 全零 ⟹
// 各项恒 0，位不变（4K 稳态 u 行占用 ~3/8）。
kernel void inverse_dct_m8(device const int* q        [[buffer(0)]],
                           device ushort* out         [[buffer(1)]],
                           constant const int* params [[buffer(2)]],
                           constant const short* mtable [[buffer(3)]],
                           constant const uint* wtable  [[buffer(4)]],
                           constant const uint* qtables [[buffer(5)]],
                           device const uint* row_slice [[buffer(6)]],
                           uint3 grp [[threadgroup_position_in_grid]],
                           uint3 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint bx0 = grp.x * 8u;
    const uint by = grp.y;
    if (bx0 >= cols || by >= rows) { return; }   // 整组一致返回：barrier 无分歧

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[512];
    threadgroup long B[512];
    threadgroup atomic_uint rmu[8];

    {   // G 暂存：tid → (组内块 b, 系数 i)；越列尾块置 0（store 守卫丢弃）
        const uint tid = lid.x + lid.y * 8u + lid.z * 64u;
        const uint bx = bx0 + (tid >> 6);
        const uint i = tid & 63u;
        long g = 0L;
        if (bx < cols) {
            g = idct_dequant(q[((size_t)by * (size_t)cols + bx) * 64u + i],
                             Q[i], wtable[i]);
        }
        G[tid] = g;
        if (tid < 8u) {
            atomic_store_explicit(rmu + tid, 0u, memory_order_relaxed);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(rmu + lid.z, 1u << lid.y,
                                     memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.z * 64u + lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(rmu + lid.z, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[lid.z * 64u + (uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint bx = bx0 + lid.z;
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (bx < cols && px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// 稀疏契约生产版：m8 同构，G 暂存改读 CSR 对。组内 9 个 CSR 边界 loff
// 以行尾钳位（bx0+j ≥ cols 的块归到行尾偏移 → 0 对），pair 散射按
// k ∈ [loff[b], loff[b+1]) 上界搜索定位组内块号。
kernel void inverse_dct_sp(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* pair_off [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],
                           device const uint* row_slice [[buffer(8)]],
                           uint3 grp [[threadgroup_position_in_grid]],
                           uint3 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint bx0 = grp.x * 8u;
    const uint by = grp.y;
    if (bx0 >= cols || by >= rows) { return; }

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[512];
    threadgroup long B[512];
    threadgroup uint loff[9];
    threadgroup atomic_uint rmu[8];

    const uint tid = lid.x + lid.y * 8u + lid.z * 64u;
    {   // 清零 + DC + loff + 掩码清零（置位统一在 B 相，跨 barrier 无竞态）
        const uint b = tid >> 6;
        long g = 0L;
        if ((tid & 63u) == 0u && bx0 + b < cols) {
            g = idct_dequant(dc[(size_t)by * (size_t)cols + bx0 + b],
                             Q[0], wtable[0]);
        }
        G[tid] = g;
        if (tid < 9u) {
            const uint bj = bx0 + tid;              // tid 即组内块号 j
            const uint gidx = by * cols + (bj < cols ? bj : cols);
            loff[tid] = pair_off[gidx];
        }
        if (tid < 8u) {
            atomic_store_explicit(rmu + tid, 0u, memory_order_relaxed);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {   // pair 散射（组内全域 [p0, p1) 协作，512 路跨步）
        const uint p0 = loff[0];
        const uint p1 = loff[8];
        for (uint k = tid; k < p1 - p0; k += 512u) {
            const ulong pk = pairs[(size_t)p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;       // 算术右移恢复符号
            uint b = 0u;
            #pragma unroll 7
            for (uint j = 1u; j < 8u; ++j) {
                if (k >= loff[j] - p0) { b = j; }
            }
            G[(size_t)b * 64u + nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(rmu + lid.z, 1u << lid.y,
                                     memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[(size_t)lid.z * 64u + lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.z * 64u + lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(rmu + lid.z, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[lid.z * 64u + (uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint bx = bx0 + lid.z;
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (bx < cols && px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// w1 对照变体：一块一组（group=(8,8,1) = 64 线程，RDNA2 恰单 wave），
// 稀疏契约同 sp——barrier 退化为组内等待，量测大组（512 线程 ×3
// barrier）的调度开销是否为主导。
kernel void inverse_dct_w1(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* pair_off [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],
                           device const uint* row_slice [[buffer(8)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint bx = grp.x;
    const uint by = grp.y;
    if (bx >= cols || by >= rows) { return; }

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[64];
    threadgroup long B[64];
    threadgroup atomic_uint rmu;

    const uint tid = lid.x + lid.y * 8u;
    const size_t gidx = (size_t)by * (size_t)cols + bx;
    {
        long g = 0L;
        if (tid == 0u) {
            g = idct_dequant(dc[gidx], Q[0], wtable[0]);
            atomic_store_explicit(&rmu, 0u, memory_order_relaxed);
        }
        G[tid] = g;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {   // pair 散射（块内 [p0, p1)，64 路跨步）
        const uint p0 = pair_off[gidx];
        const uint p1 = pair_off[gidx + 1u];
        for (uint k = tid; k < p1 - p0; k += 64u) {
            const ulong pk = pairs[(size_t)p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;
            G[nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(&rmu, 1u << lid.y, memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(&rmu, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[(uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// ws 生产变体：w1 同构，CSR 为 C 侧解码 worker 直接发射的 slice 分段
// 布局（免平面级 post-join 组装——slice 并行解码时对数未知，段按最坏
// 63 对/块预留，实际对数在 off 段尾）。段基址由辅助数组给出：
//   sblk0[s] = slice 带首全局块号（= block_y0*cols）；spbase[s] = 该
//   slice pairs 段基；off 按 off_stride 定长分段（每 slice blocks+1，
//   Python 侧装配时补齐）。slice 均为 plane 局部编号（与 row_slice 同域）。
kernel void inverse_dct_ws(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* off    [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],
                           device const uint* row_slice [[buffer(8)]],
                           device const uint* sblk0    [[buffer(9)]],
                           device const uint* spbase   [[buffer(10)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint off_stride = (uint)params[7];
    const uint bx = grp.x;
    const uint by = grp.y;
    if (bx >= cols || by >= rows) { return; }

    constant const uint* Q = qtables + (size_t)row_slice[by] * 64u;
    threadgroup long G[64];
    threadgroup long B[64];
    threadgroup atomic_uint rmu;

    const uint tid = lid.x + lid.y * 8u;
    const size_t gidx = (size_t)by * (size_t)cols + bx;
    {
        long g = 0L;
        if (tid == 0u) {
            g = idct_dequant(dc[gidx], Q[0], wtable[0]);
            atomic_store_explicit(&rmu, 0u, memory_order_relaxed);
        }
        G[tid] = g;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        const uint s = row_slice[by];
        const uint bi = (uint)gidx - sblk0[s];
        const uint ob = s * off_stride;
        const uint p0 = off[ob + bi];
        const uint p1 = off[ob + bi + 1u];
        for (uint k = tid; k < p1 - p0; k += 64u) {
            const ulong pk = pairs[(size_t)spbase[s] + p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;
            G[nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(&rmu, 1u << lid.y, memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(&rmu, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[(uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}

// A2-4 生产直发版（wsd）：ws 同构，off 改为 C 发射的原生连续布局——
// 每 slice [blocks+1] 段依 reserve 序拼接（无 per-plane 定长 stride 重排），
// 段基址 soff[s] = Σ_{s'<s}(blocks_{s'}+1) 静态可预建；配套
// tc_dev_qcap_sp_set_targets 的 worst-case pairs 段（spbase 同为确定性
// 前缀）。buffer(11) = soff[nslices]；params[7]（off_stride）忽略。

kernel void inverse_dct_wsd_v8(device const int* dc      [[buffer(0)]],
                           device const ulong* pairs [[buffer(1)]],
                           device const uint* off    [[buffer(2)]],
                           device ushort* out        [[buffer(3)]],
                           constant const int* params [[buffer(4)]],
                           constant const short* mtable [[buffer(5)]],
                           constant const uint* wtable  [[buffer(6)]],
                           constant const uint* qtables [[buffer(7)]],   /* V8：每段一行 64 */
                           device const uint* blkseg   [[buffer(8)]],   /* V8：per-block 段索引 */
                           device const uint* sblk0    [[buffer(9)]],
                           device const uint* spbase   [[buffer(10)]],
                           device const uint* soff     [[buffer(11)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint rows = (uint)params[1];
    const uint vis_w = (uint)params[2];
    const uint vis_h = (uint)params[3];
    const int mid = params[4];
    const int maxv = params[5];
    const uint stride = (uint)params[6];
    const uint dc0 = (uint)params[7];
    const uint bx = grp.x;
    const uint by = grp.y;
    if (bx >= cols || by >= rows) { return; }

    const size_t gidx = (size_t)by * (size_t)cols + bx;
    constant const uint* Q = qtables + (size_t)blkseg[gidx] * 64u;
    threadgroup long G[64];
    threadgroup long B[64];
    threadgroup atomic_uint rmu;

    const uint tid = lid.x + lid.y * 8u;
    {
        long g = 0L;
        if (tid == 0u) {
            g = idct_dequant(dc[dc0 + gidx], Q[0], wtable[0]);
            atomic_store_explicit(&rmu, 0u, memory_order_relaxed);
        }
        G[tid] = g;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        const uint s = blkseg[gidx];
        const uint bi = (uint)gidx - sblk0[s];
        const uint ob = soff[s];
        const uint p0 = off[ob + bi];
        const uint p1 = off[ob + bi + 1u];
        for (uint k = tid; k < p1 - p0; k += 64u) {
            const ulong pk = pairs[(size_t)spbase[s] + p0 + k];
            const uint nat = (uint)(pk & 63u);
            const long lvl = ((long)pk) >> 6;
            G[nat] = idct_dequant((int)lvl, Q[nat], wtable[nat]);
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    {
        bool any = false;
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            any = any || G[lid.y * 8u + (uint)v] != 0L;
        }
        if (lid.x == 0u && any) {
            atomic_fetch_or_explicit(&rmu, 1u << lid.y, memory_order_relaxed);
        }
        long acc = 0L;
        if (any) {
            #pragma unroll 8
            for (int v = 0; v < 8; ++v) {
                acc += G[lid.y * 8u + (uint)v]
                     * (long)mtable[(uint)v * 8u + lid.x];
            }
        }
        B[lid.y * 8u + lid.x] = acc;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint rm_u = atomic_load_explicit(&rmu, memory_order_relaxed);
    {
        long acc = 0L;
        #pragma unroll 8
        for (int u = 0; u < 8; ++u) {
            if ((rm_u & (1u << u)) != 0u) {
                acc += (long)mtable[(uint)u * 8u + lid.y]
                     * B[(uint)u * 8u + lid.x];
            }
        }
        int pix = (int)idct_round_shift32(acc) + mid;
        if (pix < 0) { pix = 0; }
        else if (pix > maxv) { pix = maxv; }
        const uint px = bx * 8u + lid.x;
        const uint py = by * 8u + lid.y;
        if (px < vis_w && py < vis_h) {
            out[(size_t)py * stride + px] = (ushort)pix;
        }
    }
}
"""
