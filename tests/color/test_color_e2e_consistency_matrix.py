"""P0-COLOR-06：端到端色彩一致性矩阵（golden tests）。

计划表验收：「Rec.709、P3、BT.2020 PQ/HLG、8/10-bit、limited/full range、
CPU/GPU/Composite/Color 页面输出对同一测试素材的亮度、色度与 metadata 一致；
预览与导出差异有可解释上限」。

本文件覆盖导出链路的物理验证（真实编码器写盘 → 解码读回）与 CPU 管线的
数值验证（provider 编码 → 量化 → 解码）：

矩阵维度
  色彩空间   : BT.709 SDR / BT.2020 PQ (HDR10) / full-range
  位深       : 8-bit (yuv420p) / 10-bit (yuv420p10le)
  range      : limited (MPEG) / full (JPEG)
  metadata   : 流级 primaries/trc/colorspace/range tag 与 ColorMetadataConfig 一致
  编解码器   : ffv1（无损——排除编码损失，只测色彩转换矩阵/range）

断言策略
  - 色块中心（避开 4:2:0 色度边界混色）：≤ 3 LSB（8-bit 量化 + swscale 舍入）
  - 灰轴：Y(0)=16 / Y(255)=235 / U=V=128（limited 契约）
  - 矩阵区分度守卫：故意用 BT.601 解码 BT.709 流必须显著偏色——若编码侧
    回退到 swscale 默认矩阵（BT.601），色块中心误差会爆炸，测试立即抓到
  - provider 域 golden：linear 1.0 → 709 code 235；100 nits → PQ code ≈ 0.508

GPU/Composite/Color 页面路径的引擎级一致性已由 test_color_engine_triple_golden
（三方 OCIO/builtin/GPU）与 tests/video/test_yuv_gpu_upload.py（GPU YUV 解码）
覆盖；本文件补齐「导出编码器」这最后一环。
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest

av = pytest.importorskip("av", reason="PyAV 不可用，跳过编码器端到端测试")

from src.shared.export.video_encoder import (  # noqa: E402
    ColorMetadataConfig,
    FFmpegEncoderConfig,
    FFmpegVideoEncoder,
)
from src.shared.media.color_decode import (  # noqa: E402
    ColorConverter,
    ColorMatrix,
    ColorRange,
)
from src.shared.media.frame import DecodedFrame  # noqa: E402
from src.shared.media.metadata import Rational, VideoMetadata  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")

# ── 测试图案：8 个纯色块（避开 4:2:0 色度边界，取块中心断言）────────────

W, H = 64, 64
_BLOCKS = [
    ((255, 0, 0), 0, 0),      # BT.709 Y≈63 / BT.601 Y≈81（矩阵区分点）
    ((0, 255, 0), 0, 1),      # BT.709 Y≈173 / BT.601 Y≈145
    ((0, 0, 255), 1, 0),
    ((255, 255, 0), 1, 1),
    ((16, 16, 16), 2, 0),     # limited 黑
    ((235, 235, 235), 2, 1),  # limited 白
    ((128, 128, 128), 3, 0),  # 中灰
    ((200, 30, 90), 3, 1),    # 混合色
]


def _block_pattern(bit_depth: int = 8) -> np.ndarray:
    """(H, W, 3) uint8/uint16 纯色块图案（full-range RGB）。"""
    data = np.zeros((H, W, 3), dtype=np.uint8)
    for (c, br, bc) in _BLOCKS:
        data[br * 16:(br + 1) * 16, bc * 32:(bc + 1) * 32] = c
    if bit_depth == 8:
        return np.ascontiguousarray(data)
    return np.ascontiguousarray(data.astype(np.uint16) * 257)  # 0..255 → 0..65535


def _read_plane(plane, w: int, h: int, dtype) -> np.ndarray:
    """按 line_size stride 切出有效区（PyAV plane buffer 有对齐 padding）。"""
    ls = plane.line_size
    nbytes = np.dtype(dtype).itemsize
    buf = np.frombuffer(plane, dtype=np.uint8).reshape(h, ls)
    if dtype == np.uint8:
        return np.ascontiguousarray(buf[:, :w])
    return np.ascontiguousarray(buf[:, :w * nbytes]).view(np.uint16).reshape(h, w)


def _encode_decode(color_meta: ColorMetadataConfig, pix_fmt: str, bit_depth: int,
                   data: np.ndarray, rgb_fmt: str):
    """ffv1 无损写盘 → 解码读回。返回 (流 tag 元组, (Y,U,V) planes)。"""
    cfg = FFmpegEncoderConfig(
        codec='ffv1', width=W, height=H, fps=Rational(24, 1),
        pix_fmt=pix_fmt, color_metadata=color_meta,
    )
    meta = VideoMetadata(
        width=W, height=H, fps=Rational(24, 1), total_frames=1,
        codec_name='ffv1', codec_profile=None, pixel_format=pix_fmt,
        bit_depth=bit_depth,
    )
    path = tempfile.mktemp(suffix='.mkv')
    try:
        enc = FFmpegVideoEncoder(path, cfg)
        enc.open()
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, data=data, planes=None,
            pixel_format=rgb_fmt, dtype=data.dtype, width=W, height=H,
            metadata=meta,
        ))
        enc.close()

        container = av.open(path)
        cc = container.streams.video[0].codec_context
        tags = (cc.color_primaries, cc.color_trc, cc.colorspace, cc.color_range)
        for frame in container.decode(video=0):
            dtype = np.uint8 if bit_depth == 8 else np.uint16
            y = _read_plane(frame.planes[0], W, H, dtype)
            u = _read_plane(frame.planes[1], W // 2, H // 2, dtype)
            v = _read_plane(frame.planes[2], W // 2, H // 2, dtype)
            break
        container.close()
        return tags, (y, u, v)
    finally:
        if os.path.exists(path):
            os.remove(path)


def _block_center_errors(rgb8: np.ndarray, pattern8: np.ndarray) -> np.ndarray:
    """各色块中心的 RGB 误差（块中心避开 4:2:0 色度边界）。"""
    errs = []
    for (c, br, bc) in _BLOCKS:
        y0, x0 = br * 16 + 8, bc * 32 + 16
        got = rgb8[y0, x0].astype(int)
        errs.append(int(np.abs(got - np.array(c)).max()))
    return np.array(errs)


def _decode_to_rgb8(planes, matrix: ColorMatrix, range_: ColorRange,
                    bit_depth: int) -> np.ndarray:
    """用项目 CPU 解码器（与 GPU shader 同数值）转回 8-bit RGB。"""
    y, u, v = planes
    conv = ColorConverter()
    rgb = conv.yuv_to_rgb(y, u, v, matrix=matrix, range_=range_, bit_depth=bit_depth)
    if bit_depth == 8:
        return np.clip(rgb * 255, 0, 255).astype(np.uint8)
    return (np.clip(rgb * 65535, 0, 65535).astype(np.uint16) // 257).astype(np.uint8)


# ═══════════════════════════════════════════════════════════════════════
# 1. SDR BT.709 / 8-bit / limited —— 主契约
# ═══════════════════════════════════════════════════════════════════════

class TestRec709Sdr8Bit:
    def test_roundtrip_block_centers(self):
        """BT.709 limited 8-bit：色块中心误差 ≤3 LSB（量化 + swscale 舍入）。"""
        pattern = _block_pattern(8)
        tags, planes = _encode_decode(
            ColorMetadataConfig.sdr(), 'yuv420p', 8, pattern, 'rgb24'
        )
        rgb8 = _decode_to_rgb8(planes, ColorMatrix.BT709, ColorRange.LIMITED, 8)
        errs = _block_center_errors(rgb8, pattern)
        assert errs.max() <= 3, f"色块中心误差超上限: {errs}"

    def test_stream_tags_rec709(self):
        """流级 tag = (BT709, BT709, BT709, MPEG/limited)。"""
        tags, _ = _encode_decode(
            ColorMetadataConfig.sdr(), 'yuv420p', 8, _block_pattern(8), 'rgb24'
        )
        prim, trc, spc, rng = tags
        assert prim == 1, f"primaries 应为 BT709(1)，实际 {prim}"
        assert trc == 1, f"transfer 应为 BT709(1)，实际 {trc}"
        assert spc == 1, f"colorspace 应为 BT709(1)，实际 {spc}"
        assert rng == 1, f"range 应为 MPEG/limited(1)，实际 {rng}"

    def test_limited_range_gray_axis(self):
        """limited 灰轴契约：RGB(16) → Y≈30、RGB(235) → Y≈218、灰块 U=V=128。

        注意黑/白块是 full-range RGB 值 16/235（不是 limited 域端点）：
        Y = 16 + 219*(rgb/255)，故 16→29.7、235→217.6。limited 域真正的
        端点 Y=16/235 对应 RGB 0/255，由 test_limited_range_endpoints 覆盖。
        """
        pattern = _block_pattern(8)
        _, (y, u, v) = _encode_decode(
            ColorMetadataConfig.sdr(), 'yuv420p', 8, pattern, 'rgb24'
        )
        assert abs(int(y[40, 16]) - 30) <= 1, f"RGB16 块 Y={y[40,16]} != 30"
        assert abs(int(y[40, 48]) - 218) <= 1, f"RGB235 块 Y={y[40,48]} != 218"
        # 中灰块 @ (br=3,bc=0) → 中心 (56,16)，色度 (28, 8) 中性
        assert abs(int(u[28, 8]) - 128) <= 1
        assert abs(int(v[28, 8]) - 128) <= 1

    def test_limited_range_endpoints(self):
        """limited 端点契约：RGB 0 → Y=16、RGB 255 → Y=235。"""
        pattern = np.zeros((H, W, 3), dtype=np.uint8)
        pattern[:, W // 2:] = 255
        _, (y, u, v) = _encode_decode(
            ColorMetadataConfig.sdr(), 'yuv420p', 8,
            np.ascontiguousarray(pattern), 'rgb24'
        )
        assert abs(int(y[H // 2, W // 4]) - 16) <= 1
        assert abs(int(y[H // 2, 3 * W // 4]) - 235) <= 1

    def test_wrong_matrix_guard_bt601(self):
        """矩阵区分度守卫：BT.601 解码 BT.709 流必须显著偏色。

        这是编码侧矩阵修复的回归探测器：若 swscale 回退默认 BT.601
        转换，绿块中心误差会 >20（BT.709 Y=173 vs BT.601 Y=145 的
        亮度差直接进入 RGB）。
        """
        pattern = _block_pattern(8)
        _, planes = _encode_decode(
            ColorMetadataConfig.sdr(), 'yuv420p', 8, pattern, 'rgb24'
        )
        rgb601 = _decode_to_rgb8(planes, ColorMatrix.BT601, ColorRange.LIMITED, 8)
        errs = _block_center_errors(rgb601, pattern)
        assert errs.max() > 20, (
            f"BT.601 误用误差 {errs.max()} 不够大——测试失去矩阵区分度"
        )


# ═══════════════════════════════════════════════════════════════════════
# 2. HDR10 BT.2020 / 10-bit / limited
# ═══════════════════════════════════════════════════════════════════════

class TestHdr10Bt2020TenBit:
    def test_roundtrip_block_centers_10bit(self):
        """BT.2020nc limited 10-bit：色块中心误差 ≤3 LSB（回 8-bit 域比较）。"""
        pattern = _block_pattern(10)
        tags, planes = _encode_decode(
            ColorMetadataConfig.hdr10(), 'yuv420p10le', 10, pattern, 'rgb48le'
        )
        rgb8 = _decode_to_rgb8(planes, ColorMatrix.BT2020_NC, ColorRange.LIMITED, 10)
        errs = _block_center_errors(rgb8, _block_pattern(8))
        assert errs.max() <= 3, f"10-bit 色块中心误差超上限: {errs}"

    def test_stream_tags_hdr10(self):
        """HDR10 流级 tag = (BT2020=9, SMPTE2084=16, BT2020NC=9, MPEG=1)。"""
        tags, _ = _encode_decode(
            ColorMetadataConfig.hdr10(), 'yuv420p10le', 10,
            _block_pattern(10), 'rgb48le'
        )
        prim, trc, spc, rng = tags
        assert prim == 9, f"primaries 应为 BT2020(9)，实际 {prim}"
        assert trc == 16, f"transfer 应为 SMPTE2084(16)，实际 {trc}"
        assert spc == 9, f"colorspace 应为 BT2020NC(9)，实际 {spc}"
        assert rng == 1, f"range 应为 MPEG/limited(1)，实际 {rng}"

    def test_wrong_matrix_guard_bt709(self):
        """10-bit 下 BT.709 误用同样可检出（红/绿亮度权重差较小）。

        正确矩阵 round-trip ≤3 LSB，BT.709 误用 ≈13——差距 4 倍以上，
        阈值取 10 保持区分度。
        """
        pattern = _block_pattern(10)
        _, planes = _encode_decode(
            ColorMetadataConfig.hdr10(), 'yuv420p10le', 10, pattern, 'rgb48le'
        )
        rgb709 = _decode_to_rgb8(planes, ColorMatrix.BT709, ColorRange.LIMITED, 10)
        errs = _block_center_errors(rgb709, _block_pattern(8))
        assert errs.max() > 10, f"BT.709 误用误差 {errs.max()} 不够大"


# ═══════════════════════════════════════════════════════════════════════
# 3. full-range 契约
# ═══════════════════════════════════════════════════════════════════════

class TestFullRange:
    def test_full_range_roundtrip(self):
        """full (JPEG) range：0/255 端点直通，色块中心误差 ≤4。"""
        pattern = _block_pattern(8)
        meta = ColorMetadataConfig(primaries="bt709", transfer="bt709",
                                   matrix="bt709", range="full")
        tags, planes = _encode_decode(meta, 'yuv420p', 8, pattern, 'rgb24')
        assert tags[3] == 2, f"range tag 应为 JPEG/full(2)，实际 {tags[3]}"
        rgb8 = _decode_to_rgb8(planes, ColorMatrix.BT709, ColorRange.FULL, 8)
        errs = _block_center_errors(rgb8, pattern)
        assert errs.max() <= 4, f"full-range 色块误差超上限: {errs}"


# ═══════════════════════════════════════════════════════════════════════
# 4. HLG metadata 契约（tag-only；像素 transfer 由 provider 域覆盖）
# ═══════════════════════════════════════════════════════════════════════

class TestHlgMetadata:
    def test_stream_tags_hlg(self):
        """HLG 流级 tag = (BT2020, ARIB_STD_B67=18, BT2020NC, limited)。"""
        tags, _ = _encode_decode(
            ColorMetadataConfig.hlg(), 'yuv420p10le', 10,
            _block_pattern(10), 'rgb48le'
        )
        prim, trc, spc, rng = tags
        assert prim == 9 and trc == 18 and spc == 9 and rng == 1, (
            f"HLG tag 异常: {tags}"
        )


# ═══════════════════════════════════════════════════════════════════════
# 5. CPU 管线 golden：linear → provider 编码 → 量化 → 解码 → linear
# ═══════════════════════════════════════════════════════════════════════

class TestProviderPipelineGolden:
    """composite 导出管线的数值锚点（transform_color 的两端）。"""

    def test_sdr_white_maps_to_limited_235(self):
        """linear 1.0 → REC709_VIDEO → 8-bit 灰轴 code 必须落在 235。"""
        from src.shared.color.color_transform_provider import BuiltinColorProvider
        from src.shared.color.color_space_descriptor import (
            LINEAR_SCENE, REC709_VIDEO,
        )
        provider = BuiltinColorProvider()
        lin = np.array([[[1.0, 1.0, 1.0, 1.0]]], dtype=np.float32)
        out = provider.transform(lin, LINEAR_SCENE, REC709_VIDEO)
        code8 = int(round(float(out[0, 0, 0]) * 255))
        assert abs(code8 - 235) <= 1, f"SDR 白场 code={code8} != 235 (limited)"

    def test_hdr_100nits_pq_code(self):
        """linear 100 nits → HDR10 → limited PQ code ≈ 0.4991。

        ST.2084 full 域 100-nit code = 0.5081；HDR10_VIDEO 是 LIMITED
        range，压缩后 = 0.5081/1.164 + 16/255 = 0.4991。
        """
        from src.shared.color.color_transform_provider import BuiltinColorProvider
        from src.shared.color.color_space_descriptor import (
            HDR10_VIDEO, LINEAR_SCENE,
        )
        provider = BuiltinColorProvider()
        lin = np.array([[[100.0, 100.0, 100.0, 1.0]]], dtype=np.float32)
        out = provider.transform(lin, LINEAR_SCENE, HDR10_VIDEO)
        code = float(out[0, 0, 0])
        assert abs(code - 0.4991) < 2e-3, f"100-nit limited PQ code={code} != 0.4991"
        assert 0.0 <= code <= 1.0

    def test_sdr_pipeline_roundtrip_linear(self):
        """linear → 709 encode → 8-bit 量化 → decode → linear 的可解释上限。

        预览（linear 工作空间）与导出（8-bit 709 容器）之间的差异有上限：
        8-bit 量化误差在 code 域 ≤0.5 LSB，经 OETF 逆映射后在 linear 域
        中段 ≈0.001、暗段稍大——上限 0.01 覆盖全部灰阶。
        """
        from src.shared.color.color_transform_provider import BuiltinColorProvider
        from src.shared.color.color_space_descriptor import (
            LINEAR_SCENE, REC709_VIDEO,
        )
        provider = BuiltinColorProvider()
        codes = np.linspace(0.05, 0.95, 19, dtype=np.float32)
        lin = np.array([[[c, c, c, 1.0] for c in codes]], dtype=np.float32)
        encoded = provider.transform(lin.copy(), LINEAR_SCENE, REC709_VIDEO)
        quantized = np.round(encoded * 255.0) / 255.0  # 8-bit 容器
        decoded = provider.transform(
            quantized.astype(np.float32), REC709_VIDEO, LINEAR_SCENE
        )
        err = float(np.max(np.abs(decoded[..., :3] - lin[..., :3])))
        assert err < 0.01, f"SDR 管线 round-trip 误差 {err:.4f} 超过可解释上限"

    def test_hdr_pipeline_roundtrip_linear_10bit(self):
        """linear(nits) → PQ encode → 10-bit 量化 → decode 的上限。

        10-bit PQ 量化在 100-1000 nits 段的相对误差 ≈0.4%，
        绝对误差上限按 5% of value + 1 nit 宽松覆盖。
        """
        from src.shared.color.color_transform_provider import BuiltinColorProvider
        from src.shared.color.color_space_descriptor import (
            HDR10_VIDEO, LINEAR_SCENE,
        )
        provider = BuiltinColorProvider()
        nits = np.array([10.0, 50.0, 100.0, 203.0, 500.0, 1000.0, 4000.0],
                        dtype=np.float32)
        lin = np.array([[[c, c, c, 1.0] for c in nits]], dtype=np.float32)
        encoded = provider.transform(lin.copy(), LINEAR_SCENE, HDR10_VIDEO)
        quantized = np.round(encoded * 1023.0) / 1023.0  # 10-bit 容器
        decoded = provider.transform(
            quantized.astype(np.float32), HDR10_VIDEO, LINEAR_SCENE
        )
        rel = np.abs(decoded[..., :3] - lin[..., :3]) / lin[..., :3]
        assert float(np.max(rel)) < 0.05, (
            f"HDR 10-bit 管线相对误差 {float(np.max(rel)):.3f} 超上限"
        )
