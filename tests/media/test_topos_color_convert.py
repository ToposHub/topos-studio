"""P1-12：native 融合输入转换（tc_convert_packed_rgb）逐位一致 + 编码器集成。

门：
1. `convert_packed_rgb` 输出与 ToposVideoEncoder 的 numpy 参考实现
   （`_rgb_to_yuv_planar`/`_rgb_to_gbrp`/`_box_subsample_h` + alpha 量化）
   **np.array_equal 逐位一致**——覆盖 dtype×布局×矩阵×range×位深×
   出面模式×奇偶尺寸×alpha×线程数。
2. 编码器级：同一 packed 帧序列，native 快路与强制 numpy 路径
   （TOPOS_ENCODER_DISABLE_FUSED_CVT=1）产出逐字节相同码流。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

from src.shared.codec.topos_binding import (
    CVT_BGR24,
    CVT_BGR48,
    CVT_BGRA32,
    CVT_BGRA64,
    CVT_OUT_GBR,
    CVT_OUT_YUV422,
    CVT_OUT_YUV444,
    CVT_RGB24,
    CVT_RGB48,
    CVT_RGBA32,
    CVT_RGBA64,
    ToposCodec,
)
from src.shared.export import topos_encoder as te

_MATRICES = {
    "bt709": (0.2126, 0.0722),
    "bt601": (0.299, 0.114),
    "bt2020nc": (0.2627, 0.0593),
}


def _make_src(w: int, h: int, nch: int, dtype) -> np.ndarray:
    rng = np.random.RandomState(w * 1000 + h + nch)
    info = np.iinfo(dtype)
    data = rng.randint(0, int(info.max) + 1, size=(h, w, nch)).astype(dtype)
    return np.ascontiguousarray(data)


def _numpy_reference(data: np.ndarray, pixel_format: str, out_mode: int,
                     bit_depth: int, matrix_name: str, full_range: bool,
                     alpha_bit_depth: int):
    """复刻 ToposVideoEncoder._prepare_packed 的参考路径（数学源同文件）。"""
    max_val = float(np.iinfo(data.dtype).max)
    rgbf = data.astype(np.float32) / max_val
    nch = data.shape[2]
    if nch == 4:
        if pixel_format.startswith('bgr'):
            rgbf = rgbf[..., [2, 1, 0, 3]]
    elif pixel_format.startswith('bgr'):
        rgbf = rgbf[..., ::-1]

    if out_mode == CVT_OUT_GBR:
        y, u, v = te._rgb_to_gbrp(rgbf[..., :3], bit_depth)
    else:
        kr, kb = _MATRICES[matrix_name]
        y, u, v = te._rgb_to_yuv_planar(
            rgbf[..., :3], matrix_name, full_range, bit_depth=bit_depth,
            chroma_full=(out_mode == CVT_OUT_YUV444))
    planes = [y, u, v]
    if nch == 4:
        a_nmax = (1 << alpha_bit_depth) - 1
        shift = 16 - alpha_bit_depth
        a = np.clip(np.rint(rgbf[..., 3] * a_nmax), 0, a_nmax).astype('<u4')
        planes.append((a << shift).astype('<u2'))
    return planes


def _cvt_fmt(dtype, pixel_format: str, nch: int) -> int:
    three = {3: (CVT_BGR24, CVT_RGB24), 4: (CVT_BGRA32, CVT_RGBA32)}
    sixteen = {3: (CVT_BGR48, CVT_RGB48), 4: (CVT_BGRA64, CVT_RGBA64)}
    table = sixteen if dtype == np.uint16 else three
    if pixel_format.startswith('bgr'):
        return table[nch][0]
    return table[nch][1]


@pytest.fixture(scope="module")
def codec() -> ToposCodec:
    c = ToposCodec()
    if not c.has_cvt:
        pytest.skip("dylib 缺 tc_convert_packed_rgb（旧构建）")
    return c


@pytest.mark.parametrize("dtype,prefix,nch", [
    (np.uint8, "bgr", 3), (np.uint8, "rgb", 3),
    (np.uint8, "bgr", 4), (np.uint8, "rgb", 4),
    (np.uint16, "bgr", 3), (np.uint16, "rgb", 4),
])
@pytest.mark.parametrize("out_mode", [CVT_OUT_YUV422, CVT_OUT_YUV444, CVT_OUT_GBR])
@pytest.mark.parametrize("bit_depth", [10, 12])
@pytest.mark.parametrize("full_range", [False, True])
@pytest.mark.parametrize("matrix_name", list(_MATRICES.keys()))
@pytest.mark.parametrize("w,h", [(34, 9), (33, 7), (2, 2), (64, 3)])
def test_native_matches_numpy_bit_exact(
    codec, dtype, prefix, nch, out_mode, bit_depth, full_range, matrix_name, w, h,
):
    data = _make_src(w, h, nch, dtype)
    pixel_format = f"{prefix}{'a' if nch == 4 else ''}"
    kr, kb = _MATRICES[matrix_name]
    alpha_shift = 8  # 8-bit alpha 语义（16-8）
    planes = codec.convert_packed_rgb(
        data.reshape(h, w * nch), w, h,
        fmt=_cvt_fmt(dtype, pixel_format, nch), out_mode=out_mode,
        bit_depth=bit_depth, kr=kr, kb=kb, full_range=full_range,
        alpha_shift=alpha_shift,
        alpha_opaque=False, max_workers=1,
    )
    expect = _numpy_reference(data, pixel_format, out_mode, bit_depth,
                              matrix_name, full_range, alpha_bit_depth=8)
    assert len(planes) == len(expect)
    for got, exp in zip(planes, expect):
        np.testing.assert_array_equal(got, exp)


@pytest.mark.parametrize("workers", [2, 4, 8])
def test_worker_parity(codec, workers):
    data = _make_src(67, 17, 4, np.uint8)
    one = codec.convert_packed_rgb(
        data.reshape(17, 67 * 4), 67, 17, fmt=CVT_BGRA32, max_workers=1)
    many = codec.convert_packed_rgb(
        data.reshape(17, 67 * 4), 67, 17, fmt=CVT_BGRA32, max_workers=workers)
    for a, b in zip(one, many):
        np.testing.assert_array_equal(a, b)


@pytest.mark.parametrize("width", [7, 8, 34])
@pytest.mark.parametrize("workers", [1, 4])
def test_native_planar_444_to_422_matches_numpy(codec, width, workers):
    """444→422 平面辅助 API 与导出参考路径逐位一致。"""
    if not getattr(codec, "_has_planar_cvt", False):
        pytest.skip("dylib 缺 tc_convert_yuv444_to_422（旧构建）")
    height = 9
    u = np.arange(width * height, dtype="<u2").reshape(height, width)
    v = (u * 17 + 3).astype("<u2")
    got = codec.convert_yuv444_to_422(
        u, v, bit_depth=10, max_workers=workers)
    expect = []
    for plane in (u, v):
        padded = (np.pad(plane, ((0, 0), (0, 1)), mode="edge")
                  if width & 1 else plane)
        expect.append(((padded[:, 0::2].astype(np.uint32)
                        + padded[:, 1::2].astype(np.uint32) + 1) >> 1)
                      .astype("<u2"))
    for actual, reference in zip(got, expect):
        np.testing.assert_array_equal(actual, reference)


def test_opaque_alpha_and_reuse(codec):
    data = _make_src(20, 6, 3, np.uint8)
    planes = codec.convert_packed_rgb(
        data.reshape(6, 60), 20, 6, fmt=CVT_BGR24,
        alpha_shift=8, alpha_opaque=True)
    # 不透明 = a_nmax<<shift = 255<<8
    np.testing.assert_array_equal(planes[3], np.full((6, 20), 255 << 8, '<u2'))
    # planes_out 复用：同缓冲再转换，结果一致（无写偏）
    again = codec.convert_packed_rgb(
        data.reshape(6, 60), 20, 6, fmt=CVT_BGR24,
        alpha_shift=8, alpha_opaque=True, planes_out=planes)
    assert again[0] is planes[0]
    for a, b in zip(planes, again):
        np.testing.assert_array_equal(a, b)


def _make_encoder(tmp_path, name: str, w: int = 64, h: int = 32):
    cfg = SimpleNamespace(width=w, height=h, fps=25.0,
                          pix_fmt="yuv422p10le", profile="hq", crf=None,
                          has_alpha=False)
    return te.ToposVideoEncoder(str(tmp_path / name), cfg)


class _Frame:
    def __init__(self, data, w, h, index):
        self.data = data
        self.is_planar = False
        self.pixel_format = "bgr24"
        self.width = w
        self.height = h
        self.frame_index = index


def _packets_for(enc, frames):
    enc.open()
    try:
        return [enc.encode_frame(f) for f in frames]
    finally:
        enc.close()


def test_encoder_packets_identical_native_vs_numpy(tmp_path, monkeypatch):
    """编码器级：native 快路与强制 numpy 路径产出逐字节相同码流（文件级）。"""
    if not ToposCodec().has_cvt:
        pytest.skip("dylib 缺 tc_convert_packed_rgb（旧构建）")
    frames = [
        _Frame(_make_src(64, 32, 3, np.uint8), 64, 32, i) for i in range(4)
    ]
    out_a = tmp_path / "native.mov"
    enc_native = te.ToposVideoEncoder(
        str(out_a), SimpleNamespace(
            width=64, height=32, fps=25.0, pix_fmt="yuv422p10le",
            profile="hq", crf=None, has_alpha=False))
    enc_native.open()
    try:
        for f in frames:
            enc_native.encode_frame(f)
    finally:
        enc_native.close()

    monkeypatch.setenv("TOPOS_ENCODER_DISABLE_FUSED_CVT", "1")
    out_b = tmp_path / "numpy.mov"
    enc_numpy = te.ToposVideoEncoder(
        str(out_b), SimpleNamespace(
            width=64, height=32, fps=25.0, pix_fmt="yuv422p10le",
            profile="hq", crf=None, has_alpha=False))
    enc_numpy.open()
    try:
        for f in frames:
            enc_numpy.encode_frame(f)
    finally:
        enc_numpy.close()
    monkeypatch.delenv("TOPOS_ENCODER_DISABLE_FUSED_CVT")

    assert out_a.stat().st_size == out_b.stat().st_size > 0
    assert out_a.read_bytes() == out_b.read_bytes()


def test_encoder_native_fallback_on_symbol_absent(tmp_path):
    """旧 dylib（缺符号）→ 编码器静默回退 numpy 路径，功能可用。"""
    frames = [_Frame(_make_src(64, 32, 3, np.uint8), 64, 32, i) for i in range(2)]
    out = tmp_path / "fallback.mov"
    enc = te.ToposVideoEncoder(
        str(out), SimpleNamespace(
            width=64, height=32, fps=25.0, pix_fmt="yuv422p10le",
            profile="hq", crf=None, has_alpha=False))
    enc.open()
    enc._cvt_available = False  # 模拟旧 dylib
    try:
        for f in frames:
            enc.encode_frame(f)  # 不抛异常即回退成功
    finally:
        enc.close()
    assert out.stat().st_size > 0
