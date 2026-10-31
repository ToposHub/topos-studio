"""A1（速度计划 v2）MSL forward-DCT kernel 三方差分验收。

native F-cache（``tc_dev_fcache_capture`` 导出）== numpy 整数参考 ==
Metal 输出，逐值一致（位精确）。numpy 参考实现与
``tests/native/test_a1_fcache_differential.py`` 同源（规范 =
transform.c:18-41 两趟整数矩阵乘 + plane.c 边缘复制 pad）；kTransformM
取自库内冻结表（``tc_transform_matrix``）——单一真相源。
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
from src.shared.gpu.metal.kernels_dct import METAL_FORWARD_DCT_MSL  # noqa: E402

_REAL_OK, _REAL_UNAVAILABLE_REASON = MetalComputeContext.availability_reason()
requires_metal = pytest.mark.skipif(
    not _REAL_OK, reason=f"Metal 不可用: {_REAL_UNAVAILABLE_REASON}"
)


def _pad_edge(plane: np.ndarray, dst_h: int, dst_w: int) -> np.ndarray:
    h, w = plane.shape
    out = np.empty((dst_h, dst_w), dtype=plane.dtype)
    out[:h, :w] = plane
    out[:h, w:] = plane[:, w - 1: w]
    out[h:, :] = out[h - 1: h, :]
    return out


def _reference_f(plane_padded: np.ndarray, mid: int, m: np.ndarray) -> np.ndarray:
    h, w = plane_padded.shape
    rows, cols = h // 8, w // 8
    x = plane_padded.astype(np.int64).reshape(rows, 8, cols, 8)
    x = x.transpose(0, 2, 1, 3).reshape(rows * cols, 8, 8) - mid
    mi = m.astype(np.int64)
    a = np.einsum("nyc,vc->nyv", x, mi)
    f = np.einsum("uy,nyv->nuv", mi, a)
    return f.astype(np.int32).reshape(rows * cols, 64)


def _capture_lib_f(codec: ToposCodec, w: int, h: int, bd: int,
                   planes: list) -> tuple:
    """sized 编码（m7 路径）并捕获 F-cache；返回 (三平面 F 列表, off)。"""
    lib = codec._lib
    lib.tc_dev_set_fcache_capture.argtypes = [ctypes.c_int]
    lib.tc_dev_set_fcache_capture.restype = None
    lib.tc_dev_fcache_elems.argtypes = []
    lib.tc_dev_fcache_elems.restype = ctypes.c_int64
    lib.tc_dev_fcache_copy.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.tc_dev_fcache_copy.restype = ctypes.c_int64
    lib.tc_dev_fcache_layout.argtypes = [
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    lib.tc_dev_fcache_layout.restype = None
    lib.tc_transform_matrix.argtypes = []
    lib.tc_transform_matrix.restype = ctypes.POINTER(ctypes.c_int16)

    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 5
    fc.pixel_format = 1  # 444
    fc.bit_depth = bd
    fc.qp_base = 30
    fc.qmatrix_id = 0
    fc.slice_rows = 8
    fc.sar_num = fc.sar_den = 1
    fc.reserved[0] = 8

    lib.tc_dev_set_fcache_capture(1)
    try:
        codec.encode_sized(fc, planes, max(1, codec.packet_bound(fc) // 8), 0, 63)
        elems = int(lib.tc_dev_fcache_elems())
        buf = np.empty(elems, dtype=np.int32)
        assert int(lib.tc_dev_fcache_copy(buf.ctypes.data, buf.size)) == elems
        off = (ctypes.c_uint32 * 3)()
        pn = ctypes.c_uint32(0)
        lib.tc_dev_fcache_layout(off, ctypes.byref(pn))
        assert pn.value == 3
    finally:
        lib.tc_dev_set_fcache_capture(0)
    m = np.fromiter((int(v) for v in lib.tc_transform_matrix()[:64]),
                    dtype=np.int64).reshape(8, 8)
    fs = []
    for p in range(3):
        b = int(off[p]) * 64
        e = int(off[p + 1]) * 64 if p < 2 else elems
        fs.append(buf[b:e])
    return fs, m


@requires_metal
class TestForwardDctMetal:
    # (grid 缩放, grid z 维, group) —— 四变体共用同一数值契约。
    # rows 变体一线程一行 F（z=8）；tg/prod 变体一线程一个系数（xy 各
    # ×8，threadgroup 数 = 块数）；基准版一线程一块。
    KERNEL_VARIANTS = {
        "forward_dct": (1, 1, (8, 8, 1)),
        "forward_dct_rows": (1, 8, (8, 8, 8)),
        "forward_dct_tg": (8, 1, (8, 8, 1)),
        "forward_dct_prod": (8, 1, (8, 8, 1)),
        "forward_dct_tg4": (8, 1, (8, 8, 1)),
        # m8：一组 8 块——网格 x = ceil(cols/8) 组（threads 按 8 对齐）
        "forward_dct_m8": (0, 1, (8, 8, 1)),
    }

    @pytest.mark.parametrize("kernel_name", [
        "forward_dct", "forward_dct_rows", "forward_dct_tg",
        "forward_dct_prod", "forward_dct_tg4", "forward_dct_m8"])
    @pytest.mark.parametrize("w,h,bd,seed", [
        (160, 112, 10, 0xA1E1),
        (64, 48, 12, 0xA1E2),   # 12-bit level shift
        (63, 45, 10, 0xA1E3),   # 非 8 对齐 pad 路径
    ])
    def test_three_way_bitexact(self, kernel_name, w, h, bd, seed):
        codec = ToposCodec()
        rng = np.random.default_rng(seed)
        planes = [rng.integers(0, 1 << bd, (h, w)).astype("<u2") for _ in range(3)]
        lib_fs, m = _capture_lib_f(codec, w, h, bd, planes)

        coded_w = (w + 7) // 8 * 8
        coded_h = (h + 7) // 8 * 8
        rows, cols = coded_h // 8, coded_w // 8
        mid = 1 << (bd - 1)

        ctx = MetalComputeContext()
        kernel = ctx.compile_kernel(kernel_name, METAL_FORWARD_DCT_MSL)
        grid_scale, grid_z, group = self.KERNEL_VARIANTS[kernel_name]
        if grid_scale == 0:   # m8：x 向组数 = ceil(cols/8)，threads 需块对齐
            threads = (((cols + 7) // 8) * 8, rows * 8, 1)
        else:
            threads = (cols * grid_scale, rows * grid_scale, grid_z)
        if kernel_name == "forward_dct_prod":
            # buffer3 = 预乘积表 prod[(u*8+v)*64 + y*8+x] = M[u][y]·M[v][x]
            table = np.einsum("uy,vx->uvyx", m, m).astype(np.int16)
        else:
            table = m.astype(np.int16)
        for p in range(3):
            padded = np.ascontiguousarray(_pad_edge(planes[p], coded_h, coded_w))
            src = ctx.create_buffer_from_numpy(padded)
            dst = ctx.create_buffer(rows * cols * 64 * 4)
            params = ctx.create_buffer_from_numpy(
                np.array([cols, mid], dtype=np.int32))
            mtable = ctx.create_buffer_from_numpy(table)
            ctx.dispatch_sync(
                kernel,
                buffers={0: src, 1: dst, 2: params, 3: mtable},
                threads=threads,
                group=group,
            )
            got = np.frombuffer(ctx.read_buffer(dst), dtype=np.int32)
            ref = _reference_f(padded, mid, m)
            assert np.array_equal(got, ref.reshape(-1)), (
                f"{kernel_name} plane {p}: Metal vs numpy 参考 "
                f"{int((got != ref.reshape(-1)).sum())} 值不符"
            )
            assert np.array_equal(got, lib_fs[p].reshape(-1)), (
                f"{kernel_name} plane {p}: Metal vs native F-cache "
                f"{int((got != lib_fs[p].reshape(-1)).sum())} 值不符"
            )
