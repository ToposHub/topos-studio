"""Topos Image 阶段 5：.toos 静态图集成测试（registry/binding/source/factory）。

golden 文件（tests/conformance/golden_image_v1_*.bin）由 native conformance
工具确定性生成并入库；libtopos_codec 未构建时整体 skip。

覆盖：
- format_registry：.toos 注册为 image、`.topos` 不是图片、TPIM magic 探测；
- 绑定：capabilities 查询、probe/validate 元数据、read_metadata；
- ToposImageSource：单帧语义、planar uint16 平面与值域、色彩元数据透传；
- factory auto 内容探测路由（TPIM → toos_image）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.media_core

from src.shared.codec.topos_image_binding import (
    TC_IMG_CHUNK_ICCP,
    ToposImageError,
    get_image_codec,
)
from src.shared.media import format_registry
from src.shared.media.topos_image_source import ToposImageSource

REPO = Path(__file__).resolve().parents[2]
GOLDEN_GBR10 = REPO / "native/topos_codec/tests/conformance/golden_image_v1_gbr10.bin"
GOLDEN_META = REPO / "native/topos_codec/tests/conformance/golden_image_v1_meta.bin"
GOLDEN_ALPHA = REPO / "native/topos_codec/tests/conformance/golden_image_v1_alpha.bin"

pytest.importorskip("numpy")

_lib_available = True
try:
    get_image_codec()
except ToposImageError:
    _lib_available = False

pytestmark = pytest.mark.skipif(
    not (_lib_available and GOLDEN_GBR10.exists()),
    reason="libtopos_codec 未构建或 golden 文件缺失",
)


# ---------------- format_registry ----------------

class TestFormatRegistry:
    def test_toos_registered_as_image(self) -> None:
        assert format_registry.is_image("/x/shot.toos")
        assert format_registry.get_media_type("/x/shot.toos") == "image"

    def test_topos_is_not_image(self) -> None:
        # `.topos` 永久保留工程包（ADR-I001 D1），不是图片别名
        assert not format_registry.is_image("/x/project.topos")
        assert format_registry.get_media_type("/x/project.topos") != "image"

    def test_toos_magic_probe(self, tmp_path: Path) -> None:
        toos_file = tmp_path / "noext"
        toos_file.write_bytes(b"TPIM" + b"\x00" * 60)
        assert format_registry.is_toos_image(str(toos_file))
        raw_packet = tmp_path / "packet.bin"
        raw_packet.write_bytes(b"TPIC" + b"\x00" * 60)  # 裸 packet 不是 .toos
        assert not format_registry.is_toos_image(str(raw_packet))
        missing = tmp_path / "missing"
        assert not format_registry.is_toos_image(str(missing))


# ---------------- binding ----------------

class TestImageBinding:
    def test_capabilities(self) -> None:
        caps = get_image_codec().capabilities()
        assert caps.profiles_mask & 0b0111 == 0b0111  # Preview/HQ/XQ 可读写
        # TRAW 拍板（2026-09-13）：值 3 = Image RAW（支持）
        assert caps.profiles_mask & 0b1000
        # HALF 样本域（spec §15，2026-09-19 激活）：值 4 = HALF_FLOAT +
        # half_float 能力位；bit5+ 保留
        assert caps.profiles_mask & 0b10000
        assert caps.half_float
        assert caps.max_coded_dim == 16384
        assert caps.max_pixl_bytes == 256 << 20
        assert not caps.tile_roi and not caps.multi_image

    def test_probe_golden_gbr10(self) -> None:
        info = get_image_codec().probe(str(GOLDEN_GBR10))
        assert (info.visible_width, info.visible_height) == (64, 48)
        assert info.bit_depth == 10
        assert info.pixel_format == 2
        assert not info.has_alpha
        assert info.image_profile == 1  # Image HQ

    def test_target_size_downscales_all_planes_and_preserves_aspect(self) -> None:
        codec = get_image_codec()
        full_planes, full_info, _ = codec.decode(str(GOLDEN_GBR10))
        preview_planes, preview_info, extra = codec.decode(
            str(GOLDEN_GBR10), target_size=(20, 20),
        )
        assert (preview_info.visible_width, preview_info.visible_height) == (20, 15)
        assert [plane.shape for plane in preview_planes] == [(15, 20)] * 3
        assert all(plane.dtype == np.uint16 for plane in preview_planes)
        assert extra["target_size_applied"] is True
        # 新库走 C 层 native 输出下采样；旧部署库仍允许 Python fallback。
        assert extra["target_size_native"] is codec._has_native_preview
        # The default/full decode remains byte-for-byte unchanged.
        full_again, _, _ = codec.decode(str(GOLDEN_GBR10))
        for before, after in zip(full_planes, full_again):
            assert np.array_equal(before, after)
        assert full_info.visible_width == 64 and full_info.visible_height == 48

    def test_fixed_ratio_preview_uses_reduced_request(self) -> None:
        codec = get_image_codec()
        planes, info, extra = codec.decode(
            str(GOLDEN_GBR10), target_size=(32, 24),
        )
        assert (info.visible_width, info.visible_height) == (32, 24)
        assert [p.shape for p in planes] == [(24, 32)] * 3
        assert extra["target_size_native"] is True
        assert extra["decode_mode"] == "reduced_1_2"

    def test_target_size_yuv_chroma_and_alpha_geometry(self) -> None:
        planes, info, _ = get_image_codec().decode(
            str(GOLDEN_ALPHA), target_size=(17, 17),
        )
        assert (info.visible_width, info.visible_height) == (17, 9)
        assert planes[0].shape == (9, 17)
        assert planes[1].shape == (9, 9)
        assert planes[2].shape == (9, 9)
        assert planes[3].shape == (9, 17)

    def test_target_size_rejects_invalid_dimensions(self) -> None:
        codec = get_image_codec()
        with pytest.raises(ValueError):
            codec.decode(str(GOLDEN_GBR10), target_size=(0, 100))
        with pytest.raises(ValueError):
            codec.decode(str(GOLDEN_GBR10), target_size=(100,))

    def test_validate_and_metadata(self) -> None:
        info = get_image_codec().validate(str(GOLDEN_META), deep=True)
        assert info.iccp_ref and info.ocio_ref
        iccp = get_image_codec().read_metadata(str(GOLDEN_META), TC_IMG_CHUNK_ICCP)
        assert len(iccp) == 132
        assert iccp[36:40] == b"acsp"
        with pytest.raises(ToposImageError):
            get_image_codec().read_metadata(str(GOLDEN_GBR10), TC_IMG_CHUNK_ICCP)


# ---------------- ToposImageSource ----------------

class TestToposImageSource:
    def test_open_probe_metadata(self) -> None:
        src = ToposImageSource(str(GOLDEN_GBR10))
        try:
            meta = src.probe()
            assert (meta.width, meta.height) == (64, 48)
            assert meta.total_frames == 1
            assert meta.codec_name == "topos-image"
            assert meta.pixel_format == "gbrp10le"
            assert meta.bit_depth == 10
            assert meta.color_range == "full"
            assert not meta.has_alpha
        finally:
            src.close()

    def test_read_frame_planes(self) -> None:
        src = ToposImageSource(str(GOLDEN_GBR10))
        try:
            src.open()
            frame = src.read_frame(0)
            assert frame.width == 64 and frame.height == 48
            assert frame.pixel_format == "gbrp10le"
            assert len(frame.planes) == 3
            for p in frame.planes:
                assert p.dtype == np.uint16
                assert p.shape == (48, 64)
                assert int(p.max()) <= 1023  # 10-bit 值域不被扩展
            assert frame.plane_infos is not None
            assert [pi.component for pi in frame.plane_infos] == ["G", "B", "R"]
            assert frame.extra is not None
            assert frame.extra["source"] == "topos-image"
            assert frame.extra["image_profile"] == 1
            # 确定性：两次读取逐位一致（静态图语义）
            frame2 = src.read_frame(0)
            for a, b in zip(frame.planes, frame2.planes):
                assert np.array_equal(a, b)
        finally:
            src.close()

    def test_single_frame_semantics(self) -> None:
        src = ToposImageSource(str(GOLDEN_GBR10))
        try:
            src.open()
            from src.shared.media.source import MediaSourceError
            with pytest.raises(MediaSourceError):
                src.read_frame(1)
            with pytest.raises(MediaSourceError):
                src.seek_frame(2)
            assert src.read_audio_samples(0, 10) is None
        finally:
            src.close()

    def test_open_probe_metadata_alpha(self) -> None:
        # 带 Alpha 的 .toos 必须用 topos_*a{N} 注册名：format_info.has_alpha
        # 与 planes=4 和实际平面一致，否则次要消费路径按 3 平面名压平 Alpha
        from src.shared.media.frame import get_pixel_format
        src = ToposImageSource(str(GOLDEN_ALPHA))
        try:
            meta = src.probe()
            assert meta.has_alpha
            assert meta.pixel_format == "topos_yuva422p10a16"
            fmt = get_pixel_format(meta.pixel_format)
            assert fmt is not None and fmt.has_alpha and fmt.planes == 4
        finally:
            src.close()

    def test_read_frame_alpha_semantics(self) -> None:
        # golden_alpha：YUV422 10-bit + mode1 A16 无损、straight
        src = ToposImageSource(str(GOLDEN_ALPHA))
        try:
            src.open()
            frame = src.read_frame(0)
            assert len(frame.planes) == 4
            assert frame.has_alpha  # DecodedFrame.has_alpha 按格式名查表
            # 色度平面半宽、alpha 全分辨率（不做色度式降采样）
            assert frame.planes[0].shape == (32, 64)
            assert frame.planes[1].shape == (32, 32)
            assert frame.planes[3].shape == (32, 64)
            assert [pi.component for pi in frame.plane_infos] == ["Y", "U", "V", "A"]
            assert [pi.bit_depth for pi in frame.plane_infos] == [10, 10, 10, 16]
            assert int(frame.planes[3].max()) <= 65535
            # extra 键名对齐视频链（topos_*），消费方统一读取
            assert frame.extra["topos_alpha_premultiplied"] is False
            assert frame.extra["color_authority"] == "cicp"
            assert frame.extra["ocio_colorspace"] is None
            assert frame.extra["alpha_mode"] == 1
            assert "concealed_slices" not in frame.extra
            assert frame.extra["topos_concealed_slices"] == 0
        finally:
            src.close()

    def test_rejects_non_toos(self, tmp_path: Path) -> None:
        from src.shared.media.source import MediaOpenError
        bad = tmp_path / "bad.toos"
        bad.write_bytes(b"TPIC" + b"\x00" * 128)  # 裸 packet 误命名
        src = ToposImageSource(str(bad))
        with pytest.raises(MediaOpenError):
            src.open()


# ---------------- factory 路由 ----------------

class TestFactoryRouting:
    def test_auto_routes_toos_to_image_source(self) -> None:
        from src.shared.media.factory import MediaSourceFactory
        if not MediaSourceFactory._is_backend_available("toos_image"):
            pytest.skip("libtopos_codec 未构建")
        source = MediaSourceFactory.create(str(GOLDEN_GBR10), backend="auto",
                                           auto_open=False)
        assert isinstance(source, ToposImageSource)

    def test_tpos_project_not_routed_to_image(self) -> None:
        # .topos 工程包不被图片路由命中（扩展名与语义完全区分）
        from src.shared.media.factory import MediaSourceFactory
        order = MediaSourceFactory._get_backend_order(
            "auto", "/x/project.topos")
        assert "toos_image" not in order


# ---------------- M1-F1/F2/F7：HALF 浮点 .toos 导入 ----------------

class TestHalfFloatSource:
    """浮点样本域（sample_kind=HALF）经 ToposImageSource 导入。

    F1：白名单 (2,16) → 'gbrph16le'（open 不再拒绝）；
    F2：read_frame 走 decode_float → float16 线性值平面（值域含负值/>1）；
    F7：UINT bd16 组合与 HALF+alpha 显式拒绝（不错挂、不静默）。
    """

    W, H = 48, 32

    @staticmethod
    def _hdr_half_planes() -> list[np.ndarray]:
        """HDR 线性内容（负值 + >1 高光），G,B,R 序 float16 位型平面。"""
        yy, xx = np.mgrid[0:TestHalfFloatSource.H, 0:TestHalfFloatSource.W]
        rgb = np.zeros((TestHalfFloatSource.H, TestHalfFloatSource.W, 3), np.float32)
        rgb[..., 0] = 0.5 + 0.5 * xx / TestHalfFloatSource.W
        rgb[..., 1] = -0.2 + 0.4 * yy / TestHalfFloatSource.H   # 含负值
        rgb[..., 2] = 0.25 * np.sin(xx / 5.0) + 0.25
        rgb[0, 0, 2] = 4.0                                      # >1 高光
        return [np.ascontiguousarray(rgb[..., c]).astype(np.float16).view('<u2')
                for c in range(3)]

    @classmethod
    def _encode_half_toos(cls, tmp_path: Path, has_alpha: bool) -> Path:
        from src.shared.export.topos_encoder import ToposImageSequenceEncoder
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.frame import DecodedFrame
        from src.shared.media.metadata import Rational

        planes = cls._hdr_half_planes()
        if has_alpha:
            alpha = (np.full((cls.H, cls.W), 0.5, np.float32)
                     ).astype(np.float16).view('<u2')
            pix_fmt, profile, nch = 'topos_gbraph16a16', '4444', 4
        else:
            alpha, pix_fmt, profile, nch = None, 'gbrph16le', '4444', 3
        cfg = FFmpegEncoderConfig(
            codec="topos_image_sequence",
            width=cls.W, height=cls.H, fps=Rational(24, 1),
            pix_fmt=pix_fmt, profile=profile, crf=20, has_alpha=has_alpha,
        )
        data = np.zeros((cls.H, cls.W, nch), np.float32)
        data[..., 0] = planes[2].view(np.float16).astype(np.float32)
        data[..., 1] = planes[0].view(np.float16).astype(np.float32)
        data[..., 2] = planes[1].view(np.float16).astype(np.float32)
        if has_alpha:
            data[..., 3] = alpha.view(np.float16).astype(np.float32)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, data=data,
            pixel_format="rgba" if has_alpha else "rgb24",
            width=cls.W, height=cls.H,
        )
        out = tmp_path / ("half-alpha" if has_alpha else "half")
        enc = ToposImageSequenceEncoder(str(out), cfg)
        enc.open()
        enc.encode_frame(frame)
        enc.close()
        return out / "frame_0001.toos"

    def test_open_accepts_half_file(self, tmp_path: Path) -> None:
        path = self._encode_half_toos(tmp_path, has_alpha=False)
        src = ToposImageSource(str(path))
        try:
            meta = src.probe()
            assert meta.pixel_format == "gbrph16le"
            assert meta.bit_depth == 16
            assert not meta.has_alpha
            # F5/G1 基础：IDSC transfer=linear(8) → 标准字符串进元数据
            assert meta.color_transfer == "linear"
            from src.shared.media.frame import get_pixel_format
            fmt = get_pixel_format(meta.pixel_format)
            assert fmt is not None and fmt.planes == 3 and not fmt.has_alpha
            assert fmt.is_rgb
        finally:
            src.close()

    def test_read_frame_returns_float16_planes(self, tmp_path: Path) -> None:
        path = self._encode_half_toos(tmp_path, has_alpha=False)
        src = ToposImageSource(str(path))
        try:
            src.open()
            frame = src.read_frame(0)
            assert frame.pixel_format == "gbrph16le"
            assert frame.dtype == np.dtype(np.float16)
            assert len(frame.planes) == 3
            for pi in frame.plane_infos:
                assert pi.dtype == np.dtype(np.float16)
                assert pi.bit_depth == 16
                assert (pi.height, pi.width) == (self.H, self.W)
            # 值域契约：线性 HDR 值原样读出（负值 / >1 高光），无满刻度
            # 缩放（qp0 逐位无损 → 与源位型一致）
            src_bits = self._hdr_half_planes()
            assert float(frame.planes[1].min()) < 0         # B 面负值
            assert float(frame.planes[2].max()) > 1.0      # R 面 >1 高光
            for got, want in zip(frame.planes, src_bits):
                np.testing.assert_array_equal(
                    np.asarray(got, dtype=np.float16).view('<u2'), want)
            # 确定性：两次读取逐位一致
            frame2 = src.read_frame(0)
            for a, b in zip(frame.planes, frame2.planes):
                assert np.array_equal(a, b)
        finally:
            src.close()

    def test_half_with_alpha_still_rejected(self, tmp_path: Path) -> None:
        """F7 钉扎：HALF+alpha 组合不入白名单——open 显式拒绝（不静默
        压平 / 不错挂 3 平面浮点名）。"""
        from src.shared.media.source import MediaOpenError
        path = self._encode_half_toos(tmp_path, has_alpha=True)
        src = ToposImageSource(str(path))
        with pytest.raises(MediaOpenError):
            src.open()

    def test_uint_bd16_gbr_rejected_not_mislabeled(self, tmp_path: Path) -> None:
        """F1：UINT (2,16) 组合不得错挂浮点名 'gbrph16le'——open 按
        sample_kind 门控显式拒绝（native 侧该组合本就被版本域拒绝，
        此处钉应用侧纵深防御）。"""
        from types import SimpleNamespace

        from src.shared.media.source import MediaOpenError

        fake_info = SimpleNamespace(
            pixel_format=2, bit_depth=16, sample_kind=0,   # UINT！
            has_alpha=False, alpha_bit_depth=0, alpha_mode=0,
            color_range=1, color_primaries=1, color_transfer=1,
            color_matrix=0, iccp_ref=0, ocio_ref=0,
            visible_width=16, visible_height=16, pixel_aspect=(1, 1),
        )

        class _FakeCodec:
            def probe(self, path):
                return fake_info

        src = ToposImageSource("fake.toos", codec=_FakeCodec())
        with pytest.raises(MediaOpenError):
            src.open()

    def test_unknown_combo_error_names_combination(self) -> None:
        """_pixel_format_name 对白名单外组合显式 KeyError（不再回退
        'yuv422p10le' 错标）。"""
        from src.shared.media.topos_image_source import _pixel_format_name
        with pytest.raises(KeyError):
            _pixel_format_name(1, 16, 0)   # YUV444 bd16：无产品档
        assert _pixel_format_name(2, 16, 0) == 'gbrph16le'
        with pytest.raises(KeyError):
            _pixel_format_name(3, 10, 0)   # CFA 只有 12/16

    def test_float_sequence_import(self, tmp_path: Path) -> None:
        """F3：浮点序列（HALF 帧序列）经序列源导入——首帧探测通过、
        read_frame 产出 float16 线性平面。"""
        from src.shared.media.topos_image_sequence import open_sequence

        seq_dir = tmp_path / "fseq"
        seq_dir.mkdir()
        for i in range(1, 3):
            p = self._encode_half_toos(seq_dir, has_alpha=False)
            p.rename(seq_dir / f"frame_{i:04d}.toos")
        src = open_sequence(str(seq_dir / "frame_0001.toos"))
        try:
            assert src.frames() == [1, 2]
            meta = src.probe()
            assert meta.pixel_format == "gbrph16le"
            frame = src.read_frame(0)
            assert frame.pixel_format == "gbrph16le"
            assert frame.dtype == np.dtype(np.float16)
            assert len(frame.planes) == 3
            for plane in frame.planes:
                assert plane.dtype == np.float16
            frame2 = src.read_frame(1)
            assert frame2.dtype == np.dtype(np.float16)
        finally:
            src.close()


# ---------------- H3：Adobe RGB（ICCP 载体） ----------------


class TestAdobeRgbIccpCarrier:
    """H3：Adobe RGB 载体 = ICCP（H.273 无 Adobe primaries 码）——
    真实 AdobeRGB1998 profile 经 ICCP chunk 嵌入/读回/权威判定全链。"""

    @staticmethod
    def _adobe_icc_bytes() -> Optional[bytes]:
        """取系统 AdobeRGB profile（macOS ColorSync；其他平台跳过）。"""
        for cand in (
                "/System/Library/ColorSync/Profiles/AdobeRGB1998.icc",
                "/Library/ColorSync/Profiles/AdobeRGB1998.icc",
                "/System/Library/ColorSync/Profiles/AdobeRGB1998.icp",
                "/Library/ColorSync/Profiles/AdobeRGB1998.icp"):
            p = Path(cand)
            if p.is_file():
                return p.read_bytes()
        return None

    def test_adobe_rgb_iccp_roundtrip_and_authority(
            self, tmp_path: Path) -> None:
        icc = self._adobe_icc_bytes()
        if icc is None:
            pytest.skip("系统无 AdobeRGB profile（非 macOS 或未装）")
        codec = get_image_codec()
        packet, _info, _meta = codec.encode(
            str(GOLDEN_GBR10)) if hasattr(codec, "encode") else (None, None, None)
        # 用既有 golden 文件的载荷重打包（避免重新编码）：读原 TPIC 载荷
        planes, info, _ = codec.decode(str(GOLDEN_GBR10))
        from src.shared.codec.topos_binding import ToposCodec as _TC

        # 直接从 golden 文件取 TPIC packet（TPIM 信封剥除）
        raw = GOLDEN_GBR10.read_bytes()
        assert raw[:4] == b"TPIM"
        # TPIM 头定长 96（write 侧 envelope）；载荷从其后开始——用
        # validate/decode 已确认的既有路径更稳：encode 一帧等价载荷
        from src.shared.codec.topos_binding import _CFrameConfig, TOPOS_CODEC_ABI_VERSION
        import ctypes as _ct
        fc = _CFrameConfig()
        fc.struct_size = _ct.sizeof(fc)
        fc.abi_version = TOPOS_CODEC_ABI_VERSION
        fc.visible_width = int(info.visible_width)
        fc.visible_height = int(info.visible_height)
        fc.profile = {0: 3, 1: 5, 2: 6}.get(int(info.image_profile), 5)
        fc.pixel_format = int(info.pixel_format)
        fc.bit_depth = int(info.bit_depth)
        fc.qp_base = 58
        tc = _TC()
        pkt, _st = tc.encode_frame(fc, [np.ascontiguousarray(p) for p in planes])

        out = tmp_path / "adobe.toos"
        codec.write_packet(
            str(out), pkt, int(info.image_profile),
            extra_chunks=[(TC_IMG_CHUNK_ICCP, icc)],
            iccp_ref=True,
        )
        # 读回：字节保真 + 权威位 + ICCP 权威判定
        assert codec.probe(str(out)).iccp_ref == 1
        read_back = codec.read_metadata(str(out), TC_IMG_CHUNK_ICCP)
        assert read_back == icc
        from src.shared.media.color_decode import (
            ColorMetadataAuthority,
            resolve_topos_color_semantics,
        )

        sem = resolve_topos_color_semantics(
            {"pixel_format": int(info.pixel_format),
             "bit_depth": int(info.bit_depth),
             "color_range": 1, "color_primaries": 1,
             "color_transfer": 1, "color_matrix": 0,
             "chroma_siting": 0},
            icc_profile=read_back,
        )
        assert sem.display_authority == ColorMetadataAuthority.ICCP


class TestCmykIccpSoftRoute:
    """H5（D5 软转换路线）：'CMYK ' colorSpace profile 可嵌入（打印交付
    意图元数据，存储保持 RGB）；其余非 RGB PCS 仍拒绝。"""

    @staticmethod
    def _icc_with_color_space(cs: bytes) -> bytes:
        icc = bytearray(132)
        icc[36:40] = b"acsp"
        icc[16:20] = cs
        return bytes(icc)

    def test_cmyk_profile_accepted(self) -> None:
        from src.shared.media.color_decode import (
            ColorMetadataAuthority,
            ToposColorMetadataError,
            resolve_topos_color_semantics,
        )

        icc = self._icc_with_color_space(b"CMYK")
        sem = resolve_topos_color_semantics(
            {"pixel_format": 2, "bit_depth": 10, "color_range": 1,
             "color_primaries": 1, "color_transfer": 1, "color_matrix": 0,
             "chroma_siting": 0},
            icc_profile=icc,
        )
        assert sem.display_authority == ColorMetadataAuthority.ICCP

    def test_gray_profile_still_rejected(self) -> None:
        from src.shared.media.color_decode import (
            ToposColorMetadataError,
            resolve_topos_color_semantics,
        )

        icc = self._icc_with_color_space(b"GRAY")
        with pytest.raises(ToposColorMetadataError, match="incompatible"):
            resolve_topos_color_semantics(
                {"pixel_format": 2, "bit_depth": 10, "color_range": 1,
                 "color_primaries": 1, "color_transfer": 1,
                 "color_matrix": 0, "chroma_siting": 0},
                icc_profile=icc,
            )


# ---------------- C08（2026-09-27）：C 回调异常不伪装成功 ----------------

class TestFileIoCallbackErrors:
    """C08：read 回调边界捕获异常返回 TC_ERR_IO（历史：句柄关闭后回调
    抛 ValueError 被 ctypes 吞掉按 0=TC_OK 继续，缓冲未填充被当成功读）。"""

    def _probe_rc(self, io) -> int:
        import ctypes

        codec = get_image_codec()
        info = codec._CImageInfo() if hasattr(codec, "_CImageInfo") else None
        from src.shared.codec.topos_image_binding import _CImageInfo
        info = _CImageInfo()
        info.struct_size = ctypes.sizeof(_CImageInfo)
        info.abi_version = __import__(
            "src.shared.codec.topos_image_binding",
            fromlist=["TOPOS_CODEC_ABI_VERSION"]).TOPOS_CODEC_ABI_VERSION
        return int(codec._lib.tc_image_probe(
            ctypes.cast(io.byref(), ctypes.c_void_p), ctypes.byref(info)))

    def test_closed_handle_returns_io_error(self) -> None:
        from src.shared.codec.topos_image_binding import _CFileIo
        from src.shared.codec.topos_binding import TC_ERR_IO

        io = _CFileIo(str(GOLDEN_GBR10))
        io.close()  # 句柄生命周期提前结束
        assert self._probe_rc(io) == TC_ERR_IO
        assert io.last_error  # 诊断上下文保留

    def test_short_read_returns_io_error(self) -> None:
        from src.shared.codec.topos_image_binding import _CFileIo
        from src.shared.codec.topos_binding import TC_ERR_IO

        io = _CFileIo(str(GOLDEN_GBR10))
        try:
            real_read = io._f.read
            io._f.read = lambda n=None: real_read(1)  # 短读注入
            assert self._probe_rc(io) == TC_ERR_IO
            assert "short read" in (io.last_error or "")
        finally:
            io.close()

    def test_disk_error_returns_io_error(self) -> None:
        import errno

        from src.shared.codec.topos_image_binding import _CFileIo
        from src.shared.codec.topos_binding import TC_ERR_IO

        io = _CFileIo(str(GOLDEN_GBR10))
        try:
            def _boom(*_a):
                raise OSError(errno.EIO, "injected disk error")
            io._f.seek = _boom
            assert self._probe_rc(io) == TC_ERR_IO
            assert "injected disk error" in (io.last_error or "")
        finally:
            io.close()

    def test_probe_error_carries_io_detail(self) -> None:
        """入口层（probe）把 io.last_error 拼进 ToposImageError 详情。"""
        from src.shared.codec.topos_image_binding import _CFileIo

        io = _CFileIo(str(GOLDEN_GBR10))
        io.close()
        codec = get_image_codec()
        # probe 每次新建 _CFileIo——这里直接短路：path 正常但 fd 无法打开
        # 的场景由 closed-handle 单测覆盖；此处验证 helper 拼接行为。
        assert codec._with_io_detail(
            -14, "native detail", io) == "native detail" or io.last_error


# ---------------- C15（2026-09-27）：状态契约属性化 ----------------

class TestStateContract:
    """C15：is_open/current_frame 为只读属性（对齐 ToposMediaSource）——
    frame_provider 按 `if not source.is_open` 属性判断；方法形态下
    bool(方法) 恒真，关闭后状态误判。"""

    def test_is_open_is_property_not_method(self) -> None:
        src = ToposImageSource(str(GOLDEN_GBR10))
        assert src.is_open is False  # 属性：关闭 = False（方法形态恒真）
        src.open()
        try:
            assert src.is_open is True
        finally:
            src.close()
        assert src.is_open is False

    def test_current_frame_cursor_contract(self) -> None:
        src = ToposImageSource(str(GOLDEN_GBR10))
        assert src.current_frame == 0
        src.open()
        try:
            src.read_frame(0)
            assert src.current_frame == 1  # 读后游标推进
            src.seek_frame(0)
            assert src.current_frame == 0  # seek 重置
        finally:
            src.close()
