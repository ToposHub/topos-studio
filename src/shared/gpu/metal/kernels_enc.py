"""A-enc（速度计划 A 轴编码 GPU 化）批 1：量化 → token 化 MSL kernel。

与 ``fill_color_band_from_f`` 的 token 生产规范逐位一致（量化 =
``quant.c: tc_quant_block_ctx``；token = fill 的 zigzag 掩码跳扫对环）：

- 输入 F：自然序 i32（块绝对索引 ×64 布局——A1 ``forward_dct_m8``
  kernel 的输出布局，其位精确已由 ``test_metal_kernels_dct`` 钉死）；
- 量化：``v = clamp(F, ±2^25)``；``num = |v| + half[i]``；
  ``num_eff = num > dz[i] ? num − dz[i] : 0``；``qmag = num_eff / Q[i]``
  ——除法以 C 侧 fastdiv 孪生 magic 精确复刻（AVX2 内核同式：
  ``x=ne·m1; y=ne·m0; q=(x>>25)+(((x&mask)<<26)+y)>>51``，纯 64 位整数
  位精确）。half/dz/fd 孪生表自 C 侧 ``tc_quant_ctx`` 捕获上传（单一
  真相源，免复刻 qp 钳位链）；
- 输出：``dc_raw[nblocks] i32``（= q[0]，DC 预测折叠留给 CPU——O(blocks)
  串行便宜）；``npair[nblocks] u16``；``pairs`` 槽 = gidx×63 最坏预留，
  块内 zigzag 升序，u64 = ``(uint64)lvl_m << 6 | run``（lvl_m < 2^26、
  run ≤ 62）；
- zigzag 表 ``kTcZigzag``（pos→natural）经 constant buffer 注入
  （``tc_dev_zigzag``）；
- 布局：**组=块**（grid (cols, 带行) × group (8,8)）——相 A 64 lane
  并行量化（F 组内 256B 连续载入 + threadgroup 暂存），相 B lane 0
  zigzag 扫描发射（散装载入版本实测 26ms/平面，此布局为其重构）。

差分验收：``tests/shared/gpu/metal/test_metal_kernels_enc.py``
三方对拍（C token 捕获 == numpy 参考 == GPU 输出）。
"""

from __future__ import annotations

METAL_QUANT_TOKEN_MSL = """
#include <metal_stdlib>
using namespace metal;

// A-enc 批 1 契约 kernel：块 (bx=gid.x, by=y0+gid.y)。
// params[0]=cols（块列数） params[1]=y0（带首块行——表按带分派）。
// 逐块单线程：63 次量化 + 非零发射（块级并行 = 数十万块；除法为 u32
// 精确除，与 C fastdiv 同余）。
kernel void quant_token(device const int* Fp [[buffer(0)]],
                        device int* dc_raw       [[buffer(1)]],
                        device ushort* npair     [[buffer(2)]],
                        device ulong* pairs      [[buffer(3)]],
                        constant const int* params [[buffer(4)]],
                        constant const uint* halfq [[buffer(5)]],
                        constant const uint* dz   [[buffer(6)]],
                        constant const uint* fdm1 [[buffer(7)]],
                        constant const uint* fdm0 [[buffer(8)]],
                        constant const uchar* zz  [[buffer(9)]],
                        uint2 grp [[threadgroup_position_in_grid]],
                        uint2 lid [[thread_position_in_threadgroup]]) {
    const uint cols = (uint)params[0];
    const uint y0 = (uint)params[1];
    if (grp.x >= cols) { return; }   /* 整组一致返回：barrier 无分歧 */
    const size_t gidx = (size_t)(y0 + grp.y) * (size_t)cols + (size_t)grp.x;
    const uint nat = lid.y * 8u + lid.x;   /* lane = 自然序系数 */

    /* 相 A：64 lane 各量化一系数（F 组内 256B 连续；qn 暂存 threadgroup） */
    threadgroup int qn[64];
    {
        int v = Fp[gidx * 64u + nat];
        if (v > (1 << 25)) { v = 1 << 25; }
        else if (v < -(1 << 25)) { v = -(1 << 25); }
        const uint mag = (uint)(v < 0 ? -v : v);
        const uint num = mag + halfq[nat];
        const uint ne = num > dz[nat] ? num - dz[nat] : 0u;
        const ulong x = (ulong)ne * (ulong)fdm1[nat];
        const ulong y = (ulong)ne * (ulong)fdm0[nat];
        const uint qm = (uint)((x >> 25)
            + ((((x & ((1u << 25) - 1u)) << 26) + y) >> 51));
        qn[nat] = v < 0 ? -(int)qm : (int)qm;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    /* 相 B：lane 0 走 zigzag 升序（qn 在 threadgroup——广播读零冲突；
     * 占用度下的串行链由块级并行掩盖） */
    if (lid.x == 0u && lid.y == 0u) {
        dc_raw[gidx] = qn[0];
        device ulong* out = pairs + gidx * 63u;
        uint prev = 0u;
        uint cnt = 0u;
        for (uint pos = 1u; pos < 64u; ++pos) {
            const uint npos = (uint)zz[pos];
            const int q = qn[npos];
            if (q != 0) {
                const uint lm = q >= 0 ? (uint)q * 2u : ((uint)(-q) * 2u - 1u);
                out[cnt++] = ((ulong)lm << 6) | (ulong)(pos - prev - 1u);
                prev = pos;
            }
        }
        npair[gidx] = (ushort)cnt;
    }
}
"""