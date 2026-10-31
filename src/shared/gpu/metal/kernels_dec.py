"""GPU 侧解码熵核（A3/V8 批 1 差分机）。

rans2_decode_slice：每线程一条 V7-R2 slice 的完整 rANS 解码链——
前缀归一化表由宿主以解码器同源代码预建（tc_dev_packet_slices），
kernel 只跑符号环；产物直接是 A2-4 生产 CSR 契约（dc plane 稠密 /
off 每 slice [blocks+1] 连续段 / pairs 最坏 63×blocks 段拼接），
可原位接 inverse_dct_wsd（token 全程不离开显存）。

语义对齐 tc_color_rans2_decode_sparse（逐式移植）：
  - 状态机 x ∈ [2^23, 2^32)、逐字节重整化、SCALE_BITS=12（rans.h）；
  - slot→sym 查找 = cum 行二分（C 侧 LUT 的逐值等价替身：Σfreq=4096
    且 cum[nsym]=4096 保证二分答案 freq>0，与 LUT 同段同符号）；
  - suffix/rawbits 旁路（cat>23 拆 16+cat−16 块，解码先低后高）；
  - DC 预测回读自身 dense dc 段（band 内因果行——slice 独立契约，
    has_top=0 时 top 引用不发生）；round-half-up 符号拆分均值；
  - 域检查全保留（m 上限 / idx≤63 / DC 域 ±2^25 / 尾字节精确耗尽）；
  - conceal：CRC 失败（meta crc_ok=0）或解码错 → 先清零本 slice
    CSR 段再退出（GPU 重建 mid，与 dev_qcap_zero_slice / conceal_band
    逐值一致）。

status 每 slice 一枚：0=OK；4=TRUNCATED；9=MALFORMED；11=CHECKSUM。
布局：buffer0 bytes(payload 连接) / 1 smeta(13×u32: C 9 项 + plane_cols,
dc0, off0, pairs0) / 2 tabs(709×u16) / 3 dc(i32 plane 稠密) /
4 off(u32) / 5 pairs(u64) / 6 status(u32)。threads=(n,1,1)。smeta 每 slice
13×u32：C 导出 9 项 + plane_cols, dc0, off0, pairs0（宿主几何预建；
与 kernel 侧 ``smeta + sid * 13u`` 步长一致）。
"""

from __future__ import annotations

METAL_RANS2_DECODE_MSL = r"""
#include <metal_stdlib>
using namespace metal;

constant uint TC_L = 1u << 23;
constant uint TC_SCALE_BITS = 12;
constant uint TC_SCALE_TOTAL = 4096;
constant uint TC_M_MAX_DC = 0x08000000u;
constant uint TC_M_MAX_AC = 0x04000000u;

constant uchar TC_ZZ[64] = {
     0,  1,  8, 16,  9,  2,  3, 10,
    17, 24, 32, 25, 18, 11,  4,  5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13,  6,  7, 14, 21, 28,
    35,  42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63
};

struct rans_state {
    device const uchar* buf;
    uint size;
    uint x;
    uint pos;
};

/* slot→sym：cum 行内最大 s 使 cum[s] ≤ slot（答案 freq>0 由
 * Σfreq=4096、cum[nsym]=4096 保证——与 C 侧 LUT 逐值等价） */
static inline uint tc_sym_search(device const ushort* cum, uint nsym,
                                 uint slot)
{
    uint lo = 0u;
    uint hi = nsym - 1u;
    while (lo < hi) {
        const uint mid = (lo + hi + 1u) >> 1;
        if ((uint)cum[mid] <= slot) { lo = mid; } else { hi = mid - 1u; }
    }
    return lo;
}

/* 单符号解码（状态转移与 tc_rans_get_lut 严格同式） */
static inline uint tc_rans_get(thread rans_state& st,
                               device const ushort* freq,
                               device const ushort* cum, uint nsym,
                               thread uint& sym)
{
    const uint slot = st.x & (TC_SCALE_TOTAL - 1u);
    const uint s = tc_sym_search(cum, nsym, slot);
    st.x = (uint)freq[s] * (st.x >> TC_SCALE_BITS) + slot - (uint)cum[s];
    while (st.x < TC_L) {
        if (st.pos >= st.size) { return 4u; }
        st.x = (st.x << 8u) | (uint)st.buf[st.pos];
        st.pos += 1u;
    }
    sym = s;
    return 0u;
}

/* 原始位块（tc_rans_get_chunk 同式：w ≤ 23） */
static inline uint tc_rans_get_chunk(thread rans_state& st, uint w,
                                     thread uint& c)
{
    c = st.x & ((1u << w) - 1u);
    st.x >>= w;
    while (st.x < TC_L) {
        if (st.pos >= st.size) { return 4u; }
        st.x = (st.x << 8u) | (uint)st.buf[st.pos];
        st.pos += 1u;
    }
    return 0u;
}

/* 原始位（b>23 拆块：解码先低 16 后高 b−16，与编码逆序配对） */
static inline uint tc_rans_get_rawbits(thread rans_state& st, uint b,
                                       thread uint& v)
{
    v = 0u;
    if (b > 23u) {
        uint lo = 0u;
        uint rc = tc_rans_get_chunk(st, 16u, lo);
        if (rc != 0u) { return rc; }
        uint hi = 0u;
        rc = tc_rans_get_chunk(st, b - 16u, hi);
        if (rc != 0u) { return rc; }
        v = (hi << 16u) | lo;
    } else if (b != 0u) {
        return tc_rans_get_chunk(st, b, v);
    }
    return 0u;
}

/* 类别后缀（tc_rans_get_suffix 同式：cat−1 个幅度位折进高位 1） */
static inline uint tc_get_suffix(thread rans_state& st, uint cat,
                                 thread uint& m)
{
    uint v = 0u;
    if (cat > 1u) {
        const uint rc = tc_rans_get_rawbits(st, cat - 1u, v);
        if (rc != 0u) { return rc; }
    }
    m = cat == 0u ? 0u : ((1u << (cat - 1u)) | v);
    return 0u;
}

static inline uint tc_pos_ctx(uint pos)
{
    return pos < 8u ? 0u : pos < 16u ? 1u : pos < 32u ? 2u : 3u;
}

static inline uint tc_prevlvl_ctx(uint cat, bool has_prev)
{
    if (!has_prev) { return 4u; }
    return cat == 0u ? 0u : cat == 1u ? 1u : cat <= 3u ? 2u : 3u;
}

static inline uint tc_dc_ctx(uint cat, bool has_prev)
{
    if (!has_prev) { return 0u; }
    return 1u + (cat == 0u ? 0u : cat == 1u ? 1u : cat <= 3u ? 2u : 3u);
}

/* 有符号反映射（tc_rice_unmap_signed 同式：偶→+m/2，奇→−(m+1)/2） */
static inline int tc_unmap(uint m)
{
    if ((m & 1u) == 0u) { return (int)(m >> 1); }
    return -(int)((m + 1u) >> 1);
}

/* DC 预测（round-half-up 均值符号拆分 / left / top / 0） */
static inline int tc_dc_predict(bool has_left, int left, bool has_top, int top)
{
    if (has_left && has_top) {
        const int64_t s = (int64_t)left + (int64_t)top;
        if (s >= 0) { return (int)((s + 1) >> 1); }
        return -(int)((-s) >> 1);
    }
    if (has_left) { return left; }
    if (has_top) { return top; }
    return 0;
}

kernel void rans2_decode_slice(device const uchar* bytes [[buffer(0)]],
                               device const uint* smeta  [[buffer(1)]],
                               device const ushort* tabs [[buffer(2)]],
                               device int* dc_out        [[buffer(3)]],
                               device uint* off_out      [[buffer(4)]],
                               device ulong* pairs_out   [[buffer(5)]],
                               device uint* status       [[buffer(6)]],
                               device const uint& scount [[buffer(7)]],
                               uint sid [[thread_position_in_grid]])
{
    if (sid >= scount) { return; }
    /* smeta：plane, y0, h, payload_off, payload_size, flags, dc_ctx_n,
     * lvl_ctx_n, crc_ok, plane_cols, dc0, off0, pairs0（13×u32/slice） */
    device const uint* me = smeta + (size_t)sid * 13u;
    const uint y0 = me[1];
    const uint band = me[2];
    const uint flags = me[5];
    const uint crc_ok = me[8];
    const uint lvl_model = flags & 0x3u;
    const uint dc_model = (flags >> 2u) & 0x1u;

    const uint cols = me[9];
    const size_t dc0 = (size_t)me[10];
    const size_t off0 = (size_t)me[11];
    const size_t pairs0 = (size_t)me[12];
    const uint nblocks = band * cols;

    /* conceal 契约：先清零本 slice CSR 段（off=0 → GPU 重建 mid） */
    for (uint i = 0u; i <= nblocks; ++i) { off_out[off0 + i] = 0u; }
    device int* dc_band = dc_out + dc0 + (size_t)y0 * cols;
    for (uint i = 0u; i < nblocks; ++i) { dc_band[i] = 0; }
    if (crc_ok == 0u) { status[sid] = 11u; return; }

    device const ushort* tab = tabs + (size_t)sid * 709u;
    device const ushort* dc_f = tab;
    device const ushort* dc_c = tab + 145u;
    device const ushort* run_f = dc_c + 150u;
    device const ushort* run_c = run_f + 64u;
    device const ushort* lvl_f = run_c + 65u;
    device const ushort* lvl_c = lvl_f + 140u;

    /* rANS 流 = payload + prefix（tc_dev_packet_slices 已验 ≥ 4 字节）；
     * 终态 4B 大端 + 数据字节正序（tc_rans_enc_flush 布局） */
    device const uchar* payload = bytes + (size_t)me[3];
    const uint dc_ctx_n = me[6];
    const uint lvl_ctx_n = me[7];
    const uint prefix = 1u + dc_ctx_n * 29u + 64u + lvl_ctx_n * 28u;
    rans_state st;
    st.buf = payload + prefix;
    st.size = me[4] - prefix;
    st.x = ((uint)st.buf[0] << 24) | ((uint)st.buf[1] << 16)
         | ((uint)st.buf[2] << 8) | (uint)st.buf[3];
    st.pos = 4u;
    if (st.x < TC_L) { status[sid] = 9u; return; }

    uint cnt = 0u;
    uint prev_dc_cat = 0u;
    bool first_block = true;
    for (uint by = 0u; by < band; ++by) {
        device uint* off_row = off_out + off0 + (size_t)by * cols;
        device int* dc_row = dc_out + dc0 + (size_t)(y0 + by) * cols;
        device int* dc_top = dc_row - (size_t)cols;
        for (uint bx = 0u; bx < cols; ++bx) {
            off_row[bx] = cnt;
            uint rc = 0u;
            uint cat = 0u;
            uint m = 0u;
            const uint dctx = first_block ? 0u : tc_dc_ctx(prev_dc_cat, true);
            const uint dr = (dc_model == 1u) ? dctx : 0u;
            rc = tc_rans_get(st, dc_f + (size_t)dr * 29u,
                             dc_c + (size_t)dr * 30u, 29u, cat);
            if (rc != 0u) { status[sid] = rc; return; }
            rc = tc_get_suffix(st, cat, m);
            if (rc != 0u) { status[sid] = rc; return; }
            if (m > TC_M_MAX_DC) { status[sid] = 9u; return; }
            const int top = (by != 0u) ? dc_top[bx] : 0;
            const int pred = tc_dc_predict(bx != 0u,
                                           (bx != 0u) ? dc_row[bx - 1u] : 0,
                                           by != 0u, top);
            const int64_t v = (int64_t)pred + (int64_t)tc_unmap(m);
            if (v > (1 << 25) || v < -(1 << 25)) { status[sid] = 9u; return; }
            dc_row[bx] = (int)v;
            prev_dc_cat = cat;
            first_block = false;

            uint pos = 1u;
            uint prev_cat = 0u;
            bool has_prev = false;
            for (;;) {
                uint run_sym = 0u;
                rc = tc_rans_get(st, run_f, run_c, 64u, run_sym);
                if (rc != 0u) { status[sid] = rc; return; }
                if (run_sym == 63u) { break; }
                const uint idx = pos + run_sym;
                if (idx > 63u) { status[sid] = 9u; return; }
                uint lr = 0u;
                if (lvl_model == 1u) {
                    lr = tc_pos_ctx(idx);
                } else if (lvl_model == 2u) {
                    lr = tc_prevlvl_ctx(prev_cat, has_prev);
                }
                rc = tc_rans_get(st, lvl_f + (size_t)lr * 28u,
                                 lvl_c + (size_t)lr * 29u, 28u, cat);
                if (rc != 0u) { status[sid] = rc; return; }
                rc = tc_get_suffix(st, cat, m);
                if (rc != 0u) { status[sid] = rc; return; }
                if (m > TC_M_MAX_AC) { status[sid] = 9u; return; }
                const uint nat = (uint)TC_ZZ[idx];
                pairs_out[pairs0 + cnt] =
                    ((ulong)(long)tc_unmap(m) << 6u) | (ulong)nat;
                cnt += 1u;
                prev_cat = cat;
                has_prev = true;
                pos = idx + 1u;
            }
        }
    }
    off_out[off0 + (size_t)nblocks] = cnt;
    if (st.pos != st.size) { status[sid] = 9u; return; }
    status[sid] = 0u;
}
"""


METAL_RANS2_DECODE_SEGMENT_MSL = r"""
#include <metal_stdlib>
using namespace metal;

/* V8 批 4：每线程一个段的完整 rANS 解码链（段化多状态 rANS）。
 * 与 rans2_decode_slice 的差异（topos_v8_format_spec_batch1 布局契约）：
 *  - 流 = 4B 大端终态 + 数据（无 per-slice 前缀；模型来自瓦片共享表）；
 *  - 表按瓦片共享（tab_index 指向 709×u16 瓦片表行；段只读）；
 *  - DC 预测段局部因果：left = 段内前块、top = 段内上块行（i ≥ cols）；
 *  - conceal：瓦片 CRC 坏 → 清零本段 CSR 段（GPU 重建 mid）。
 * smeta 14×u32/段：plane, seg0, nblocks, stream_off, stream_len, flags,
 * dc_ctx_n, lvl_ctx_n, crc_ok, plane_cols, dc0, off0, pairs0, tab_index。
 * 产物 CSR 契约同 A2-4（dc 平面稠密 / off 每段 [nblocks+1] / pairs 最坏
 * 63×nblocks 段拼接），可原位接 inverse_dct_wsd_v8。 */

constant uint V8_L = 1u << 23;
constant uint V8_SCALE_BITS = 12;
constant uint V8_SCALE_TOTAL = 4096;
constant uint V8_M_MAX_DC = 0x08000000u;
constant uint V8_M_MAX_AC = 0x04000000u;

constant uchar V8_ZZ[64] = {
     0,  1,  8, 16,  9,  2,  3, 10,
    17, 24, 32, 25, 18, 11,  4,  5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13,  6,  7, 14, 21, 28,
    35,  42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63
};

struct v8_rans_state {
    device const uchar* buf;
    uint size;
    uint x;
    uint pos;
};

static inline uint v8_sym_search(device const ushort* cum, uint nsym,
                                 uint slot)
{
    uint lo = 0u;
    uint hi = nsym - 1u;
    while (lo < hi) {
        const uint mid = (lo + hi + 1u) >> 1;
        if ((uint)cum[mid] <= slot) { lo = mid; } else { hi = mid - 1u; }
    }
    return lo;
}

static inline uint v8_get(thread v8_rans_state& st,
                          device const ushort* freq,
                          device const ushort* cum, uint nsym,
                          thread uint& sym)
{
    const uint slot = st.x & (V8_SCALE_TOTAL - 1u);
    const uint s = v8_sym_search(cum, nsym, slot);
    st.x = (uint)freq[s] * (st.x >> V8_SCALE_BITS) + slot - (uint)cum[s];
    while (st.x < V8_L) {
        if (st.pos >= st.size) { return 4u; }
        st.x = (st.x << 8u) | (uint)st.buf[st.pos];
        st.pos += 1u;
    }
    sym = s;
    return 0u;
}

static inline uint v8_get_chunk(thread v8_rans_state& st, uint w,
                                thread uint& c)
{
    c = st.x & ((1u << w) - 1u);
    st.x >>= w;
    while (st.x < V8_L) {
        if (st.pos >= st.size) { return 4u; }
        st.x = (st.x << 8u) | (uint)st.buf[st.pos];
        st.pos += 1u;
    }
    return 0u;
}

static inline uint v8_get_rawbits(thread v8_rans_state& st, uint b,
                                  thread uint& v)
{
    v = 0u;
    if (b > 23u) {
        uint lo = 0u;
        uint rc = v8_get_chunk(st, 16u, lo);
        if (rc != 0u) { return rc; }
        uint hi = 0u;
        rc = v8_get_chunk(st, b - 16u, hi);
        if (rc != 0u) { return rc; }
        v = (hi << 16u) | lo;
    } else if (b != 0u) {
        return v8_get_chunk(st, b, v);
    }
    return 0u;
}

static inline uint v8_get_suffix(thread v8_rans_state& st, uint cat,
                                 thread uint& m)
{
    uint v = 0u;
    if (cat > 1u) {
        const uint rc = v8_get_rawbits(st, cat - 1u, v);
        if (rc != 0u) { return rc; }
    }
    m = cat == 0u ? 0u : ((1u << (cat - 1u)) | v);
    return 0u;
}

static inline uint v8_pos_ctx(uint pos)
{
    return pos < 8u ? 0u : pos < 16u ? 1u : pos < 32u ? 2u : 3u;
}

static inline uint v8_prevlvl_ctx(uint cat, bool has_prev)
{
    if (!has_prev) { return 4u; }
    return cat == 0u ? 0u : cat == 1u ? 1u : cat <= 3u ? 2u : 3u;
}

static inline uint v8_dc_ctx(uint cat, bool has_prev)
{
    if (!has_prev) { return 0u; }
    return 1u + (cat == 0u ? 0u : cat == 1u ? 1u : cat <= 3u ? 2u : 3u);
}

static inline int v8_unmap(uint m)
{
    if ((m & 1u) == 0u) { return (int)(m >> 1); }
    return -(int)((m + 1u) >> 1);
}

static inline int v8_dc_predict(bool has_left, int left, bool has_top, int top)
{
    if (has_left && has_top) {
        const int64_t s = (int64_t)left + (int64_t)top;
        if (s >= 0) { return (int)((s + 1) >> 1); }
        return -(int)((-s) >> 1);
    }
    if (has_left) { return left; }
    if (has_top) { return top; }
    return 0;
}

kernel void rans2_decode_segment(device const uchar* bytes  [[buffer(0)]],
                                 device const uint* smeta   [[buffer(1)]],
                                 device const ushort* tabs  [[buffer(2)]],
                                 device int* dc_out         [[buffer(3)]],
                                 device uint* off_out       [[buffer(4)]],
                                 device ulong* pairs_out    [[buffer(5)]],
                                 device uint* status        [[buffer(6)]],
                                 device const uint& scount  [[buffer(7)]],
                                 uint sid [[thread_position_in_grid]])
{
    if (sid >= scount) { return; }
    device const uint* me = smeta + (size_t)sid * 14u;
    const uint seg0 = me[1];
    const uint nblocks = me[2];
    const uint flags = me[5];
    const uint crc_ok = me[8];
    const uint cols = me[9];
    const size_t dc0 = (size_t)me[10];
    const size_t off0 = (size_t)me[11];
    const size_t pairs0 = (size_t)me[12];
    const uint tab_index = me[13];
    const uint lvl_model = flags & 0x3u;
    const uint dc_model = (flags >> 2u) & 0x1u;

    /* conceal 契约：先清零本段 CSR 段 */
    for (uint i = 0u; i <= nblocks; ++i) { off_out[off0 + i] = 0u; }
    device int* dc_seg = dc_out + dc0;
    for (uint i = 0u; i < nblocks; ++i) { dc_seg[i] = 0; }
    if (crc_ok == 0u) { status[sid] = 11u; return; }

    device const ushort* tab = tabs + (size_t)tab_index * 709u;
    device const ushort* dc_f = tab;
    device const ushort* dc_c = tab + 145u;
    device const ushort* run_f = dc_c + 150u;
    device const ushort* run_c = run_f + 64u;
    device const ushort* lvl_f = run_c + 65u;
    device const ushort* lvl_c = lvl_f + 140u;

    /* 流 = 4B 大端终态 + 数据（无前缀） */
    device const uchar* stream = bytes + (size_t)me[3];
    v8_rans_state st;
    st.buf = stream + 4u;
    st.size = me[4] - 4u;
    st.x = ((uint)stream[0] << 24) | ((uint)stream[1] << 16)
         | ((uint)stream[2] << 8) | (uint)stream[3];
    st.pos = 0u;  /* buf 已含 +4 跳过终态；pos 相对 buf */
    if (st.x < V8_L) { status[sid] = 9u; return; }

    uint cnt = 0u;
    uint prev_dc_cat = 0u;
    for (uint i = 0u; i < nblocks; ++i) {
        off_out[off0 + i] = cnt;
        uint rc = 0u;
        uint cat = 0u;
        uint m = 0u;
        const uint dctx = (i == 0u || dc_model != 1u)
            ? 0u : v8_dc_ctx(prev_dc_cat, true);
        rc = v8_get(st, dc_f + (size_t)dctx * 29u,
                    dc_c + (size_t)dctx * 30u, 29u, cat);
        if (rc != 0u) { status[sid] = rc; return; }
        rc = v8_get_suffix(st, cat, m);
        if (rc != 0u) { status[sid] = rc; return; }
        if (m > V8_M_MAX_DC) { status[sid] = 9u; return; }
        /* 段局部 DC 因果：left = 段内前块；top = 段内上块行（i ≥ cols） */
        const int left = (i != 0u) ? dc_seg[i - 1u] : 0;
        const int top = (i >= cols) ? dc_seg[i - cols] : 0;
        const int pred = v8_dc_predict(i != 0u, left, i >= cols, top);
        const int64_t v = (int64_t)pred + (int64_t)v8_unmap(m);
        if (v > (1 << 25) || v < -(1 << 25)) { status[sid] = 9u; return; }
        dc_seg[i] = (int)v;
        prev_dc_cat = cat;

        uint pos = 1u;
        uint prev_cat = 0u;
        bool has_prev = false;
        for (;;) {
            uint run_sym = 0u;
            rc = v8_get(st, run_f, run_c, 64u, run_sym);
            if (rc != 0u) { status[sid] = rc; return; }
            if (run_sym == 63u) { break; }
            const uint idx = pos + run_sym;
            if (idx > 63u) { status[sid] = 9u; return; }
            uint lr = 0u;
            if (lvl_model == 1u) {
                lr = v8_pos_ctx(idx);
            } else if (lvl_model == 2u) {
                lr = v8_prevlvl_ctx(prev_cat, has_prev);
            }
            rc = v8_get(st, lvl_f + (size_t)lr * 28u,
                        lvl_c + (size_t)lr * 29u, 28u, cat);
            if (rc != 0u) { status[sid] = rc; return; }
            rc = v8_get_suffix(st, cat, m);
            if (rc != 0u) { status[sid] = rc; return; }
            if (m > V8_M_MAX_AC) { status[sid] = 9u; return; }
            const uint nat = (uint)V8_ZZ[idx];
            pairs_out[pairs0 + cnt] =
                ((ulong)(long)v8_unmap(m) << 6u) | (ulong)nat;
            cnt += 1u;
            prev_cat = cat;
            has_prev = true;
            pos = idx + 1u;
        }
    }
    off_out[off0 + (size_t)nblocks] = cnt;
    if (st.pos != st.size) { status[sid] = 9u; return; }
    status[sid] = 0u;
}
"""
