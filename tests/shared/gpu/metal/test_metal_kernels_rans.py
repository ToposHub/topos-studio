"""A3/V8 批 1 差分机：GPU 侧 rANS2 slice 解码（rans2_decode_slice）。

三重验收：
  1. GPU CSR vs CPU 生产融合发射 CSR（tc_color_rans2_decode_sparse）
     位精确——dc / off 逐元素，pairs 按 slice 段（最坏预留布局 vs C 连续
     实际布局，段内逐 u64）；
  2. 端到端"token 不下行"链：payload bytes 上载 → GPU rANS → CSR 显存
     原位接 inverse_dct_wsd → 像素下行一次——与 CPU 参考解码平面逐值
     一致（CSR 永不离开显存）；
  3. conceal 寄存：破坏某 slice payload（CRC 失败）→ kernel status=11
     且该段 CSR 全零 → GPU 链重建 == CPU conceal 参考平面。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION, ToposCodec, ToposDecoder, _CFrameConfig,
)
from src.shared.gpu.metal.context import MetalComputeContext  # noqa: E402
from src.shared.gpu.metal.kernels_dec import METAL_RANS2_DECODE_MSL  # noqa: E402
from src.shared.gpu.metal.kernels_idct import METAL_INVERSE_DCT_MSL  # noqa: E402
from test_metal_kernels_idct import (  # noqa: E402
    _bind_qcap,
    _capture_csr,
    _decode_reference,
    _frozen_tables,
    _plane_dims,
    requires_metal,
)

_META9 = 9      # tc_dev_packet_slices meta 每 slice u32 数
_TABS = 709     # 归一化表 u16 数/slice
_SMETA = 13     # + plane_cols, dc0, off0, pairs0


def _bind_rans(lib) -> None:
    lib.tc_dev_packet_slices.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint16), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.tc_dev_packet_slices.restype = ctypes.c_int32


def _make_fc(w, h, pf, bd, qp=30, slice_rows=8):
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 5 if pf == 1 else 3
    fc.pixel_format = pf
    fc.bit_depth = bd
    fc.qmatrix_id = 1
    fc.qp_base = qp
    fc.slice_rows = slice_rows
    fc.sar_num = fc.sar_den = 1
    fc.reserved[0] = 8          # V7-R2 rans2
    return fc


def _packet_slices(codec: ToposCodec, packet: bytes):
    """tc_dev_packet_slices → (meta9[n,9], payload_bytes[u8], tabs[n,709])。"""
    _bind_rans(codec._lib)
    n_max = 512
    meta = np.zeros((n_max, _META9), dtype=np.uint32)
    payload = np.zeros(len(packet), dtype=np.uint8)
    tabs = np.zeros((n_max, _TABS), dtype=np.uint16)
    n = ctypes.c_uint32(0)
    used = ctypes.c_size_t(0)
    rc = codec._lib.tc_dev_packet_slices(
        packet, len(packet),
        meta.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)), _META9,
        payload.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), payload.size,
        tabs.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)), _TABS,
        ctypes.byref(n), ctypes.byref(used))
    assert rc == 0, f"tc_dev_packet_slices rc={rc}"
    return meta[:n.value].copy(), payload[:used.value].copy(), tabs[:n.value].copy()


def _build_smeta(meta9: np.ndarray, pf: int, w: int):
    """C 9 项 meta → 13 项 smeta（补 plane_cols/dc0/off0/pairs0）。

    段基址与 A2-4 生产直发同构：off0 = Σ(blocks+1) 前缀、pairs0 =
    Σ(63×blocks) 最坏前缀（slice 序拼接）、dc0 = plane 稠密基。
    """
    n = len(meta9)
    smeta = np.zeros((n, _SMETA), dtype=np.uint32)
    smeta[:, :_META9] = meta9
    luma_cols = (w + 7) // 8
    chroma_cols = (w + 15) // 16 if pf == 0 else (w + 7) // 8
    rows = {}
    for p in (0, 1, 2):
        sel = meta9[:, 0] == p
        rows[p] = int((meta9[sel, 1] + meta9[sel, 2]).max())
    dc_base = {0: 0, 1: rows[0] * luma_cols,
               2: rows[0] * luma_cols + rows[1] * chroma_cols}
    off0 = 0
    pairs0 = 0
    for i in range(n):
        p = int(meta9[i, 0])
        cols = luma_cols if p == 0 else chroma_cols
        blocks = int(meta9[i, 2]) * cols
        smeta[i, 9] = cols
        smeta[i, 10] = dc_base[p]
        smeta[i, 11] = off0
        smeta[i, 12] = pairs0
        off0 += blocks + 1
        pairs0 += 63 * blocks
    return smeta, off0, pairs0


def _gpu_rans_decode(ctx: MetalComputeContext, codec: ToposCodec, packet: bytes,
                     pf: int, w: int, bd: int):
    """payload 上载 → GPU rANS → CSR（读回 numpy + 原位 buffer 两份视图）。

    返回 (dc, off, pairs, status, gpu_buffers, meta9, smeta)——gpu_buffers
    的 3/4/5 即显存 CSR，可直接接 inverse_dct_wsd（token 不下行验收用）。
    """
    meta9, payload, tabs = _packet_slices(codec, packet)
    smeta, off_total, pairs_total = _build_smeta(meta9, pf, w)
    n = len(meta9)
    rows = {}
    for p in (0, 1, 2):
        sel = meta9[:, 0] == p
        rows[p] = int((meta9[sel, 1] + meta9[sel, 2]).max())
    luma_cols = (w + 7) // 8
    chroma_cols = (w + 15) // 16 if pf == 0 else (w + 7) // 8
    dc_total = rows[0] * luma_cols + (rows[1] + rows[2]) * chroma_cols

    bufs = {
        0: ctx.create_buffer_from_numpy(payload),
        1: ctx.create_buffer_from_numpy(smeta),
        2: ctx.create_buffer_from_numpy(tabs),
        3: ctx.create_buffer_from_numpy(np.zeros(dc_total, np.int32)),
        4: ctx.create_buffer_from_numpy(np.zeros(off_total, np.uint32)),
        5: ctx.create_buffer_from_numpy(np.zeros(pairs_total, "<u8")),
        6: ctx.create_buffer_from_numpy(np.zeros(n, np.uint32)),
        7: ctx.create_buffer_from_numpy(np.array([n], np.uint32)),
    }
    k = ctx.compile_kernel("rans2_decode_slice", METAL_RANS2_DECODE_MSL)
    g = 64
    threads = (((n + g - 1) // g) * g, 1, 1)
    ctx.dispatch_sync(k, buffers=bufs, threads=threads, group=(g, 1, 1))
    dc = np.frombuffer(ctx.read_buffer(bufs[3]), np.int32)
    off = np.frombuffer(ctx.read_buffer(bufs[4]), np.uint32)
    status = np.frombuffer(ctx.read_buffer(bufs[6]), np.uint32)
    return dc, off, bufs, status, meta9, smeta


def _read_pairs_segment(ctx, bufs, smeta, meta9):
    """按 slice 读回实际 pairs 段（差分用；端到端链路不读）。"""
    off_buf = np.frombuffer(ctx.read_buffer(bufs[4]), np.uint32)
    all_pairs = np.frombuffer(ctx.read_buffer(bufs[5]), "<u8")
    out = []
    for i in range(len(meta9)):
        blocks = int(meta9[i, 2]) * int(smeta[i, 9])
        cnt_i = int(off_buf[int(smeta[i, 11]) + blocks])
        out.append(all_pairs[int(smeta[i, 12]):int(smeta[i, 12]) + cnt_i])
    return out


@requires_metal
class TestGpuRans2Decode:

    CASES = [
        (96, 64, 1, 10, 8),      # 444 对齐
        (313, 141, 1, 12, 8),    # 444 非 8 对齐 + 12-bit
        (150, 90, 0, 10, 8),     # 422 色度非对齐
        (128, 96, 1, 10, 2),     # slice_rows=2：V8 链数预演（细分带几何）
    ]

    @pytest.mark.parametrize("w,h,pf,bd,srows", CASES)
    def test_gpu_vs_cpu_csr(self, w, h, pf, bd, srows):
        """GPU rANS CSR vs C 生产融合发射 CSR 位精确。"""
        codec = ToposCodec()
        _bind_qcap(codec._lib)
        rng = np.random.default_rng(0xA3E0 + w + h + pf + srows)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, slice_rows=srows),
                                       [p.tobytes() for p in planes])
        _, qmeta, _, cpu_dc, cpu_off, cpu_pairs = _capture_csr(
            codec, packet, dims, 1)

        ctx = MetalComputeContext()
        gpu_dc, gpu_off, bufs, status, meta9, smeta = _gpu_rans_decode(
            ctx, codec, packet, pf, w, bd)
        assert int(status.max(initial=0)) == 0, f"status={status}"

        # packet 序 == qcap 序（逐 slice plane/y0/h 对齐断言）
        for i, m in enumerate(qmeta):
            assert int(meta9[i, 0]) == m.plane
            assert int(meta9[i, 1]) == m.block_y0
            assert int(meta9[i, 2]) == m.block_h

        np.testing.assert_array_equal(gpu_dc, cpu_dc)
        np.testing.assert_array_equal(gpu_off, cpu_off)
        gpu_seg = _read_pairs_segment(ctx, bufs, smeta, meta9)
        blocks_l = [m.block_h * m.cols for m in qmeta]
        off_seg = np.concatenate([[0], np.cumsum([b + 1 for b in blocks_l])])
        # C 连续布局的 pairs 段基 = 实际计数前缀（非 off 段前缀）
        actual = [int(cpu_off[off_seg[i] + blocks_l[i]])
                  for i in range(len(qmeta))]
        pairs_cum = np.concatenate([[0], np.cumsum(actual)])
        for i in range(len(qmeta)):
            np.testing.assert_array_equal(
                gpu_seg[i], cpu_pairs[pairs_cum[i]:pairs_cum[i] + actual[i]],
                err_msg=f"slice {i} pairs")

    @pytest.mark.parametrize("w,h,pf,bd,srows", CASES)
    def test_endtoend_token_stays_on_gpu(self, w, h, pf, bd, srows):
        """payload 上载 → GPU rANS → CSR 显存原位接 wsd → 像素；对 CPU 参考。"""
        codec = ToposCodec()
        _bind_qcap(codec._lib)
        _, mtable, wtable = _frozen_tables(codec)
        rng = np.random.default_rng(0xA3E1 + w + h + pf + srows)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, slice_rows=srows),
                                       [p.tobytes() for p in planes])
        ref = _decode_reference(codec, packet, w, h, pf)
        _, qmeta, qtab_all, _, _, _ = _capture_csr(codec, packet, dims, 1)

        ctx = MetalComputeContext()
        dc, off, bufs, status, meta9, smeta = _gpu_rans_decode(
            ctx, codec, packet, pf, w, bd)
        assert int(status.max(initial=0)) == 0

        mid, maxv = 1 << (bd - 1), (1 << bd) - 1
        wsd = ctx.compile_kernel("inverse_dct_wsd", METAL_INVERSE_DCT_MSL)
        rows = {p: max(m.block_y0 + m.block_h for m in qmeta if m.plane == p)
                for p in (0, 1, 2)}
        dc_off = {0: 0}
        for p in (1, 2):
            prev = [m for m in qmeta if m.plane == p - 1]
            dc_off[p] = dc_off[p - 1] + rows[p - 1] * prev[0].cols
        for p, (ph, pw) in enumerate(dims):
            sl = [(gi, m) for gi, m in enumerate(qmeta) if m.plane == p]
            cols = sl[0][1].cols
            qtab_p = np.array([qtab_all[gi] for gi, _ in sl], np.uint32)
            row_slice = np.zeros(rows[p], np.uint32)
            sblk0 = np.empty(len(sl), np.uint32)
            soff = np.empty(len(sl), np.uint32)
            spbase = np.empty(len(sl), np.uint32)
            for si, (gi, m) in enumerate(sl):
                row_slice[m.block_y0:m.block_y0 + m.block_h] = si
                sblk0[si] = m.block_y0 * cols
                soff[si] = smeta[gi, 11]      # GPU off 段基（连续段）
                spbase[si] = smeta[gi, 12]    # GPU pairs 最坏预留段基
            params = ctx.create_buffer_from_numpy(
                np.array([cols, rows[p], pw, ph, mid, maxv, pw,
                          int(dc_off[p])], np.int32))
            out = np.zeros(ph * pw, "<u2")
            chain = {0: bufs[3], 1: bufs[5], 2: bufs[4],     # CSR 原位
                     3: ctx.create_buffer_from_numpy(out),
                     4: params,
                     5: ctx.create_buffer_from_numpy(mtable),
                     6: ctx.create_buffer_from_numpy(wtable),
                     7: ctx.create_buffer_from_numpy(qtab_p),
                     8: ctx.create_buffer_from_numpy(row_slice),
                     9: ctx.create_buffer_from_numpy(sblk0),
                     10: ctx.create_buffer_from_numpy(spbase),
                     11: ctx.create_buffer_from_numpy(soff)}
            ctx.dispatch_sync(wsd, buffers=chain,
                              threads=(cols * 8, rows[p] * 8, 1),
                              group=(8, 8, 1))
            got = np.frombuffer(ctx.read_buffer(chain[3]), "<u2")
            got = got.reshape(ph, pw)
            diff = int((got != ref[p]).sum())
            assert diff == 0, f"plane {p}: {diff}/{ref[p].size} 值不符"

    def test_corrupt_slice_conceal(self):
        """CRC 失败 → status=11 + CSR 段全零 → GPU 链 == CPU conceal 参考。"""
        w, h, pf, bd = 313, 141, 1, 12
        codec = ToposCodec()
        _bind_qcap(codec._lib)
        rng = np.random.default_rng(0xA3E2)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd),
                                       [p.tobytes() for p in planes])
        meta_probe, _, _ = _packet_slices(codec, packet)
        victim = len(meta_probe) // 2
        # 定位 victim payload 的包内绝对偏移：段内偏移 + 前缀之后翻转 1 字节
        poff = int(meta_probe[victim, 3])
        psz = int(meta_probe[victim, 4])
        dc_ctx_n = int(meta_probe[victim, 6])
        lvl_ctx_n = int(meta_probe[victim, 7])
        prefix = 1 + dc_ctx_n * 29 + 64 + lvl_ctx_n * 28
        bad = bytearray(packet)
        bad[poff + prefix + 4] ^= 0xFF
        bad = bytes(bad)

        arrays = [np.zeros(d, "<u2") for d in dims]
        with ToposDecoder(codec) as dec:     # CPU 参考：conceal 生效
            views = dec.make_views(arrays)
            info = dec.decode_views(bad, views, len(arrays))
        assert info.concealed_slices > 0

        _, qmeta, qtab_all, _, _, _ = _capture_csr(codec, bad, dims, 1)
        ctx = MetalComputeContext()
        dc, off, bufs, status, meta9, smeta = _gpu_rans_decode(
            ctx, codec, bad, pf, w, bd)
        assert int(status[victim]) == 11
        assert int(np.delete(status, victim).max(initial=0)) == 0

        m = meta9[victim]
        blocks = int(m[2]) * int(smeta[victim, 9])
        seg = off[int(smeta[victim, 11]):int(smeta[victim, 11]) + blocks + 1]
        assert int(seg.max()) == 0, "concealed slice off 段必须全零"
        p = int(m[0])
        cols = int(smeta[victim, 9])
        dc_seg = dc[int(smeta[victim, 10]) + int(m[1]) * cols:
                    int(smeta[victim, 10]) + (int(m[1]) + int(m[2])) * cols]
        assert int(np.abs(dc_seg).max(initial=0)) == 0

        # 端到端：GPU 链（含 conceal 段）== CPU conceal 参考平面
        _, mtable, wtable = _frozen_tables(codec)
        mid, maxv = 1 << (bd - 1), (1 << bd) - 1
        wsd = ctx.compile_kernel("inverse_dct_wsd", METAL_INVERSE_DCT_MSL)
        rows = {q: max(mm.block_y0 + mm.block_h for mm in qmeta if mm.plane == q)
                for q in (0, 1, 2)}
        dc_off = {0: 0}
        for q in (1, 2):
            prev = [mm for mm in qmeta if mm.plane == q - 1]
            dc_off[q] = dc_off[q - 1] + rows[q - 1] * prev[0].cols
        for p, (ph, pw) in enumerate(dims):
            sl = [(gi, mm) for gi, mm in enumerate(qmeta) if mm.plane == p]
            cols = sl[0][1].cols
            qtab_p = np.array([qtab_all[gi] for gi, _ in sl], np.uint32)
            row_slice = np.zeros(rows[p], np.uint32)
            sblk0 = np.empty(len(sl), np.uint32)
            soff = np.empty(len(sl), np.uint32)
            spbase = np.empty(len(sl), np.uint32)
            for si, (gi, mm) in enumerate(sl):
                row_slice[mm.block_y0:mm.block_y0 + mm.block_h] = si
                sblk0[si] = mm.block_y0 * cols
                soff[si] = smeta[gi, 11]
                spbase[si] = smeta[gi, 12]
            params = ctx.create_buffer_from_numpy(
                np.array([cols, rows[p], pw, ph, mid, maxv, pw,
                          int(dc_off[p])], np.int32))
            out = np.zeros(ph * pw, "<u2")
            chain = {0: bufs[3], 1: bufs[5], 2: bufs[4],
                     3: ctx.create_buffer_from_numpy(out),
                     4: params,
                     5: ctx.create_buffer_from_numpy(mtable),
                     6: ctx.create_buffer_from_numpy(wtable),
                     7: ctx.create_buffer_from_numpy(qtab_p),
                     8: ctx.create_buffer_from_numpy(row_slice),
                     9: ctx.create_buffer_from_numpy(sblk0),
                     10: ctx.create_buffer_from_numpy(spbase),
                     11: ctx.create_buffer_from_numpy(soff)}
            ctx.dispatch_sync(wsd, buffers=chain,
                              threads=(cols * 8, rows[p] * 8, 1),
                              group=(8, 8, 1))
            got = np.frombuffer(ctx.read_buffer(chain[3]), "<u2").reshape(ph, pw)
            diff = int((got != arrays[p]).sum())
            assert diff == 0, f"conceal plane {p}: {diff}/{arrays[p].size} 值不符"
