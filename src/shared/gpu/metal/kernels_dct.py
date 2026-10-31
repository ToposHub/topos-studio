"""编码侧 forward DCT 的 MSL kernel（速度计划 v2 A1：GPU 化一期）。

与 ``native/topos_codec/src/transform/transform.c`` 的规范 scalar
（两趟整数矩阵乘，ADR-C002 冻结表 kTransformM）**逐位一致**：

- 输入：pad 后 u16 平面（边缘复制，``tc_plane_pad_u16`` 语义），
  stride = cols*8（tight）；level shift ``x' = int(u16) − mid``（mid =
  1<<(bd−1)），12-bit 域内结果必在 int16 宽度；
- 算术：A[y][v] = Σ_x x'[y][x]·M[v][x]，F[u][v] = Σ_y M[u][y]·A[y][v]，
  int（32 位）累加——|A| < 2^18、|F| ≤ 2^25 < 2^31（合法域无溢出，
  MSL/C 同为有符号 32 位补码，整数域累加次序无关 → 位精确）；
- 输出：自然序 F[u*8+v]，块绝对索引 (by*cols+bx)*64（F-cache 布局）；
- 表：kTransformM[64]（int16）经 constant buffer 注入（源 = 库内冻结
  表 ``tc_transform_matrix()``，单一真相源）。

三个数值等价变体（同一 F-cache 布局契约，三方对拍全过）：

- ``forward_dct``：差分机契约版（一线程一块，两趟矩阵乘；线程局部
  int[8][8]×2 在 AMD 上溢出 scratch memory——仅作正确性基准保留）；
- ``forward_dct_rows``：行驻留重构（一线程一行 F，无 barrier，ushort4×2
  向量载入；A 行按需重算，MAC 冗余 4.5× 换零溢出）；
- ``forward_dct_tg``：threadgroup 暂存重构（group=一块 64 线程，X 以
  short 暂存共享内存，锁步广播读）。

差分验收：``tests/shared/gpu/metal/test_metal_kernels_dct.py``
三方对拍（native F-cache 捕获 == numpy 参考 == Metal 输出）。
"""

from __future__ import annotations

METAL_FORWARD_DCT_MSL = """
#include <metal_stdlib>
using namespace metal;

// A1 差分机契约 kernel：块 (bx=gid.x, by=gid.y)。
// params[0]=cols（块列数） params[1]=mid（level shift）。
// 布局/算术与 transform.c scalar 逐位一致（见模块 docstring）。
kernel void forward_dct(device const ushort* src [[buffer(0)]],
                        device int* dst              [[buffer(1)]],
                        device const int* params     [[buffer(2)]],
                        device const short* mtable   [[buffer(3)]],
                        uint2 gid [[thread_position_in_grid]]) {
    const uint cols = (uint)params[0];
    const int mid = params[1];
    if (gid.x >= cols) { return; }
    const uint bx = gid.x, by = gid.y;
    const size_t stride = (size_t)cols * 8u;

    int x[8][8];
    for (int y = 0; y < 8; ++y) {
        const device ushort* row = src + (size_t)(by * 8u + (uint)y) * stride + (size_t)(bx * 8u);
        for (int c = 0; c < 8; ++c) {
            x[y][c] = (int)row[c] - mid;
        }
    }
    int A[8][8];
    for (int y = 0; y < 8; ++y) {
        for (int v = 0; v < 8; ++v) {
            int acc = 0;
            for (int c = 0; c < 8; ++c) {
                acc += x[y][c] * (int)mtable[v * 8 + c];
            }
            A[y][v] = acc;
        }
    }
    device int* out = dst + ((size_t)by * (size_t)cols + (size_t)bx) * 64u;
    for (int u = 0; u < 8; ++u) {
        for (int v = 0; v < 8; ++v) {
            int acc = 0;
            for (int y = 0; y < 8; ++y) {
                acc += (int)mtable[u * 8 + y] * A[y][v];
            }
            out[u * 8 + v] = acc;
        }
    }
}

// 重构 v2（行驻留）：3D 网格 (bx, by, u)，每线程算 F 的一行（8 输出）。
// 无线程局部大数组、无 barrier：X 行以两个 ushort4 向量载入（行基址恒 16B
// 对齐：stride=cols*8 样本=cols*16B、bx*8 样本=bx*16B），A 行按需重算
// （每块 MAC 冗余 4.5×——ALU 便宜、scratch 溢出贵）。全循环固定 8 次
// 可展开，acc/A 常量索引提升进寄存器。
kernel void forward_dct_rows(device const ushort* src [[buffer(0)]],
                             device int* dst              [[buffer(1)]],
                             device const int* params     [[buffer(2)]],
                             device const short* mtable   [[buffer(3)]],
                             uint3 gid [[thread_position_in_grid]]) {
    const uint cols = (uint)params[0];
    const int mid = params[1];
    if (gid.x >= cols) { return; }
    const uint bx = gid.x, by = gid.y;
    const int u = (int)gid.z;
    const size_t stride = (size_t)cols * 8u;

    int acc[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    #pragma unroll 8
    for (int y = 0; y < 8; ++y) {
        const device ushort4* rowp = (const device ushort4*)(
            src + (size_t)(by * 8u + (uint)y) * stride + (size_t)(bx * 8u));
        const ushort4 xra = rowp[0];
        const ushort4 xrb = rowp[1];
        const int w = (int)mtable[u * 8 + y];
        #pragma unroll 8
        for (int v = 0; v < 8; ++v) {
            const int a = ((int)xra[0] - mid) * (int)mtable[v * 8 + 0]
                        + ((int)xra[1] - mid) * (int)mtable[v * 8 + 1]
                        + ((int)xra[2] - mid) * (int)mtable[v * 8 + 2]
                        + ((int)xra[3] - mid) * (int)mtable[v * 8 + 3]
                        + ((int)xrb[0] - mid) * (int)mtable[v * 8 + 4]
                        + ((int)xrb[1] - mid) * (int)mtable[v * 8 + 5]
                        + ((int)xrb[2] - mid) * (int)mtable[v * 8 + 6]
                        + ((int)xrb[3] - mid) * (int)mtable[v * 8 + 7];
            acc[v] += w * a;
        }
    }
    device int* out = dst + ((size_t)by * (size_t)cols + (size_t)bx) * 64u
                    + (size_t)(u * 8);
    #pragma unroll 8
    for (int v = 0; v < 8; ++v) { out[v] = acc[v]; }
}

// 重构 v3（threadgroup 暂存）：2D group 网格 (bx, by)，group=(8,8,1)，
// 64 线程各算一个 F[u][v]；X 移位后以 short 暂存 threadgroup（|x'| <
// 2^12 域内必安全），一次 barrier。所有线程锁步同址读 xs → 广播无 bank
// conflict。单项乘积 ≤ 13·13·2^12 < 2^19，64 项 < 2^25。
kernel void forward_dct_tg(device const ushort* src [[buffer(0)]],
                           device int* dst              [[buffer(1)]],
                           device const int* params     [[buffer(2)]],
                           device const short* mtable   [[buffer(3)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const int mid = params[1];
    if (grp.x >= cols) { return; }   // 整组一致返回：barrier 无分歧
    const uint bx = grp.x, by = grp.y;
    const size_t stride = (size_t)cols * 8u;

    threadgroup short xs[64];
    xs[lid.y * 8u + lid.x] = (short)((int)src[
        (size_t)(by * 8u + lid.y) * stride + (size_t)(bx * 8u + lid.x)] - mid);
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const int u = (int)lid.y, v = (int)lid.x;
    int acc = 0;
    #pragma unroll 8
    for (int y = 0; y < 8; ++y) {
        const int wu = (int)mtable[u * 8 + y];
        #pragma unroll 8
        for (int x = 0; x < 8; ++x) {
            acc += wu * (int)mtable[v * 8 + x] * (int)xs[y * 8 + x];
        }
    }
    dst[((size_t)by * (size_t)cols + (size_t)bx) * 64u + (uint)(u * 8 + v)] = acc;
}

// 重构 v4（预乘积表）：tg 的算术精简——CPU 侧预计算 prod[(u*8+v)*64 +
// y*8+x] = M[u][y]·M[v][x]（|值| ≤ 169，int16），kernel 每项一次
// scalar 广播读 + 一次乘法（替代两次 device 向量读 + 两次乘法）。
// 表走 constant 地址空间（AMD scalar cache，组内同址广播零冲突）。
// staging 以 8 线程 × 两个 ushort4 向量载入。
kernel void forward_dct_prod(device const ushort* src  [[buffer(0)]],
                              device int* dst           [[buffer(1)]],
                              constant const int* params [[buffer(2)]],
                              constant const short* prod [[buffer(3)]],
                              uint2 grp [[threadgroup_position_in_grid]],
                              uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const int mid = params[1];
    if (grp.x >= cols) { return; }
    const uint bx = grp.x, by = grp.y;
    const size_t stride = (size_t)cols * 8u;

    threadgroup short xs[64];
    if (lid.y == 0u) {
        const device ushort4* rp = (const device ushort4*)(
            src + (size_t)(by * 8u + lid.x) * stride + (size_t)(bx * 8u));
        const ushort4 a = rp[0];
        const ushort4 b = rp[1];
        threadgroup short* xw = xs + lid.x * 8u;
        xw[0] = (short)((int)a[0] - mid);
        xw[1] = (short)((int)a[1] - mid);
        xw[2] = (short)((int)a[2] - mid);
        xw[3] = (short)((int)a[3] - mid);
        xw[4] = (short)((int)b[0] - mid);
        xw[5] = (short)((int)b[1] - mid);
        xw[6] = (short)((int)b[2] - mid);
        xw[7] = (short)((int)b[3] - mid);
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    constant const short* mp = prod + (size_t)(lid.y * 8u + lid.x) * 64u;
    int acc = 0;
    #pragma unroll 8
    for (int y = 0; y < 8; ++y) {
        #pragma unroll 8
        for (int x = 0; x < 8; ++x) {
            acc += (int)mp[y * 8 + x] * (int)xs[y * 8 + x];
        }
    }
    dst[((size_t)by * (size_t)cols + (size_t)bx) * 64u
        + (uint)(lid.y * 8u + lid.x)] = acc;
}

// 重构 v5（tg + 4 累加器破链）：与 tg 同结构，但 64 项部分和拆 4 条
// 独立累加链（y%4 分桶，最后合并）——整数加法结合律保证位不变。
// 单链 64 级串行依赖是占用率杀手；破链后指令级并行恢复。
kernel void forward_dct_tg4(device const ushort* src  [[buffer(0)]],
                            device int* dst           [[buffer(1)]],
                            constant const int* params [[buffer(2)]],
                            constant const short* mtable [[buffer(3)]],
                            uint2 grp [[threadgroup_position_in_grid]],
                            uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const int mid = params[1];
    if (grp.x >= cols) { return; }
    const uint bx = grp.x, by = grp.y;
    const size_t stride = (size_t)cols * 8u;

    threadgroup short xs[64];
    xs[lid.y * 8u + lid.x] = (short)((int)src[
        (size_t)(by * 8u + lid.y) * stride + (size_t)(bx * 8u + lid.x)] - mid);
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const int u = (int)lid.y, v = (int)lid.x;
    int accs[4] = {0, 0, 0, 0};
    #pragma unroll 8
    for (int y = 0; y < 8; ++y) {
        const int wu = (int)mtable[u * 8 + y];
        #pragma unroll 8
        for (int x = 0; x < 8; ++x) {
            accs[y & 3] += wu * (int)mtable[v * 8 + x] * (int)xs[y * 8 + x];
        }
    }
    dst[((size_t)by * (size_t)cols + (size_t)bx) * 64u + (uint)(u * 8 + v)]
        = (accs[0] + accs[1]) + (accs[2] + accs[3]);
}

// 重构 v6（一组 8 块）：骨架诊断证明本结构 100% bound 在每组固定成本
// （staging 载入 + barrier + store），计算免费（4× MAC 仅 +3%）——故以
// 组内并行多块摊薄：group=(8,8) 覆盖块行内 8 个连续块，1KB threadgroup
// 暂存，barrier/组调度数 ÷8，每线程 512 MAC（b 外层循环使 xs 读锁步
// 广播）。尾组（cols 非 8 倍数）逐样本守卫 + 块级 store 守卫。
kernel void forward_dct_m8(device const ushort* src   [[buffer(0)]],
                           device int* dst            [[buffer(1)]],
                           constant const int* params [[buffer(2)]],
                           constant const short* mtable [[buffer(3)]],
                           uint2 grp [[threadgroup_position_in_grid]],
                           uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const int mid = params[1];
    if (grp.x >= cols) { return; }
    const uint bx0 = grp.x * 8u;
    const uint by = grp.y;
    const size_t stride = (size_t)cols * 8u;
    threadgroup short xs[512];   // 8 块 × 64：xs[r*64 + b*8 + x]

    {
        const uint r = lid.y;             // 块内样本行
        const uint q = lid.x;             // 行内第 q 个线程，负责跨度样本 q*8..q*8+7
        threadgroup short* xw = xs + (size_t)r * 64u + (size_t)q * 8u;
        if (bx0 + 8u <= cols) {
            const device ushort4* rp = (const device ushort4*)(
                src + (size_t)(by * 8u + r) * stride
                   + (size_t)(bx0 * 8u + q * 8u));
            const ushort4 a = rp[0];      // 跨度样本 q*8+0..3
            const ushort4 b = rp[1];      // 跨度样本 q*8+4..7
            xw[0] = (short)((int)a[0] - mid);
            xw[1] = (short)((int)a[1] - mid);
            xw[2] = (short)((int)a[2] - mid);
            xw[3] = (short)((int)a[3] - mid);
            xw[4] = (short)((int)b[0] - mid);
            xw[5] = (short)((int)b[1] - mid);
            xw[6] = (short)((int)b[2] - mid);
            xw[7] = (short)((int)b[3] - mid);
        } else {
            #pragma unroll 8
            for (uint k = 0; k < 8u; ++k) {
                const uint sx = bx0 * 8u + q * 8u + k;
                xw[k] = sx < cols * 8u
                    ? (short)((int)src[(size_t)(by * 8u + r) * stride + sx] - mid)
                    : (short)0;
            }
        }
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const int u = (int)lid.y, v = (int)lid.x;
    int accs[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    #pragma unroll 8
    for (int b = 0; b < 8; ++b) {
        #pragma unroll 8
        for (int y = 0; y < 8; ++y) {
            const int wu = (int)mtable[u * 8 + y];
            #pragma unroll 8
            for (int x = 0; x < 8; ++x) {
                accs[b] += wu * (int)mtable[v * 8 + x]
                         * (int)xs[(size_t)y * 64u + (size_t)b * 8u + x];
            }
        }
    }
    #pragma unroll 8
    for (int b = 0; b < 8; ++b) {
        if (bx0 + (uint)b < cols) {
            dst[((size_t)by * (size_t)cols + (size_t)(bx0 + (uint)b)) * 64u
                + (uint)(u * 8 + v)] = accs[b];
        }
    }
}
"""
