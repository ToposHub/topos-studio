"""HALF 样本域（.toos float16，spec §15，2026-09-19）Python 通路测试。

覆盖：
- 冻结映射：numpy 实现 vs native ``tc_image_half_to_codes``/``codes_to_half``
  全 65536 值逐位一致（P1-12 契约模式）+ golden 向量；
- ``ToposImageSequenceEncoder`` gbrph16le / topos_gbraph16a16 → .toos：
  IDSC sample_kind=HALF、image_profile 4、transfer=linear(8)；
  qp0（Q=1）+ 码域有界内容逐位无损（native test_half_map 同款内容）；
  crf=20（Q=8 工作地板）HDR 内容逐位无损——实测无损窗口 qp 11–36
  （2026-09-19，tools 复测；qp≥48 出现 ±2 half-ULP）；
- ``decode_float``：HALF → float16 平面；UINT 文件显式拒绝（样本域不混用）；
- 构造器交叉校验：HALF 需 4444 档（codec profile 5）、alpha 仅 mode1（a16）。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import numpy as np
import pytest

from src.shared.codec import topos_binding
from src.shared.codec.topos_image_binding import (
    TC_IMG_PROFILE_HALF_FLOAT,
    TC_IMG_SAMPLE_KIND_HALF,
    codes_to_half_bits,
    get_image_codec,
    half_bits_to_codes,
)
from src.shared.export.topos_encoder import (
    ToposImageSequenceEncoder,
    parse_topos_pix_fmt,
)
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


def _lib_available() -> bool:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit:
        return Path(explicit).is_file()
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


pytestmark = pytest.mark.skipif(
    not _lib_available(), reason="libtopos_codec 未构建"
)

W, H = 96, 64


# ---------------- 冻结映射 ----------------

class TestHalfMapping:
    def test_golden(self) -> None:
        # (half 位模式, 码值)：spec §15 golden 向量（与 native 同表）
        gold = [
            (0x0000, 0x8000),  # +0.0 → 码 0x8000（level shift 后为 0）
            (0x8000, 0x8000),  # -0.0 → 归一到 +0 的码
            (0x0001, 0x8001),  # +2^-24 最小次正规
            (0x3800, 0xB800),  # +0.5
            (0x3C00, 0xBC00),  # +1.0
            (0xBC00, 0x4400),  # -1.0
            (0x7BFF, 0xFBFF),  # +65504 最大有限
            (0xFBFF, 0x0401),  # -65504
            (0x7C00, 0xFC00),  # +Inf
            (0xFC00, 0x0400),  # -Inf
            (0x7E00, 0xFE00),  # qNaN
        ]
        h = np.array([g[0] for g in gold], dtype='<u2')
        c = np.array([g[1] for g in gold], dtype='<u2')
        np.testing.assert_array_equal(half_bits_to_codes(h), c)
        # 逆映射：±0 统一返回 +0（0x0000），其余逐点还原
        back = codes_to_half_bits(c)
        expect = h.copy()
        expect[1] = 0x0000
        np.testing.assert_array_equal(back, expect)
        # 码 0（前向不可达）饱和到 0xFFFF（-NaN）
        assert int(codes_to_half_bits(np.array([0], '<u2'))[0]) == 0xFFFF

    def test_parity_with_native_full_domain(self) -> None:
        """numpy 映射与 native 批量 API 在全 65536 域逐位一致。"""
        codec = get_image_codec()
        if not codec._has_native_half_map:
            pytest.skip("native 库无 tc_image_half_to_codes（旧构建）")
        lib = codec._lib
        all16 = np.arange(65536, dtype='<u2')
        c_native = np.empty(65536, dtype='<u2')
        rc = lib.tc_image_half_to_codes(
            all16.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
            ctypes.c_size_t(65536),
            c_native.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)))
        assert rc == 0
        np.testing.assert_array_equal(half_bits_to_codes(all16), c_native)

        h_native = np.empty(65536, dtype='<u2')
        rc = lib.tc_image_codes_to_half(
            c_native.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
            ctypes.c_size_t(65536),
            h_native.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)))
        assert rc == 0
        # -0 归一为 +0 后应与 numpy 逆映射一致（numpy 同语义）
        np.testing.assert_array_equal(codes_to_half_bits(c_native), h_native)

    def test_monotonic_finite(self) -> None:
        pos = np.arange(0x0001, 0x7C00, dtype='<u2')  # 正有限（含次正规）
        codes = half_bits_to_codes(pos).astype(np.int64)
        assert bool(np.all(np.diff(codes) > 0))
        neg = np.arange(0x8001, 0xFC00, dtype='<u2')  # 负有限（值降 → 码降）
        codes = half_bits_to_codes(neg).astype(np.int64)
        assert bool(np.all(np.diff(codes) < 0))


# ---------------- 编码器 → .toos → decode_float ----------------

def _enc_config(pix_fmt: str, crf: int | None = 20, has_alpha: bool = False,
                profile: str = '4444') -> FFmpegEncoderConfig:
    return FFmpegEncoderConfig(
        codec="topos_image_sequence",
        width=W, height=H, fps=Rational(24, 1),
        pix_fmt=pix_fmt, profile=profile, crf=crf, has_alpha=has_alpha,
    )


def _frame_from_half_planes(planes16: list[np.ndarray], alpha16: np.ndarray | None,
                            index: int = 0) -> DecodedFrame:
    """half 位模式平面 → float32 RGB(A) 帧（float16→float32 精确无损）。"""
    nch = 4 if alpha16 is not None else 3
    data = np.zeros((H, W, nch), dtype=np.float32)
    data[..., 0] = planes16[2].view(np.float16).astype(np.float32)  # R
    data[..., 1] = planes16[0].view(np.float16).astype(np.float32)  # G
    data[..., 2] = planes16[1].view(np.float16).astype(np.float32)  # B
    if alpha16 is not None:
        data[..., 3] = alpha16.view(np.float16).astype(np.float32)
    return DecodedFrame(
        frame_index=index, pts=index, time_seconds=index / 24.0,
        data=data, pixel_format="rgba" if nch == 4 else "rgb24",
        width=W, height=H,
    )


def _bounded_half_planes() -> tuple[list[np.ndarray], np.ndarray]:
    """码域有界内容（native test_half_map 同款，与测试顺序无关的固定种子）。

    Q=1（qp≤10）token 域前提：block DC 码值偏移 ≤0x3000。
    """
    s = 0x48D1590F1C2E3617

    def rnd() -> int:
        nonlocal s
        s ^= (s >> 12) & 0xFFFFFFFFFFFF
        s &= 0xFFFFFFFFFFFFFFFF
        s ^= (s << 25) & 0xFFFFFFFFFFFFFFFF
        s &= 0xFFFFFFFFFFFFFFFF
        s ^= (s >> 27) & 0xFFFFFFFFFFFF
        s &= 0xFFFFFFFFFFFFFFFF
        return (s * 0x2545F4914F6CDD1D >> 32) & 0xFFFFFFFF

    n = W * H
    g = np.empty(n, '<u2'); b = np.empty(n, '<u2')
    r = np.empty(n, '<u2'); a = np.empty(n, '<u2')
    for i in range(n):
        mg = (i * 0x5800) // (n - 1) - 0x2C00
        g[i] = (0x8000 | -mg) if mg < 0 else mg
        hb = (rnd() % 0x2C01) * (1 if rnd() & 1 else -1)
        if i == 0: hb = 0
        elif i == 1: hb = 0x7C00   # +Inf
        elif i == 2: hb = -0x7C00  # -Inf
        elif i == 3: hb = 0x7E00   # qNaN
        elif i == 4: hb = -1       # -2^-24
        b[i] = (0x8000 | -hb) if hb < 0 else hb
        mr = (i % 97) - 48
        r[i] = (0x8000 | -mr) if mr < 0 else mr
        ma = (rnd() % 0x2C01) * (1 if rnd() & 1 else -1)
        a[i] = (0x8000 | -ma) if ma < 0 else ma
    return ([x.reshape(H, W).copy() for x in (g, b, r)], a.reshape(H, W).copy())


def _hdr_half_planes() -> tuple[list[np.ndarray], np.ndarray]:
    """HDR 线性内容：负值、>1.0 高光、纹理与噪声（crf=20 实测无损窗口内）。"""
    yy, xx = np.mgrid[0:H, 0:W]
    rng = np.random.default_rng(7)
    rgb = np.zeros((H, W, 3), np.float32)
    rgb[..., 0] = -2.0 + 4.0 * xx / W                 # 负值 → 正值 ramp
    rgb[..., 1] = (rng.random((H, W)) - 0.5) * 0.1    # 零均值噪声
    rgb[..., 2] = 0.05 * np.sin(xx / 7.0) + 0.02      # 平滑纹理
    rgb[0, 0, 2] = 6.0                                 # 孤立 HDR 高光
    rgb[1, 1, 2] = 16.0
    alpha = (xx / W * 0.5).astype(np.float32)          # 0..0.5
    planes = [np.ascontiguousarray(rgb[..., c]).astype(np.float16).view('<u2')
              for c in range(3)]
    a16 = np.ascontiguousarray(alpha).astype(np.float16).view('<u2')
    return planes, a16


class TestHalfExportRoundtrip:
    def test_lossy_float_tier_rejects_nonfinite_before_publish(self, tmp_path) -> None:
        planes, _ = _hdr_half_planes()
        planes[0][0, 0] = 0x7C00  # +Inf
        out = tmp_path / "nonfinite-lossy"
        enc = ToposImageSequenceEncoder(
            str(out), _enc_config('gbrph16le', profile='float-ultra'))
        enc.open()
        with pytest.raises(RuntimeError, match="Inf/NaN"):
            enc.encode_frame(_frame_from_half_planes(planes, None))
        enc.abort()
        assert not list(out.glob("*.toos"))

    def test_qp0_bounded_bitexact_no_alpha(self, tmp_path) -> None:
        planes, _ = _bounded_half_planes()
        out = tmp_path / "half-q0"
        enc = ToposImageSequenceEncoder(str(out), _enc_config('gbrph16le', crf=0))
        enc.open(); enc.encode_frame(_frame_from_half_planes(planes, None)); enc.close()

        codec = get_image_codec()
        path = str(out / "frame_0001.toos")
        info = codec.validate(path, deep=True)
        assert info.sample_kind == TC_IMG_SAMPLE_KIND_HALF
        assert info.image_profile == TC_IMG_PROFILE_HALF_FLOAT
        assert info.bit_depth == 16
        assert info.pixel_format == 2
        assert info.color_transfer == 8  # linear（HALF 冻结契约）
        assert not info.has_alpha

        fplanes, finfo, _ = codec.decode_float(path)
        assert finfo.sample_kind == TC_IMG_SAMPLE_KIND_HALF
        for got, src in zip(fplanes, planes):  # G,B,R 序
            assert got.dtype == np.float16
            np.testing.assert_array_equal(got.view('<u2'), src)

    def test_crf20_hdr_bitexact_with_alpha(self, tmp_path) -> None:
        planes, a16 = _hdr_half_planes()
        out = tmp_path / "half-hdr"
        enc = ToposImageSequenceEncoder(
            str(out), _enc_config('topos_gbraph16a16', crf=20, has_alpha=True))
        enc.open(); enc.encode_frame(_frame_from_half_planes(planes, a16)); enc.close()

        codec = get_image_codec()
        path = str(out / "frame_0001.toos")
        info = codec.validate(path, deep=True)
        assert info.sample_kind == TC_IMG_SAMPLE_KIND_HALF
        assert info.alpha_mode == 1 and info.alpha_bit_depth == 16

        fplanes, _, _ = codec.decode_float(path)
        assert len(fplanes) == 4
        for got, src in zip(fplanes[:3], planes):
            np.testing.assert_array_equal(got.view('<u2'), src)
        np.testing.assert_array_equal(fplanes[3].view('<u2'), a16)

    def test_decode_float_rejects_uint_file(self, tmp_path) -> None:
        """样本域不混用：UINT .toos 上 decode_float 显式报错。"""
        from src.shared.codec.topos_binding import (
            TOPOS_CODEC_ABI_VERSION, ToposCodec, _CFrameConfig,
        )
        codec_c = ToposCodec()
        cfg = _CFrameConfig()
        cfg.struct_size = ctypes.sizeof(_CFrameConfig)
        cfg.abi_version = TOPOS_CODEC_ABI_VERSION
        cfg.visible_width, cfg.visible_height = 32, 16
        cfg.profile, cfg.pixel_format, cfg.bit_depth = 5, 2, 10
        pkt = codec_c.encode(cfg, [np.zeros((16, 32), '<u2') for _ in range(3)])
        uint_path = tmp_path / "uint.toos"
        get_image_codec().write_packet(str(uint_path), pkt, 1)  # Image HQ

        with pytest.raises(Exception, match="sample_kind"):
            get_image_codec().decode_float(str(uint_path))

    def test_half_requires_4444_tier(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="4444"):
            ToposImageSequenceEncoder(
                str(tmp_path / "x"), _enc_config('gbrph16le', profile='standard'))

    def test_half_alpha_requires_mode1_name(self, tmp_path) -> None:
        # gbrph16le + has_alpha（隐式 mode2 a12）→ 构造期拒绝
        with pytest.raises(ValueError, match="mode1"):
            ToposImageSequenceEncoder(
                str(tmp_path / "y"),
                _enc_config('gbrph16le', crf=20, has_alpha=True))


class TestPixFmtParsing:
    def test_half_names_parse(self) -> None:
        assert parse_topos_pix_fmt('gbrph16le') == ('planar', 2, 16, None)
        assert parse_topos_pix_fmt('topos_gbraph16a16') == ('planar', 2, 16, 16)
        assert parse_topos_pix_fmt('gbrp16le') == ('planar', 2, 16, None)  # 整数档不变

    def test_integer_float_domain_rejects_non_float_half_input(self, tmp_path) -> None:
        """HALF 样本域只接受 float 输入——uint8 帧显式报错（不静默标定）。"""
        out = tmp_path / "half-int"
        enc = ToposImageSequenceEncoder(str(out), _enc_config('gbrph16le', crf=20))
        enc.open()
        data = np.zeros((H, W, 3), np.uint8)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=data, pixel_format="rgb24", width=W, height=H)
        with pytest.raises(RuntimeError, match="float"):
            enc.encode_frame(frame)
        enc.abort()


class TestHalfScaledDecode:
    """G11：HALF × target_size 缩放解码钉扎（此前零覆盖）。

    钉住三件事：几何（包围盒内保比）、half 域 dtype/值域、decode 与
    decode_float 双入口码域逐位一致；native reduced 与 python_resize
    两路径都须满足（路径经 extra['target_size_native'] 可见）。
    """

    @staticmethod
    def _smooth_half_planes() -> list[np.ndarray]:
        """平滑有限内容（值域 ±0.25，qp0 token 域内）——缩放解码的均值
        保持断言需要无孤立尖峰的内容（尖峰在重采样网格上的落点差会主导
        全局均值差）。"""
        yy, xx = np.mgrid[0:H, 0:W]
        # 幅值 <0.0625（half 位模式偏移 <0x2C00——指数段 10 内，与
        # _bounded_half_planes 同口径，qp0 token 域内）
        g = (0.06 - 0.12 * xx / W).astype(np.float16)
        b = (0.05 * np.sin(xx / 9.0)).astype(np.float16)
        r = (0.06 * yy / H - 0.03).astype(np.float16)
        return [np.ascontiguousarray(p).view('<u2') for p in (g, b, r)]

    def _write_half_file(self, tmp_path: Path, finite: bool = False) -> str:
        # bounded 内容含故意的 ±Inf/qNaN 样本（映射 golden 用）——均值类
        # 断言须用平滑有限内容
        if finite:
            planes = self._smooth_half_planes()
            crf = 20   # qp0 的 AC level 预算对真实浮点内容过紧——均值
                       # 断言不依赖无损，Q=8 即可
        else:
            planes, _ = _bounded_half_planes()
            crf = 0
        out = tmp_path / "half-scaled"
        enc = ToposImageSequenceEncoder(str(out), _enc_config('gbrph16le',
                                                              crf=crf))
        enc.open()
        enc.encode_frame(_frame_from_half_planes(planes, None))
        enc.close()
        return str(out / "frame_0001.toos")

    def test_decode_float_target_size_geometry_and_domain(
            self, tmp_path) -> None:
        codec = get_image_codec()
        path = self._write_half_file(tmp_path, finite=True)
        full, finfo, _ = codec.decode_float(path)
        assert (finfo.visible_width, finfo.visible_height) == (W, H)

        splanes, sinfo, extra = codec.decode_float(path, target_size=(48, 48))
        assert sinfo.sample_kind == TC_IMG_SAMPLE_KIND_HALF
        # 96x64 → 48x48 包围盒内保比 = 48x32
        assert (sinfo.visible_width, sinfo.visible_height) == (48, 32)
        assert [p.shape for p in splanes] == [(32, 48)] * 3
        assert all(p.dtype == np.float16 for p in splanes)
        assert extra["target_size_applied"] is True
        assert isinstance(extra["target_size_native"], bool)
        # 盒式/area 类重采样保局部均值：全局均值与全解码一致（界远小于
        # 值域；容差容纳 native 与 python 两口径的重采样差 + half 舍入）
        for sp, fp in zip(splanes, full):
            assert abs(float(np.mean(sp)) - float(np.mean(fp))) < 1e-2

    def test_decode_and_decode_float_codes_identical(self, tmp_path) -> None:
        """decode（码域）与 decode_float 的逆映射必须逐位一致——
        HALF 文件在两个入口共享同一条解码管线（G11 契约）。"""
        codec = get_image_codec()
        path = self._write_half_file(tmp_path)
        codes, cinfo, _ = codec.decode(path, target_size=(48, 48))
        floats, finfo, _ = codec.decode_float(path, target_size=(48, 48))
        assert cinfo.sample_kind == TC_IMG_SAMPLE_KIND_HALF
        assert (finfo.visible_width, finfo.visible_height) == (
            cinfo.visible_width, cinfo.visible_height)
        assert len(codes) == len(floats)
        for c_plane, f_plane in zip(codes, floats):
            np.testing.assert_array_equal(
                c_plane, half_bits_to_codes(f_plane.view('<u2')))

    def test_decode_float_rejects_scale_up(self, tmp_path) -> None:
        """放大请求回退原始分辨率（与 decode() 同语义，不放大）。"""
        codec = get_image_codec()
        path = self._write_half_file(tmp_path)
        _planes, info, _extra = codec.decode_float(path,
                                                   target_size=(4096, 4096))
        assert (info.visible_width, info.visible_height) == (W, H)
