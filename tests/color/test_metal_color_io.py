"""4.4 Metal 色彩 IO 真机对拍：NV12 SDR → 共享参考；P010 HDR 合成往返。

真实 HDR 素材本机没有——HDR 输入验证用**合成数据**（已知线性值的
P010 图），容差按 10-bit 量化 + fp16 存储推导（见各断言注释）。
无 Metal 设备机器自动 skip。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.frame_color_description import (
    ColorFrameDescription,
    DisplayOutputDescription,
    display_output_for,
)
from src.features.color.core.metal_color_io import (
    MetalColorIO,
    build_decode_params,
    build_display_params,
    synthesize_p010_from_linear,
)
from src.shared.color.color_transform_provider import (
    _linear_to_pq_np,
    _linear_to_srgb_np,
    _pq_to_linear_np,
    _srgb_to_linear_np,
    _tonemap_aces_np,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.kernels_video import yuv_to_rgba_reference

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备（NOT RUN）")


@pytest.fixture(scope="module")
def metal_ctx():
    ctx = MetalComputeContext()
    yield ctx
    # MTLTexture/Buffer 由 Python 引用离场释放；上下文进程级共享


def _color_bars(width: int, height: int) -> np.ndarray:
    """75% 色条（码值域）——SDR 对拍已知测试图。"""
    bars = np.array([
        [0.75, 0.75, 0.75],   # 白（75%）
        [0.75, 0.75, 0.0],    # 黄
        [0.0, 0.75, 0.75],    # 青
        [0.0, 0.75, 0.0],     # 绿
        [0.75, 0.0, 0.75],    # 品红
        [0.75, 0.0, 0.0],     # 红
        [0.0, 0.0, 0.75],     # 蓝
    ], dtype=np.float32)
    repeats = max(1, width // 7) + 1
    tiles = np.tile(bars[np.newaxis, :, :], (1, repeats, 1))[:, :width, :]
    return np.tile(tiles, (height, 1, 1))


def _rgb_to_nv12(code_rgb: np.ndarray) -> tuple:
    """码值 RGB → NV12 平面（BT.709 limited；合成器，8-bit）。"""
    ry, rg, rb = 0.2126, 0.7152, 0.0722
    y = code_rgb @ np.array([ry, rg, rb])
    cb = code_rgb @ np.array([-0.114572, -0.385428, 0.5])
    cr = code_rgb @ np.array([0.5, -0.454153, -0.045847])
    y_code = np.round(y * 219.0 + 16.0)
    cb_code = np.round((cb + 0.5) * 224.0 + 16.0)
    cr_code = np.round((cr + 0.5) * 224.0 + 16.0)
    cb2 = (cb_code[0::2, 0::2] + cb_code[0::2, 1::2]
           + cb_code[1::2, 0::2] + cb_code[1::2, 1::2]) * 0.25
    cr2 = (cr_code[0::2, 0::2] + cr_code[0::2, 1::2]
           + cr_code[1::2, 0::2] + cr_code[1::2, 1::2]) * 0.25
    return (np.clip(np.round(y_code), 0, 255).astype(np.uint8),
            np.stack([np.clip(np.round(cb2), 0, 255),
                      np.clip(np.round(cr2), 0, 255)], axis=-1).astype(np.uint8))


# ── 参数打包（纯逻辑层） ─────────────────────────────────────────────────

class TestParamPacking:
    def test_decode_params_match_kernels_video_code_domain(self):
        # 码值域参数逐字段 == 共享 build_yuv_convert_params（同一数学）
        from src.shared.gpu.metal.kernels_video import build_yuv_convert_params
        desc = ColorFrameDescription("nv12", 64, 32)
        params = build_decode_params(desc)
        ref = build_yuv_convert_params(8, "limited", "bt709")
        for name in ("max_code", "y_offset", "y_scale", "c_neutral",
                     "c_scale", "cr_to_r", "cb_to_g", "cr_to_g", "cb_to_b"):
            assert params[name][0] == ref[name][0]

    def test_chroma_siting_pack(self):
        left = build_decode_params(ColorFrameDescription("nv12", 64, 32))
        center = build_decode_params(
            ColorFrameDescription("nv12", 64, 32, chroma_location="center"))
        assert left["chroma_bilinear"][0] == 0      # GL 对拍口径（gid>>1）
        assert center["chroma_bilinear"][0] == 1

    def test_signal_mode_is_identity_transform(self):
        # signal（SDR 默认）：transfer 解码与色域变换恒等（GL 对拍口径）
        params = build_decode_params(ColorFrameDescription(
            "nv12", 64, 32, transfer="bt709", primaries="bt2020"))
        assert params["transfer_mode"][0] == 0
        assert params["linear_scale"][0] == 1.0
        np.testing.assert_array_equal(
            params["primaries_m"][0], np.eye(3, dtype=np.float32).reshape(9))

    def test_linear_mode_pq_scale_and_primaries(self):
        # linear：PQ 解码出 nit（0..10000）→ 按 SDR 参考白归一（1=100nit）
        params = build_decode_params(ColorFrameDescription(
            "p010", 64, 32, bit_depth=10, transfer="pq",
            primaries="bt2020"), working_mode="linear")
        assert params["transfer_mode"][0] == 3
        assert params["linear_scale"][0] == pytest.approx(0.01)
        assert not np.allclose(params["primaries_m"][0],
                               np.eye(3, dtype=np.float32).reshape(9))

    def test_working_mode_rejected_when_unknown(self):
        with pytest.raises(ValueError, match="工作模式"):
            build_decode_params(ColorFrameDescription("nv12", 8, 4),
                                working_mode="dolby")

    def test_display_params_modes(self):
        assert build_display_params(
            display_output_for(ColorFrameDescription("nv12", 64, 32))
        )["mode"][0] == 0
        hdr = ColorFrameDescription("p010", 64, 32, bit_depth=10,
                                    transfer="pq")
        assert build_display_params(
            display_output_for(hdr, hdr_display=True))["mode"][0] == 3
        assert build_display_params(
            display_output_for(hdr, hdr_display=True))["out_scale"][0] \
            == pytest.approx(100.0)
        assert build_display_params(
            display_output_for(hdr, hdr_display=False))["mode"][0] == 2


# ── NV12 SDR（真机） ─────────────────────────────────────────────────────

@requires_metal
class TestNv12SdrOnMetal:
    def test_color_bars_match_shared_reference(self, metal_ctx):
        """NV12 SDR 色条 → 码值域 RGB 与共享 numpy 参考（1/255 容差）。

        SDR 默认 ``signal`` 工作模式：与既有 GL NV12 路径同一语义
        （码值直通，不做 transfer 往返）。
        """
        io = MetalColorIO(metal_ctx)
        width, height = 56, 16
        desc = ColorFrameDescription("nv12", width, height)
        bars = _color_bars(width, height)
        y_plane, uv_plane = _rgb_to_nv12(bars)
        y_tex, uv_tex = io.upload_yuv_planes(y_plane, uv_plane, desc)
        out = io.decode_to_working(None, desc, y_tex=y_tex, uv_tex=uv_tex)
        metal_ctx.finish()
        got = io.read_rgba16f(out)

        y_norm = y_plane.astype(np.float32) / 255.0
        uv_norm = uv_plane.astype(np.float32) / 255.0
        ref = yuv_to_rgba_reference(
            y_norm, uv_norm, bit_depth=8, color_range="limited",
            color_matrix="bt709")
        # 容差：fp16 存储（2^-11 相对）+ GPU unorm 采样精确 → 1/255 足够
        np.testing.assert_allclose(got[..., :3], ref, atol=1.5 / 255.0)
        assert (got[..., 3] == 1.0).all()          # YUV 源 Alpha=1

    def test_color_bars_linear_mode_matches_provider(self, metal_ctx):
        """显式 linear 工作模式：BT.709 解码与 provider 逐条一致。"""
        from src.shared.color.color_transform_provider import (
            _bt709_to_linear_np,
        )
        io = MetalColorIO(metal_ctx)
        width, height = 56, 16
        desc = ColorFrameDescription("nv12", width, height, transfer="bt709")
        bars = _color_bars(width, height)
        y_plane, uv_plane = _rgb_to_nv12(bars)
        y_tex, uv_tex = io.upload_yuv_planes(y_plane, uv_plane, desc)
        out = io.decode_to_working(None, desc, y_tex=y_tex, uv_tex=uv_tex,
                                   working_mode="linear")
        metal_ctx.finish()
        got = io.read_rgba16f(out)
        ref = _bt709_to_linear_np(yuv_to_rgba_reference(
            y_plane.astype(np.float32) / 255.0,
            uv_plane.astype(np.float32) / 255.0,
            bit_depth=8, color_range="limited", color_matrix="bt709"))
        np.testing.assert_allclose(got[..., :3], ref, atol=2.0 / 255.0)

    def test_left_siting_is_gl_nearest_neighbour(self, metal_ctx):
        """left 色度位置 = GL ``gid>>1`` 最近邻：纯色度图逐位一致。"""
        io = MetalColorIO(metal_ctx)
        width, height = 16, 8
        desc = ColorFrameDescription("nv12", width, height)
        solid = np.full((height, width, 3), (64.0 / 255.0, 128.0 / 255.0,
                                             200.0 / 255.0), np.float32)
        y_plane, uv_plane = _rgb_to_nv12(solid)
        y_tex, uv_tex = io.upload_yuv_planes(y_plane, uv_plane, desc)
        out = io.decode_to_working(None, desc, y_tex=y_tex, uv_tex=uv_tex)
        metal_ctx.finish()
        got = io.read_rgba16f(out)
        ref = yuv_to_rgba_reference(
            y_plane.astype(np.float32) / 255.0,
            uv_plane.astype(np.float32) / 255.0,
            bit_depth=8, color_range="limited", color_matrix="bt709")
        np.testing.assert_allclose(got[..., :3], ref, atol=1.0 / 255.0)

    def test_center_siting_bilinear_chroma(self, metal_ctx):
        """center 色度位置：水平色度渐变按双线性插值（非最近邻阶梯）。"""
        io = MetalColorIO(metal_ctx)
        width, height = 8, 4
        desc = ColorFrameDescription(
            "nv12", width, height, chroma_location="center")
        # 色度平面：U 从左到右线性渐变；Y 恒定
        y_plane = np.full((height, width), 126, np.uint8)
        uv_plane = np.zeros((height // 2, width // 2, 2), np.uint8)
        uv_plane[..., 0] = np.tile(
            np.linspace(40, 220, width // 2, dtype=np.uint8),
            (height // 2, 1))
        uv_plane[..., 1] = 128
        y_tex, uv_tex = io.upload_yuv_planes(y_plane, uv_plane, desc)
        out = io.decode_to_working(None, desc, y_tex=y_tex, uv_tex=uv_tex)
        metal_ctx.finish()
        got = io.read_rgba16f(out)
        # 双线性参考（clamp-to-edge，u = t - 0.5 权重公式——与 kernel
        # 的手工 bilinar 一致；t = (x+0.5)/2）
        chroma = uv_plane[0, :, 0].astype(np.float64) / 255.0
        upsampled = np.zeros(width)
        for x in range(width):
            u = (x + 0.5) / 2.0 - 0.5
            i0 = int(np.floor(u))
            f = u - i0
            def c(i):
                return chroma[min(max(i, 0), len(chroma) - 1)]
            upsampled[x] = c(i0) * (1.0 - f) + c(i0 + 1) * f
        # 验证 B 通道随 U 双线性变化（BT.709 B = Y + 1.8556*Cb）
        y = (126.0 - 16.0) / 219.0
        cb_expected = (upsampled * 255.0 - 128.0) / 224.0
        b_expected = y + 1.8556 * cb_expected
        np.testing.assert_allclose(got[0, :, 2], b_expected, atol=1.5 / 255.0)


# ── P010 / HDR（真机，合成数据） ─────────────────────────────────────────

@requires_metal
class TestP010HdrOnMetal:
    def test_known_linear_roundtrip(self, metal_ctx):
        """已知线性值 P010 往返：线性值/Alpha 保持，>1 不被 clamp。"""
        io = MetalColorIO(metal_ctx)
        width, height = 16, 8
        desc = ColorFrameDescription(
            "p010", width, height, bit_depth=10, transfer="pq",
            primaries="bt2020")
        # 已知线性（工作空间，1.0=100nit）：黑 / SDR 白 / 4x 白 / 10x 白
        linear = np.zeros((height, width, 3), np.float32)
        linear[:, 0:4] = 0.0
        linear[:, 4:8] = 1.0
        linear[:, 8:12] = 4.0
        linear[:, 12:16] = 10.0
        y_plane, uv_plane = synthesize_p010_from_linear(linear, desc)
        y_tex, uv_tex = io.upload_yuv_planes(y_plane, uv_plane, desc)
        out = io.decode_to_working(None, desc, y_tex=y_tex, uv_tex=uv_tex)
        metal_ctx.finish()
        got = io.read_rgba16f(out)

        # 容差：PQ 10-bit 量化在 4% 对比度内 + fp16 存储 → HDR 相对 5%
        for column, expected in ((0, 0.0), (6, 1.0), (10, 4.0), (14, 10.0)):
            value = float(got[:, column, :3].mean())
            assert value == pytest.approx(expected, rel=0.05, abs=0.01), (
                f"列 {column}: 期望 {expected}, 得到 {value}")
        assert (got[..., 3] == 1.0).all()
        # HDR 中间结果不被 clamp：10x 白（1000nit == 工作空间 10.0）保留
        assert float(got[..., :3].max()) > 9.0

    def test_bt709_primaries_of_hdr(self, metal_ctx):
        """BT.2020 输入的色域变换：纯红按 3×3 变到 bt709（无理由不剪）。"""
        io = MetalColorIO(metal_ctx)
        width, height = 8, 4
        desc = ColorFrameDescription(
            "p010", width, height, bit_depth=10, transfer="pq",
            primaries="bt2020")
        linear = np.zeros((height, width, 3), np.float32)
        linear[..., 0] = 4.0                     # BT.2020 红 @ 4x 白
        y_plane, uv_plane = synthesize_p010_from_linear(linear, desc)
        y_tex, uv_tex = io.upload_yuv_planes(y_plane, uv_plane, desc)
        out = io.decode_to_working(None, desc, y_tex=y_tex, uv_tex=uv_tex)
        metal_ctx.finish()
        got = io.read_rgba16f(out)
        # 合成器把 bt709 线性红 (4,0,0) 变换到 bt2020 再 PQ/YUV 编码；
        # 解码侧逆变换回 bt709 —— 往返应回到 (4, 0, 0)（量化容差内）。
        center = got[height // 2, width // 2, :3]
        assert center[0] == pytest.approx(4.0, rel=0.05, abs=0.1)
        assert abs(float(center[1])) < 0.25
        assert abs(float(center[2])) < 0.25
        assert np.isfinite(got).all()

    def test_rgba16f_hdr_alpha_passthrough(self, metal_ctx):
        """RGBA16F HDR 输入：线性值与 Alpha 直通（不 clamp、不重建）。"""
        io = MetalColorIO(metal_ctx)
        width, height = 8, 4
        desc = ColorFrameDescription(
            "rgba16f", width, height, bit_depth=16, transfer="linear")
        source = np.zeros((height, width, 4), np.float32)
        source[..., 0] = 12.0                     # HDR 高光
        source[..., 1] = -0.25                    # 负值（越界诊断）
        source[..., 2] = 0.5
        source[..., 3] = 0.375                    # 半透明 Alpha
        src_tex = io.upload_rgba16f(source)
        out = io.decode_to_working(src_tex, desc)
        metal_ctx.finish()
        got = io.read_rgba16f(out)
        np.testing.assert_allclose(got, source.astype(np.float16).astype(
            np.float32), atol=1e-3)
        assert (got[..., 3] == 0.375).all()


# ── 展示变换（输出边界一次；真机） ───────────────────────────────────────

@requires_metal
class TestDisplayEncodeOnMetal:
    def _encode(self, metal_ctx, linear, output_kind, **kwargs):
        from src.features.color.core.frame_color_description import (
            DisplayOutputDescription,
        )
        io = MetalColorIO(metal_ctx)
        rgba = linear if linear.shape[-1] == 4 else np.concatenate(
            [linear, np.ones(linear.shape[:2] + (1,), np.float32)], axis=-1)
        desc = ColorFrameDescription(
            "rgba16f", rgba.shape[1], rgba.shape[0], bit_depth=16,
            transfer="linear")
        working = io.decode_to_working(io.upload_rgba16f(rgba), desc)
        displayed = io.encode_for_display(
            working, DisplayOutputDescription(output_kind, **kwargs))
        metal_ctx.finish()
        return io.read_rgba16f(displayed)

    def test_sdr_encode_matches_provider(self, metal_ctx):
        """线性→sRGB（硬剪）与 provider ``linear_to_srgb_np`` 逐条一致。"""
        linear = np.array([[[0.0, 0.18, 0.5, 2.0]]], np.float32)
        got = self._encode(metal_ctx, linear, "sdr_encode")
        ref = np.clip(_linear_to_srgb_np(linear), 0.0, 1.0)[..., :3]
        np.testing.assert_allclose(got[..., :3], ref, atol=1.5 / 255.0)

    def test_hdr_sdr_tonemap_matches_provider(self, metal_ctx):
        """HDR→SDR ACES tonemap 与 provider ``_tonemap_aces_np`` 同源。"""
        linear = np.array([[[0.5, 4.0, 10.0, 25.0]]], np.float32)
        got = self._encode(metal_ctx, linear, "hdr_sdr_tonemap")
        mapped = _tonemap_aces_np(linear, target_peak=100.0)
        ref = _linear_to_srgb_np(mapped)[..., :3]
        np.testing.assert_allclose(got[..., :3], ref, atol=2.0 / 255.0)

    def test_hdr_pq_encode_roundtrip(self, metal_ctx):
        """线性→PQ 编码后：provider PQ 解码还原线性（10-bit 内往返）。"""
        linear = np.repeat(np.array([[[1.0], [4.0], [10.0], [40.0]]],
                                 np.float32), 3, axis=2)
        got = self._encode(metal_ctx, linear, "hdr_pq")
        # 展示码值即 PQ code [0,1]
        back = _pq_to_linear_np(np.asarray(got[..., :3])) / 100.0   # nit→工作空间
        np.testing.assert_allclose(back, linear, rtol=0.02, atol=0.01)

    def test_edr_linear_no_clamp(self, metal_ctx):
        """EDR 线性直通：×headroom，>1 交给系统合成器，不被剪。"""
        linear = np.repeat(np.array([[[1.0], [2.0], [4.0], [1.0]]],
                                 np.float32), 3, axis=2)
        got = self._encode(metal_ctx, linear, "edr_linear", edr_headroom=4.0)
        np.testing.assert_allclose(got[..., :3], linear * 4.0, rtol=1e-3)

    def test_sdr_passthrough_identity(self, metal_ctx):
        """SDR 直通恒等：展示变换不施加第二次（F5.1 编码信号直通）。"""
        code = np.array([[[0.0625, 0.5, 0.9, 1.0]]], np.float32)
        got = self._encode(metal_ctx, code, "sdr_passthrough")
        np.testing.assert_allclose(got[..., :3], code[..., :3], atol=1.0 / 255.0)


# ── 合成器自检（纯逻辑层） ───────────────────────────────────────────────

class TestSynthesizer:
    def test_p010_planes_left_aligned_10bit(self):
        desc = ColorFrameDescription(
            "p010", 8, 4, bit_depth=10, transfer="pq", primaries="bt2020")
        linear = np.full((4, 8, 3), 1.0, np.float32)
        y, uv = synthesize_p010_from_linear(linear, desc)
        # 码值左对齐：低 6 位为 0
        assert (y.astype(np.uint32) & 0x3F == 0).all()
        assert (uv.astype(np.uint32) & 0x3F == 0).all()
        # SDR 白（100nit）的 PQ 码 ≈ 0.508 → 10-bit limited Y ≈ 509
        assert 480 < int(y.max()) >> 6 < 540

    def test_requires_p010(self):
        with pytest.raises(ValueError, match="p010"):
            synthesize_p010_from_linear(
                np.zeros((2, 2, 3), np.float32),
                ColorFrameDescription("nv12", 2, 2))
