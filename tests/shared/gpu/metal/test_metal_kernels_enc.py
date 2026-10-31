"""A-enc（速度计划 A 轴）批 1 验收：quant_token kernel 三方差分。

C token 捕获（``tc_dev_set_tok_capture``）== numpy 整数参考 == Metal
输出，逐值一致（位精确）。numpy 参考与 ``fill_color_band_from_f`` +
``quant.c: tc_quant_block_ctx`` 同源：

- 量化：v=clamp(F,±2^25)；num=|v|+half；num_eff=num>dz?num−dz:0；
  q=num_eff/Q（Q/half/dz 取自捕获——单一真相源，免复刻 qp 钳位链）；
- DC 预测折叠（tc_dc_predict：双邻 round-half-up 均值，符号拆分；
  跨带行链连续）；rice 映射（v≥0→2v；v<0→2|v|−1）；
- (run, lvl) 对按 zigzag 升序（kTcZigzag 经 ``tc_dev_zigzag`` 注入）。

GPU 输入 F 用 numpy 参考值——``test_metal_kernels_dct`` 已三方钉死
numpy F == lib F-cache == Metal DCT 输出，此处等价取用。
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
    TOPOS_CODEC_ABI_VERSION, ToposCodec, _CFrameConfig,
)
from src.shared.gpu.metal.context import MetalComputeContext  # noqa: E402
from src.shared.gpu.metal.kernels_enc import METAL_QUANT_TOKEN_MSL  # noqa: E402
from tests.shared.gpu.metal.test_metal_kernels_dct import (  # noqa: E402
    _pad_edge, _reference_f,
)

_REAL_OK, _REAL_UNAVAILABLE_REASON = MetalComputeContext.availability_reason()
requires_metal = pytest.mark.skipif(
    not _REAL_OK, reason=f"Metal 不可用: {_REAL_UNAVAILABLE_REASON}"
)


class _CTokBand(ctypes.Structure):
    _fields_ = [
        ("plane", ctypes.c_uint32),
        ("y0", ctypes.c_uint32),
        ("h", ctypes.c_uint32),
        ("cols", ctypes.c_uint32),
        ("token_count", ctypes.c_uint32),
    ]


def _bind_tok(lib) -> None:
    for nm, at, rt in [
        ("tc_dev_set_tok_capture", [ctypes.c_int], None),
        ("tc_dev_tok_bands", [], ctypes.c_uint32),
        ("tc_dev_tok_meta_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_dc_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_np_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_run_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_lvl_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_q_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_half_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_dz_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_fdm1_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_tok_fdm0_copy", [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int64),
        ("tc_dev_zigzag", [], ctypes.POINTER(ctypes.c_uint8)),
        ("tc_transform_matrix", [], ctypes.POINTER(ctypes.c_int16)),
    ]:
        f = getattr(lib, nm)
        f.argtypes = at
        f.restype = rt


def _make_fc(w, h, pf, bd, qp):
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 5 if pf == 1 else 3
    fc.pixel_format = pf
    fc.bit_depth = bd
    fc.qp_base = qp
    fc.qmatrix_id = 1
    fc.slice_rows = 8
    fc.sar_num = fc.sar_den = 1
    fc.reserved[0] = 8
    return fc


def _plane_dims(pf, w, h):
    if pf == 0:
        return [(h, w), (h, (w + 1) // 2), (h, (w + 1) // 2)]
    return [(h, w)] * 3


def _capture_tokens(codec, fc, data):
    """tok 捕获编码 → (meta 列表, dc, npair, run, lvl, Q/half/dz[band,64])。"""
    lib = codec._lib
    lib.tc_dev_set_tok_capture(1)
    try:
        codec.encode_frame(fc, data)
        nb = int(lib.tc_dev_tok_bands())
        assert nb > 0
        meta_c = (_CTokBand * nb)()
        assert int(lib.tc_dev_tok_meta_copy(meta_c, nb)) == nb
        meta = [meta_c[i] for i in range(nb)]
        blocks_total = sum(int(m.h) * int(m.cols) for m in meta)
        dc = np.empty(blocks_total, dtype=np.uint32)
        assert int(lib.tc_dev_tok_dc_copy(dc.ctypes.data, dc.size)) == blocks_total
        npair = np.empty(blocks_total, dtype=np.uint16)
        assert int(lib.tc_dev_tok_np_copy(npair.ctypes.data, npair.size)) == blocks_total
        cap_pairs = sum(int(m.token_count) for m in meta)
        run = np.empty(max(1, cap_pairs), dtype=np.uint8)
        nrun = int(lib.tc_dev_tok_run_copy(run.ctypes.data, run.size))
        lvl = np.empty(max(1, cap_pairs), dtype=np.uint32)
        nlvl = int(lib.tc_dev_tok_lvl_copy(lvl.ctypes.data, lvl.size))
        assert nrun == nlvl == cap_pairs
        q = np.empty(nb * 64, dtype=np.uint32)
        half = np.empty(nb * 64, dtype=np.uint32)
        dz = np.empty(nb * 64, dtype=np.uint32)
        fdm1 = np.empty(nb * 64, dtype=np.uint32)
        fdm0 = np.empty(nb * 64, dtype=np.uint32)
        assert int(lib.tc_dev_tok_q_copy(q.ctypes.data, q.size)) == nb * 64
        assert int(lib.tc_dev_tok_half_copy(half.ctypes.data, half.size)) == nb * 64
        assert int(lib.tc_dev_tok_dz_copy(dz.ctypes.data, dz.size)) == nb * 64
        assert int(lib.tc_dev_tok_fdm1_copy(fdm1.ctypes.data, fdm1.size)) == nb * 64
        assert int(lib.tc_dev_tok_fdm0_copy(fdm0.ctypes.data, fdm0.size)) == nb * 64
    finally:
        lib.tc_dev_set_tok_capture(0)
    return (meta, dc, npair, run[:nrun].copy(), lvl[:nlvl].copy(),
            q.reshape(nb, 64), half.reshape(nb, 64), dz.reshape(nb, 64),
            fdm1.reshape(nb, 64), fdm0.reshape(nb, 64))


def _dc_predict(has_left, left, has_top, top):
    if has_left and has_top:
        s = int(left) + int(top)
        return (s + 1) >> 1 if s >= 0 else -((-s) >> 1)
    if has_left:
        return int(left)
    if has_top:
        return int(top)
    return 0


def _rice_map(v: np.ndarray) -> np.ndarray:
    return np.where(v >= 0, v * 2, (-v) * 2 - 1)


@requires_metal
class TestQuantTokenMetal:
    @pytest.mark.parametrize("w,h,pf,bd,qp", [
        (96, 64, 1, 10, 30),
        (313, 141, 1, 12, 58),
        (150, 90, 0, 10, 45),
        (200, 96, 1, 10, 63),
    ])
    def test_three_way_tokens(self, w, h, pf, bd, qp):
        codec = ToposCodec()
        lib = codec._lib
        _bind_tok(lib)
        zz = np.fromiter((int(v) for v in lib.tc_dev_zigzag()[:64]),
                         dtype=np.int64)
        m = np.fromiter((int(v) for v in lib.tc_transform_matrix()[:64]),
                        dtype=np.int64).reshape(8, 8)
        rng = np.random.default_rng(0xAE1 + w + qp)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        data = [p.tobytes() for p in planes]
        fc = _make_fc(w, h, pf, bd, qp)
        mid = 1 << (bd - 1)

        (meta, dc_c, np_c, run_c, lvl_c, Q, half, dz,
         fdm1, fdm0) = _capture_tokens(codec, fc, data)

        ctx = MetalComputeContext()
        kernel = ctx.compile_kernel("quant_token", METAL_QUANT_TOKEN_MSL)
        band_of = {(mm.plane, mm.y0): bi for bi, mm in enumerate(meta)}

        for p, (ph, pw) in enumerate(dims):
            rows, cols = (ph + 7) // 8, (pw + 7) // 8
            padded = np.ascontiguousarray(_pad_edge(planes[p], rows * 8, cols * 8))
            F = _reference_f(padded, mid, m).reshape(rows * cols, 64)

            # ---- numpy 参考（分带量化；DC 折叠全域串行——行链跨带连续）----
            q0_ref = np.empty(rows * cols, dtype=np.int64)
            np_ref = np.empty(rows * cols, dtype=np.int64)
            run_ref, lvl_ref = [], []
            for mm in [x for x in meta if x.plane == p]:
                bi = band_of[(mm.plane, mm.y0)]
                Fb = F[mm.y0 * cols:(mm.y0 + mm.h) * cols].astype(np.int64)
                v = np.clip(Fb, -(1 << 25), 1 << 25)
                mag = np.where(v < 0, -v, v)
                num = mag + half[bi][None, :]
                ne = np.where(num > dz[bi][None, :], num - dz[bi][None, :],
                              0).astype(np.uint64)
                qmag = (ne // Q[bi][None, :].astype(np.uint64)).astype(np.int64)
                qn = np.where(v < 0, -qmag, qmag)
                for i in range(mm.h):
                    lo = (mm.y0 + i) * cols
                    q0_ref[lo:lo + cols] = qn[i * cols:(i + 1) * cols, 0]
                mask = qn[:, zz] != 0
                blk, pidx = np.nonzero(mask[:, 1:])
                pos = np.arange(1, 64, dtype=np.int64)[pidx]
                lvl = qn[blk, zz[pos]]
                prev = np.zeros_like(pos)
                same = np.diff(blk, prepend=blk[0] - 1) == 0
                prev[1:][same[1:]] = pos[:-1][same[1:]]
                run_ref.append(pos - prev - 1)
                lvl_ref.append(_rice_map(lvl))
                cnts = mask[:, 1:].sum(axis=1)
                for i in range(mm.h):
                    lo = (mm.y0 + i) * cols
                    np_ref[lo:lo + cols] = cnts[i * cols:(i + 1) * cols]
            run_ref = (np.concatenate(run_ref) if run_ref
                       else np.array([], np.int64))
            lvl_ref = (np.concatenate(lvl_ref) if lvl_ref
                       else np.array([], np.int64))
            # DC 折叠：带局部上下文（fill 每带 memset——带首行无 top，
            # slice 独立性；带内行链连续）
            dc_fold = np.empty(rows * cols, dtype=np.int64)
            for mm in [x for x in meta if x.plane == p]:
                prev_row = np.zeros(cols, dtype=np.int64)
                row = np.zeros(cols, dtype=np.int64)
                for i in range(mm.h):
                    by = mm.y0 + i
                    for bx in range(cols):
                        pred = _dc_predict(bx > 0, row[bx - 1] if bx > 0 else 0,
                                           i > 0, prev_row[bx])
                        r = int(q0_ref[by * cols + bx]) - pred
                        dc_fold[by * cols + bx] = r * 2 if r >= 0 else (-r) * 2 - 1
                        row[bx] = int(q0_ref[by * cols + bx])
                    prev_row, row = row, prev_row

            # ---- GPU：F 上传 → 逐带 dispatch quant_token ----
            Fbuf = ctx.create_buffer_from_numpy(np.ascontiguousarray(F, dtype=np.int32))
            dcbuf = ctx.create_buffer(rows * cols * 4)
            npbuf = ctx.create_buffer(rows * cols * 2)
            pairsbuf = ctx.create_buffer(rows * cols * 63 * 8)
            zzbuf = ctx.create_buffer_from_numpy(
                np.ascontiguousarray(zz.astype(np.uint8)))
            for mm in [x for x in meta if x.plane == p]:
                bi = band_of[(mm.plane, mm.y0)]
                params = ctx.create_buffer_from_numpy(
                    np.array([cols, mm.y0], dtype=np.int32))
                ctx.dispatch_sync(kernel, buffers={
                    0: Fbuf, 1: dcbuf, 2: npbuf, 3: pairsbuf, 4: params,
                    5: ctx.create_buffer_from_numpy(np.ascontiguousarray(half[bi])),
                    6: ctx.create_buffer_from_numpy(np.ascontiguousarray(dz[bi])),
                    7: ctx.create_buffer_from_numpy(np.ascontiguousarray(fdm1[bi])),
                    8: ctx.create_buffer_from_numpy(np.ascontiguousarray(fdm0[bi])),
                    9: zzbuf},
                    threads=(cols * 8, mm.h * 8, 1), group=(8, 8, 1))
            gpu_dc = np.frombuffer(ctx.read_buffer(dcbuf), dtype=np.int32)
            gpu_np = np.frombuffer(ctx.read_buffer(npbuf), dtype=np.uint16)
            gpu_pairs = np.frombuffer(
                ctx.read_buffer(pairsbuf), dtype="<u8").reshape(rows * cols, 63)
            g_np = gpu_np.astype(np.int64)
            grun = (gpu_pairs & np.uint64(63)).astype(np.int64)
            glvl = (gpu_pairs >> np.uint64(6)).astype(np.int64)
            sel = np.repeat(g_np, 63) > np.tile(np.arange(63, dtype=np.int64),
                                                rows * cols)
            grun = grun.reshape(-1)[sel]
            glvl = glvl.reshape(-1)[sel]

            # ---- C 捕获按 plane 切片 ----
            base = sum(int(x.h) * int(x.cols) for x in meta if x.plane < p)
            nblk_p = rows * cols
            c_dc = dc_c[base:base + nblk_p].astype(np.int64)
            c_np = np_c[base:base + nblk_p].astype(np.int64)
            tok_base = sum(int(x.token_count) for x in meta if x.plane < p)
            tok_n = sum(int(x.token_count) for x in meta if x.plane == p)
            c_run = run_c[tok_base:tok_base + tok_n].astype(np.int64)
            c_lvl = lvl_c[tok_base:tok_base + tok_n].astype(np.int64)

            # ---- 三方比对 ----
            np.testing.assert_array_equal(gpu_dc, q0_ref, err_msg=f"p{p} GPU dc_raw")
            np.testing.assert_array_equal(c_dc, dc_fold, err_msg=f"p{p} C dc_m")
            np.testing.assert_array_equal(c_np, np_ref, err_msg=f"p{p} C npair")
            np.testing.assert_array_equal(g_np, np_ref, err_msg=f"p{p} GPU npair")
            np.testing.assert_array_equal(c_run, run_ref, err_msg=f"p{p} C run")
            np.testing.assert_array_equal(grun, run_ref, err_msg=f"p{p} GPU run")
            np.testing.assert_array_equal(c_lvl, lvl_ref, err_msg=f"p{p} C lvl")
            np.testing.assert_array_equal(glvl, lvl_ref, err_msg=f"p{p} GPU lvl")
