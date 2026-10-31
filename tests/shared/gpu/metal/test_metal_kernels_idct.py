"""A2（速度计划 v2）MSL 反量化+IDCT kernel 三方差分验收。

native 解码平面（``tc_dev_qcap_capture`` 捕获同一包的量化系数重建）
== numpy 整数参考 == Metal 输出，逐字节一致（位精确）。numpy 参考与
``transform.c`` 规范逆变换 + ``color_store.h`` 重建钳位同源：F' =
clamp(q·Q, ±2^25) → x̂ = round(Mᵀ·(F'⊙W)·M / 2^32) → clamp(x̂+mid)；
kTransformM/kTransformW 取自库内冻结表（``tc_transform_matrix`` /
``tc_transform_weights``）——单一真相源。

两个差分源：

- 真实包（encode_frame → decode_views）：覆盖 V1 Rice 与 V7-R2 rans2
  两族熵、444/422 几何、10/12-bit、非 8 对齐可见区；
- 合成 q：直接生成量化系数（含 DC-only 块、±2^25 钳位边界、稀疏模式），
  锚定 kernel 数值域（真实包不触及防御钳位）。
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

from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION, ToposCodec, ToposDecoder, _CFrameConfig,
)
from src.shared.gpu.metal.context import MetalComputeContext  # noqa: E402
from src.shared.gpu.metal.kernels_idct import METAL_INVERSE_DCT_MSL  # noqa: E402

_REAL_OK, _REAL_UNAVAILABLE_REASON = MetalComputeContext.availability_reason()
requires_metal = pytest.mark.skipif(
    not _REAL_OK, reason=f"Metal 不可用: {_REAL_UNAVAILABLE_REASON}"
)


class _CQcapSlice(ctypes.Structure):
    _fields_ = [
        ("plane", ctypes.c_uint32),
        ("block_y0", ctypes.c_uint32),
        ("block_h", ctypes.c_uint32),
        ("cols", ctypes.c_uint32),
        ("qp_eff", ctypes.c_uint32),
        ("off_elems", ctypes.c_uint32),
    ]


def _bind_qcap(lib) -> None:
    lib.tc_dev_set_qcap_capture.argtypes = [ctypes.c_int]
    lib.tc_dev_set_qcap_capture.restype = None
    lib.tc_dev_qcap_elems.argtypes = []
    lib.tc_dev_qcap_elems.restype = ctypes.c_int64
    lib.tc_dev_qcap_copy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.tc_dev_qcap_copy.restype = ctypes.c_int64
    lib.tc_dev_qcap_slices.argtypes = []
    lib.tc_dev_qcap_slices.restype = ctypes.c_uint32
    lib.tc_dev_qcap_meta_copy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.tc_dev_qcap_meta_copy.restype = ctypes.c_int64
    lib.tc_dev_qcap_q_copy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.tc_dev_qcap_q_copy.restype = ctypes.c_int64
    lib.tc_transform_matrix.argtypes = []
    lib.tc_transform_matrix.restype = ctypes.POINTER(ctypes.c_int16)
    lib.tc_transform_weights.argtypes = []
    lib.tc_transform_weights.restype = ctypes.POINTER(ctypes.c_uint32)
    lib.tc_dev_set_qcap_sparse.argtypes = [ctypes.c_int]
    lib.tc_dev_set_qcap_sparse.restype = None
    lib.tc_dev_qcap_sp_dc_copy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.tc_dev_qcap_sp_dc_copy.restype = ctypes.c_int64
    lib.tc_dev_qcap_sp_off_copy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.tc_dev_qcap_sp_off_copy.restype = ctypes.c_int64
    lib.tc_dev_qcap_sp_pairs_copy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.tc_dev_qcap_sp_pairs_copy.restype = ctypes.c_int64
    lib.tc_dev_qcap_sp_npairs.argtypes = []
    lib.tc_dev_qcap_sp_npairs.restype = ctypes.c_uint64


def _plane_dims(pixel_format: int, w: int, h: int):
    if pixel_format == 0:   # 4:2:2
        return [(h, w), (h, (w + 1) // 2), (h, (w + 1) // 2)]
    return [(h, w), (h, w), (h, w)]


def _make_fc(w: int, h: int, pixel_format: int, bd: int, entropy: int,
             qp: int = 30) -> _CFrameConfig:
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 5 if pixel_format == 1 else 3   # Pro444 仅限 444；422 用 hq
    fc.pixel_format = pixel_format
    fc.bit_depth = bd
    fc.qmatrix_id = 1
    fc.qp_base = qp
    fc.qp_delta_chroma = 0
    fc.slice_rows = 8
    fc.sar_num = fc.sar_den = 1
    fc.reserved[0] = entropy    # 0 = V1 Rice；8 = V7-R2 rans2（产品默认）
    return fc


def _decode_reference(codec: ToposCodec, packet, w: int, h: int,
                      pixel_format: int):
    """关闭捕获的常规解码 → 参考 u16 平面（native sink 输出）。"""
    dims = _plane_dims(pixel_format, w, h)
    arrays = [np.zeros(d, dtype="<u2") for d in dims]
    with ToposDecoder(codec) as dec:
        views = dec.make_views(arrays)
        info = dec.decode_views(packet, views, len(arrays))
        assert info.concealed_slices == 0
    return arrays


def _decode_capture(codec: ToposCodec, packet, w: int, h: int,
                    pixel_format: int):
    """开启捕获的解码 → (qbuf, meta 列表, Qbuf)。平面不写（dev 语义）。"""
    lib = codec._lib
    dims = _plane_dims(pixel_format, w, h)
    arrays = [np.zeros(d, dtype="<u2") for d in dims]
    lib.tc_dev_set_qcap_capture(1)
    try:
        with ToposDecoder(codec) as dec:
            views = dec.make_views(arrays)
            info = dec.decode_views(packet, views, len(arrays))
            assert info.concealed_slices == 0
            n = int(lib.tc_dev_qcap_slices())
            assert n == info.slice_count, "捕获 slice 数与包 slice 数不符"
            qbuf = np.empty(int(lib.tc_dev_qcap_elems()), dtype=np.int32)
            assert int(lib.tc_dev_qcap_copy(qbuf.ctypes.data, qbuf.size)) == qbuf.size
            meta = (_CQcapSlice * n)()
            assert int(lib.tc_dev_qcap_meta_copy(meta, n)) == n
            qtab = np.empty(n * 64, dtype=np.uint32)
            assert int(lib.tc_dev_qcap_q_copy(qtab.ctypes.data, qtab.size)) == n * 64
    finally:
        lib.tc_dev_set_qcap_capture(0)
    return qbuf, [meta[i] for i in range(n)], qtab.reshape(n, 64)


def _capture_csr(codec: ToposCodec, packet, dims, mode: int):
    """capture+sparse(mode) 解码并取回全部 CSR 产物。

    mode 语义（codec.c）：1 = 生产融合发射（V7-R2；其余熵族自动回落
    legacy 扫描）；2 = 强制 legacy 扫描（差分 oracle）。返回
    (qbuf, meta, qtab[n,64], dc_all, off_all, pairs_all)。
    """
    lib = codec._lib
    arrays = [np.zeros(d, dtype="<u2") for d in dims]
    lib.tc_dev_set_qcap_capture(1)
    lib.tc_dev_set_qcap_sparse(mode)
    try:
        with ToposDecoder(codec) as dec:
            views = dec.make_views(arrays)
            info = dec.decode_views(packet, views, len(arrays))
        n = int(lib.tc_dev_qcap_slices())
        assert n == info.slice_count
        qbuf = np.empty(int(lib.tc_dev_qcap_elems()), dtype=np.int32)
        assert int(lib.tc_dev_qcap_copy(qbuf.ctypes.data, qbuf.size)) == qbuf.size
        meta_c = (_CQcapSlice * n)()
        assert int(lib.tc_dev_qcap_meta_copy(meta_c, n)) == n
        qtab = np.empty(n * 64, dtype=np.uint32)
        assert int(lib.tc_dev_qcap_q_copy(qtab.ctypes.data, qtab.size)) == n * 64
        meta = [meta_c[i] for i in range(n)]
        qtab = qtab.reshape(n, 64)
        nblocks_total = sum(m.block_h * m.cols for m in meta)
        dc_all = np.empty(nblocks_total, dtype=np.int32)
        assert int(lib.tc_dev_qcap_sp_dc_copy(dc_all.ctypes.data, dc_all.size)) == dc_all.size
        nseg = sum(m.block_h * m.cols + 1 for m in meta)
        off_all = np.empty(nseg, dtype=np.uint32)
        assert int(lib.tc_dev_qcap_sp_off_copy(off_all.ctypes.data, off_all.size)) == off_all.size
        pairs_all = np.empty(max(1, 1 << 22), dtype="<u8")
        got = int(lib.tc_dev_qcap_sp_pairs_copy(pairs_all.ctypes.data, pairs_all.size))
        assert got > 0
        pairs_all = np.ascontiguousarray(pairs_all[:got])
    finally:
        lib.tc_dev_set_qcap_sparse(0)
        lib.tc_dev_set_qcap_capture(0)
    return qbuf, meta, qtab, dc_all, off_all, pairs_all


def _ws_reconstruct(ctx, meta, qtab_all, dc_all, off_all, pairs_all, dims, bd,
                    mtable, wtable, variant="ws"):
    """C 发射 CSR → inverse_dct_ws/wsd 重建可见平面（生产移交管线）。

    variant="ws"：per-plane 定长 stride off（getter 路径）；"wsd"：C 原生
    连续 off 段 + soff 段基址 + dc0 平面段基（ext 直发路径，params[7]）。
    """
    mid, maxv = 1 << (bd - 1), (1 << bd) - 1
    off_seg = np.empty(len(meta) + 1, dtype=np.int64)
    off_seg[0] = 0
    for i, m in enumerate(meta):
        off_seg[i + 1] = off_seg[i] + m.block_h * m.cols + 1
    actual = np.array(
        [int(off_all[off_seg[i] + m.block_h * m.cols]) for i, m in enumerate(meta)],
        dtype=np.int64)
    pairs_cum = np.concatenate([[0], np.cumsum(actual)])
    outs = []
    for p, (ph, pw) in enumerate(dims):
        sl = [(gi, m) for gi, m in enumerate(meta) if m.plane == p]
        cols = sl[0][1].cols
        rows = max(m.block_y0 + m.block_h for _, m in sl)
        plane_blocks = rows * cols
        dc_off = 0
        for pp in range(p):
            slp = [m for m in meta if m.plane == pp]
            dc_off += max(m.block_y0 + m.block_h for m in slp) * slp[0].cols
        dc = dc_all[dc_off:dc_off + plane_blocks]
        blocks_s = [m.block_h * cols for _, m in sl]
        stride = max(blocks_s) + 1
        off_p = np.zeros(len(sl) * stride, dtype=np.uint32)
        sblk0 = np.empty(len(sl), dtype=np.uint32)
        spbase = np.empty(len(sl), dtype=np.uint32)
        qtab_p = np.empty((len(sl), 64), dtype=np.uint32)
        row_slice = np.zeros(rows, dtype=np.uint32)
        soff = np.empty(len(sl), dtype=np.uint32)
        for si, (gi, m) in enumerate(sl):
            nn = m.block_h * cols
            off_p[si * stride:si * stride + nn + 1] = off_all[off_seg[gi]:off_seg[gi] + nn + 1]
            sblk0[si] = m.block_y0 * cols
            spbase[si] = pairs_cum[gi]
            qtab_p[si] = qtab_all[gi]
            soff[si] = off_seg[gi]
            row_slice[m.block_y0:m.block_y0 + m.block_h] = si
        # wsd：连续 off（C 原生布局直接用 off_all；段基址 soff/dcf 基 dc0）
        p8 = stride if variant == "ws" else int(dc_off)
        params = ctx.create_buffer_from_numpy(
            np.array([cols, rows, pw, ph, mid, maxv, pw, p8], dtype=np.int32))
        out = np.zeros(ph * pw, dtype="<u2")
        off_buf = off_p if variant == "ws" else off_all
        dc_buf = dc if variant == "ws" else dc_all
        bufs = {0: ctx.create_buffer_from_numpy(np.ascontiguousarray(dc_buf)),
                1: ctx.create_buffer_from_numpy(pairs_all),
                2: ctx.create_buffer_from_numpy(np.ascontiguousarray(off_buf)),
                3: ctx.create_buffer_from_numpy(out),
                4: params,
                5: ctx.create_buffer_from_numpy(mtable),
                6: ctx.create_buffer_from_numpy(wtable),
                7: ctx.create_buffer_from_numpy(qtab_p),
                8: ctx.create_buffer_from_numpy(row_slice),
                9: ctx.create_buffer_from_numpy(sblk0),
                10: ctx.create_buffer_from_numpy(spbase)}
        if variant == "wsd":
            bufs[11] = ctx.create_buffer_from_numpy(soff)
        kname = "inverse_dct_ws" if variant == "ws" else "inverse_dct_wsd"
        k = ctx.compile_kernel(kname, METAL_INVERSE_DCT_MSL)
        ctx.dispatch_sync(k, buffers=bufs,
                          threads=(cols * 8, rows * 8, 1), group=(8, 8, 1))
        outs.append(np.frombuffer(ctx.read_buffer(bufs[3]), dtype="<u2").reshape(ph, pw))
    return outs


def _assemble_plane(qbuf, meta, qtab, plane: int):
    """捕获 → 平面级 (q[nblocks,64], qtab[nslices,64], row_slice[rows], cols)。"""
    sl = [(gi, m) for gi, m in enumerate(meta) if m.plane == plane]
    cols = sl[0][1].cols
    rows = max(m.block_y0 + m.block_h for _, m in sl)
    q = np.zeros(rows * cols * 64, dtype=np.int32)
    row_slice = np.zeros(rows, dtype=np.uint32)
    qtab_p = np.zeros((len(sl), 64), dtype=np.uint32)
    for si, (gi, m) in enumerate(sl):
        b = m.block_y0 * cols * 64
        e = (m.block_y0 + m.block_h) * cols * 64
        assert e - b == m.block_h * cols * 64
        q[b:e] = qbuf[m.off_elems:m.off_elems + m.block_h * cols * 64]
        row_slice[m.block_y0:m.block_y0 + m.block_h] = si
        qtab_p[si] = qtab[gi]
    return q.reshape(rows * cols, 64), qtab_p, row_slice, cols


def _reference_plane(q2, qtab, row_slice, m, w, rows, cols, vis_h, vis_w,
                     mid, maxv) -> np.ndarray:
    """numpy 参考：反量化 + 逆变换 + 钳位（transform.c/color_store.h 同源）。"""
    slice_of_block = np.repeat(row_slice, cols)
    qblock = qtab[slice_of_block]                        # (nblocks, 64)
    f = q2.astype(np.int64) * qblock.astype(np.int64)
    np.clip(f, -(1 << 25), 1 << 25, out=f)
    g = f * w[None, :]
    xh = np.einsum("uy,nuv,vx->nyx", m, g.reshape(-1, 8, 8), m)
    r = np.where(xh >= 0, (xh + (1 << 31)) >> 32,
                 -((-xh + (1 << 31)) >> 32))
    v = np.clip(r + mid, 0, maxv).astype(np.uint16)
    img = v.reshape(rows, cols, 8, 8).transpose(0, 2, 1, 3).reshape(rows * 8, cols * 8)
    return np.ascontiguousarray(img[:vis_h, :vis_w])


def _sparsify(q2):
    """稠密 q → CSR 稀疏契约（dc / pair_off / pairs，见 kernels_idct 模块）。"""
    nblocks = q2.shape[0]
    dc = np.ascontiguousarray(q2[:, 0]).astype(np.int32)
    mask = q2[:, 1:] != 0
    blk, nat = np.nonzero(mask)                # 行主序：块号升序、块内 nat 升序
    nat = (nat + 1).astype(np.int64)
    levels = q2[blk, nat].astype(np.int64)
    counts = mask.sum(axis=1)
    pair_off = np.zeros(nblocks + 1, dtype=np.uint32)
    pair_off[1:] = np.cumsum(counts)
    pairs = ((levels << 6) | nat).astype(np.uint64)
    return dc, pair_off, np.ascontiguousarray(pairs, dtype="<u8")


class _MetalRunner:
    def __init__(self):
        self.ctx = MetalComputeContext()
        self.kernels = {name: self.ctx.compile_kernel(name, METAL_INVERSE_DCT_MSL)
                        for name in ("inverse_dct_px", "inverse_dct_m8",
                                     "inverse_dct_sp", "inverse_dct_w1")}

    def run_dense(self, name, q_flat, qtab, row_slice, cols, rows,
                  vis_w, vis_h, mid, maxv, mtable, wtable) -> np.ndarray:
        out = np.zeros(vis_h * vis_w, dtype="<u2")
        params = self.ctx.create_buffer_from_numpy(
            np.array([cols, rows, vis_w, vis_h, mid, maxv, vis_w], dtype=np.int32))
        bufs = {0: self.ctx.create_buffer_from_numpy(np.ascontiguousarray(q_flat, dtype=np.int32)),
                1: self.ctx.create_buffer_from_numpy(out),
                2: params,
                3: self.ctx.create_buffer_from_numpy(mtable),
                4: self.ctx.create_buffer_from_numpy(wtable),
                5: self.ctx.create_buffer_from_numpy(np.ascontiguousarray(qtab)),
                6: self.ctx.create_buffer_from_numpy(np.ascontiguousarray(row_slice))}
        if name == "inverse_dct_px":
            threads, group = (cols * 8, rows * 8, 1), (8, 8, 1)
        else:
            threads, group = (((cols + 7) // 8) * 8, rows * 8, 8), (8, 8, 8)
        self.ctx.dispatch_sync(self.kernels[name], buffers=bufs,
                               threads=threads, group=group)
        got = np.frombuffer(self.ctx.read_buffer(bufs[1]), dtype="<u2")
        return got.reshape(vis_h, vis_w).copy()

    def run_sparse(self, name, dc, pair_off, pairs, qtab, row_slice, cols, rows,
                   vis_w, vis_h, mid, maxv, mtable, wtable) -> np.ndarray:
        out = np.zeros(vis_h * vis_w, dtype="<u2")
        params = self.ctx.create_buffer_from_numpy(
            np.array([cols, rows, vis_w, vis_h, mid, maxv, vis_w], dtype=np.int32))
        bufs = {0: self.ctx.create_buffer_from_numpy(dc),
                1: self.ctx.create_buffer_from_numpy(pairs),
                2: self.ctx.create_buffer_from_numpy(pair_off),
                3: self.ctx.create_buffer_from_numpy(out),
                4: params,
                5: self.ctx.create_buffer_from_numpy(mtable),
                6: self.ctx.create_buffer_from_numpy(wtable),
                7: self.ctx.create_buffer_from_numpy(np.ascontiguousarray(qtab)),
                8: self.ctx.create_buffer_from_numpy(np.ascontiguousarray(row_slice))}
        if name == "inverse_dct_w1":    # 一块一组（64 线程 = 单 wave）
            threads, group = (cols * 8, rows * 8, 1), (8, 8, 1)
        else:
            threads, group = (((cols + 7) // 8) * 8, rows * 8, 8), (8, 8, 8)
        self.ctx.dispatch_sync(self.kernels[name], buffers=bufs,
                               threads=threads, group=group)
        got = np.frombuffer(self.ctx.read_buffer(bufs[3]), dtype="<u2")
        return got.reshape(vis_h, vis_w).copy()


def _frozen_tables(codec: ToposCodec):
    lib = codec._lib
    _bind_qcap(lib)
    m = np.fromiter((int(v) for v in lib.tc_transform_matrix()[:64]),
                    dtype=np.int64).reshape(8, 8)
    w = np.fromiter((int(v) for v in lib.tc_transform_weights()[:64]),
                    dtype=np.uint64)
    return m, m.astype(np.int16), w.astype(np.uint32)


def _assert_plane_equal(got: np.ndarray, ref: np.ndarray, label: str) -> None:
    assert got.shape == ref.shape, f"{label}: 形状 {got.shape} != {ref.shape}"
    diff = int((got != ref).sum())
    assert diff == 0, f"{label}: {diff}/{ref.size} 值不符"


@requires_metal
class TestInverseDctMetal:
    # (w, h, pixel_format, bd, entropy)：两族熵 × 444/422 × 10/12-bit ×
    # 非 8 对齐可见区（313×141 / 422 色度 75 宽）。
    REAL_CASES = [
        (96, 64, 1, 10, 8),     # V7-R2 rans2（产品默认）对齐
        (313, 141, 1, 12, 8),   # rans2 非 8 对齐 + 12-bit
        (150, 90, 0, 10, 8),    # rans2 422（色度 75 宽非 8 对齐）
        (96, 64, 1, 10, 0),     # V1 Rice 族
    ]

    @pytest.mark.parametrize("w,h,pf,bd,entropy", REAL_CASES)
    def test_real_packet_three_way(self, w, h, pf, bd, entropy):
        codec = ToposCodec()
        rng = np.random.default_rng(0xA2E0 + w + h + bd + entropy)
        m, mtable, wtable = _frozen_tables(codec)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, entropy),
                                       [p.tobytes() for p in planes])

        ref_planes = _decode_reference(codec, packet, w, h, pf)
        qbuf, meta, qtab_all = _decode_capture(codec, packet, w, h, pf)

        runner = _MetalRunner()
        mid, maxv = 1 << (bd - 1), (1 << bd) - 1
        for p, (ph, pw) in enumerate(dims):
            q2, qtab, row_slice, cols = _assemble_plane(qbuf, meta, qtab_all, p)
            rows = q2.shape[0] // cols
            ref = _reference_plane(q2, qtab, row_slice, m, wtable.astype(np.int64),
                                   rows, cols, ph, pw, mid, maxv)
            _assert_plane_equal(ref, ref_planes[p], f"numpy plane {p}")
            for name in ("inverse_dct_px", "inverse_dct_m8"):
                got = runner.run_dense(name, q2.reshape(-1), qtab, row_slice,
                                       cols, rows, pw, ph, mid, maxv,
                                       mtable, wtable)
                _assert_plane_equal(got, ref_planes[p], f"{name} plane {p}")
            dc, pair_off, pairs = _sparsify(q2)
            for sname in ("inverse_dct_sp", "inverse_dct_w1"):
                got = runner.run_sparse(sname, dc, pair_off, pairs, qtab,
                                        row_slice, cols, rows, pw, ph, mid,
                                        maxv, mtable, wtable)
                _assert_plane_equal(got, ref_planes[p], f"{sname} plane {p}")

    @pytest.mark.parametrize("seed", [0xA2F1, 0xA2F2])
    def test_synthetic_q(self, seed):
        """合成 q：DC-only 块、防御钳位边界（±2^25）、稀疏模式直测数值域。"""
        codec = ToposCodec()
        rng = np.random.default_rng(seed)
        m, mtable, wtable = _frozen_tables(codec)
        rows, cols = 9, 21                 # 168 块；行带 5+4 → 2 个 slice
        vis_w, vis_h = 165, 67             # 非 8 对齐可见区
        mid, maxv = 2048, 4095             # 12-bit
        nblocks = rows * cols
        row_slice = np.zeros(rows, dtype=np.uint32)
        row_slice[5:] = 1
        qtab = rng.integers(1, 4096, (2, 64)).astype(np.uint32)

        q2 = np.zeros((nblocks, 64), dtype=np.int32)
        q2[:, 0] = rng.integers(-(1 << 20), (1 << 20) + 1, nblocks)
        for b in range(nblocks):
            kind = b % 8
            if kind == 0:
                continue                    # DC-only 块
            nnz = 1 + int(rng.integers(0, 20))
            nats = rng.integers(1, 64, nnz)
            q2[b, nats] = rng.choice([-1, 1], nnz) * rng.integers(
                1, 1 << 21, nnz)
        # 钳位边界：少数块直接给 q·Q 超过 ±2^25 的值
        q2[7, 30] = (1 << 28)
        q2[7, 31] = -(1 << 28)

        ref = _reference_plane(q2, qtab, row_slice, m,
                               wtable.astype(np.int64), rows, cols,
                               vis_h, vis_w, mid, maxv)
        runner = _MetalRunner()
        for name in ("inverse_dct_px", "inverse_dct_m8"):
            got = runner.run_dense(name, q2.reshape(-1), qtab, row_slice,
                                   cols, rows, vis_w, vis_h, mid, maxv,
                                   mtable, wtable)
            _assert_plane_equal(got, ref, f"synthetic {name}")
        dc, pair_off, pairs = _sparsify(q2)
        for sname in ("inverse_dct_sp", "inverse_dct_w1"):
            got = runner.run_sparse(sname, dc, pair_off, pairs, qtab,
                                    row_slice, cols, rows, vis_w, vis_h, mid,
                                    maxv, mtable, wtable)
            _assert_plane_equal(got, ref, f"synthetic {sname}")

    @pytest.mark.parametrize("w,h,pf,bd,entropy", [
        (96, 64, 1, 10, 8),
        (313, 141, 1, 12, 8),
        (150, 90, 0, 10, 0),
    ])
    def test_c_sparse_emission_ws(self, w, h, pf, bd, entropy):
        """C worker 直产 CSR（sparse=1：rans2 走生产融合发射）→ ws kernel
        逐字节一致——生产移交契约（无 Python 中转）端到端验收。"""
        codec = ToposCodec()
        _bind_qcap(codec._lib)
        _, mtable, wtable = _frozen_tables(codec)
        rng = np.random.default_rng(0xA2E3 + w + entropy)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, entropy),
                                       [p.tobytes() for p in planes])
        ref_planes = _decode_reference(codec, packet, w, h, pf)
        _, meta, qtab, dc_all, off_all, pairs_all = _capture_csr(
            codec, packet, dims, mode=1)
        for variant in ("ws", "wsd"):
            got_planes = _ws_reconstruct(MetalComputeContext(), meta, qtab, dc_all,
                                         off_all, pairs_all, dims, bd, mtable,
                                         wtable, variant)
            for p in range(len(dims)):
                _assert_plane_equal(got_planes[p], ref_planes[p],
                                    f"inverse_dct_{variant} plane {p}")

    @pytest.mark.parametrize("w,h,pf,bd,entropy", [
        (96, 64, 1, 10, 8),
        (313, 141, 1, 12, 8),   # 非对齐 + 12-bit
        (200, 96, 1, 12, 8),
        (150, 90, 0, 10, 0),    # 422 Rice：两模式同走 legacy（契约健全性）
    ])
    def test_fused_vs_legacy_csr(self, w, h, pf, bd, entropy):
        """A2-4 融合发射（sparse=1）vs legacy 扫描发射（sparse=2）：
        dc/off 逐值一致；pairs 块内多重集一致（发射序 = zigzag 解码序 vs
        自然序，多重集等价即位精确——kernel 求和序固定与装配序无关）。"""
        codec = ToposCodec()
        _bind_qcap(codec._lib)
        rng = np.random.default_rng(0xA2F5 + w + entropy * 7)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, entropy),
                                       [p.tobytes() for p in planes])
        _, meta1, _, dc1, off1, pairs1 = _capture_csr(codec, packet, dims, 1)
        _, meta2, _, dc2, off2, pairs2 = _capture_csr(codec, packet, dims, 2)
        assert [(m.plane, m.block_y0, m.block_h, m.cols) for m in meta1] == \
               [(m.plane, m.block_y0, m.block_h, m.cols) for m in meta2]
        np.testing.assert_array_equal(dc1, dc2)
        np.testing.assert_array_equal(off1, off2)
        # 块内多重集：全局块号 = plane<<24 | (block_y0*cols + i)，
        # 以 off 段差分得每块对数，按 (块, pair) 排序后整体比较
        def _by_block(meta, off_all, pairs):
            ids, counts = [], []
            seg = 0
            for m in meta:
                n = m.block_h * m.cols
                st = off_all[seg:seg + n + 1].astype(np.int64)
                cnt = np.diff(st)
                bid = ((m.plane << 24) + m.block_y0 * m.cols
                       + np.arange(n, dtype=np.int64))
                ids.append(bid)
                counts.append(cnt)
                seg += n + 1
            rep = np.repeat(np.concatenate(ids), np.concatenate(counts))
            order = np.lexsort((pairs, rep))
            return rep[order], pairs[order]

        r1, q1 = _by_block(meta1, off1, pairs1)
        r2, q2 = _by_block(meta2, off2, pairs2)
        np.testing.assert_array_equal(r1, r2)
        np.testing.assert_array_equal(q1, q2)

    def test_concealed_slice_gpu_mid(self):
        """A2-4 conceal 契约：CRC 失败 slice 的 CSR 清零 → GPU 重建 mid，
        与 native conceal_band 的 bd_mid 填充逐值一致（含早失败路径——
        CRC 在解码分支之前，此前该路径 CSR 残留上一帧）。"""
        codec = ToposCodec()
        _bind_qcap(codec._lib)
        w, h, pf, bd = 96, 64, 1, 10
        dims = _plane_dims(pf, w, h)
        rng = np.random.default_rng(0xA2C7)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, 8),
                                       [p.tobytes() for p in planes])

        # 确定性找一个「CRC 失败 → conceal」的注错位（帧头区跳过）
        bad = None
        for off in range(96, len(packet) - 1, 13):
            cand = bytearray(packet)
            cand[off] ^= 0xFF
            try:
                arrays = [np.zeros(d, dtype="<u2") for d in dims]
                with ToposDecoder(codec) as dec:
                    views = dec.make_views(arrays)
                    info = dec.decode_views(bytes(cand), views, len(arrays))
                if info.concealed_slices > 0:
                    bad = bytes(cand)
                    break
            except Exception:
                continue
        assert bad is not None, "未找到触发 conceal 的注错位"

        # 参考：同一坏包无捕获解码（CPU conceal 写 bd_mid）
        ref_arrays = [np.zeros(d, dtype="<u2") for d in dims]
        with ToposDecoder(codec) as dec:
            views = dec.make_views(ref_arrays)
            dec.decode_views(bad, views, len(ref_arrays))
        # 捕获：融合发射 + conceal 清零
        _, meta, qtab, dc_all, off_all, pairs_all = _capture_csr(
            codec, bad, dims, mode=1)
        # 直接验证清零：存在 slice 的 off 段尾 = 0 且其 dc 带区全零
        # （dc 区布局：按 plane 依次 rows_p×cols_p 拼接，同 C 侧 seg 推导）
        seg = 0
        base = {}
        acc = 0
        for m in meta:
            if m.plane not in base:
                rows_p = max(mm.block_y0 + mm.block_h
                             for mm in meta if mm.plane == m.plane)
                base[m.plane] = acc
                acc += rows_p * m.cols
        zeroed = 0
        for m in meta:
            n = m.block_h * m.cols
            if int(off_all[seg + n]) == 0:
                b = base[m.plane] + m.block_y0 * m.cols
                if np.all(dc_all[b:b + n] == 0):
                    zeroed += 1
            seg += n + 1
        assert zeroed >= 1, "conceal slice 的 CSR 未清零"

        _, mtable, wtable = _frozen_tables(codec)
        for variant in ("ws", "wsd"):
            got_planes = _ws_reconstruct(MetalComputeContext(), meta, qtab, dc_all,
                                         off_all, pairs_all, dims, bd, mtable,
                                         wtable, variant)
            for p in range(len(dims)):
                _assert_plane_equal(got_planes[p], ref_arrays[p],
                                    f"conceal {variant} plane {p}")
